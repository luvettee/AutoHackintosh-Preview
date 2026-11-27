#!/usr/bin/env python3
"""Provision the pinned ACPICA compiler used to process ACPI tables."""

from __future__ import annotations

import argparse
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile

from fetch_deps import ACPICA_SHA256, ACPICA_URL, ACPICA_VERSION, download

DEFAULT_VERSION = ACPICA_VERSION
PRIMARY_NAME = "iasl-dev"
SLOTS = (PRIMARY_NAME, "iasl-stable", "iasl")


def _version_of(path: str) -> str | None:
    try:
        out = subprocess.run([path, "-v"], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode:
        return None
    match = re.search(r"\bversion\s+(\d{8})\b", out.stdout + out.stderr, re.IGNORECASE)
    return match[1] if match else None


def _selected(target: str) -> str | None:
    return next((os.path.join(target, name) for name in SLOTS
                 if os.path.lexists(os.path.join(target, name))), None)


def _build_from_source(workdir: str) -> str:
    tarball = os.path.join(workdir, "acpica.tar.gz")
    download(ACPICA_URL, tarball, expect_sha256=ACPICA_SHA256)
    with tarfile.open(tarball) as source:
        source.extractall(workdir, filter="data")
    src = os.path.join(workdir, f"acpica-{DEFAULT_VERSION}")
    result = subprocess.run(["make", "iasl"], cwd=src, capture_output=True, text=True)
    binary = os.path.join(src, "generate", "unix", "bin", "iasl")
    if result.returncode or _version_of(binary) != DEFAULT_VERSION:
        raise RuntimeError(f"ACPICA build failed: {result.stderr[-2000:]}")
    return binary


def provision(target: str, version: str, dry_run: bool) -> int:
    if version != DEFAULT_VERSION:
        print(f"No source digest is pinned for ACPICA {version}", file=sys.stderr)
        return 3
    selected = _selected(target)
    if selected and not os.path.islink(selected) and _version_of(selected) == version:
        return 0
    dest = os.path.join(target, PRIMARY_NAME)
    if dry_run:
        print(f"Would build ACPICA {version} and install it at {dest}")
        return 0

    try:
        os.makedirs(target, mode=0o700, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="autohackintosh-iasl-") as work:
            binary = _build_from_source(work)
            fd, staged = tempfile.mkstemp(dir=target, prefix=".iasl-")
            try:
                with os.fdopen(fd, "wb") as out, open(binary, "rb") as inp:
                    shutil.copyfileobj(inp, out)
                    os.fchmod(out.fileno(), 0o755)
                if _version_of(staged) != version:
                    raise RuntimeError("staged compiler failed its version check")
                os.replace(staged, dest)
            finally:
                if os.path.exists(staged):
                    os.unlink(staged)
        return 0
    except (OSError, RuntimeError, tarfile.TarError, subprocess.SubprocessError) as exc:
        print(f"Could not provision ACPICA {version}: {exc}", file=sys.stderr)
        return 3


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--target", required=True)
    ap.add_argument("--version", default=DEFAULT_VERSION)
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    if platform.system() != "Linux":
        return 3
    if args.check:
        selected = _selected(args.target)
        usable = (args.version == DEFAULT_VERSION and selected is not None
                  and not os.path.islink(selected) and _version_of(selected) == args.version)
        return 0 if usable else 3
    return provision(args.target, args.version, args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
