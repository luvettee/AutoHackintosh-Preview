#!/usr/bin/env python3
"""Download a macOS recovery image directly from Apple's servers.

THE PROTOCOL
------------
Matches Acidanthera's ``macrecovery.py`` (the reference implementation).

1. ``GET  https://osrecovery.apple.com/`` returns a ``session=...`` cookie.
   Posting to the payload endpoint without it returns HTTP 401.
2. ``POST https://osrecovery.apple.com/InstallationPayload/RecoveryImage`` with
   ``cid``/``sn``/``bid``/``k``/``fg``/``os``. The reply is ``key: value``
   lines:

       AP  product id                    AU  image URL        AT  image token
       AH  image hash                    CU  chunklist URL    CT  chunklist token
       CH  chunklist hash

   The image is fetched with ``Cookie: AssetToken=<AT>``; the chunklist with
   ``Cookie: AssetToken=<CT>`` at the ``CU`` URL. ``AH`` is the image's hash
   and ``CH`` the chunklist's; ``AP`` is a *product* id, not a board-id echo.

3. The chunklist is a binary structure that must be verified:

       header   '<4sIBBBxQQQ'  magic b'CNKL', header size, version, chunk
                               method, signature method, chunk count, chunk
                               offset, signature offset
       records  '<I32s'        size + sha256, one per chunk
       then     a 256-byte RSA signature (method 1) — verified against Apple's
                EFI ROM public key — or a 32-byte digest (method 2, unsigned,
                and therefore refused as verification evidence)

   The signature is ``pow(sig, 0x10001, pubkey) == PKCS1-padded(sha256(header
   + records))``. Only then are the per-chunk SHA-256s checked against the
   image. The signed image must contain a build for the requested Darwin major.

INTEGRITY RULES THAT MATTER
---------------------------
* A failed download or failed verification deletes/quarantines the file; it is
  never left as a "good enough" image.
* A ``--check`` cache hit is *not* a success unless it re-verifies cryptographically.
* The image is written to a temporary file and published atomically only after
  the chunklist verifies.
* A size threshold is not a substitute for authenticity.

LEGAL POSTURE
-------------
The image is Apple's; this script downloads it on the user's behalf and does not
bundle or redistribute Apple binaries. Installing macOS on non-Apple hardware
is restricted by Apple's licence — see the README.

USAGE
-----
    ./fetch_recovery.py --dest ~/.cache/autohackintosh/recovery
    ./fetch_recovery.py --dest ... --macos 24
    ./fetch_recovery.py --dest ... --check
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import plistlib
import re
import subprocess
import os
import random
import struct
import sys
import tempfile
import urllib.request

SESSION_URL = "https://osrecovery.apple.com/"
RECOVERY_URL = "https://osrecovery.apple.com/InstallationPayload/RecoveryImage"

#: MacPro7,1 — yields a modern recovery image for a supported Mac.
RECOVERY_TARGETS = {
    "23": ("Mac-827FAC58A8FDFA22", "default"),
    "24": ("Mac-7BA5B2D9E42DDD94", "default"),
    "25": ("Mac-CFF7D910A743CAAF", "latest"),
}
#: 17 zeros is the neutral serial macrecovery uses (MLB_ZERO).
DEFAULT_SERIAL = "00000000000000000"

_CHUNK_HEADER = struct.Struct("<4sIBBBxQQQ")
_CHUNK_RECORD = struct.Struct("<I32s")
_CHUNK_SIZE = 10 * 1024 * 1024

#: Apple's EFI ROM public key, used to verify the chunklist signature.
#: From OpenCorePkg/Utilities/macrecovery/macrecovery.py (Apache-2.0).
APPLE_EFI_ROM_PUBLIC_KEY_1 = int(
    "C3E748CAD9CD384329E10E25A91E43E1A762FF529ADE578C935BDDF9B13F2179D4855"
    "E6FC89E9E29CA12517D17DFA1EDCE0BEBF0EA7B461FFE61D94E2BDF72C196F89ACD"
    "3536B644064014DAE25A15DB6BB0852ECBD120916318D1CCDEA3C84C92ED743FC176"
    "D0BACA920D3FCF3158AFF731F88CE0623182A8ED67E650515F75745909F07D415F55"
    "FC15A35654D118C55A462D37A3ACDA08612F3F3F6571761EFCCBCC299AEE99B3A4FD"
    "6212CCFFF5EF37A2C334E871191F7E1C31960E010A54E86FA3F62E6D6905E1CD5773"
    "2410A3EB0C6B4DEFDABE9F59BF1618758C751CD56CEF851D1C0EAA1C558E37AC108"
    "DA9089863D20E2E7E4BF475EC66FE6B3EFDCF", 16)


class RecoveryError(RuntimeError):
    """A verification or download failure; the file is quarantined, not kept."""


def _hex_id(length: int) -> str:
    return "".join(random.choices("0123456789ABCDEF", k=length))


def _query(url: str, headers: dict, data: bytes | None = None, raw: bool = False):
    request = urllib.request.Request(url, headers=headers, data=data)
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            body = response.read()
            if raw:
                return dict(response.headers), body
            return dict(response.headers), body.decode("utf-8", "replace")
    except urllib.error.HTTPError as error:
        raise RecoveryError(f"HTTP {error.code} from {url}") from error


def get_session() -> str:
    """Step 1: the session cookie. Without it Apple answers 401."""
    headers, _ = _query(SESSION_URL, {
        "Host": "osrecovery.apple.com",
        "Connection": "close",
        "User-Agent": "InternetRecovery/1.0",
    })
    for name, value in headers.items():
        if name.lower() == "set-cookie":
            for part in value.split("; "):
                if part.startswith("session="):
                    return part
    raise RecoveryError("Apple returned no session cookie")


def get_image_info(session: str, board: str, serial: str = DEFAULT_SERIAL,
                   os_type: str = "default") -> dict:
    """Step 2: the image description.

    Returns the response dict. Requires AP, AU, AH, AT, CU, CH, CT; anything
    missing is a verification failure, not an option.
    """
    headers = {
        "Host": "osrecovery.apple.com",
        "Connection": "close",
        "User-Agent": "InternetRecovery/1.0",
        "Cookie": session,
        "Content-Type": "text/plain",
    }
    post = {
        "cid": _hex_id(16), "sn": serial, "bid": board,
        "k": _hex_id(64), "fg": _hex_id(64), "os": os_type,
    }
    body = "\n".join(f"{key}={value}" for key, value in post.items()).encode()
    _, output = _query(RECOVERY_URL, headers, body)

    info = {}
    for line in output.splitlines():
        key, _, value = line.partition(": ")
        if key and value:
            info[key] = value
    for required in ("AP", "AU", "AH", "AT", "CU", "CH", "CT"):
        if required not in info:
            raise RecoveryError(
                f"Apple's reply lacked {required}; it said:\n{output[:800]}")
    return info


def _download(url: str, dest: str, token: str) -> str:
    """Stream to ``dest`` atomically; returns the SHA-256 of what was written."""
    from urllib.parse import urlparse
    host = urlparse(url).hostname
    headers = {
        "Host": host,
        "Connection": "close",
        "User-Agent": "InternetRecovery/1.0",
        "Cookie": f"AssetToken={token}",
    }
    partial = dest + ".part"
    digest = hashlib.sha256()
    request = urllib.request.Request(url, headers=headers)
    last_report = 0
    with urllib.request.urlopen(request, timeout=600) as response, open(partial, "wb") as fh:
        while True:
            chunk = response.read(1 << 20)
            if not chunk:
                break
            fh.write(chunk)
            digest.update(chunk)
            written = fh.tell()
            if written - last_report >= 64 << 20:
                last_report = written
                print(f"  ... {written / 1024**2:.0f} MiB", file=sys.stderr, flush=True)
    os.replace(partial, dest)
    return digest.hexdigest()


def _signature_padded(digest: bytes) -> int:
    """The PKCS1 plaintext the RSA signature must decrypt to (matches macrecovery)."""
    return int(
        f"0x1{'f' * 404}003031300d060960864801650304020105000420{'0' * 64}",
        16) | int.from_bytes(digest, "big")


def verify_chunklist(dmg_path: str, chunklist_path: str) -> tuple[bool, str]:
    """Verify a recovery image against Apple's signed chunklist.

    Returns (ok, explanation). Only a CNKL chunklist whose header+records RSA-
    signature verifies against Apple's EFI ROM public key is accepted; a
    signature-method-2 ("missing digital signature") or malformed file fails
    verification, it does not quietly pass.
    """
    try:
        with open(chunklist_path, "rb") as fh:
            header = fh.read(_CHUNK_HEADER.size)
            if len(header) != _CHUNK_HEADER.size:
                return False, "chunklist shorter than its header"
            magic, header_size, version, chunk_method, sig_method, count, \
                chunk_offset, signature_offset = _CHUNK_HEADER.unpack(header)
            if magic != b"CNKL":
                return False, f"chunklist magic is {magic!r}, expected b'CNKL'"
            if header_size != _CHUNK_HEADER.size or version != 1 or chunk_method != 1:
                return False, f"unexpected chunklist header {header_size}/{version}/{chunk_method}"
            if sig_method not in (1, 2):
                return False, f"chunklist signature method {sig_method} is unsupported"
            if chunk_offset != _CHUNK_HEADER.size:
                return False, f"chunk offset {chunk_offset} != {_CHUNK_HEADER.size}"
            if count == 0:
                return False, "chunklist contains zero chunks"
            if signature_offset != chunk_offset + _CHUNK_RECORD.size * count:
                return False, "signature offset disagrees with chunk count"
            records = []
            hash_ctx = hashlib.sha256(header)
            for _ in range(count):
                record = fh.read(_CHUNK_RECORD.size)
                if len(record) != _CHUNK_RECORD.size:
                    return False, "chunklist truncated inside its chunk records"
                hash_ctx.update(record)
                size, digest = _CHUNK_RECORD.unpack(record)
                records.append((size, digest))
            digest = hash_ctx.digest()
            if sig_method == 1:
                sig = fh.read(256)
                if len(sig) != 256:
                    return False, "chunklist signature block shorter than 256 bytes"
                signature = int.from_bytes(sig, "little")
                if pow(signature, 0x10001, APPLE_EFI_ROM_PUBLIC_KEY_1) != _signature_padded(digest):
                    return False, "chunklist RSA signature did not verify"
            elif sig_method == 2:
                # Method 2 carries no signature: treated as unsigned and refused.
                rest = fh.read(32)
                if rest == digest:
                    return False, "chunklist is unsigned (signature method 2)"
                return False, "chunklist signature method 2 with unexpected trailing data"
            if fh.read(1):
                return False, "trailing bytes after the chunklist signature"
    except OSError as exc:
        return False, f"chunklist unreadable: {exc}"

    total = sum(size for size, _ in records)
    actual = os.path.getsize(dmg_path)
    if total != actual:
        return False, f"image is {actual} bytes, chunklist describes {total}"
    with open(dmg_path, "rb") as fh:
        for index, (size, expected) in enumerate(records):
            data = fh.read(size)
            if hashlib.sha256(data).digest() != expected:
                return False, f"chunk {index} of {count} failed its SHA-256"
    return True, f"all {count} chunks verified + signed chunklist"


def verify_release(dmg: str, chunklist: str, darwin: str) -> dict:
    if darwin not in RECOVERY_TARGETS:
        raise RecoveryError(f"unsupported Darwin major: {darwin}")
    ok, why = verify_chunklist(dmg, chunklist)
    if not ok:
        raise RecoveryError(f"recovery image failed verification: {why}")
    try:
        result = subprocess.run(["7z", "e", "-so", "-r", dmg, "SystemVersion.plist"],
                                capture_output=True, timeout=120, check=True)
        version = plistlib.loads(result.stdout)
        build = version["ProductBuildVersion"]
        product = version["ProductVersion"]
        if not isinstance(build, str) or not re.fullmatch(r"[0-9]{2}[A-Z][0-9]{1,6}[a-z]?", build):
            raise ValueError("invalid build version")
        if not isinstance(product, str) or not re.fullmatch(r"[0-9]+(?:\.[0-9]+){1,2}", product):
            raise ValueError("invalid product version")
    except (OSError, subprocess.SubprocessError, ValueError, TypeError, KeyError,
            plistlib.InvalidFileException) as exc:
        raise RecoveryError("could not inspect signed recovery SystemVersion.plist (requires 7z)") from exc
    if not build.startswith(darwin):
        raise RecoveryError(f"signed recovery build {build} does not match Darwin {darwin}")
    return {"verified": why, "build": build, "version": product}


def fetch_into(dest: str, darwin: str = "24") -> dict:
    if darwin not in RECOVERY_TARGETS:
        raise RecoveryError(f"unsupported Darwin major: {darwin}")
    os.makedirs(dest, exist_ok=True)
    fd = os.open(dest, os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        return _fetch_locked(dest, darwin)
    finally:
        os.close(fd)


def _fetch_locked(dest: str, darwin: str) -> dict:
    board, os_type = RECOVERY_TARGETS[darwin]
    dmg = os.path.join(dest, "BaseSystem.dmg")
    chunklist = os.path.join(dest, "BaseSystem.chunklist")
    if os.path.isfile(dmg) and os.path.isfile(chunklist):
        try:
            verified = verify_release(dmg, chunklist, darwin)
        except RecoveryError as exc:
            _say(f"cached recovery is unusable: {exc}")
        else:
            return {"status": "cached", "dmg": dmg, "bytes": os.path.getsize(dmg),
                    "board": board, "requested_os_type": os_type, **verified}

    session = get_session()
    info = get_image_info(session, board, DEFAULT_SERIAL, os_type)

    # AP is a product identifier; comparing across the default/latest requests is
    # how upstream confirms the image is for this class of Mac. We require it to
    # be present and print it, but do not compare it to the board id.
    product = info["AP"]

    # Temporary files; published atomically only after verification.
    with tempfile.TemporaryDirectory(dir=dest, prefix=".recovery-") as tmp:
        tmp_dmg = os.path.join(tmp, "BaseSystem.dmg")
        tmp_cl = os.path.join(tmp, "BaseSystem.chunklist")
        try:
            print(f"downloading {info['CU']} (chunklist)", file=sys.stderr)
            _download(info["CU"], tmp_cl, info["CT"])
        except Exception as exc:
            _quarantine(tmp_cl)
            raise RecoveryError(f"could not download the signed chunklist: {exc}")
        try:
            print(f"downloading {info['AU']} ({product})", file=sys.stderr)
            digest = _download(info["AU"], tmp_dmg, info["AT"])
        except Exception as exc:
            _quarantine(tmp_dmg)
            _quarantine(tmp_cl)
            raise RecoveryError(f"could not download the recovery image: {exc}")

        verified = verify_release(tmp_dmg, tmp_cl, darwin)
        os.replace(tmp_dmg, dmg)
        os.replace(tmp_cl, chunklist)

    return {"status": "downloaded", "dmg": dmg, "bytes": os.path.getsize(dmg),
            "sha256": digest, "product": product, "requested_os_type": os_type,
            "board": board, **verified}


def _quarantine(path: str) -> None:
    """Remove a failed or unverified download so a later run cannot reuse it."""
    try:
        if os.path.exists(path):
            os.unlink(path)
    except OSError:
        pass


def _say(msg: str) -> None:
    print(f"[recovery] {msg}", file=sys.stderr, flush=True)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dest", required=True)
    ap.add_argument("--macos", default="24", choices=RECOVERY_TARGETS, help="Darwin major")
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args(argv)

    dest = os.path.abspath(os.path.expanduser(args.dest))
    if args.check:
        dmg = os.path.join(dest, "BaseSystem.dmg")
        chunklist = os.path.join(dest, "BaseSystem.chunklist")
        if not os.path.isfile(dmg):
            print("no BaseSystem.dmg present")
            return 1
        if not os.path.isfile(chunklist):
            size = os.path.getsize(dmg)
            print(f"{dmg}: {size / 1024**2:.0f} MiB, no chunklist — NOT verified")
            return 1
        try:
            verified = verify_release(dmg, chunklist, args.macos)
        except RecoveryError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        print(f"{dmg}: {verified['verified']} ({verified['build']})")
        return 0

    try:
        result = fetch_into(dest, args.macos)
    except Exception as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        return 1

    print(f"{result['status']}: {result['dmg']} ({result['bytes'] / 1024**2:.0f} MiB)")
    print(f"verification: {result['verified']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
