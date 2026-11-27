#!/usr/bin/env python3
"""Build a bootable macOS recovery and OpenCore stick from Linux."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
from contextlib import ExitStack

import consent
import fetch_recovery
import verify_usb
from fetch_deps import sha256_of

EFI_PART_MIB = 512
ESP_LABEL = "EFI"
RECOVERY_LABEL = "Recovery HD"
RECOVERY_FILES = ("BaseSystem.dmg", "BaseSystem.chunklist")


class BuildError(RuntimeError):
    pass


class TargetRefused(BuildError):
    pass


def _sudo(cmd: list[str], check: bool = True) -> subprocess.CompletedProcess:
    prefix = [] if os.geteuid() == 0 else ["sudo", "-n"]
    result = subprocess.run(prefix + cmd, capture_output=True, text=True, check=False)
    if check and result.returncode != 0:
        raise BuildError(f"{cmd[0]} failed ({result.returncode}): {result.stderr.strip()[:2000]}")
    return result


def check_prerequisites(efi_dir: str, recovery_dir: str, extra_dir: str | None) -> list[str]:
    problems = []
    try:
        _payload(efi_dir, recovery_dir, extra_dir)
    except (BuildError, OSError) as exc:
        problems.append(str(exc))
    for tool in ("lsblk", "wipefs", "sgdisk", "partprobe", "udevadm", "mkfs.vfat",
                 "mkfs.hfsplus", "mount", "umount", "sync"):
        if not shutil.which(tool):
            problems.append(f"missing tool: {tool}")
    return problems


def plan(info: dict) -> dict:
    return {
        "device": info.get("device"),
        "serial": info.get("serial"),
        "partition_table": [
            {"number": 1, "size_mib": EFI_PART_MIB, "type": "EF00", "fs": "vfat", "label": ESP_LABEL},
            {"number": 2, "size_mib": None, "type": "AF00", "fs": "hfsplus", "label": RECOVERY_LABEL},
        ],
        "will_destroy": info.get("filesystems") or [],
    }


def backup_partition_table(device: str, backup_dir: str) -> None:
    _sudo(["sgdisk", "--backup", os.path.join(backup_dir, "sgdisk-backup.bin"), device])
    output = _sudo(["sgdisk", "--print", device]).stdout
    with open(os.path.join(backup_dir, "partition-table.txt"), "x", opener=_private_open) as fh:
        fh.write(output)


def _private_open(path: str, flags: int) -> int:
    return os.open(path, flags, 0o600)


def _block_sysfs(path: str) -> Path:
    node = os.stat(path)
    if not stat.S_ISBLK(node.st_mode):
        raise BuildError(f"{path} must be a block device")
    return Path(f"/sys/dev/block/{os.major(node.st_rdev)}:{os.minor(node.st_rdev)}").resolve(strict=True)


def _canonical_device(device: str) -> str:
    real = os.path.realpath(device)
    sysfs = _block_sysfs(real)
    if (sysfs / "partition").exists() or real != f"/dev/{sysfs.name}":
        raise BuildError(f"{device} must resolve to a canonical whole disk")
    return real


def _partition_nodes(device: str) -> tuple[str, str]:
    disk = _block_sysfs(device)
    children = {}
    for entry in disk.iterdir():
        partition_number = entry / "partition"
        if not partition_number.is_file():
            continue
        number = int(partition_number.read_text())
        metadata = dict(line.split("=", 1) for line in (entry / "uevent").read_text().splitlines())
        name = metadata.get("DEVNAME")
        if name != entry.name or metadata.get("DEVTYPE") != "partition":
            raise BuildError("invalid partition device metadata")
        node = f"/dev/{name}"
        actual = _block_sysfs(node)
        if actual != entry or actual.parent != disk or number in children:
            raise BuildError("ambiguous partition parent or number")
        children[number] = node
    if set(children) != {1, 2}:
        raise BuildError(f"expected exactly partitions 1 and 2; found {sorted(children)}")
    return children[1], children[2]


def _tree(root: Path, destination: Path):
    if not stat.S_ISDIR(root.lstat().st_mode):
        raise BuildError(f"payload directory is missing or unsafe: {root}")
    yield destination, root
    for child in sorted(root.iterdir()):
        mode = child.lstat().st_mode
        if stat.S_ISDIR(mode):
            yield from _tree(child, destination / child.name)
        elif stat.S_ISREG(mode):
            yield destination / child.name, child
        else:
            raise BuildError(f"non-regular payload entry: {child}")


def _payload(efi: str, recovery: str, extra: str | None) -> dict[Path, Path]:
    entries = dict(_tree(Path(efi), Path("esp/EFI")))
    if not stat.S_ISDIR(Path(recovery).lstat().st_mode):
        raise BuildError("recovery directory is unsafe")
    entries[Path("recovery/com.apple.recovery.boot")] = Path(recovery)
    for name in RECOVERY_FILES:
        entries[Path("recovery/com.apple.recovery.boot") / name] = Path(recovery) / name
    for key in ("esp/EFI/OC/config.plist", "esp/EFI/BOOT/BOOTx64.efi",
                *(f"recovery/com.apple.recovery.boot/{name}" for name in RECOVERY_FILES)):
        source = entries.get(Path(key))
        if source is None or not stat.S_ISREG(source.lstat().st_mode) or not source.stat().st_size:
            raise BuildError(f"missing or unsafe required payload: {key}")
    if extra:
        extra_path = Path(extra).absolute()
        if extra_path.name.casefold() == "com.apple.recovery.boot" or not extra_path.name:
            raise BuildError("extra payload collides with the recovery directory")
        entries.update(_tree(extra_path, Path("recovery") / extra_path.name))
    folded = {"/".join(part.rstrip(" .").casefold() for part in path.parts)
              for path in entries}
    if len(folded) != len(entries):
        raise BuildError("payload filenames collide on the target filesystem")
    return entries


def _manifest(entries: dict[Path, Path]) -> dict:
    manifest = {}
    for destination, source in entries.items():
        mode = source.lstat().st_mode
        if stat.S_ISREG(mode):
            manifest[destination.as_posix()] = sha256_of(source)
        elif stat.S_ISDIR(mode):
            manifest[destination.as_posix()] = None
        else:
            raise BuildError(f"non-regular payload entry: {source}")
    return manifest


def _snapshot(entries: dict[Path, Path], root: Path) -> dict:
    for destination, source in entries.items():
        target = root / destination
        if stat.S_ISDIR(source.lstat().st_mode):
            target.mkdir(parents=True, exist_ok=True)
            target.chmod(0o755)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, "rb") as inp:
                if not stat.S_ISREG(os.fstat(inp.fileno()).st_mode):
                    raise BuildError(f"non-regular payload entry: {source}")
                with target.open("xb") as out:
                    shutil.copyfileobj(inp, out)
                    os.fchmod(out.fileno(), 0o644)
    return _staged_manifest(root)


def _volume_entries(root: Path, volume: str) -> dict:
    entries = dict(_tree(root, Path(volume)))
    del entries[Path(volume)]
    return entries


def _staged_manifest(root: Path) -> dict:
    if {item.name for item in root.iterdir()} != {"esp", "recovery"}:
        raise BuildError("unexpected volume in staged payload")
    return _manifest({**_volume_entries(root / "esp", "esp"),
                      **_volume_entries(root / "recovery", "recovery")})


def _live_plan(device: str, sources: tuple, serial: str | None, macos: str, validation: dict | None) -> consent.Plan:
    manifest = _manifest(_payload(*sources))
    device = _canonical_device(device)
    info = verify_usb.describe(device)
    code, problems, _ = verify_usb.evaluate(info, serial, exact_serial=bool(serial))
    if code:
        raise TargetRefused("identity or usage verification failed: " + "; ".join(problems))
    disk = _block_sysfs(device)
    info["target_darwin"] = macos
    info["efi_validation"] = validation
    info["disk_sequence"] = (disk / "diskseq").read_text().strip()
    info["device_number"] = (disk / "dev").read_text().strip()
    raw = _sudo(["lsblk", "--json", "--bytes", "--tree",
                 "--output", "PATH,TYPE,PARTN,MAJ:MIN,SIZE,START,PTTYPE,PTUUID,PARTUUID,FSTYPE,UUID,LABEL",
                 "--", device]).stdout
    info["topology"] = json.loads(raw)["blockdevices"]
    raw_signatures = _sudo(["wipefs", "--json", "--", device]).stdout
    info["disk_signatures"] = json.loads(raw_signatures)["signatures"]
    if (len(info["topology"]) != 1 or info["topology"][0]["path"] != device
            or info["topology"][0]["type"] != "disk"):
        raise BuildError("could not verify target topology")
    view = plan(info)
    review = consent.plan_from_info(
        info, "erase-and-write-installer",
        ["Save the old partition table to a private backup",
         f"Erase signatures and the partition table on {device}",
         json.dumps(view["partition_table"], sort_keys=True),
         "Copy and verify each approved payload file, then unmount both volumes"],
        "All existing data on the selected device will be lost.",
    )
    review.payload_manifest = manifest
    return review


def _write_media(esp: str, rec: str, snapshot: Path, manifest: dict) -> None:
    # Mount roots are cleaned with rmdir only. A busy or nonempty directory is
    # left in place for inspection, including after an interrupted mount.
    root = Path(tempfile.mkdtemp(prefix="autohackintosh-mount-"))
    mounts = {"esp": root / "esp", "recovery": root / "recovery"}
    mounted = []
    uncertain = set()
    try:
        for volume, node in (("esp", esp), ("recovery", rec)):
            mountpoint = mounts[volume]
            mountpoint.mkdir()
            uncertain.add(mountpoint)
            _sudo(["mount", "--", node, str(mountpoint)])
            mounted.append(mountpoint)
            for name, expected in manifest.items():
                destination = Path(name)
                if destination.parts[0] != volume:
                    continue
                written = mountpoint.joinpath(*destination.parts[1:])
                if expected is None:
                    _sudo(["mkdir", "--", str(written)])
                else:
                    _sudo(["cp", "--no-dereference", "--no-target-directory", "--",
                           str(snapshot / destination), str(written)])
        _sudo(["sync"])
        on_media = _manifest({**_volume_entries(mounts["esp"], "esp"),
                              **_volume_entries(mounts["recovery"], "recovery")})
        if on_media != manifest:
            raise BuildError("readback tree differs from approved payload")
    finally:
        failures = []
        for mountpoint in reversed(mounted):
            try:
                _sudo(["umount", "--", str(mountpoint)])
                uncertain.discard(mountpoint)
            except (BuildError, OSError) as exc:
                failures.append(f"{mountpoint}: {exc}")
        for directory in (*mounts.values(), root):
            if directory in uncertain or (directory == root and uncertain):
                continue
            try:
                directory.rmdir()
            except FileNotFoundError:
                pass
            except OSError as exc:
                failures.append(str(exc))
        if failures:
            raise BuildError("mount cleanup failed: " + "; ".join(failures))


def build(device: str, efi_dir: str, recovery_dir: str, extra_dir: str | None,
          serial_expected: str | None, backup_dir: str, *, dry_run: bool = False, macos: str = "24",
          validation: dict | None = None, validate_efi=None) -> dict:
    sources = (efi_dir, recovery_dir, extra_dir)
    problems = check_prerequisites(*sources)
    if problems:
        raise BuildError("; ".join(problems))
    approved_plan = _live_plan(device, sources, serial_expected, macos, validation)
    if dry_run:
        if validate_efi is not None:
            validate_efi(Path(efi_dir))
        fetch_recovery.verify_release(*(str(Path(recovery_dir) / name) for name in RECOVERY_FILES), macos)
        return {"outcome": "planned", "plan": json.loads(approved_plan.canonical())}
    if not serial_expected:
        raise BuildError("an exact serial is required to select the erase target")

    target = approved_plan.device
    with ExitStack() as cleanup:
        snapshot = Path(cleanup.enter_context(tempfile.TemporaryDirectory(prefix="autohackintosh-payload-")))
        manifest = _snapshot(_payload(*sources), snapshot)
        recovery = snapshot / "recovery/com.apple.recovery.boot"
        fetch_recovery.verify_release(*(str(recovery / name) for name in RECOVERY_FILES), macos)
        if manifest != approved_plan.payload_manifest:
            raise BuildError("payload changed while staging")
        os.makedirs(backup_dir, mode=0o700, exist_ok=True)
        transaction = tempfile.mkdtemp(prefix="erase-", dir=backup_dir)
        if validate_efi is not None:
            validate_efi(snapshot / "esp/EFI")
            if _staged_manifest(snapshot) != manifest:
                raise BuildError("staged payload changed during validation")
        approval = consent.request_approval(approved_plan, transaction)
        backup_partition_table(target, transaction)

        if _staged_manifest(snapshot) != manifest:
            raise BuildError("staged payload changed since review")
        if validate_efi is not None:
            validate_efi(snapshot / "esp/EFI")
            if _staged_manifest(snapshot) != manifest:
                raise BuildError("staged payload changed during validation")
        live_plan = _live_plan(device, sources, serial_expected, macos, validation)
        approval.consume(live_plan)
        _sudo(["wipefs", "-a", "--", target])
        def check_connection():
            disk = _block_sysfs(target)
            if ((disk / "diskseq").read_text().strip() != approved_plan.device_state["disk_sequence"]
                    or (disk / "dev").read_text().strip() != approved_plan.device_state["device_number"]):
                raise BuildError("target disk connection changed")

        check_connection()
        _sudo(["sgdisk", "--zap-all", target])
        check_connection()
        _sudo(["sgdisk", "-o",
               "-n", f"1:0:+{EFI_PART_MIB}M", "-t", "1:EF00", "-c", f"1:{ESP_LABEL}",
               "-n", "2:0:0", "-t", "2:AF00", "-c", f"2:{RECOVERY_LABEL}", target])
        _sudo(["partprobe", target])
        _sudo(["udevadm", "settle", "--timeout=15"])
        for command, index in ((["mkfs.vfat", "-F", "32", "-n", ESP_LABEL], 0),
                               (["mkfs.hfsplus", "-v", RECOVERY_LABEL], 1)):
            check_connection()
            esp, rec = _partition_nodes(target)
            state = verify_usb.describe(target)
            code, problems, _ = verify_usb.evaluate(state, serial_expected, exact_serial=True)
            if code:
                raise BuildError("target became unsafe before formatting: " + "; ".join(problems))
            _sudo([*command, (esp, rec)[index]])
        check_connection()
        if _partition_nodes(target) != (esp, rec):
            raise BuildError("partition topology changed before mounting")
        _write_media(esp, rec, snapshot, manifest)
    return {"outcome": "written", "device": target, "esp": esp, "recovery": rec,
            "verified_files": sum(value is not None for value in manifest.values())}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--device", required=True)
    ap.add_argument("--efi", required=True)
    ap.add_argument("--recovery-dir", required=True)
    ap.add_argument("--extra")
    ap.add_argument("--macos", default="24", choices=fetch_recovery.RECOVERY_TARGETS)
    ap.add_argument("--i-authorise-erase", metavar="SERIAL")
    ap.add_argument("--backup-dir", default=os.path.join(os.getcwd(), "autohackintosh-backup"))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    if args.i_authorise_erase and not args.dry_run:
        ap.error("use autohackintosh.py stick for writes so EFI validation is enforced")
    try:
        result = build(args.device, args.efi, args.recovery_dir, args.extra,
                       args.i_authorise_erase, args.backup_dir,
                       dry_run=args.dry_run or not args.i_authorise_erase, macos=args.macos)
    except (BuildError, consent.ConsentRequired, consent.ConsentDeclined, fetch_recovery.RecoveryError, OSError, ValueError) as exc:
        print(f"REFUSED/FAILED: {exc}", file=sys.stderr)
        return 1
    if args.json or result["outcome"] == "planned":
        print(json.dumps(result, indent=2))
    else:
        print(f"Done. {result['esp']} holds the EFI and {result['recovery']} holds recovery.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
