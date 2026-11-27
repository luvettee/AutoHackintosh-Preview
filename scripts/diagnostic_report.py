#!/usr/bin/env python3
"""Write private diagnostics and a shareable report with constrained values."""

from __future__ import annotations

import datetime
import json
import os
import plistlib
import sys
import tempfile


class ReportError(RuntimeError):
    """A report could not be produced with the required privacy guarantees."""


# ---------------------------------------------------------------------------
# Shareable schema — the only fields allowed into public output
# ---------------------------------------------------------------------------
def _object(value) -> dict:
    return value if isinstance(value, dict) else {}


def _enum(value, choices):
    return value if isinstance(value, str) and value in choices else None


def _bounded_int(value, maximum: int):
    return value if type(value) is int and 0 <= value <= maximum else None


def _shareable_hardware(hw: dict) -> dict:
    cpu = _object(hw.get("CPU"))
    board = _object(hw.get("Motherboard"))
    bios = _object(hw.get("BIOS"))
    return {
        "cpu": {
            "manufacturer": _enum(cpu.get("Manufacturer"), {"Intel", "AMD", "GenuineIntel", "AuthenticAMD"}),
            "cores": _bounded_int(cpu.get("Core Count"), 256),
            "threads": _bounded_int(cpu.get("Thread Count"), 512),
        },
        "platform": _enum(board.get("Platform"), {"Desktop", "Laptop"}),
        "firmware": _enum(bios.get("Firmware Type"), {"UEFI", "Legacy", "BIOS"}),
    }


def _shareable_opencore(config: dict) -> dict:
    nvram = _object(_object(_object(config.get("NVRAM")).get("Add")).get(
        "7C436110-AB2A-4BBB-A880-FE41995C9F82"))
    generic = _object(_object(config.get("PlatformInfo")).get("Generic"))
    security = _object(_object(config.get("Misc")).get("Security"))
    flags = nvram.get("boot-args")
    known_flags = {"-v", "-wegnoegpu", "debug=0x100", "keepsyms=1", "nvfb=1", "nvaccel=1"}
    return {
        "smbios_model": _enum(generic.get("SystemProductName"),
                              {"MacPro7,1", "iMacPro1,1", "iMac19,1", "iMac20,1", "iMac20,2",
                               "MacBookPro15,1", "MacBookPro16,1", "Macmini8,1"}),
        "boot_flags": sorted(set(flags.split()) & known_flags) if isinstance(flags, str) else [],
        "secure_boot_model": _enum(security.get("SecureBootModel"), {"Default", "Disabled"}),
    }


def _shareable_assessment(verdict) -> dict:
    verdict = _object(verdict)
    return {name: bool(verdict[name]) for name in
            ("natively_supported", "needs_patches", "experimental_driver", "unsupported", "unknown")
            if isinstance(verdict.get(name), list)}


def _kext_versions(efi_dir: str) -> list[dict]:
    out = []
    kdir = os.path.join(efi_dir, "EFI", "OC", "Kexts")
    if not os.path.isdir(kdir):
        return out
    for entry in sorted(os.listdir(kdir)):
        info = os.path.join(kdir, entry, "Contents", "Info.plist")
        record = {"kext": entry}
        if os.path.isfile(info):
            try:
                with open(info, "rb") as fh:
                    plist = plistlib.load(fh)
                record["version"] = plist.get("CFBundleVersion")
                record["identifier"] = plist.get("CFBundleIdentifier")
            except Exception:  # noqa: BLE001
                record["version"] = "unreadable"
        out.append(record)
    return out


def _load_config(efi_dir: str) -> dict:
    path = os.path.join(efi_dir, "EFI", "OC", "config.plist")
    if not os.path.isfile(path):
        return {}
    with open(path, "rb") as fh:
        return plistlib.load(fh)


# ---------------------------------------------------------------------------
# Private-file plumbing — create 0600 from byte one, never follow a symlink
# ---------------------------------------------------------------------------
def _private_dir(path: str) -> str:
    """Create a private directory (0700); refuse rather than leave it open."""
    try:
        os.makedirs(path, mode=0o700, exist_ok=True)
        fd = os.open(path, os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fchmod(fd, 0o700)
        finally:
            os.close(fd)
    except OSError as exc:
        raise ReportError(f"could not lock down private dir {path}: {exc}")
    return path


def _private_write(path: str, data: str) -> None:
    """Write ``data`` to ``path`` with 0600 from the first byte, atomically and
    without following a symlink.

    A symlink at ``path`` is never followed: the report would otherwise land on
    an arbitrary target and could leak private content there.
    """
    directory = os.path.dirname(path) or "."
    if os.path.islink(path):
        raise ReportError(f"refusing to write {path}: it is a symlink")
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".private-", suffix=".tmp")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(data)
        # Atomic rename, no symlink follow on the destination.
        if os.path.islink(path):
            raise ReportError(f"refusing to replace {path}: it is a symlink")
        os.rename(tmp, path)
    except OSError as exc:
        raise ReportError(f"could not write {path}: {exc}") from exc
    finally:
        if os.path.lexists(tmp):
            os.unlink(tmp)


def _public_write(path: str, data: str) -> None:
    """Write shareable output. Symlinks are refused; no special perms needed."""
    directory = os.path.dirname(path) or "."
    if os.path.islink(path):
        raise ReportError(f"refusing to write {path}: it is a symlink")
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".public-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(data)
        if os.path.islink(path):
            raise ReportError(f"refusing to replace {path}: it is a symlink")
        os.rename(tmp, path)
    except OSError as exc:
        raise ReportError(f"could not write {path}: {exc}") from exc
    finally:
        if os.path.lexists(tmp):
            os.unlink(tmp)


# ---------------------------------------------------------------------------
# Report assembly
# ---------------------------------------------------------------------------
def build(workdir: str, run_results: dict | None = None) -> dict:
    """Create private + shareable reports under ``workdir/reports``.

    ``run_results`` is the *current* run's stage results, passed in so the
    report reflects this run rather than whatever ``last-run.json`` happened to
    contain. When ``None`` (standalone use) the file is read as a fallback.

    The private copy is written first with strict permissions; only when it is
    safely on disk is the redacted shareable copy produced. If the private path
    cannot be made private, the whole report fails rather than produce a
    misleading shareable output.
    """
    workdir = os.path.abspath(os.path.expanduser(workdir))
    reports_dir = _private_dir(os.path.join(workdir, "reports"))
    private_dir = _private_dir(os.path.join(reports_dir, "private"))

    if run_results is not None:
        last_run = {"results": run_results}
    else:
        last_run_path = os.path.join(workdir, "last-run.json")
        last_run = {}
        if os.path.isfile(last_run_path):
            try:
                with open(last_run_path, encoding="utf-8") as fh:
                    last_run = json.load(fh)
            except (OSError, json.JSONDecodeError):
                last_run = {}

    sysreport = os.path.join(workdir, "sysreport", "Report.json")
    hardware = {}
    if os.path.isfile(sysreport):
        try:
            with open(sysreport, encoding="utf-8") as fh:
                hardware = json.load(fh)
        except (OSError, json.JSONDecodeError):
            hardware = {}

    efi_dir = os.path.join(workdir, "efi")
    config = _load_config(efi_dir)
    manifest = {}
    manifest_path = os.path.join(workdir, "deps", "manifest.json")
    if os.path.isfile(manifest_path):
        try:
            with open(manifest_path, encoding="utf-8") as fh:
                manifest = json.load(fh)
        except (OSError, json.JSONDecodeError):
            manifest = {}

    results = _object(_object(last_run).get("results"))
    efi_result = _object(results.get("efi"))
    assess = _object(results.get("assess"))

    ocvalidate = _object(efi_result.get("ocvalidate"))
    target = _object(assess.get("recommended_macos")).get("darwin")
    shareable = {
        "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "generator": "autohackintosh",
        "hardware": _shareable_hardware(_object(hardware)),
        "assessment": _shareable_assessment(assess.get("verdict")),
        "recommended_darwin": _enum(target, {"23", "24", "25"}),
        "open_core": _shareable_opencore(_object(config)),
        "validation": {
            "ocvalidate_ok": ocvalidate.get("ok") if type(ocvalidate.get("ok")) is bool else None,
            "ocvalidate_issues": _bounded_int(ocvalidate.get("issues"), 10000),
        },
        "known_issues": [
            "Hackintosh installation success does not imply a stable daily driver.",
            "NVIDIA acceleration depends on an experimental third-party driver.",
        ],
    }

    private = dict(shareable)
    private.update({
        "full_hardware_report": hardware,
        "full_opencore_config": config,
        "kexts": _kext_versions(efi_dir),
        "dependency_manifest": manifest,
        "full_policy_changes": efi_result.get("policy_changes"),
        "notices": efi_result.get("notices"),
        "last_run_summary": last_run,
        "report_source": "unredacted local copy; do not share",
    })

    # Write the private copy first with strict permissions; only then shareable.
    private_path = os.path.join(private_dir, "diagnostic-report.private.json")
    try:
        _private_write(private_path, json.dumps(private, indent=2, default=str))
    except ReportError as exc:
        raise ReportError(f"cannot create private report safely: {exc}") from exc

    shareable_json = os.path.join(reports_dir, "diagnostic-report.shareable.json")
    shareable_md = os.path.join(reports_dir, "diagnostic-report.shareable.md")
    _public_write(shareable_json, json.dumps(shareable, indent=2, default=str))
    _public_write(shareable_md, _as_markdown(shareable))

    return {
        "ok": True,
        "private": private_path,
        "shareable_json": shareable_json,
        "shareable_markdown": shareable_md,
        "redacted_values": None,
        "note": "shareable output contains constrained categories and validation results",
    }


def _as_markdown(report: dict) -> str:
    lines = ["# AutoHackintosh diagnostic report", "", f"Generated {report['generated_at']}"]
    sections = {
        "Hardware": {**report["hardware"]["cpu"],
                     "platform": report["hardware"]["platform"], "firmware": report["hardware"]["firmware"]},
        "Assessment": {**report["assessment"], "recommended_darwin": report["recommended_darwin"]},
        "OpenCore": report["open_core"],
        "Validation": report["validation"],
    }
    for heading, fields in sections.items():
        lines.extend(("", f"## {heading}", ""))
        lines.extend(f"- {key}: {json.dumps(value)}" for key, value in fields.items() if value is not None)
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    result = build(sys.argv[1] if len(sys.argv) > 1 else os.path.expanduser("~/.cache/autohackintosh"))
    print(json.dumps(result, indent=2))
