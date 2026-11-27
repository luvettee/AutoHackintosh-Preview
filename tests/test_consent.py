"""Approval tests use a PTY only to demonstrate that it cannot approve."""

import concurrent.futures
import os
import pty
import stat
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import consent


def plan():
    return consent.plan_from_info(
        {"device": "/dev/synthetic", "removable": "1", "serial": "SYNTH"},
        "write-media", ["erase", "format"], "Existing data will be lost.",
    )


class Approval(unittest.TestCase):
    def test_automated_pty_is_refused_even_with_correct_phrase(self):
        master, slave = pty.openpty()
        self.addCleanup(os.close, master)
        self.addCleanup(os.close, slave)
        proposed = plan()
        os.write(master, (consent.expected_phrase(proposed) + "\n").encode())
        with mock.patch.object(consent.os, "open", side_effect=lambda *args: os.dup(slave)):
            with self.assertRaisesRegex(consent.ConsentRequired, "foreground Linux virtual console"):
                consent.confirm(proposed)

    def test_recording_and_atomic_concurrent_consumption_stay_private(self):
        with tempfile.TemporaryDirectory() as tmp:
            proposed = plan()
            with mock.patch.object(consent, "confirm", side_effect=consent.expected_phrase):
                approval = consent.request_approval(proposed, tmp)
            journal = Path(tmp) / "approval.json"
            self.assertEqual(stat.S_IMODE(journal.stat().st_mode), 0o600)

            # Separate handles with independent locks mimic competing processes
            # and exercise the real exclusive publication below the lock.
            contender = type(approval)(proposed.canonical(), str(journal))
            barrier = threading.Barrier(2)

            def spend(handle):
                barrier.wait()
                try:
                    handle.consume(proposed)
                except consent.ConsentRequired:
                    return False
                return True

            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                self.assertEqual(sorted(pool.map(spend, (approval, contender))), [False, True])
            for path in Path(tmp).iterdir():
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertTrue(journal.with_suffix(".json.consumed").is_file())
            with self.assertRaisesRegex(consent.ConsentDeclined, "already been used"):
                approval.consume(proposed)

    def test_wrong_phrase_never_records_approval(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(consent, "confirm", return_value="yes"):
                with self.assertRaisesRegex(consent.ConsentDeclined, "was not approved"):
                    consent.request_approval(plan(), tmp)
            self.assertEqual(list(Path(tmp).iterdir()), [])
