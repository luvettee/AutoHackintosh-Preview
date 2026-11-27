#!/usr/bin/env python3
"""Tests for ``scripts/report_normalize.py``.

The property under test is narrow and important: repairs must make the report
acceptable to the validator *without* changing anything that influences an EFI
decision. Each test therefore asserts both "the field was repaired" and "the
fields the engine actually reads are untouched".
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(os.path.dirname(HERE), "scripts")
sys.path.insert(0, SCRIPTS)

import report_normalize  # noqa: E402


def _report():
    return {
        "CPU": {"Manufacturer": "Intel", "Processor Name": "12th Gen Intel(R) Core(TM) TESTCPU-1200"},
        "Motherboard": {"Name": "TESTBOARD-A1", "Chipset": "TESTCHIP"},
        "GPU": {"NVIDIA TESTCHIPA": {"Device ID": "TEST-0000", "Resizable BAR": "Enabled"}},
        "Monitor": {
            "DELL U2720Q": {"Connector Type": "HDMI-A", "Connected GPU": "NVIDIA TESTCHIPA"},
            "Old Panel": {"Connector Type": "VGA", "Connected GPU": "NVIDIA TESTCHIPA"},
            "Odd One": {"Connector Type": "DP-1", "Connected GPU": "NVIDIA TESTCHIPA"},
        },
        "System Devices": {
            "Thermal Zone": {"Device ID": "PNP0C0B", "Bus Type": "ACPI", "ACPI Path": "\\_TZ_.FAN3"},
            "Good Device": {"Device ID": "8086-7AD0", "Bus Type": "PCI", "ACPI Path": "\\_SB.PC00.HDAS"},
        },
    }


class ConnectorNormalization(unittest.TestCase):
    def test_hdmi_linux_suffix_is_mapped(self):
        self.assertEqual(report_normalize.normalize_connector("HDMI-A"), "HDMI")

    def test_dp_numbered_is_mapped(self):
        self.assertEqual(report_normalize.normalize_connector("DP-1"), "DP")

    def test_edp_is_preserved(self):
        self.assertEqual(report_normalize.normalize_connector("eDP-1"), "eDP")

    def test_valid_value_passes_through(self):
        self.assertEqual(report_normalize.normalize_connector("VGA"), "VGA")

    def test_unknown_is_dropped_not_guessed(self):
        self.assertIsNone(report_normalize.normalize_connector("SOME-VENDOR-THING"))

    def test_dvi_d_names(self):
        self.assertEqual(report_normalize.normalize_connector("DVI-D-1"), "DVI")


class SanitizeBehaviour(unittest.TestCase):
    def test_out_of_namespace_acpi_path_dropped(self):
        report = _report()
        result = report_normalize.sanitize(report)
        self.assertNotIn("ACPI Path", report["System Devices"]["Thermal Zone"])

    def test_in_namespace_acpi_path_preserved(self):
        report = _report()
        report_normalize.sanitize(report)
        self.assertEqual(report["System Devices"]["Good Device"]["ACPI Path"], "\\_SB.PC00.HDAS")

    def test_engine_relevant_fields_untouched(self):
        """Device ID and Bus Type decide ACPI patches and audio layout: never altered."""
        before = _report()
        report = _report()
        report_normalize.sanitize(report)
        for name, props in before["System Devices"].items():
            self.assertEqual(props.get("Device ID"), report["System Devices"][name].get("Device ID"))
            self.assertEqual(props.get("Bus Type"), report["System Devices"][name].get("Bus Type"))
        self.assertEqual(before["GPU"], report["GPU"])
        self.assertEqual(before["CPU"], report["CPU"])
        self.assertEqual(before["Motherboard"], report["Motherboard"])

    def test_vga_semantics_preserved(self):
        """VGA detection must still work after normalisation."""
        report = _report()
        report_normalize.sanitize(report)
        self.assertEqual(report["Monitor"]["Old Panel"]["Connector Type"], "VGA")
        self.assertEqual(report["Monitor"]["DELL U2720Q"]["Connector Type"], "HDMI")

    def test_edits_are_reported(self):
        result = report_normalize.sanitize(_report())
        fields = {edit["field"] for edit in result["edits"]}
        self.assertIn("Monitor.DELL U2720Q.Connector Type", fields)
        self.assertIn("System Devices.Thermal Zone.ACPI Path", fields)

    def test_empty_report_is_safe(self):
        self.assertEqual(report_normalize.sanitize({})["edits"], [])


class SanitizeFile(unittest.TestCase):
    def test_backup_created_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "Report.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(_report(), fh)
            first = report_normalize.sanitize_file(path)
            second = report_normalize.sanitize_file(path)
            self.assertTrue(os.path.exists(first["backup"]))
            self.assertEqual(second["edits"], [],
                             "a second pass must be a no-op (idempotent)")

    def test_backup_preserves_original(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "Report.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(_report(), fh)
            result = report_normalize.sanitize_file(path)
            with open(result["backup"], encoding="utf-8") as fh:
                original = json.load(fh)
            self.assertEqual(original["Monitor"]["DELL U2720Q"]["Connector Type"], "HDMI-A")


if __name__ == "__main__":
    unittest.main()
