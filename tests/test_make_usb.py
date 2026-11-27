"""Exercise the erase workflow with a synthetic disk and command harness."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import autohackintosh
import consent
import make_usb


DEVICE = "/dev/review-disk"
PARTS = ("/dev/first-volume", "/dev/second-volume")


class BuildWorkflow(unittest.TestCase):
    def setUp(self):
        tmp = self.enterContext(tempfile.TemporaryDirectory())
        self.root = Path(tmp)
        self.efi = self.root / "input/EFI"
        self.recovery = self.root / "input/recovery"
        self.extra = self.root / "input/Extra"
        self.extra.mkdir(parents=True)
        self.backup = self.root / "backup"
        self.disk = self.root / "sys" / "review-disk"
        self.disk.mkdir(parents=True)
        self.nodes = {DEVICE: self.disk}
        for number, node in enumerate(PARTS, 1):
            entry = self.disk / Path(node).name
            entry.mkdir()
            (entry / "partition").write_text(str(number))
            (entry / "uevent").write_text(f"DEVNAME={entry.name}\nDEVTYPE=partition\n")
            self.nodes[node] = entry
        (self.disk / "diskseq").write_text("18")
        (self.disk / "dev").write_text("189:1")
        self.sources = [
            self.efi / "OC/config.plist",
            self.efi / "OC/Kexts/Example.kext/Contents/MacOS/Example",
            self.efi / "BOOT/BOOTx64.efi",
            self.extra / "driver.tar.gz",
            *(self.recovery / name for name in make_usb.RECOVERY_FILES),
        ]
        for i, path in enumerate(self.sources):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(f"payload-{i}".encode())
        self.info = {"device": DEVICE, "resolved": DEVICE, "kernel_name": self.disk.name,
                     "exists": True, "removable": "1", "serial": "SYNTH123",
                     "is_usb": True, "filesystems": ["old FAT OLD"], "in_use": []}
        self.commands = []
        self.after_backup = lambda: None
        self.fail_unmount = False
        self.mounts = {}
        self.after_sync = lambda: None
        self.enterContext(mock.patch.object(make_usb, "_block_sysfs", side_effect=self.nodes.__getitem__))
        self.enterContext(mock.patch.object(make_usb.verify_usb, "describe",
                                          side_effect=lambda _: self.info.copy()))
        self.enterContext(mock.patch.object(make_usb.shutil, "which", return_value="/fixture/tool"))
        self.enterContext(mock.patch.object(make_usb.fetch_recovery, "verify_release"))
        self.enterContext(mock.patch.object(consent, "confirm", side_effect=consent.expected_phrase))
        self.enterContext(mock.patch.object(make_usb, "_sudo", side_effect=self.command))

    def command(self, args):
        self.commands.append(args)
        command = args[0]
        output = ""
        if command == "lsblk":
            output = json.dumps({"blockdevices": [{"path": DEVICE, "type": "disk", "label": self.info["filesystems"]}]})
        elif command == "wipefs" and "--json" in args:
            output = '{"signatures":[]}'
        elif command == "sgdisk" and "--backup" in args:
            Path(args[2]).write_bytes(b"table")
            self.after_backup()
        elif command == "mount":
            self.mounts[args[-2]] = Path(args[-1])
        elif command == "cp":
            shutil.copyfile(Path(args[-2]), Path(args[-1]))
        elif command == "mkdir":
            Path(args[-1]).mkdir()
        elif command == "sync":
            self.after_sync()
        elif command == "umount":
            target = Path(args[-1])
            if self.fail_unmount and target == self.mounts[PARTS[1]]:
                # Model a failed command at the process boundary.
                process = subprocess.CompletedProcess(args, 32, "", "target is busy")
                with mock.patch.object(make_usb.subprocess, "run", return_value=process):
                    return self.real_sudo(args)
            for path in sorted(target.rglob("*"), key=lambda p: -len(p.parts)):
                (path.rmdir if path.is_dir() else path.unlink)()
        return subprocess.CompletedProcess(args, 0, output, "")

    real_sudo = staticmethod(make_usb._sudo)

    def build(self, **kwargs):
        return make_usb.build(DEVICE, str(self.efi), str(self.recovery), str(self.extra),
                              "SYNTH123", str(self.backup), **kwargs)

    def assert_no_erase(self):
        self.assertFalse(any(args[0] == "wipefs" and "-a" in args for args in self.commands))

    def test_success_exercises_format_copy_readback_and_cleanup(self):
        result = self.build()
        self.assertEqual(result["outcome"], "written")
        self.assertEqual(result["verified_files"], len(self.sources))
        self.assertIn(["wipefs", "-a", "--", DEVICE], self.commands)
        self.assertEqual(next(a[-1] for a in self.commands if a[0] == "mkfs.hfsplus"), PARTS[1])
        self.assertFalse(self.mounts[PARTS[1]].parent.exists())

    def test_failed_unmount_never_deletes_mounted_files_or_reports_success(self):
        self.fail_unmount = True
        with self.assertRaisesRegex(make_usb.BuildError, "mount cleanup failed.*target is busy"):
            self.build()
        mounted = self.mounts[PARTS[1]]
        self.addCleanup(shutil.rmtree, mounted.parent)
        self.assertEqual((mounted / "Extra/driver.tar.gz").read_bytes(), self.sources[3].read_bytes())
        self.assertNotIn("rm", [args[0] for args in self.commands])
        self.assertFalse(self.mounts[PARTS[0]].exists())

    def test_filesystem_metadata_drift_after_backup_refuses(self):
        self.after_backup = lambda: self.info.update(filesystems=["old FAT CHANGED"])
        with self.assertRaisesRegex(consent.ConsentDeclined, "changed since approval"):
            self.build()
        self.assert_no_erase()

    def test_device_connection_drift_after_backup_refuses(self):
        self.after_backup = lambda: (self.disk / "diskseq").write_text("19")
        with self.assertRaisesRegex(consent.ConsentDeclined, "changed since approval"):
            self.build()
        self.assert_no_erase()

    def test_operation_drift_after_backup_refuses(self):
        def change_layout():
            self.enterContext(mock.patch.object(make_usb, "EFI_PART_MIB", 256))
        self.after_backup = change_layout
        with self.assertRaisesRegex(consent.ConsentDeclined, "changed since approval"):
            self.build()
        self.assert_no_erase()

    def test_each_payload_file_is_bound_to_review(self):
        for source in self.sources:
            with self.subTest(path=source.relative_to(self.root)):
                old = source.read_bytes()
                self.after_backup = lambda: source.write_bytes(b"changed")
                with self.assertRaisesRegex(consent.ConsentDeclined, "changed since approval"):
                    self.build()
                source.write_bytes(old)
                self.assert_no_erase()

    def test_new_payload_file_after_review_refuses(self):
        self.after_backup = lambda: (self.extra / "added").write_bytes(b"unexpected")
        with self.assertRaisesRegex(consent.ConsentDeclined, "changed since approval"):
            self.build()
        self.assert_no_erase()

    def test_busy_disk_refused_before_approval(self):
        self.info["in_use"] = ["device"]
        with self.assertRaisesRegex(make_usb.BuildError, "mounted or used as swap"):
            self.build()
        consent.confirm.assert_not_called()
        self.assert_no_erase()

    def test_missing_chunklist_refuses_before_approval(self):
        self.sources[-1].unlink()
        with self.assertRaisesRegex(make_usb.BuildError, "BaseSystem.chunklist"):
            self.build()
        consent.confirm.assert_not_called()
        self.assertFalse(self.backup.exists())
        self.assert_no_erase()

    def test_symlink_in_efi_is_rejected_before_approval(self):
        (self.efi / "OC" / "link.efi").symlink_to(self.sources[2])
        with self.assertRaisesRegex(make_usb.BuildError, "non-regular payload entry"):
            self.build()
        consent.confirm.assert_not_called()
        self.assert_no_erase()

    def test_dry_run_neither_prompts_nor_writes(self):
        result = self.build(dry_run=True)
        self.assertEqual(result["outcome"], "planned")
        self.assertIn("recovery/Extra/driver.tar.gz", result["plan"]["payload_manifest"])
        self.assertFalse(self.backup.exists())
        consent.confirm.assert_not_called()
        self.assert_no_erase()

    def prepare_verified_efi(self, include_nullmoth=False, validator_exit=0):
        self.workdir = self.root / "work"
        self.workdir.mkdir()
        self.paths = autohackintosh.workdir_paths(str(self.workdir))
        self.paths.update(efi=str(self.efi.parent), recovery=str(self.recovery), backup=str(self.backup))
        inventory = Path(self.paths["sysreport"])
        (inventory / "ACPI").mkdir(parents=True)
        self.report = inventory / "Report.json"
        self.report.write_text(json.dumps({"CPU": {"Manufacturer": "Intel"}}))
        (inventory / "ACPI/fixture.bin").write_bytes(b"synthetic ACPI")
        self.enterContext(mock.patch.object(autohackintosh, "workdir_paths", return_value=self.paths))
        self.validator = ({"fixture_identity": "pinned"}, f"#!/bin/sh\nexit {validator_exit}\n".encode())
        self.enterContext(mock.patch.object(autohackintosh, "_validator_bytes", return_value=self.validator))
        self.record = {"darwin": "24", "include_nullmoth": include_nullmoth,
                       "efi": autohackintosh._efi_manifest(self.efi),
                       "inventory": autohackintosh._inventory_evidence(str(self.workdir)),
                       "validator": self.validator[0]}
        self.record_path = self.workdir / autohackintosh.EFI_RECORD
        self.record_path.write_text(json.dumps(self.record))

    def stick(self, **kwargs):
        return autohackintosh.stage_stick(str(self.workdir), DEVICE, kwargs.pop("serial", None),
                                         kwargs.pop("dry_run", True), "24", **kwargs)

    def test_orchestrator_propagates_refused_dry_run(self):
        self.prepare_verified_efi()
        self.info["in_use"] = ["existing-partition"]
        result = self.stick()
        self.assertFalse(result["ok"])
        self.assertEqual(result["outcome"], "refused")
        self.assertIn("mounted or used as swap", result["error"])
        self.assertFalse(autohackintosh._milestones(str(self.root), {"stick": result})["usb-written-and-read"])
        self.assert_no_erase()

    def test_added_staged_efi_file_refuses_before_erase(self):
        original_snapshot = make_usb._snapshot

        def stage(entries, root):
            manifest = original_snapshot(entries, root)
            self.after_backup = lambda: (root / "esp/EFI/OC/unapproved.efi").write_bytes(b"unapproved")
            return manifest

        with mock.patch.object(make_usb, "_snapshot", side_effect=stage):
            with self.assertRaisesRegex(make_usb.BuildError, "staged payload changed since review"):
                self.build()
        self.assert_no_erase()

    def test_unexpected_on_media_file_prevents_success(self):
        self.after_sync = lambda: (self.mounts[PARTS[0]] / "unexpected").write_bytes(b"unapproved")
        with self.assertRaisesRegex(make_usb.BuildError, "readback tree differs"):
            self.build()
        self.assertTrue(all(not mount.exists() for mount in self.mounts.values()))

    def test_modified_config_with_old_validation_is_refused(self):
        self.prepare_verified_efi()
        self.sources[0].write_bytes(b"modified config")
        result = self.stick(serial="SYNTH123", dry_run=False)
        self.assertIn("EFI contents changed", result["error"])
        self.assertEqual(self.commands, [])

    def test_modified_inventory_and_acpi_with_old_validation_are_refused(self):
        self.prepare_verified_efi()
        acpi = self.report.parent / "ACPI/fixture.bin"
        for path in (self.report, acpi):
            with self.subTest(path=path.name):
                original = path.read_bytes()
                path.write_bytes(original + b" ")
                result = self.stick(serial="SYNTH123", dry_run=False)
                self.assertIn("inventory or ACPI changed", result["error"])
                self.assertEqual(self.commands, [])
                path.write_bytes(original)

    def test_matching_validator_is_run_against_staged_config_before_consent(self):
        self.prepare_verified_efi(validator_exit=1)
        with mock.patch.object(autohackintosh, "_run", wraps=autohackintosh._run) as runner:
            result = self.stick(serial="SYNTH123", dry_run=False)
        self.assertIn("ocvalidate refused the staged configuration", result["error"])
        config_path = Path(runner.call_args.args[0][1])
        self.assertNotEqual(config_path, self.sources[0])
        self.assertEqual(config_path.parts[-3:], ("EFI", "OC", "config.plist"))
        consent.confirm.assert_not_called()
        self.assert_no_erase()

    def test_changed_validator_identity_prevents_approval(self):
        self.prepare_verified_efi()
        self.record["validator"] = {"fixture_identity": "old"}
        self.record_path.write_text(json.dumps(self.record))
        result = self.stick(serial="SYNTH123", dry_run=False)
        self.assertIn("validator identity changed", result["error"])
        consent.confirm.assert_not_called()
        self.assert_no_erase()

    def test_unsupported_cpu_blocks_media_even_with_driver_opt_in(self):
        self.prepare_verified_efi(include_nullmoth=True)
        self.report.write_text(json.dumps({"CPU": {"Manufacturer": "Synthetic unknown"}}))
        with mock.patch.object(autohackintosh, "_nullmoth_payload") as payload:
            result = self.stick(include_nullmoth=True)
        self.assertIn("blocking unsupported", result["error"])
        payload.assert_not_called()
        self.assertEqual(self.commands, [])

    def test_experimental_candidate_is_not_staged_without_opt_in(self):
        self.prepare_verified_efi()
        self.report.write_text(json.dumps({"CPU": {"Manufacturer": "Intel"}, "GPU": {
            "candidate": {"Device ID": "10DE-FFFF", "Manufacturer": "NVIDIA",
                          "Device Type": "Discrete GPU"},
        }}))
        self.record["inventory"] = autohackintosh._inventory_evidence(str(self.workdir))
        self.record_path.write_text(json.dumps(self.record))
        with mock.patch.object(autohackintosh, "_nullmoth_ids", return_value={"FFFF"}), \
             mock.patch.object(autohackintosh, "_nullmoth_payload") as payload:
            result = self.stick()
        self.assertEqual(result["outcome"], "planned")
        self.assertFalse(result["plan"]["device_state"]["efi_validation"]["include_nullmoth"])
        payload.assert_not_called()

    def test_explicit_driver_opt_in_is_included_in_review(self):
        self.prepare_verified_efi(include_nullmoth=True)
        self.report.write_text(json.dumps({"CPU": {"Manufacturer": "Intel"}, "GPU": {
            "candidate": {"Device ID": "10DE-FFFF", "Manufacturer": "NVIDIA",
                          "Device Type": "Discrete GPU"},
        }}))
        self.record["inventory"] = autohackintosh._inventory_evidence(str(self.workdir))
        self.record_path.write_text(json.dumps(self.record))
        with mock.patch.object(autohackintosh, "_nullmoth_ids", return_value={"FFFF"}), \
             mock.patch.object(autohackintosh, "_nullmoth_payload", return_value=str(self.extra)) as payload:
            result = self.stick(include_nullmoth=True)
        self.assertEqual(result["outcome"], "planned")
        self.assertTrue(result["plan"]["device_state"]["efi_validation"]["include_nullmoth"])
        self.assertIn("recovery/Extra/driver.tar.gz", result["plan"]["payload_manifest"])
        payload.assert_called_once()

    def test_noncanonical_partition_numbers_refuse_before_format(self):
        for number, part in zip((3, 4), PARTS):
            (self.nodes[part] / "partition").write_text(str(number))
        with self.assertRaisesRegex(make_usb.BuildError, "found \\[3, 4\\]"):
            self.build()
        self.assertFalse(any(args[0].startswith("mkfs") for args in self.commands))

    def test_prefix_cannot_bypass_parent_verification(self):
        foreign = self.root / "other-disk" / self.nodes[PARTS[1]].name
        foreign.mkdir(parents=True)
        self.nodes[PARTS[1]] = foreign
        with self.assertRaisesRegex(make_usb.BuildError, "ambiguous partition parent"):
            self.build()
        self.assertFalse(any(args[0].startswith("mkfs") for args in self.commands))

    def test_third_partition_refuses(self):
        node = self.disk / "third-volume"
        node.mkdir()
        (node / "partition").write_text("3")
        (node / "uevent").write_text("DEVNAME=third-volume\nDEVTYPE=partition\n")
        self.nodes["/dev/third-volume"] = node
        with self.assertRaisesRegex(make_usb.BuildError, "exactly partitions 1 and 2"):
            make_usb._partition_nodes(DEVICE)

    def test_cli_rejects_headless_confirmation_option(self):
        with self.assertRaises(SystemExit) as exc:
            make_usb.main(["--device", DEVICE, "--efi", str(self.efi),
                           "--recovery-dir", str(self.recovery),
                           "--i-authorise-erase", "SYNTH123",
                           "--confirm-code", "COMPUTED_BY_AGENT"])
        self.assertEqual(exc.exception.code, 2)
        self.assertEqual(self.commands, [])
