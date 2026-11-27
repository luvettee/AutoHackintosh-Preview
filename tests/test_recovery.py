#!/usr/bin/env python3
"""Tests for ``scripts/fetch_recovery.py`` verification.

These use synthetic CNKL chunklists and DMGs, never a real Apple download. The
signing key here is a test key, NOT Apple's private key — the test builds a
chunklist with a valid RSA signature over the test digest and asserts it both
verifies and rejects tampered variants. The point is the *logic* of the
verifier, not Apple's specific key.
"""

from __future__ import annotations

import hashlib
import os
import plistlib
import subprocess
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(os.path.dirname(HERE), "scripts")
sys.path.insert(0, SCRIPTS)

import fetch_recovery  # noqa: E402

H = fetch_recovery._CHUNK_HEADER
R = fetch_recovery._CHUNK_RECORD


def _chunklist(digest: bytes, sig_method: int = 1, sig: bytes = b"") -> bytes:
    """Build a minimal valid CNKL chunklist for a one-chunk image of `digest`."""
    header = H.pack(b"CNKL", H.size, 1, 1, sig_method, 1, H.size,
                    H.size + R.size)
    record = R.pack(1024, digest)
    if sig_method == 1:
        tail = sig.ljust(256, b"\x00")
    else:
        tail = digest
    return header + record + tail


def _image_payload(seed: bytes = b"x" * 1024) -> bytes:
    return seed


def _sha256(b: bytes) -> bytes:
    return hashlib.sha256(b).digest()


class ChunklistVerification(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dmg = os.path.join(self.tmp.name, "BaseSystem.dmg")
        self.cl = os.path.join(self.tmp.name, "BaseSystem.chunklist")
        self.data = _image_payload()
        with open(self.dmg, "wb") as fh:
            fh.write(self.data)

    def tearDown(self):
        self.tmp.cleanup()

    def _write_cl(self, data: bytes):
        with open(self.cl, "wb") as fh:
            fh.write(data)

    def test_valid_signed_chunklist_verifies(self):
        """A correctly signed chunklist over matching bytes passes."""
        from cryptography.hazmat.primitives.asymmetric import rsa
        digest = _sha256(self.data)
        header = H.pack(b"CNKL", H.size, 1, 1, 1, 1, H.size, H.size + R.size)
        record = R.pack(1024, digest)
        padded = fetch_recovery._signature_padded(hashlib.sha256(header + record).digest())

        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        n = key.public_key().public_numbers().n
        d = key.private_numbers().d
        sig = pow(padded, d, n)
        cl = header + record + sig.to_bytes(256, "little")
        self._write_cl(cl)
        with mock.patch.object(fetch_recovery, "APPLE_EFI_ROM_PUBLIC_KEY_1", n):
            ok, why = fetch_recovery.verify_chunklist(self.dmg, self.cl)
        self.assertTrue(ok, why)

    def test_tampered_signature_fails(self):
        """A signature for different bytes does not verify."""
        from cryptography.hazmat.primitives.asymmetric import rsa
        digest = _sha256(self.data)
        header = H.pack(b"CNKL", H.size, 1, 1, 1, 1, H.size, H.size + R.size)
        record = R.pack(1024, digest)
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        n = key.public_key().public_numbers().n
        d = key.private_numbers().d
        wrong_padded = fetch_recovery._signature_padded(hashlib.sha256(b"tampered").digest())
        sig = pow(wrong_padded, d, n)
        cl = header + record + sig.to_bytes(256, "little")
        self._write_cl(cl)
        with mock.patch.object(fetch_recovery, "APPLE_EFI_ROM_PUBLIC_KEY_1", n):
            ok, why = fetch_recovery.verify_chunklist(self.dmg, self.cl)
        self.assertFalse(ok)

    def test_unsigned_method2_is_refused(self):
        """An unsigned chunklist (method 2) must NOT be treated as verified."""
        digest = _sha256(self.data)
        cl = _chunklist(digest, sig_method=2)
        self._write_cl(cl)
        ok, why = fetch_recovery.verify_chunklist(self.dmg, self.cl)
        self.assertFalse(ok)

    def _sign(self, content):
        from cryptography.hazmat.primitives.asymmetric import rsa
        self.test_key = rsa.generate_private_key(public_exponent=65537, key_size=2048).private_numbers()
        self.enterContext(mock.patch.object(fetch_recovery, "APPLE_EFI_ROM_PUBLIC_KEY_1",
                                          self.test_key.public_numbers.n))
        return self._sign_with_current_key(content)

    def _sign_with_current_key(self, content):
        key = self.test_key
        unsigned = _chunklist(_sha256(content))[:-256]
        padded = fetch_recovery._signature_padded(_sha256(unsigned))
        signature = pow(padded, key.d, key.public_numbers.n).to_bytes(256, "little")
        return unsigned + signature

    def test_corrupt_image_fails_after_valid_signature(self):
        self._write_cl(self._sign(self.data))
        self.assertTrue(fetch_recovery.verify_chunklist(self.dmg, self.cl)[0])
        Path(self.dmg).write_bytes(b"y" * 1024)
        valid, error = fetch_recovery.verify_chunklist(self.dmg, self.cl)
        self.assertFalse(valid)
        self.assertIn("failed its SHA-256", error)

    def test_matching_signed_cached_recovery_reused(self):
        self._write_cl(self._sign(self.data))
        version = plistlib.dumps({"ProductVersion": "15.4.1", "ProductBuildVersion": "24E263"})
        with mock.patch.object(fetch_recovery.subprocess, "run",
                               return_value=subprocess.CompletedProcess([], 0, version)), \
             mock.patch.object(fetch_recovery, "get_session") as request:
            result = fetch_recovery.fetch_into(self.tmp.name, "24")
        self.assertEqual(result["status"], "cached")
        self.assertEqual(result["build"], "24E263")
        request.assert_not_called()

    def test_signed_recovery_cache_cannot_satisfy_another_darwin_major(self):
        self._write_cl(self._sign(self.data))
        versions = [("15.4.1", "24E263"), ("26.0", "25A123")]
        inspect = [subprocess.CompletedProcess([], 0, plistlib.dumps({
            "ProductVersion": product, "ProductBuildVersion": build,
        })) for product, build in versions]
        new_payload = b"z" * 1024
        new_signed_chunklist = self._sign_with_current_key(new_payload)

        def download(url, dest, token):
            Path(dest).write_bytes(new_payload if url == "image" else new_signed_chunklist)
            return hashlib.sha256(Path(dest).read_bytes()).hexdigest()

        with mock.patch.object(fetch_recovery.subprocess, "run", side_effect=inspect), \
             mock.patch.object(fetch_recovery, "get_session", return_value="session"), \
             mock.patch.object(fetch_recovery, "get_image_info", return_value={
                 "AP": "product", "AU": "image", "CU": "chunklist", "CT": "ct", "AT": "at",
             }) as request, mock.patch.object(fetch_recovery, "_download", side_effect=download):
            result = fetch_recovery.fetch_into(self.tmp.name, "25")
        self.assertEqual(result["status"], "downloaded")
        self.assertEqual(result["build"], "25A123")
        self.assertEqual(Path(self.dmg).read_bytes(), new_payload)
        request.assert_called_once_with("session", fetch_recovery.RECOVERY_TARGETS["25"][0],
                                        fetch_recovery.DEFAULT_SERIAL, "latest")

    def test_wrong_magic_fails(self):
        bad = b"XXXX" + b"\x00" * 40
        self._write_cl(bad)
        ok, why = fetch_recovery.verify_chunklist(self.dmg, self.cl)
        self.assertFalse(ok)

    def test_truncated_chunklist_fails(self):
        digest = _sha256(self.data)
        cl = _chunklist(digest, sig_method=1)[:40]
        self._write_cl(cl)
        ok, why = fetch_recovery.verify_chunklist(self.dmg, self.cl)
        self.assertFalse(ok)

    def test_zero_chunk_is_refused(self):
        """A chunklist describing zero chunks must not validate a zero image."""
        header = H.pack(b"CNKL", H.size, 1, 1, 1, 0, H.size, H.size)
        self._write_cl(header)
        open(self.dmg, "wb").close()
        ok, why = fetch_recovery.verify_chunklist(self.dmg, self.cl)
        self.assertFalse(ok)


class MissingRequiredFields(unittest.TestCase):
    def test_missing_required_field_raises(self):
        """AP/AU/AT/CU/CH/CT all required; any missing is a hard failure."""
        response = "AP: product\nAU: image\nAH: hash\nAT: token\nCU: chunklist\nCT: chunk-token"
        with mock.patch.object(fetch_recovery, "_query", return_value=({}, response)):
            with self.assertRaisesRegex(fetch_recovery.RecoveryError, "lacked CH"):
                fetch_recovery.get_image_info("session", "Mac-SYNTHETIC")


if __name__ == "__main__":
    unittest.main()
