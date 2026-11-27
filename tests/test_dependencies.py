"""Offline regression coverage for caches, compiler pins and assessment."""

import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import autohackintosh
import fetch_deps
import iasl_provision


class DependencyCache(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.cache = self.root / "cache"
        self.downloads = self.cache / "downloads"
        self.downloads.mkdir(parents=True)
        self.archive = self.downloads / "sample.zip"
        self.marker = "engine/app/p1401"
        self.script = self.marker + "/worker.py"
        with zipfile.ZipFile(self.archive, "w") as archive:
            archive.writestr(self.script, b"approved = True\n")
        self.pin = {"asset": self.archive.name, "sha256": fetch_deps.sha256_of(self.archive),
                    "url": "https://example.invalid/sample.zip"}

    def extract(self):
        return Path(fetch_deps.extract("1401", str(self.archive), str(self.cache),
                                       self.marker, expected=self.pin["sha256"]))

    def test_modified_extracted_code_does_not_survive_valid_archive_digest(self):
        root = self.extract()
        path = root / self.script
        path.write_bytes(b"modified = True\n")
        (root / ".autohackintosh-sha256").write_text(self.pin["sha256"])
        self.assertEqual(self.extract(), root)
        self.assertEqual(path.read_bytes(), b"approved = True\n")

    def test_extra_modules_in_extracted_cache_are_refused(self):
        root = self.extract()
        (root / self.marker / "extra.py").write_text("modified = True")
        with mock.patch.dict(fetch_deps.PINS, {"1401": self.pin}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "invalid extracted tree"):
                fetch_deps.verified_root(str(self.cache), "1401")

    def test_check_does_not_create_missing_workdir(self):
        missing = self.root / "missing"
        with mock.patch.dict(fetch_deps.PINS, {"1401": self.pin}, clear=True), \
             mock.patch.object(fetch_deps.urllib.request, "urlopen") as network:
            self.assertEqual(fetch_deps.main(["--workdir", str(missing), "--check"]), 1)
        self.assertFalse(missing.exists())
        network.assert_not_called()

    def test_check_detects_mutated_tree_without_repairing_it(self):
        path = self.extract() / self.script
        path.write_bytes(b"modified")
        with mock.patch.dict(fetch_deps.PINS, {"1401": self.pin}, clear=True):
            code = fetch_deps.main(["--workdir", str(self.cache), "--check"])
        self.assertEqual(code, 1)
        self.assertEqual(path.read_bytes(), b"modified")

    def test_failed_download_leaves_no_partial_or_replacement(self):
        self.archive.write_bytes(b"old invalid bytes")
        with mock.patch.object(fetch_deps.urllib.request, "urlopen", return_value=io.BytesIO(b"wrong archive")):
            with self.assertRaisesRegex(RuntimeError, "SHA-256 mismatch"):
                fetch_deps.stage("1401", self.pin, str(self.cache), verify_only=False)
        self.assertEqual(self.archive.read_bytes(), b"old invalid bytes")
        self.assertEqual(list(self.downloads.iterdir()), [self.archive])

    def test_nullmoth_never_reuses_modified_staged_or_downloaded_payload(self):
        contents = b"pinned driver"
        pin = {"asset": "fixture.tar.gz", "url": "https://example.invalid/asset",
               "sha256": hashlib.sha256(contents).hexdigest()}
        deps = self.root / "deps"
        legacy = self.root / "NullMoth" / pin["asset"]
        legacy.parent.mkdir()
        legacy.write_bytes(b"old staged file")
        cached = deps / "downloads" / pin["asset"]
        cached.parent.mkdir(parents=True)
        cached.write_bytes(b"old cached file")
        finish = self.root / self.marker / "finish-setup.sh"
        finish.parent.mkdir(parents=True)
        finish.write_bytes(b"pinned helper")
        staging = self.root / "fresh"
        staging.mkdir()
        with mock.patch.object(autohackintosh, "NULLMOTH_PIN", pin), \
             mock.patch.object(fetch_deps, "verified_root", return_value=str(self.root)), \
             mock.patch.object(fetch_deps.urllib.request, "urlopen", return_value=io.BytesIO(contents)) as request:
            payload = Path(autohackintosh._nullmoth_payload(str(self.root), str(staging)))
        request.assert_called_once()
        self.assertEqual((payload / pin["asset"]).read_bytes(), contents)
        self.assertEqual((payload / finish.name).read_bytes(), finish.read_bytes())
        self.assertEqual(legacy.read_bytes(), b"old staged file")


class Compiler(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def script(self, name, version, status=0):
        path = self.root / name
        path.write_text(f"#!/bin/sh\nprintf 'ASL+ Compiler version {version}\\n'\nexit {status}\n")
        path.chmod(0o700)
        return path

    def test_temporary_destination_symlink_is_never_followed(self):
        version = iasl_provision.DEFAULT_VERSION
        built = self.script("built", version)
        victim = self.root / "victim"
        victim.write_text("keep")
        primary = self.root / "iasl-dev"
        primary.symlink_to(victim)
        (self.root / "iasl-dev.tmp").symlink_to(victim)
        with mock.patch.object(iasl_provision, "_build_from_source", return_value=str(built)):
            self.assertEqual(iasl_provision.provision(str(self.root), version, False), 0)
        self.assertEqual(victim.read_text(), "keep")
        self.assertFalse(primary.is_symlink())
        self.assertEqual(iasl_provision._version_of(str(primary)), version)

    def test_failed_source_build_does_not_accept_incorrect_system_compiler(self):
        wrong = self.script("iasl", "20251212")
        with mock.patch.dict(os.environ, {"PATH": str(wrong.parent)}), \
             mock.patch.object(iasl_provision, "_build_from_source", return_value=str(wrong)):
            self.assertEqual(iasl_provision.provision(str(self.root), iasl_provision.DEFAULT_VERSION, False), 3)
        self.assertFalse((self.root / "iasl-dev").exists())

    def test_check_validates_selected_slot_and_process_exit_status(self):
        version = iasl_provision.DEFAULT_VERSION
        self.script("iasl-dev", version, 1)
        self.script("iasl-stable", version)
        self.assertEqual(iasl_provision.main(["--target", str(self.root), "--check"]), 3)


class Assessment(unittest.TestCase):
    def test_nvidia_requires_discrete_gpu_and_an_exact_id_in_the_verified_table(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            report = root / "sysreport/Report.json"
            report.parent.mkdir()
            gpu = {"Manufacturer": "NVIDIA", "Device Type": "Discrete GPU"}
            report.write_text(json.dumps({"CPU": {"Manufacturer": "Intel"}, "GPU": {
                "older device": {**gpu, "Device ID": "10DE-0191"},
                "table match": {**gpu, "Device ID": "10de-ffff"},
                "integrated fixture": {**gpu, "Device ID": "10DE-FFFF", "Device Type": "Integrated GPU"},
            }}))
            app = root / "engine/app/p1401"
            app.mkdir(parents=True)
            (app / "nvidia_gsp_ids.json").write_text('{"ids":["FFFF"]}')
            with mock.patch.object(fetch_deps, "verified_root", return_value=str(root)):
                verdict = autohackintosh.stage_assess(tmp)["verdict"]
        self.assertEqual(len(verdict["experimental_driver"]), 1)
        self.assertIn("table match", verdict["experimental_driver"][0])
        self.assertTrue(any("older device" in item for item in verdict["unknown"]))
        self.assertTrue(any("integrated fixture" in item for item in verdict["unknown"]))

    def test_engine_nullmoth_policy_disables_implicit_eligibility_and_restores_upstream(self):
        engine = SimpleNamespace(nullmoth=SimpleNamespace(supported=lambda gpu: True))
        original = engine.nullmoth.supported
        with autohackintosh._nullmoth_policy(engine, False):
            self.assertFalse(engine.nullmoth.supported({}))
        self.assertIs(engine.nullmoth.supported, original)
        with autohackintosh._nullmoth_policy(engine, True):
            self.assertTrue(engine.nullmoth.supported({}))

    def test_workflow_locks_down_existing_inventory_workdir(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "workspace"
            report = root / "sysreport/Report.json"
            report.parent.mkdir(parents=True)
            root.chmod(0o755)
            report.write_text(json.dumps({"CPU": {"Manufacturer": "Intel"}}))
            with mock.patch("builtins.print"):
                code = autohackintosh.main(["assess", "--workdir", str(root)])
            self.assertEqual(code, 0)
            self.assertEqual(root.stat().st_mode & 0o777, 0o700)
            self.assertEqual((root / "last-run.json").stat().st_mode & 0o777, 0o600)

    def test_vendor_ids_alone_do_not_confer_native_support(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = Path(tmp) / "sysreport" / "Report.json"
            report.parent.mkdir()
            report.write_text(json.dumps({
                "GPU": {"synthetic intel": {"Device ID": "8086-FFFF"},
                        "synthetic amd": {"Device ID": "1002-FFFF"}},
                "Sound": {"synthetic codec": {"Bus Type": "HDAUDIO"}},
            }))
            result = autohackintosh.stage_assess(tmp)
        self.assertTrue(result["ok"])
        verdict = result["verdict"]
        self.assertEqual(verdict["natively_supported"], [])
        for name in ("synthetic intel", "synthetic amd"):
            self.assertTrue(any(name in entry for entry in verdict["unknown"]))
        self.assertTrue(any("AppleALC" in entry for entry in verdict["needs_patches"]))
