#!/usr/bin/env python3
"""Tests for ``scripts/diagnostic_report.py``.

The contract under test changed with the pre-release refinement: shareable
output is now built from a fixed *allowlist* of typed fields, not by scrubbing
arbitrary text. The tests therefore assert two things: (a) the allowlist does
not pass sensitive values through, and (b) the private file is created 0600
from its first byte and a symlink is never followed.

These run entirely on synthetic fixtures; no real hardware values are needed.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(os.path.dirname(HERE), "scripts")
sys.path.insert(0, SCRIPTS)

import diagnostic_report  # noqa: E402

#: Values that must never reach shareable output, no matter where they hide.
SECRETS = [
    "FAKESN000001", "FAKEMLB000AB", "FAKEROM001",
    "00000000-1111-2222-3333-444444444444", "aa:bb:cc:dd:ee:ff",
    "hunter2", "someone@example.com", "/home/username/Documents",
]


def _workdir_with_sensitive_values(tmp: str) -> str:
    """A workdir whose Report.json and config carry invented sensitive values."""
    sysreport = os.path.join(tmp, "sysreport")
    os.makedirs(sysreport, exist_ok=True)
    with open(os.path.join(sysreport, "Report.json"), "w") as fh:
        json.dump({
            "CPU": {"Manufacturer": "Intel", "Processor Name": "Test CPU",
                    "Codename": "Test", "Core Count": 8},
            "Motherboard": {"Name": "Test Board", "Chipset": "TEST", "Platform": "Desktop"},
            "BIOS": {"Firmware Type": "UEFI", "Version": "1.0"},
            "GPU": {"Test GPU": {"Device ID": "TEST-0000", "Codename": "TESTCHIPA",
                                 "Resizable BAR": "Enabled",
                                 # A serial-looking value the old path could leak.
                                 "Serial Number": "FAKESN000001"}},
            "credentials": {"password": "hunter2", "appleid": "someone@example.com"},
            "home": "/home/username/Documents",
        }, fh)
    efi = os.path.join(tmp, "efi", "EFI", "OC")
    os.makedirs(efi, exist_ok=True)
    # A config with identifying SMBIOS values the shareable must drop.
    config = {
        "PlatformInfo": {"Generic": {
            "SystemProductName": "MacPro7,1",
            "SystemSerialNumber": "FAKESN000001",
            "MLB": "FAKEMLB000AB",
            "SystemUUID": "00000000-1111-2222-3333-444444444444",
        }},
        "NVRAM": {"Add": {"7C436110-AB2A-4BBB-A880-FE41995C9F82": {
            "boot-args": "nvfb=1 nvaccel=1 keepsyms=1",
            "csr-active-config": b"\x43\x0a\x00\x00",
        }}},
        "Misc": {"Security": {"SecureBootModel": "Disabled"}},
        "Kernel": {"Add": [{"BundlePath": "Lilu.kext", "Enabled": True}]},
        "ACPI": {"Add": [{"Path": "SSDT-EC.aml", "Enabled": True}]},
        "UEFI": {"Drivers": [{"Path": "OpenRuntime.efi", "Enabled": True}]},
    }
    import plistlib
    with open(os.path.join(efi, "config.plist"), "wb") as fh:
        plistlib.dump(config, fh)
    return tmp


class ShareableIsAllowlist(unittest.TestCase):
    def _build(self, tmp):
        _workdir_with_sensitive_values(tmp)
        return diagnostic_report.build(tmp)

    def test_shareable_contains_none_of_the_secrets(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            result = self._build(tmp)
            blob = Path(result["shareable_json"]).read_text()
            for secret in SECRETS:
                self.assertNotIn(secret, blob, f"{secret!r} leaked into shareable JSON")
            self.assertIn("MacPro7,1", blob)

    def test_markdown_contains_none_of_the_secrets(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            result = self._build(tmp)
            blob = Path(result["shareable_markdown"]).read_text()
            for secret in SECRETS:
                self.assertNotIn(secret, blob, f"{secret!r} leaked into shareable Markdown")

    def test_shareable_omits_unstructured_fields(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            result = self._build(tmp)
            payload = json.loads(Path(result["shareable_json"]).read_text())
            self.assertNotIn("notices", payload)
            self.assertNotIn("full_hardware_report", payload)
            self.assertNotIn("full_policy_changes", payload)

    def test_populated_allowed_fields_and_nested_outputs_cannot_leak_text(self):
        import plistlib

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(_workdir_with_sensitive_values(tmp))
            secret = "synthetic-secret-allowed-text"
            hw_path = root / "sysreport/Report.json"
            hardware = json.loads(hw_path.read_text())
            hardware["CPU"].update({"Manufacturer": {"nested": secret},
                                    "Processor Name": secret, "Core Count": True})
            hardware["BIOS"]["Version"] = secret
            hardware["GPU"][secret] = {"Device ID": secret}
            hw_path.write_text(json.dumps(hardware))
            config_path = root / "efi/EFI/OC/config.plist"
            config = plistlib.loads(config_path.read_bytes())
            nvram = config["NVRAM"]["Add"]["7C436110-AB2A-4BBB-A880-FE41995C9F82"]
            nvram["boot-args"] = "-v private=" + secret
            config["ACPI"]["Add"].append({"Path": secret, "Enabled": True})
            config_path.write_bytes(plistlib.dumps(config))
            (root / "last-run.json").write_text(json.dumps({
                "results": {"efi": {"ocvalidate": {"ok": False, "issues": 9}}},
            }))
            result = diagnostic_report.build(tmp, run_results={
                "efi": {"ocvalidate": {"ok": True, "issues": 0, "text": secret},
                        "compat_repairs": [{"id": secret, "reason": secret}],
                        "policy_changes": [{"before": {"value": secret}, "why": secret}],
                        "compat_skipped": [secret]},
                "assess": {"verdict": {"unknown": [secret]},
                           "recommended_macos": {"darwin": "24", "why": secret}},
                "inventory": {"normalized_edits": [{"value": secret}]},
            })
            public_json = Path(result["shareable_json"]).read_text()
            public_md = Path(result["shareable_markdown"]).read_text()
            self.assertNotIn(secret, public_json)
            self.assertNotIn(secret, public_md)
            self.assertIn(secret, Path(result["private"]).read_text())
            public = json.loads(public_json)
            self.assertEqual(public["open_core"]["boot_flags"], ["-v"])
            self.assertIsNone(public["hardware"]["cpu"]["cores"])
            self.assertIsNone(public["hardware"]["cpu"]["manufacturer"])
            self.assertEqual(public["assessment"], {"unknown": True})
            self.assertEqual(public["validation"], {"ocvalidate_ok": True, "ocvalidate_issues": 0})


class PrivateFileSafety(unittest.TestCase):
    def _workdir(self, tmp):
        _workdir_with_sensitive_values(tmp)
        return tmp

    def test_private_report_is_0600_from_the_start(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            result = diagnostic_report.build(self._workdir(tmp))
            mode = os.stat(result["private"]).st_mode & 0o777
            self.assertEqual(mode, 0o600, "private report must be 0600")

    def test_private_dir_is_0700(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            result = diagnostic_report.build(self._workdir(tmp))
            mode = os.stat(os.path.dirname(result["private"])).st_mode & 0o777
            self.assertEqual(mode, 0o700)

    def test_symlink_at_output_path_is_not_followed(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp, \
             tempfile.TemporaryDirectory() as other:
            victim = os.path.join(other, "victim.txt")
            with open(victim, "w") as fh:
                fh.write("original")
            reports_dir = os.path.join(tmp, "reports")
            os.makedirs(reports_dir, exist_ok=True)
            target = os.path.join(reports_dir, "diagnostic-report.shareable.json")
            os.symlink(victim, target)
            with self.assertRaises(diagnostic_report.ReportError):
                diagnostic_report.build(self._workdir(tmp))
            # The symlink target must be untouched.
            self.assertEqual(Path(victim).read_text(), "original")

    def test_empty_workdir_still_produces_a_report(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            result = diagnostic_report.build(tmp)
            self.assertTrue(result["ok"])
            self.assertTrue(os.path.isfile(result["shareable_json"]))


class ReportAssembly(unittest.TestCase):
    def test_build_reports_private_and_shareable_locations(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            result = diagnostic_report.build(tmp)
            for key in ("private", "shareable_json", "shareable_markdown"):
                self.assertTrue(os.path.isfile(result[key]), f"{key} missing")


if __name__ == "__main__":
    unittest.main()
