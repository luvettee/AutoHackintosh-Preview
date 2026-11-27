"""Normalise a Hardware Sniffer report so 1401's validator accepts it on Linux.

THE PROBLEM
-----------
``Hardware-Sniffer/Scripts/platforms/linux.py`` fills some fields from Linux
sysfs/DRM values whose spelling was never produced by the Windows backend that
1401's JSON schema was written against. The validator is strict and rejects the
*whole report* over one cosmetic mismatch, so the build stops before any
hardware-specific reasoning happens. Observed on a real machine:

  * ``System Devices.*.ACPI Path`` values outside the ``\\_SB`` namespace
    (for example a thermal-zone device) where the schema demands
    ``^[\\\\]?_SB(\\.[A-Z0-9_]+)+$``.
  * ``Monitor.*."Connector Type"`` of ``HDMI-A`` where the schema demands one
    of VGA/DVI/HDMI/LVDS/DP/eDP/Internal/Uninitialized.

WHY NORMALISING IS SAFE (NOT "CORRUPTING MEANINGFUL INFORMATION")
-----------------------------------------------------------------
Every field repaired here is consumed by the engine through a narrow, verified
interface:

  * ``System Devices`` is read only via ``Device ID`` / ``Bus Type`` / ``Device``
    (see ``acpi_guru`` thermal/IMEI checks and ``config_prodigy`` audio-controller
    lookup). ``ACPI Path`` on that section is never read. Dropping an
    unparseable one removes information no EFI decision depends on.
  * ``Connector Type`` is compared only against ``"VGA"`` (to detect a
    VGA-only display) and ``("DVI", "HDMI")``. Mapping ``HDMI-A`` -> ``HDMI``
    is exact: both compare equal where it matters and differ nowhere.

The normaliser never invents a value, never changes a device ID, PCI path, or
capability flag, and records every edit it makes so the diagnostic report can
show them. Anything it cannot map with certainty is dropped rather than guessed.
"""

from __future__ import annotations

import json
import re

#: Values the schema accepts for ``Monitor.*."Connector Type"``.
CONNECTOR_TYPES = ("VGA", "DVI", "HDMI", "LVDS", "DP", "eDP", "Internal", "Uninitialized")

#: ACPI name path the schema accepts for device ``ACPI Path`` fields.
_ACPI_PATH_RE = re.compile(r"^[\\]?_SB(\.[A-Z0-9_]+)+$")

#: Only these sections' ACPI Path is cosmetic. Elsewhere it feeds ACPI patching.
ACPI_PATH_FREE_SECTIONS = ("System Devices",)

#: Linux DRM connector names -> the schema's vocabulary.
_CONNECTOR_MAP = {
    "HDMI": "HDMI", "HDMIA": "HDMI", "HDMIB": "HDMI",
    "DP": "DP", "DP1": "DP", "DP2": "DP", "DP3": "DP",
    "EDP": "eDP", "EDP1": "eDP",
    "DVI": "DVI", "DVID": "DVI", "DVII": "DVI",
    "VGA": "VGA", "VGA1": "VGA",
    "LVDS": "LVDS", "LVDS1": "LVDS",
}


def normalize_connector(value: str) -> str | None:
    """Map a Linux DRM connector name such as ``HDMI-A`` to schema spelling."""
    if not isinstance(value, str):
        return None
    if value in CONNECTOR_TYPES:
        return value
    # "HDMI-A", "DP-1", "DVI-I-2", "eDP-1", "VGA-1"
    head = re.split(r"[-_ ]+", value.strip(), maxsplit=1)[0].upper()
    mapped = _CONNECTOR_MAP.get(head)
    if mapped:
        return mapped
    for candidate in CONNECTOR_TYPES:  # exact suffix match, e.g. "HDMI-A" already handled
        if value.upper().startswith(candidate.upper()):
            return candidate
    return None


def sanitize(report: dict) -> dict:
    """Repair a report in place. Returns ``{"edits": [...]}`` describing every change."""
    edits: list[dict] = []

    for section in ACPI_PATH_FREE_SECTIONS:
        for name, props in (report.get(section) or {}).items():
            if not isinstance(props, dict):
                continue
            ap = props.get("ACPI Path")
            if isinstance(ap, str) and not _ACPI_PATH_RE.match(ap):
                del props["ACPI Path"]
                edits.append({
                    "field": f"{section}.{name}.ACPI Path",
                    "action": "dropped",
                    "value": ap,
                    "why": "outside \\_SB namespace; this section's ACPI Path is not read for any EFI decision",
                })

    for name, props in (report.get("Monitor") or {}).items():
        if not isinstance(props, dict):
            continue
        raw = props.get("Connector Type")
        if raw is None:
            continue
        fixed = normalize_connector(raw)
        if fixed == raw:
            continue
        if fixed is None:
            del props["Connector Type"]
            edits.append({
                "field": f"Monitor.{name}.Connector Type",
                "action": "dropped",
                "value": raw,
                "why": "unrecognised connector name; treated as not-VGA which is the safe default",
            })
        else:
            props["Connector Type"] = fixed
            edits.append({
                "field": f"Monitor.{name}.Connector Type",
                "action": "mapped",
                "value": f"{raw} -> {fixed}",
                "why": "Linux DRM connector spelling; engine only compares against VGA/DVI/HDMI",
            })

    return {"edits": edits}


def sanitize_file(path: str) -> dict:
    """Load, sanitise and rewrite ``path``. Keeps a ``.orig`` copy on first run."""
    import os
    import shutil

    with open(path, encoding="utf-8") as fh:
        report = json.load(fh)
    backup = path + ".orig"
    if not os.path.exists(backup):
        shutil.copy2(path, backup)
    result = sanitize(report)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2)
    result["path"] = path
    result["backup"] = backup
    return result


if __name__ == "__main__":
    import sys

    for target in sys.argv[1:]:
        print(json.dumps(sanitize_file(target), indent=2))
