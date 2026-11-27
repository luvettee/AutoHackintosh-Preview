#!/usr/bin/env python3
"""Verify USB admission against synthetic kernel and mount state."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(os.path.dirname(HERE), "scripts")
sys.path.insert(0, SCRIPTS)

import verify_usb  # noqa: E402


def _info(**overrides) -> dict:
    base = {
        "device": "/dev/sdz",
        "resolved": "/dev/sdz",
        "kernel_name": "sdz",
        "exists": True,
        "removable": "1",
        "model": "TEST-STICK-16G",
        "vendor": "TESTVENDOR",
        "serial": "AAAAAAAABBBBBBBBCCCCCCCC",
        "size_bytes": 64 * 1024**3,
        "transport_sysfs": "usb2",
        "transport_lsblk": "usb",
        "is_usb": True,
        "by_id": ["usb-TESTVENDOR_TEST-STICK-16G_AAAAAAAABBBBBBBBCCCCCCCC-0:0"],
        "partitions": [{"name": "sdz1", "resolved": "/dev/sdz1"}],
        "in_use": [],
        "hosts_root": False,
        "pttype": "dos",
        "filesystems": ["sdz1 vfat TESTVOL"],
        "holders": [],
        "raid_member": None,
        "is_multipath": False,
        "luks_member": None,
        "lvm_member": None,
        "is_device_mapper": False,
    }
    base.update(overrides)
    return base


class IdentityEvaluation(unittest.TestCase):
    def test_good_usb_is_confirmed(self):
        code, problems, _ = verify_usb.evaluate(_info(), "AAAAAAAABBBBBBBBCCCCCCCC")
        self.assertEqual(code, 0, problems)
        self.assertEqual(problems, [])

    def test_internal_disk_is_refused(self):
        """A fixed disk must be refused even when the serial matches exactly."""
        info = _info(
            device="/dev/nvme0n1", kernel_name="nvme0n1", removable="0",
            is_usb=False, transport_sysfs="nvme0", transport_lsblk="nvme",
            serial="SAMPLE1XX", by_id=["nvme-FIXTURE_SAMPLE1XX"],
            partitions=[{"name": "nvme0n1p1", "resolved": "/dev/nvme0n1p1"}],
        )
        code, problems, _ = verify_usb.evaluate(info, "SAMPLE1XX")
        self.assertEqual(code, 1)
        joined = " ".join(problems)
        self.assertIn("removable", joined)
        self.assertIn("not on a USB bus", joined)
        self.assertIn("usb-*", joined)

    def test_serial_mismatch_is_refused(self):
        code, problems, _ = verify_usb.evaluate(_info(), "DEADBEEFCAFE")
        self.assertEqual(code, 1)
        self.assertTrue(any("serial mismatch" in p for p in problems))

    def test_missing_serial_cannot_be_trusted(self):
        code, problems, _ = verify_usb.evaluate(_info(serial=None), "E0D5")
        self.assertEqual(code, 1)
        self.assertTrue(any("no serial readable" in p for p in problems))

    def test_mounted_partitions_are_refused(self):
        info = _info(in_use=["sdz1"])
        code, problems, _ = verify_usb.evaluate(info, "AAAAAAAABBBBBBBBCCCCCCCC")
        self.assertEqual(code, 1)
        self.assertTrue(any("mounted" in p for p in problems))

    def test_disk_hosting_root_is_refused(self):
        info = _info(hosts_root=True)
        code, problems, _ = verify_usb.evaluate(info, "AAAAAAAABBBBBBBBCCCCCCCC")
        self.assertEqual(code, 1)
        self.assertTrue(any("root filesystem" in p for p in problems))

    def test_holder_claims_are_refused(self):
        """A disk held by LVM/dm-crypt/multipath must be refused even unmounted."""
        info = _info(holders=["dm-0"])
        code, problems, _ = verify_usb.evaluate(info, "AAAAAAAABBBBBBBBCCCCCCCC")
        self.assertEqual(code, 1)
        self.assertTrue(any("held open" in p for p in problems))

    def test_raid_member_is_refused(self):
        info = _info(raid_member="sdz1")
        code, problems, _ = verify_usb.evaluate(info, "AAAAAAAABBBBBBBBCCCCCCCC")
        self.assertEqual(code, 1)
        self.assertTrue(any("mdraid" in p for p in problems))

    def test_multipath_slave_is_refused(self):
        info = _info(is_multipath=True)
        code, problems, _ = verify_usb.evaluate(info, "AAAAAAAABBBBBBBBCCCCCCCC")
        self.assertEqual(code, 1)
        self.assertTrue(any("multipath" in p for p in problems))

    def test_luks_backing_disk_is_refused(self):
        info = _info(luks_member=True)
        code, problems, _ = verify_usb.evaluate(info, "AAAAAAAABBBBBBBBCCCCCCCC")
        self.assertEqual(code, 1)
        self.assertTrue(any("LUKS" in p or "dm-crypt" in p for p in problems))

    def test_lvm_backing_disk_is_refused(self):
        info = _info(lvm_member=True)
        code, problems, _ = verify_usb.evaluate(info, "AAAAAAAABBBBBBBBCCCCCCCC")
        self.assertEqual(code, 1)
        self.assertTrue(any("LVM" in p for p in problems))

    def test_missing_device_is_unverified_not_confirmed(self):
        code, problems, _ = verify_usb.evaluate({"error": "no such dir"}, "X")
        self.assertEqual(code, 2)
        self.assertTrue(problems)

    def test_ambiguous_identity_without_by_id_warns(self):
        """No by-id entries is a warning, not silent success."""
        code, _, warnings = verify_usb.evaluate(
            _info(by_id=[]), "AAAAAAAABBBBBBBBCCCCCCCC")
        self.assertEqual(code, 0)
        self.assertTrue(any("by-id" in w for w in warnings))

    def test_small_disk_warns_about_size(self):
        _, _, warnings = verify_usb.evaluate(
            _info(size_bytes=4 * 1024**3), "AAAAAAAABBBBBBBBCCCCCCCC")
        self.assertTrue(any("too small" in w for w in warnings))


class BaseName(unittest.TestCase):
    def test_nvme_partition(self):
        self.assertEqual(verify_usb.base_name("/dev/nvme0n1p2"), "nvme0n1")

    def test_sd_partition(self):
        self.assertEqual(verify_usb.base_name("/dev/sdz1"), "sdz")

    def test_whole_disk(self):
        self.assertEqual(verify_usb.base_name("/dev/sdb"), "sdb")

    def test_mmc(self):
        self.assertEqual(verify_usb.base_name("/dev/mmcblk0"), "mmcblk0")


class DescriptionProbes(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.dev = "/dev/synthetic-unit"
        self.disk = self.tmp / "synthetic-unit"
        self.disk.mkdir()
        (self.disk / "holders").mkdir()
        (self.disk / "removable").write_text("1")
        (self.disk / "serial").write_text("SYNTH")
        (self.disk / "dev").write_text("189:7")
        self.enterContext(mock.patch.object(verify_usb, "SYS_BLOCK", str(self.tmp)))
        real_exists = os.path.exists
        self.enterContext(mock.patch.object(os.path, "exists",
                                          side_effect=lambda p: p == self.dev or real_exists(p)))
        self.enterContext(mock.patch.object(verify_usb, "_run",
                                           side_effect=lambda cmd: "usb" if "TRAN" in cmd else ""))
        self.enterContext(mock.patch.object(verify_usb, "resolve_by_id", return_value=[]))
        self.enterContext(mock.patch.object(verify_usb, "_root_device", return_value=None))
        self.enterContext(mock.patch.object(verify_usb, "mounted_sources", return_value=[]))
        self.busy = self.enterContext(mock.patch.object(verify_usb, "mounted_device_numbers",
                                                        return_value=set()))
        self.swapon = self.enterContext(mock.patch.object(
            verify_usb.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "", "")))
        self.enterContext(mock.patch.dict(verify_usb._probe_failed, {"mount": False, "swap": False}))

    def test_whole_disk_mount_detected_by_device_id_with_unrelated_source_path(self):
        self.busy.return_value = {"189:7"}
        info = verify_usb.describe(self.dev)
        self.assertEqual(info["in_use"], ["synthetic-unit"])
        self.assertIn("mounted", " ".join(verify_usb.evaluate(info, "SYNTH", True)[1]))

    def test_partition_with_nonmatching_name_has_its_holders_checked(self):
        partition = self.disk / "synthetic-part"
        partition.mkdir()
        (partition / "partition").write_text("2")
        (partition / "dev").write_text("189:8")
        (partition / "holders").mkdir()
        (partition / "holders" / "dm-0").touch()
        info = verify_usb.describe(self.dev)
        self.assertEqual(info["holders"], ["dm-0"])
        self.assertIn("held open", " ".join(verify_usb.evaluate(info, "SYNTH", True)[1]))

    def test_missing_holder_probe_fails_closed(self):
        (self.disk / "holders").rmdir()
        info = verify_usb.describe(self.dev)
        self.assertTrue(info["probe_failed"])
        self.assertIn("required probe failed", " ".join(verify_usb.evaluate(info, "SYNTH", True)[1]))

    def test_udev_serial_is_not_truncated_at_by_id_separators(self):
        (self.disk / "serial").unlink()
        with mock.patch.object(verify_usb, "_run", side_effect=lambda args:
                               "ID_SERIAL_SHORT=SYNTH_SERIAL_WITH_UNDERSCORES\n" if args[0] == "udevadm"
                               else "usb" if "TRAN" in args else ""):
            info = verify_usb.describe(self.dev)
        self.assertEqual(info["serial"], "SYNTH_SERIAL_WITH_UNDERSCORES")
        self.assertEqual(verify_usb.evaluate(info, info["serial"], True)[0], 0)
        self.assertNotEqual(verify_usb.evaluate(info, "UNDERSCORES", True)[0], 0)

    def test_missing_swapon_fails_closed(self):
        self.swapon.side_effect = FileNotFoundError("swapon")
        info = verify_usb.describe(self.dev)
        self.assertTrue(info["probe_failed"])
        self.assertIn("required probe failed", " ".join(verify_usb.evaluate(info, "SYNTH", True)[1]))


class CommandLine(unittest.TestCase):
    def _run(self, *args):
        return subprocess.run(
            [sys.executable, os.path.join(SCRIPTS, "verify_usb.py"), *args],
            capture_output=True, text=True)

    def test_missing_device_argument_exits_3(self):
        self.assertEqual(self._run().returncode, 3)

    def test_nonexistent_device_is_unverified(self):
        out = self._run("--device", "/dev/definitely-not-a-disk")
        self.assertEqual(out.returncode, 2)

    def test_json_output_is_machine_readable(self):
        out = self._run("--device", "/dev/definitely-not-a-disk", "--json")
        payload = json.loads(out.stdout)
        self.assertIn("exit_code", payload)
        self.assertEqual(payload["exit_code"], 2)


if __name__ == "__main__":
    unittest.main()
