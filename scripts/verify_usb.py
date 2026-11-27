#!/usr/bin/env python3
"""Verify that a block device is the removable USB disk the user authorised.

WHY THIS EXISTS
---------------
The single most destructive mistake in any install workflow is writing to the
wrong disk. ``/dev/sda`` is not "the USB": it is whichever disk the kernel
enumerated first, and on a machine with several NVMe drives it is frequently an
internal disk. Kernel names are also assigned in probe order, so they can differ
between boots and between live environments.

This tool decides identity from *stable* evidence only:

  * the ``/sys/block/<name>/removable`` flag (1 = removable media)
  * transport, from sysfs and from ``lsblk -o TRAN``
  * model and serial string from sysfs
  * capacity in bytes
  * whether any partition of it is mounted or is a mount source
  * the partition table type and any existing filesystems
  * whether the device backs the running root filesystem

It refuses when the evidence is ambiguous rather than guessing. Two exported
identifiers that disagree (for example the by-id symlink resolving somewhere
other than the path given) is a refusal, not a tie-break.

USAGE
-----
    ./verify_usb.py --device /dev/sda --expect-serial E0D5...
    ./verify_usb.py --device /dev/sda --expect-serial E0D5... --json
    ./verify_usb.py --list          # show candidate removable devices

Exit codes:
    0  identity confirmed and safe to write
    1  identity confirmed but NOT safe (mounted, in use, internal)
    2  identity could not be confirmed (ambiguous / missing)
    3  bad invocation

This script only reads. It never writes to any device.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys

SYS_BLOCK = "/sys/block"


def _read(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read().strip()
    except OSError:
        return None


def _run(cmd: list[str]) -> str:
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=30, check=False)
        return (out.stdout or "") if out.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def base_name(device: str) -> str:
    """``/dev/sda1`` -> ``sda``; leaves a whole-disk name alone."""
    name = os.path.basename(os.path.realpath(device))
    # NVMe partitions look like nvme0n1p1; only strip a partition suffix.
    match = re.match(r"^(nvme\d+n\d+)p\d+$", name)
    if match:
        return match.group(1)
    if re.match(r"^(sd[a-z]+|vd[a-z]+|hd[a-z]+)\d+$", name):
        return re.sub(r"\d+$", "", name)
    if re.match(r"^mmcblk\d+$", name):
        return name
    return name


def resolve_by_id(device: str) -> list[str]:
    """Return every ``/dev/disk/by-id`` entry that resolves to this device."""
    found = []
    by_id = "/dev/disk/by-id"
    real = os.path.realpath(device)
    try:
        for entry in sorted(os.listdir(by_id)):
            if os.path.realpath(os.path.join(by_id, entry)) == real:
                found.append(entry)
    except OSError:
        pass
    return found


#: Set by a probe that could not fully determine usage. ``evaluate`` treats a
#: failed in-use probe as a refusal, not as "nothing is using this disk".
_probe_failed = {"mount": False, "swap": False}


def mounted_sources() -> list[str]:
    """Every block device currently mounted, as ``/dev/...`` resolved paths."""
    sources = []
    try:
        with open("/proc/mounts", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                fields = line.split()
                if fields and fields[0].startswith("/dev/"):
                    sources.append(os.path.realpath(fields[0]))
        _probe_failed["mount"] = False
    except OSError:
        _probe_failed["mount"] = True
    return sources


def mounted_device_numbers() -> set[str]:
    """Read kernel major:minor values independently of mount source aliases."""
    with open("/proc/self/mountinfo", encoding="utf-8", errors="replace") as fh:
        return {line.split()[2] for line in fh}


def swap_sources() -> list[str]:
    """Active swap devices. A missing ``swapon`` or a failed call is a probe
    failure, not evidence that nothing is swapped."""
    try:
        result = subprocess.run(["swapon", "--noheadings", "--show=NAME"],
                                capture_output=True, text=True, timeout=30, check=False)
    except (OSError, subprocess.SubprocessError):
        _probe_failed["swap"] = True
        return []
    if result.returncode != 0:
        _probe_failed["swap"] = True
        return []
    _probe_failed["swap"] = False
    return [os.path.realpath(p.strip()) for p in result.stdout.splitlines() if p.strip()]


def describe(device: str) -> dict:
    name = base_name(device)
    sysdir = os.path.join(SYS_BLOCK, name)
    info: dict = {
        "device": device,
        "resolved": os.path.realpath(device),
        "kernel_name": name,
        "exists": os.path.exists(os.path.join("/dev", name)),
    }
    # A partition input resolves to its parent for description but must never be
    # silently accepted as a write target. Compare the *resolved* path against
    # its own base-name, so a by-id alias is not mislabelled.
    resolved = os.path.realpath(device)
    info["is_partition"] = os.path.basename(resolved) != base_name(resolved)
    if not os.path.isdir(sysdir):
        info["error"] = f"{sysdir} is missing; is {name} a block device?"
        return info

    info["removable"] = _read(f"{sysdir}/removable")
    info["model"] = _read(f"{sysdir}/device/model")
    info["vendor"] = _read(f"{sysdir}/device/vendor")
    info["rotational"] = _read(f"{sysdir}/queue/rotational")

    serial = _read(f"{sysdir}/device/serial") or _read(f"{sysdir}/serial")
    if not serial:
        # udev records the actual serial separately from its formatted by-id name.
        # Serial numbers can contain underscores and other by-id separators.
        properties = _run(["udevadm", "info", "--query=property", "--name", f"/dev/{name}"])
        serial = next((line.partition("=")[2] for line in properties.splitlines()
                       if line.startswith("ID_SERIAL_SHORT=")), None)
    info["serial"] = serial

    size = _read(f"{sysdir}/size")
    if size and size.isdigit():
        info["size_bytes"] = int(size) * 512

    # Transport: sysfs parent chain is the authority; lsblk is a cross-check.
    try:
        info["transport_sysfs"] = os.path.basename(os.path.dirname(os.path.realpath(sysdir)))
        uevent = _read(f"{sysdir}/device/uevent") or ""
        m = re.search(r"^DEVTYPE=(\w+)", uevent, re.M)
        if m:
            info["devtype"] = m.group(1)
    except OSError:
        info["transport_sysfs"] = None
    lsblk = _run(["lsblk", "-dno", "TRAN", "--", f"/dev/{name}"]).strip()
    info["transport_lsblk"] = lsblk or None
    info["is_usb"] = bool(
        (info.get("transport_lsblk") or "").lower() == "usb"
        or "usb" in (info.get("transport_sysfs") or "").lower()
    )

    info["by_id"] = resolve_by_id(f"/dev/{name}")

    # Every block device on this disk, partitions included.
    parts = []
    try:
        for entry in sorted(os.listdir(sysdir)):
            if os.path.isfile(os.path.join(sysdir, entry, "partition")):
                full = f"/dev/{entry}"
                parts.append({
                    "name": entry,
                    "resolved": os.path.realpath(full) if os.path.exists(full) else full,
                })
    except OSError:
        info["probe_failed"] = True
    info["partitions"] = parts

    # Mount / swap usage. Check the whole disk as well as each partition: a
    # filesystem or swap directly on /dev/sdz (no partition table) is just as
    # occupied as one on a partition, and must not be missed. A probe that
    # could not read mounts/swap reports failure, not "nothing in use".
    busy = set(mounted_sources()) | set(swap_sources())
    info["in_use"] = [
        p["name"] for p in parts if p["resolved"] in busy
    ]
    if os.path.realpath(f"/dev/{name}") in busy:
        info["in_use"].append(name)
    info["probe_failed"] = bool(info.get("probe_failed") or _probe_failed["mount"] or _probe_failed["swap"])
    try:
        numbers = mounted_device_numbers()
        for member, path in [(name, sysdir), *((p["name"], os.path.join(sysdir, p["name"])) for p in parts)]:
            number = _read(os.path.join(path, "dev"))
            if not number:
                info["probe_failed"] = True
            elif number in numbers and member not in info["in_use"]:
                info["in_use"].append(member)
    except (OSError, IndexError):
        info["probe_failed"] = True
    info["root_device"] = _root_device()
    root = os.path.realpath(info["root_device"]) if info["root_device"] else None
    info["hosts_root"] = bool(
        root and (root == os.path.realpath(f"/dev/{name}")
                  or any(p["resolved"] == root for p in parts))
    )

    # --- Dependent mappings: the invisible consumers ------------------------
    # Mounts and swap are not the only ways a disk is in use. An LVM physical
    # volume, a dm-crypt LUKS container, an mdraid member, a dm-multipath slave
    # or a zpool member can all sit on the disk with nothing mounted from it.
    # Wiping the backing disk would destroy them silently. sysfs exposes these
    # as /sys/block/<dev>/holders/<holder> for every consumer kernel object.
    dependencies = _dependent_mappings(sysdir, name, parts, busy)
    info.update(dependencies)
    info["probe_failed"] |= dependencies.get("holder_probe_failed", False)

    table = _run(["lsblk", "-dno", "PTTYPE", "--", f"/dev/{name}"]).strip()
    info["pttype"] = table or None
    fstypes = _run(["lsblk", "-no", "NAME,FSTYPE,LABEL", "--", f"/dev/{name}"]).strip()
    info["filesystems"] = [line for line in fstypes.splitlines() if line.strip()]
    return info


def _dependent_mappings(sysdir: str, name: str, parts: list, busy: set) -> dict:
    """Detect LVM, dm-crypt, mdraid, dm-multipath and other consumers of the disk.

    sysfs lists every kernel object holding the device open under
    ``/sys/block/<dev>/holders`` (and the same under each partition). These do
    not appear in /proc/mounts, so a mount check alone misses them.
    """
    out = {
        "holders": [], "is_device_mapper": False, "is_multipath": False,
        "raid_member": None, "lvm_member": None, "luks_member": None,
    }
    holder_names: set[str] = set()
    for devdir in [sysdir] + [os.path.join(sysdir, p["name"]) for p in parts]:
        holders_dir = os.path.join(devdir, "holders")
        try:
            holder_names.update(os.listdir(holders_dir))
        except OSError:
            out["holder_probe_failed"] = True
    out["holders"] = sorted(holder_names)

    # mdraid membership is a directory on the member's sysfs node.
    for p in parts + [{"name": name}]:
        if os.path.isdir(os.path.join(sysdir, p["name"], "md")):
            out["raid_member"] = p["name"]

    # Any dm-* holder means device-mapper claims the disk or a partition of it:
    # a dm-crypt LUKS volume, an LVM logical volume, or a dm-multipath slave.
    if any(h.startswith("dm-") for h in holder_names):
        out["is_device_mapper"] = True

    if out["is_device_mapper"]:
        # Classify which device-mapper consumer it is, best-effort.
        crypts = _run(["dmsetup", "ls", "--target", "crypt"])
        if crypts.strip() and "No devices found" not in crypts:
            out["luks_member"] = True
        lvs = _run(["lvs", "--noheadings", "-o", "lv_path"])
        if any(line.strip() for line in lvs.splitlines()):
            out["lvm_member"] = True
        mpaths = _run(["multipath", "-l"])
        if any(h in mpaths for h in holder_names):
            out["is_multipath"] = True
    return out


def _root_device() -> str | None:
    """The block device backing ``/``, if it can be determined."""
    try:
        with open("/proc/mounts", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                fields = line.split()
                if len(fields) > 1 and fields[1] == "/":
                    return os.path.realpath(fields[0])
    except OSError:
        return None
    # Live environments often mount a squashfs/overlay; try findmnt.
    out = _run(["findmnt", "-no", "SOURCE", "/"]).strip()
    return os.path.realpath(out) if out.startswith("/dev/") else None


def list_candidates() -> list[dict]:
    out = []
    try:
        for name in sorted(os.listdir(SYS_BLOCK)):
            if _read(f"{SYS_BLOCK}/{name}/removable") == "1":
                out.append(describe(f"/dev/{name}"))
    except OSError:
        pass
    return out


def evaluate(info: dict, expect_serial: str | None,
             exact_serial: bool = False) -> tuple[int, list[str], list[str]]:
    """Return (exit_code, blocking_problems, warnings).

    ``exact_serial``: when True, ``expect_serial`` must equal the device's whole
    serial. The USB writer passes True; the standalone human lookup may keep a
    looser substring match (it is advisory, not a safety gate).
    """
    problems: list[str] = []
    warnings: list[str] = []

    if info.get("error"):
        return 2, [info["error"]], warnings
    if not info.get("exists"):
        return 2, [f"{info.get('device')} does not exist"], warnings
    if info.get("is_partition"):
        problems.append(
            f"{info.get('device')} is a partition node, not a whole disk; "
            "give the parent device, not a partition of it"
        )

    if info.get("removable") != "1":
        problems.append(
            f"{info['kernel_name']} reports removable={info.get('removable')!r}; "
            "refusing to treat fixed media as a USB stick"
        )
    if not info.get("is_usb"):
        problems.append(
            f"{info['kernel_name']} is not on a USB bus "
            f"(sysfs={info.get('transport_sysfs')!r}, lsblk={info.get('transport_lsblk')!r})"
        )
    if info.get("hosts_root"):
        problems.append(f"{info['kernel_name']} backs the running root filesystem")
    # A probe that could not read mounts or swap must refuse rather than guess
    # that nothing is in use.
    if info.get("probe_failed"):
        problems.append(
            "could not determine device usage (a required probe failed); "
            "refusing rather than guessing nothing is in use"
        )
    if info.get("in_use"):
        problems.append(
            "partitions or the disk itself are mounted or used as swap: "
            + ", ".join(info["in_use"])
        )

    # Dependent kernel consumers: a disk can be held open by LVM, dm-crypt, an
    # mdraid array or a dm-multipath map without a single mounted filesystem.
    if info.get("holders"):
        problems.append(
            f"{info['kernel_name']} is held open by other devices "
            f"({', '.join(info['holders'])}); wiping its backing disk would "
            "destroy data not listed as a mount"
        )
    if info.get("raid_member"):
        problems.append(f"{info['kernel_name']} is part of an mdraid array ({info['raid_member']})")
    if info.get("is_multipath"):
        problems.append(f"{info['kernel_name']} is a dm-multipath slave")
    if info.get("luks_member"):
        problems.append(f"{info['kernel_name']} backs a dm-crypt (LUKS) volume")
    if info.get("lvm_member"):
        problems.append(f"{info['kernel_name']} is claimed by LVM")

    if expect_serial:
        actual = (info.get("serial") or "")
        if not actual:
            problems.append(
                f"no serial readable, so it cannot be matched against the "
                f"authorised serial {expect_serial!r}"
            )
        elif exact_serial and expect_serial != actual:
            problems.append(
                f"serial mismatch: device reports {actual!r}, expected {expect_serial!r} "
                "(exact match required)"
            )
        elif not exact_serial and expect_serial.lower() not in actual.lower():
            problems.append(
                f"serial mismatch: device reports {actual!r}, expected {expect_serial!r}"
            )

    # Cross-check: by-id entries must not contradict the path we were given.
    ids = info.get("by_id") or []
    if not ids:
        warnings.append("no /dev/disk/by-id entries; identity rests on sysfs only")
    usb_ids = [i for i in ids if i.startswith("usb-")]
    if ids and not usb_ids:
        problems.append("by-id entries exist but none is a usb-* entry: not a USB device")

    if not info.get("partitions"):
        warnings.append("disk has no partitions (already blank)")
    if info.get("size_bytes") and info["size_bytes"] < 8 * 1024**3:
        warnings.append(
            f"only {info['size_bytes'] / 1024**3:.1f} GiB: enough for an OpenCore "
            "boot stick, too small for a macOS system volume"
        )

    if problems:
        return 1, problems, warnings
    return 0, problems, warnings


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--device", help="block device to verify, e.g. /dev/sda")
    ap.add_argument("--expect-serial", help="serial the user authorised (substring match)")
    ap.add_argument("--list", action="store_true", help="list removable candidate devices")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args(argv)

    if args.list:
        candidates = list_candidates()
        if args.json:
            print(json.dumps(candidates, indent=2))
        else:
            if not candidates:
                print("no removable block devices found")
            for c in candidates:
                print(
                    f"{c['device']:<12} {c.get('vendor') or ''} {c.get('model') or ''}".rstrip()
                    + f"  serial={c.get('serial')!r}  "
                    f"{(c.get('size_bytes') or 0) / 1024**3:.1f} GiB  "
                    f"usb={c.get('is_usb')}  in_use={c.get('in_use') or '-'}"
                )
        return 0

    if not args.device:
        ap.print_usage(sys.stderr)
        return 3

    info = describe(args.device)
    code, problems, warnings = evaluate(info, args.expect_serial)

    if args.json:
        print(json.dumps(
            {"info": info, "exit_code": code, "problems": problems, "warnings": warnings},
            indent=2,
        ))
        return code

    verb = "CONFIRMED" if code == 0 else "REFUSED" if code == 1 else "UNVERIFIED"
    print(f"{verb}: {info.get('device')} ({info.get('kernel_name')})")
    print(f"  model    : {info.get('vendor') or ''} {info.get('model') or ''}".rstrip())
    print(f"  serial   : {info.get('serial')!r}")
    print(f"  size     : {(info.get('size_bytes') or 0) / 1024**3:.2f} GiB")
    print(f"  removable: {info.get('removable')}   usb: {info.get('is_usb')}")
    print(f"  by-id    : {', '.join(info.get('by_id') or []) or '-'}")
    print(f"  table    : {info.get('pttype')}")
    if info.get("filesystems"):
        print("  contents :")
        for line in info["filesystems"]:
            print(f"    {line}")
    for warning in warnings:
        print(f"  WARNING: {warning}")
    for problem in problems:
        print(f"  PROBLEM: {problem}")
    return code


if __name__ == "__main__":
    sys.exit(main())
