#!/usr/bin/env python3
"""Tests for ``scripts/linuxcompat.py``.

These target the subtle bugs that actually cost debugging time when 1401's
engine is run on Linux. Each test encodes a specific regression so it cannot
come back silently.

The engine itself is not required: fake modules reproduce the shapes that
mattered (a ``BytesIO`` response subclass, a ``fetch`` default parameter, a
callable sitting next to a boolean default).
"""

from __future__ import annotations

import io
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(os.path.dirname(HERE), "scripts")
sys.path.insert(0, SCRIPTS)

import linuxcompat  # noqa: E402


class Response(io.BytesIO):
    """Mirrors p1401.mirror.Response: a stream that is not bytes."""

    def __init__(self, data: bytes):
        super().__init__(data)

    def getcode(self) -> int:
        return 200


class Coercion(unittest.TestCase):
    def test_bytes_pass_through_unchanged(self):
        self.assertEqual(linuxcompat.coerce_bytes(b"abc"), b"abc")

    def test_response_is_drained(self):
        self.assertEqual(linuxcompat.coerce_bytes(Response(b"abc")), b"abc")

    def test_wants_bytes_detects_response(self):
        self.assertTrue(linuxcompat.wants_bytes(Response(b"")))

    def test_wants_bytes_rejects_bytes(self):
        self.assertFalse(linuxcompat.wants_bytes(b""))

    def test_object_without_read_is_not_coerced(self):
        sentinel = object()
        self.assertIs(linuxcompat.coerce_bytes(sentinel), sentinel)


class FetchDefaultRewriting(unittest.TestCase):
    """Regression: wrapping only the outer call left the inner default unguarded."""

    def test_default_fetch_is_wrapped(self):
        def fn(out_dir, notices, fetch=lambda url: Response(b"payload")):
            return fetch("http://example")

        guarded = linuxcompat._guard_fetch_argument(fn)
        # Called without naming fetch: the *default* must still yield bytes.
        self.assertEqual(guarded("/tmp", []), b"payload")

    def test_callable_default_next_to_boolean_is_not_misplaced(self):
        """Regression: index arithmetic moved fetch into the `nvidia` slot."""

        def place(out_dir, notices, nvidia=False, fetch=lambda url: Response(b"x")):
            return (out_dir, notices, nvidia, fetch("u"))

        guarded = linuxcompat._guard_fetch_argument(place)
        out_dir, notices, nvidia, data = guarded("/tmp", [])
        self.assertEqual(out_dir, "/tmp")
        self.assertEqual(notices, [])
        self.assertIs(nvidia, False, "the boolean default must not be replaced by a function")
        self.assertEqual(data, b"x")

    def test_keyword_fetch_is_wrapped(self):
        def fn(out_dir, notices, fetch=None):
            return fetch("u")

        guarded = linuxcompat._guard_fetch_argument(fn)
        self.assertEqual(guarded("/tmp", [], fetch=lambda url: Response(b"kw")), b"kw")

    def test_positional_fetch_and_other_callable_defaults(self):
        def fallback(url):
            return Response(b"default")

        def unrelated():
            return Response(b"unchanged")

        def place(out_dir, fetch=fallback, callback=unrelated):
            return fetch(out_dir), callback

        guarded = linuxcompat._guard_fetch_argument(place)
        result, callback = guarded("u", lambda url: Response(b"explicit"))
        self.assertEqual(result, b"explicit")
        self.assertIs(callback, unrelated)
        self.assertIs(place.__defaults__[0], fallback)

    def test_wrapper_is_marked_to_prevent_double_wrapping(self):
        def fn(fetch=None):
            return fetch("u") if fetch else b""

        guarded = linuxcompat._guard_fetch_argument(fn)
        self.assertTrue(getattr(guarded, "_autohackintosh_bytes", False))


class CommunityGuard(unittest.TestCase):
    def test_apply_efi_drivers_receives_bytes(self):
        class FakeCommunity:
            called_with = None

            def apply_efi_drivers(self, config_path, customization, macos_version,
                                  notices, fetch, **kw):
                body = fetch("http://example")
                FakeCommunity.called_with = body
                return len(body)

        module = FakeCommunity()
        self.assertTrue(linuxcompat.patch_community(module))
        length = module.apply_efi_drivers(
            "/cfg", {}, "24", [], lambda url: Response(b"12345"))
        self.assertEqual(length, 5)
        self.assertIsInstance(FakeCommunity.called_with, bytes)

    def test_patching_is_idempotent(self):
        class FakeCommunity:
            def apply_efi_drivers(self, fetch):
                return []

        module = FakeCommunity()
        self.assertTrue(linuxcompat.patch_community(module))
        self.assertFalse(linuxcompat.patch_community(module),
                         "a second patch must be a no-op")

    def test_missing_target_is_skipped_not_fatal(self):
        class Empty:
            pass

        self.assertFalse(linuxcompat.patch_community(Empty()))


class ApplyContract(unittest.TestCase):
    def test_apply_raises_compat_error_on_unimportable_engine(self):
        with self.assertRaises(linuxcompat.CompatError):
            linuxcompat.apply("/nonexistent/engine/app")

    def test_applied_records_are_unique(self):
        ids = [entry["id"] for entry in linuxcompat.APPLIED]
        self.assertEqual(len(ids), len(set(ids)))


if __name__ == "__main__":
    unittest.main()
