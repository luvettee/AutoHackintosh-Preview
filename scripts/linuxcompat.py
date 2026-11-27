"""AutoHackintosh Linux compatibility layer for NullMoth 1401's headless engine.

1401 ships Windows and macOS builds only. Its engine is plain Python and runs
on Linux (see references/1401-linux-engine.md). Two behaviours differ enough to
break, or silently degrade, a build.

THE FAILURE MODE THAT MATTERS
-----------------------------
``p1401/mirror.py`` defines ``Response(io.BytesIO)``: it is a response *and* a
stream. ``mirror.fetch`` returns one. Callers that do ``len(data)`` on it, or
hand it to ``hashlib``, raise ``TypeError: object of type 'Response' has no
len()``.

Those call sites sit inside broad ``except Exception`` handlers whose stated
purpose is "an optional dependency must never break a build". So the build
reports success while silently skipping work. Observed on real hardware:

  * ``own_virtualsmc``   -- the NullMoth VirtualSMC sensor build was skipped,
    silently downgrading fan-sensor support.
  * ``own_experimental`` -- experimental driver staging was skipped.

A silent skip is worse than a crash: the EFI looks correct and is not.

WHY NOT COERCE INSIDE ``make_request``
--------------------------------------
Tried first, and wrong. ``make_request`` returns a response-like object *by
design*: ``p1401/apple.py`` reads ``.headers``/``.getcode()``/``.info()`` off it
for the Apple recovery downloader. A global coercion broke that path with
``'bytes' object has no attribute 'getcode'``.

So repairs are scoped to the consumers that want bytes, each verified against
1401 3.1 source:

  * ``own_virtualsmc.replace(out_dir, notices, fetch=mirror.fetch)``
  * ``own_experimental.place(out_dir, notices, nvidia=False, fetch=mirror.fetch)``
  * ``community.apply_efi_drivers(..., fetch)``

``fetch`` is bound to the wrapper both as a default argument and as a positional
or keyword override, because the engine calls some of these without naming it.
Every wrapper is a no-op on bytes, is idempotent, and is skipped when the target
is absent, so a future release that fixes this upstream is left untouched.
Applied repairs are recorded and surfaced in the diagnostic report: they change
upstream behaviour and must stay visible.
"""

from __future__ import annotations

import functools
import inspect

#: Repairs applied in this process: ``{"id", "reason", "detail"}``.
APPLIED: list[dict] = []


class CompatError(RuntimeError):
    """The engine could not be imported or a required repair could not be applied."""


def _record(repair_id: str, reason: str, detail: str) -> None:
    entry = {"id": repair_id, "reason": reason, "detail": detail}
    if entry not in APPLIED:
        APPLIED.append(entry)


def wants_bytes(value) -> bool:
    """True when ``value`` is a response-like stream rather than bytes.

    ``p1401.mirror.Response`` subclasses ``io.BytesIO``: ``read()`` works but
    ``len()``/``hashlib`` do not, which is exactly the broken case.
    """
    return not isinstance(value, (bytes, bytearray, memoryview)) and callable(
        getattr(value, "read", None)
    )


def coerce_bytes(value):
    """Drain a response-like object to bytes; pass everything else through."""
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value)
    if wants_bytes(value):
        try:
            return value.read()
        finally:
            close = getattr(value, "close", None)
            if callable(close):
                close()
    return value


def _wrap_fetch(fetch):
    """Return a callable yielding bytes for whatever ``fetch`` returns."""
    if not callable(fetch):
        return fetch

    @functools.wraps(fetch)
    def wrapped(url, *args, **kwargs):
        return coerce_bytes(fetch(url, *args, **kwargs))

    return wrapped


def _guard_fetch_argument(fn):
    """Bind and wrap only the actual fetch parameter, including its default."""
    signature = inspect.signature(fn)
    if "fetch" not in signature.parameters:
        raise CompatError(f"{fn.__name__} has no fetch callback")

    @functools.wraps(fn)
    def guarded(*args, **kwargs):
        bound = signature.bind(*args, **kwargs)
        bound.apply_defaults()
        bound.arguments["fetch"] = _wrap_fetch(bound.arguments["fetch"])
        return fn(*bound.args, **bound.kwargs)

    guarded._autohackintosh_bytes = True
    return guarded


def patch_function(module, attribute: str, module_name: str) -> bool:
    """Guard ``module.attribute``'s ``fetch`` argument (and its default binding)."""
    fn = getattr(module, attribute, None)
    if fn is None or getattr(fn, "_autohackintosh_bytes", False):
        return False
    setattr(module, attribute, _guard_fetch_argument(fn))
    _record(
        f"{module_name}.{attribute}",
        "feeds a p1401.mirror.Response (a BytesIO, not bytes) to len()/hashlib inside a silent except",
        "wrapped fetch (argument and default) so it yields bytes",
    )
    return True


def patch_community(p1401_community) -> bool:
    """Guard ``community.apply_efi_drivers``'s ``fetch`` argument (1401 3.1: community.py:317)."""
    apply_fn = getattr(p1401_community, "apply_efi_drivers", None)
    if apply_fn is None or getattr(apply_fn, "_autohackintosh_bytes", False):
        return False

    p1401_community.apply_efi_drivers = _guard_fetch_argument(apply_fn)
    _record(
        "community.apply_efi_drivers",
        "compares len(body) against a pinned size after fetch()",
        "wrapped the fetch callback so it yields bytes",
    )
    return True


def apply(app_root: str) -> dict:
    """Import 1401's engine from ``app_root`` and install every applicable repair.

    ``app_root`` is the directory holding ``p1401/`` and ``upstream/``, i.e.
    ``<1401>/engine/app``.
    """
    import importlib
    import sys

    for path in (app_root, f"{app_root}/upstream/OpCore-Simplify"):
        if path not in sys.path:
            sys.path.insert(0, path)

    try:
        community = importlib.import_module("p1401.community")
    except Exception as exc:  # noqa: BLE001
        raise CompatError(f"cannot import 1401's engine from {app_root}: {exc}") from exc

    active = set()
    skipped: list[str] = []

    patch_community(community)
    if getattr(getattr(community, "apply_efi_drivers", None), "_autohackintosh_bytes", False):
        active.add("community.apply_efi_drivers")
    else:
        skipped.append("community.apply_efi_drivers (absent)")

    targets = {
        "own_virtualsmc": ("replace",),
        "own_experimental": ("place",),
    }
    for name, functions in targets.items():
        try:
            module = importlib.import_module(f"p1401.{name}")
        except ModuleNotFoundError as exc:
            if exc.name != f"p1401.{name}":
                raise
            skipped.append(f"{name} (not in this release)")
            continue
        for attribute in functions:
            patch_function(module, attribute, name)
            if getattr(getattr(module, attribute, None), "_autohackintosh_bytes", False):
                active.add(f"{name}.{attribute}")
            else:
                skipped.append(f"{name}.{attribute} (absent)")

    return {
        "applied": [entry for entry in APPLIED if entry["id"] in active],
        "skipped": skipped,
        "modules": sorted({e["id"].rsplit(".", 1)[0] for e in APPLIED}),
    }


def self_test() -> dict:
    """Verify the coercion primitive still converts a BytesIO-like response to bytes.

    Upstream could change the contract underneath the wrappers. This exercises
    the *behaviour that originally failed* (``len()``/``hashlib`` on a response)
    and reports whether every applied repair still produces bytes, so a
    regression surfaces loudly instead of reintroducing the silent-skip bug.
    """
    import hashlib
    import io

    class Probe(io.BytesIO):
        def getcode(self): return 200

    failures = []
    for entry in APPLIED:
        try:
            out = coerce_bytes(Probe(b"probe"))
            if not isinstance(out, (bytes, bytearray)):
                failures.append({"id": entry["id"], "error": f"returned {type(out)}"})
            else:
                hashlib.sha256(out)  # must not raise
        except Exception as exc:  # noqa: BLE001
            failures.append({"id": entry["id"], "error": f"{type(exc).__name__}: {exc}"})
    return {"ok": not failures, "repairs_checked": len(APPLIED), "failures": failures}
