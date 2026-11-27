#!/usr/bin/env python3
"""Acquire, verify and stage every upstream dependency AutoHackintosh needs.

DESIGN PRINCIPLES
-----------------
* **Pin and verify.** Each artifact is pinned to an exact release and, where the
  publisher provides one, checked against a published SHA-256 before use. A
  mismatch deletes the file and fails loudly.
* **Never execute a remote script.** Everything here is a direct release
  download. Nothing is piped into ``bash``.
* **Idempotent.** Re-running reuses verified artifacts and skips work already
  done.
* **Record provenance.** ``manifest.json`` records URL, version, digest and
  bytes for every artifact, so a diagnostic report can state exactly what was
  used.

WHAT GETS STAGED
----------------
  1401 (Windows build)   -> its ``engine/app/`` is plain Python and is what
                            actually runs on Linux. Used for its rule engine,
                            vendored Hardware Sniffer and OpCore-Simplify.
  OpenCore (release zip) -> ``ocvalidate`` (the version matching the EFI) and
                            the OpenCore binaries the EFI references.
  ACPICA                 -> a native ``iasl`` (see iasl_provision.py).
  macOS recovery         -> ``BaseSystem.dmg`` via Apple's own install-assistant
                            protocol (see fetch_recovery.py).

NullMoth's NVIDIA driver is *optional* and only staged when the hardware needs
it; see ``references/nvidia-nullmoth.md``.

USAGE
-----
    ./fetch_deps.py --workdir ~/.cache/autohackintosh
    ./fetch_deps.py --workdir ... --check          # verify only, download nothing
    ./fetch_deps.py --workdir ... --json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import tempfile
import uuid
from pathlib import Path, PurePosixPath
import shutil
import sys
import urllib.request
import zipfile

#: Pinned upstream releases. Bump deliberately, re-read release notes, re-test.
#: ``sha256`` is the *expected* digest published with the release; a cache or a
#: download that does not match it is refused, not merely described.
PINS = {
    "1401": {
        "repo": "nullmoth/1401",
        "version": "v3.1",
        "asset": "1401-Windows-3.1.0.zip",
        "sha256": "db0121d1dbec70770d5fe27be42cb3610c11306a1384d4acbb53f304528513f8",
        "why": "headless rule engine + vendored Hardware Sniffer and OpCore-Simplify",
        "license": "PolyForm Noncommercial 1.0.0",
        "url": "https://github.com/nullmoth/1401/releases/download/v3.1/1401-Windows-3.1.0.zip",
    },
    "opencore": {
        "repo": "acidanthera/OpenCorePkg",
        "version": "1.0.8",
        "asset": "OpenCore-1.0.8-RELEASE.zip",
        # This digest was verified on 2026-11-27 from the downloaded artifact;
        # OpenCore does not publish a per-asset checksum, so it is a
        # *recorded* digest, not a publisher-signed one. See AUDIT F06 note.
        "expected_sha256": "2011e8b7216ecb2645d97ea710df965947edd406846e92050b9bc4190be6d27b",
        "why": "OpenCore binaries plus the matching ocvalidate used for validation",
        "license": "BSD-3-Clause",
        "url": "https://github.com/acidanthera/OpenCorePkg/releases/download/1.0.8/OpenCore-1.0.8-RELEASE.zip",
    },
}

#: ACPICA release compiled for iasl (1401 pins this exact version; see iasl_provision.py).
ACPICA_VERSION = "20260408"
ACPICA_URL = f"https://codeload.github.com/acpica/acpica/tar.gz/refs/tags/{ACPICA_VERSION}"
ACPICA_SHA256 = "ddc5d3e0f54030e2348484fff681861a161efb4e388e20631209574e7884ad39"
MARKERS = {"1401": "engine/app/p1401", "opencore": "Utilities/ocvalidate"}


def sha256_of(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download(url: str, dest: str, *, expect_sha256: str | None = None,
             expect_size: int | None = None, timeout: int = 300) -> dict:
    """Download with verification. Never leaves a partial or unverified file."""
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    request = urllib.request.Request(url, headers={"User-Agent": "autohackintosh"})
    with tempfile.NamedTemporaryFile(dir=os.path.dirname(dest), prefix=".download-",
                                     delete=False) as fh:
        partial = fh.name
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                shutil.copyfileobj(response, fh, length=1 << 20)
            fh.flush()
            size = os.path.getsize(partial)
            digest = sha256_of(partial)
            if expect_size is not None and size != expect_size:
                raise RuntimeError(f"{url}: expected {expect_size} bytes, got {size}")
            if expect_sha256 is not None and digest != expect_sha256:
                raise RuntimeError(f"{url}: SHA-256 mismatch")
            os.replace(partial, dest)
        finally:
            Path(partial).unlink(missing_ok=True)
    return {"url": url, "path": dest, "bytes": size, "sha256": digest}


def fetch_published_sums(entry: dict) -> dict[str, str]:
    """Fetch a release's ``SHA256SUMS.txt`` when the project publishes one."""
    url = entry["url"]
    sums_url = url.rsplit("/", 1)[0] + "/SHA256SUMS.txt"
    try:
        request = urllib.request.Request(sums_url, headers={"User-Agent": "autohackintosh"})
        with urllib.request.urlopen(request, timeout=60) as response:
            text = response.read().decode("utf-8", "replace")
    except Exception:
        return {}
    sums = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 2 and len(parts[0]) == 64:
            sums[parts[1].lstrip("*")] = parts[0]
    return sums


def _expected_digest(entry: dict) -> str | None:
    """The trusted digest for this artifact: publisher's, or our recorded one.

    The pin's ``sha256`` is a publisher checksum; ``expected_sha256`` is a
    locally recorded one. Either way the cache/download must match it — there
    is no "hash it anyway and carry on" path.
    """
    return entry.get("sha256") or entry.get("expected_sha256")


def stage(key: str, entry: dict, workdir: str, *, verify_only: bool) -> dict:
    dest = os.path.join(workdir, "downloads", entry["asset"])
    expected = _expected_digest(entry)
    if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected):
        # A required artifact without a trusted digest fails closed.
        if verify_only:
            return {"key": key, "status": "missing", "reason": "no trusted digest", **entry}
        raise RuntimeError(f"{key}: no pinned digest to verify {entry['asset']} against")

    if os.path.lexists(dest):
        digest = sha256_of(dest) if os.path.isfile(dest) and not os.path.islink(dest) else None
        if digest == expected:
            return {"key": key, "status": "cached", "path": dest, "verified": True,
                    "bytes": os.path.getsize(dest), "sha256": digest, **entry}
        if verify_only:
            return {"key": key, "status": "cached-invalid",
                    "expected": expected, "found": digest, **entry}
    if verify_only:
        return {"key": key, "status": "missing", **entry}

    result = download(entry["url"], dest, expect_sha256=expected)
    how = "publisher SHA256SUMS.txt" if entry.get("sha256") else "recorded project digest"
    return {"key": key, "status": "downloaded", "verified_against": how,
            **entry, **result}


def _dir_containing(root: str, marker: str) -> str | None:
    """Directory at or one level below ``root`` that contains ``marker``.

    Release archives disagree about wrapping everything in a named directory
    (1401's zip puts every path under ``1401/``), so both depths are checked and
    the *containing directory* is returned, never the marker path itself.
    """
    if os.path.exists(os.path.join(root, marker)):
        return root
    try:
        for entry in sorted(os.listdir(root)):
            candidate = os.path.join(root, entry)
            if os.path.isdir(candidate) and os.path.exists(os.path.join(candidate, marker)):
                return candidate
    except OSError:
        pass
    return None


def _zip_entries(zf: zipfile.ZipFile) -> dict:
    entries = {}
    for member in zf.infolist():
        path = PurePosixPath(member.filename)
        if path.is_absolute() or not path.parts or ".." in path.parts or path in entries:
            raise RuntimeError(f"unsafe or duplicate ZIP member: {member.filename!r}")
        entries[path] = member
    return entries


def _same_contents(a, b) -> bool:
    while True:
        chunk = a.read(1 << 20)
        if chunk != b.read(1 << 20):
            return False
        if not chunk:
            return True


def _verified_tree(target: Path, archive: str, marker: str) -> str | None:
    if target.is_symlink() or not target.is_dir():
        return None
    with zipfile.ZipFile(archive) as zf:
        entries = _zip_entries(zf)
        files = {path: member for path, member in entries.items() if not member.is_dir()}
        dirs = {parent for path in entries for parent in path.parents}
        dirs.update(path for path, member in entries.items() if member.is_dir())
        actual_files = set()
        for directory, subdirs, names in os.walk(target):
            directory = Path(directory)
            for name in subdirs:
                path = directory / name
                if path.is_symlink() or path.relative_to(target) not in dirs:
                    return None
            for name in names:
                path = directory / name
                relative = path.relative_to(target)
                if path.is_symlink() or not path.is_file() or relative not in files:
                    return None
                with path.open("rb") as local, zf.open(files[relative]) as original:
                    if not _same_contents(local, original):
                        return None
                actual_files.add(relative)
        if actual_files != set(files):
            return None
        if any(not (target / directory).is_dir() for directory in dirs):
            return None
    return _dir_containing(str(target), marker)


def extract(key: str, archive: str, workdir: str, marker: str, *, expected: str) -> str:
    """Reuse extraction only when the archive pin and complete tree match."""
    if os.path.islink(archive) or sha256_of(archive) != expected:
        raise RuntimeError(f"{key}: archive does not match its pinned SHA-256")
    target = Path(workdir) / key
    found = _verified_tree(target, archive, marker)
    if found:
        return found
    with tempfile.TemporaryDirectory(dir=workdir, prefix=f".{key}-extract-") as tmp:
        staged = Path(tmp) / "tree"
        staged.mkdir()
        with zipfile.ZipFile(archive) as zf:
            _zip_entries(zf)
            zf.extractall(staged)
        root = _dir_containing(str(staged), marker)
        if not root or sha256_of(archive) != expected:
            raise RuntimeError(f"{key}: invalid extraction or archive changed")
        relative = Path(root).relative_to(staged)
        if os.path.lexists(target):
            target.rename(target.with_name(f".{key}-invalid-{uuid.uuid4().hex}"))
        staged.rename(target)
    return str(target / relative)


def verified_root(workdir: str, key: str) -> str:
    entry = PINS[key]
    cached = stage(key, entry, workdir, verify_only=True)
    if cached["status"] != "cached":
        raise RuntimeError(f"{key}: missing or invalid pinned archive")
    root = _verified_tree(Path(workdir) / key, cached["path"], MARKERS[key])
    if root is None:
        raise RuntimeError(f"{key}: missing or invalid extracted tree")
    return root


def main(argv: list[str] | None = None) -> int:
    import fetch_recovery

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--workdir", required=True, help="where dependencies are staged")
    ap.add_argument("--check", action="store_true", help="verify what exists; download nothing")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--skip-recovery", action="store_true")
    ap.add_argument("--macos", default="24", choices=fetch_recovery.RECOVERY_TARGETS, help="Darwin major")
    args = ap.parse_args(argv)

    workdir = os.path.abspath(os.path.expanduser(args.workdir))
    if not args.check:
        os.makedirs(workdir, exist_ok=True)
    report: dict = {"workdir": workdir, "fetched": [], "staged": {}, "problems": []}

    for key, entry in PINS.items():
        try:
            report["fetched"].append(stage(key, entry, workdir, verify_only=args.check))
        except Exception as exc:  # noqa: BLE001
            report["problems"].append(f"{key}: {exc}")

    # Look archives up by key, never by position: a failed earlier download
    # must not silently select the wrong archive for a later one.
    fetched = {item.get("key"): item for item in report["fetched"] if isinstance(item, dict)}

    if args.check:
        # --check is read-only: report what is present and verified, change
        # nothing. No extraction, no chmod, no downloads, no manifest write.
        bad = [i for i in report["fetched"]
               if isinstance(i, dict) and i.get("status") in ("missing", "cached-invalid")]
        for key, item in fetched.items():
            if item.get("status") == "cached" and not _verified_tree(
                    Path(workdir) / key, item["path"], MARKERS[key]):
                report["problems"].append(f"{key}: missing or invalid extracted tree")
        if args.json:
            print(json.dumps(report, indent=2))
        else:
            for item in report["fetched"]:
                print(f"{item['status']:>15}  {item['asset']}")
            for problem in report["problems"]:
                print(f"PROBLEM: {problem}", file=sys.stderr)
        return 1 if report["problems"] or bad else 0

    engine_app = None
    first = fetched.get("1401")
    if not first or not first.get("path"):
        report["problems"].append("1401 artifact missing; cannot extract")
    else:
        try:
            root = extract("1401", first["path"], workdir, MARKERS["1401"], expected=_expected_digest(PINS["1401"]))
            engine_app = os.path.join(root, "engine", "app")
            report["staged"]["engine_app"] = engine_app
            extras = {
                "hardware_sniffer": os.path.join(engine_app, "upstream", "Hardware-Sniffer"),
                "opcore_scripts": os.path.join(engine_app, "upstream", "OpCore-Simplify", "Scripts"),
            }
            for name, path in extras.items():
                report["staged"][name] = path if os.path.isdir(path) else None
                if not os.path.isdir(path):
                    report["problems"].append(f"{name} missing from the 1401 archive")
            pinned = os.path.join(engine_app, "upstream", "PINNED.json")
            if os.path.isfile(pinned):
                with open(pinned, encoding="utf-8") as fh:
                    report["staged"]["upstream_pins"] = json.load(fh)
        except Exception as exc:  # noqa: BLE001
            report["problems"].append(f"1401 extraction: {exc}")

    oc = fetched.get("opencore")
    if not oc or not oc.get("path"):
        report["problems"].append("OpenCore artifact missing; cannot extract")
    else:
        try:
            oc_root = extract("opencore", oc["path"], workdir, MARKERS["opencore"], expected=_expected_digest(PINS["opencore"]))
            report["staged"]["opencore"] = oc_root
            binary = os.path.join(oc_root, "Utilities", "ocvalidate", "ocvalidate.linux")
            if not os.path.isfile(binary):
                raise RuntimeError("release lacks the Linux ocvalidate binary")
            os.chmod(binary, 0o755)
            report["staged"]["ocvalidate"] = binary
        except Exception as exc:  # noqa: BLE001
            report["problems"].append(f"OpenCore extraction: {exc}")

    if not args.skip_recovery:
        try:
            report["staged"]["recovery"] = fetch_recovery.fetch_into(
                os.path.join(workdir, "recovery"), args.macos)
        except (OSError, fetch_recovery.RecoveryError) as exc:
            report["problems"].append(f"recovery: {exc}")

    report["acpica"] = {"version": ACPICA_VERSION, "url": ACPICA_URL}
    manifest_path = os.path.join(workdir, "manifest.json")
    with open(manifest_path, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2)

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        for item in report["fetched"]:
            print(f"{item['status']:>10}  {item.get('asset')}  {item.get('bytes', 0):>12} bytes")
        for name, path in report["staged"].items():
            if isinstance(path, str):
                print(f"    staged  {name}: {path}")
        print(f"\nmanifest: {manifest_path}")
        for problem in report["problems"]:
            print(f"PROBLEM: {problem}", file=sys.stderr)
    return 1 if report["problems"] else 0


if __name__ == "__main__":
    sys.exit(main())
