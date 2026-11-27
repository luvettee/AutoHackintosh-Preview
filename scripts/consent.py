#!/usr/bin/env python3
"""Review an erase plan on the local Linux console and consume it once."""

from __future__ import annotations

import array
import fcntl
import hashlib
import json
import os
import tempfile
import termios
import threading
import time
from dataclasses import asdict, dataclass, field


class ConsentRequired(RuntimeError):
    pass


class ConsentDeclined(RuntimeError):
    pass


@dataclass
class Plan:
    operation: str
    device: str
    kernel_name: str
    model: str
    serial: str | None
    size_bytes: int | None
    transport: str | None
    removable: bool
    contents: list[str]
    actions: list[str]
    consequence: str
    device_state: dict = field(default_factory=dict)
    payload_manifest: dict = field(default_factory=dict)

    def canonical(self) -> str:
        return json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))

    def fingerprint(self) -> str:
        return hashlib.sha256(self.canonical().encode()).hexdigest()[:16].upper()

    def render(self) -> str:
        # JSON escapes control characters from device labels and payload names.
        return json.dumps(asdict(self), indent=2, sort_keys=True) + "\n"


def plan_from_info(info: dict, operation: str, actions: list[str], consequence: str) -> Plan:
    return Plan(
        operation, info.get("resolved") or info.get("device") or "?",
        info.get("kernel_name") or "?",
        f"{info.get('vendor') or ''} {info.get('model') or ''}".strip() or "unknown",
        info.get("serial"), info.get("size_bytes"),
        info.get("transport_lsblk") or info.get("transport_sysfs"),
        info.get("removable") == "1", info.get("filesystems") or [], actions, consequence,
        device_state=info,
    )


def expected_phrase(plan: Plan) -> str:
    return f"ERASE {plan.fingerprint()}"


def _check_console(fd: int) -> None:
    device = array.array("I", [0])
    # TIOCGDEV returns the backing terminal device for the /dev/tty alias.
    fcntl.ioctl(fd, 0x80045432, device, True)
    if (os.major(device[0]) != 4 or not 1 <= os.minor(device[0]) <= 63
            or os.tcgetpgrp(fd) != os.getpgrp()):
        raise ConsentRequired("confirmation requires a foreground Linux virtual console")


def confirm(plan: Plan) -> str:
    try:
        fd = os.open("/dev/tty", os.O_RDWR | os.O_CLOEXEC)
        with os.fdopen(fd, "rb", buffering=0) as terminal:
            _check_console(fd)
            termios.tcflush(fd, termios.TCIFLUSH)
            prompt = plan.render() + f"\nType {expected_phrase(plan)} to approve.\n> "
            with os.fdopen(os.dup(fd), "wb") as out:
                out.write(prompt.encode())
            return terminal.readline(128).decode("utf-8", "replace").strip()
    except OSError as exc:
        raise ConsentRequired("confirmation requires a foreground Linux virtual console") from exc


class _Approval:
    def __init__(self, canonical_plan: str, path: str):
        self._canonical_plan = canonical_plan
        self._path = path
        self._lock = threading.Lock()
        self._used = False

    def consume(self, live_plan: Plan) -> None:
        with self._lock:
            if self._used:
                raise ConsentDeclined("approval has already been used")
            if self._canonical_plan != live_plan.canonical():
                raise ConsentDeclined("device state, plan or payload changed since approval")
            self._used = True
            _journal(self._path, self._canonical_plan, consumed=True)


def _journal(path: str, canonical_plan: str, *, consumed: bool) -> None:
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=os.path.dirname(path), prefix=".approval-",
        ) as record:
            json.dump({
                "plan": json.loads(canonical_plan), "source": "interactive-console",
                "time": time.time(), "consumed": consumed,
            }, record)
            record.flush()
            os.fsync(record.fileno())
            # Exclusive publication also prevents replay through a duplicate
            # object or process. Both names refer to a 0600 inode.
            os.link(record.name, path + (".consumed" if consumed else ""))
    except OSError as exc:
        raise ConsentRequired(f"could not write private approval record: {exc}") from exc


def request_approval(plan: Plan, private_dir: str) -> _Approval:
    canonical_plan = plan.canonical()
    if confirm(plan) != expected_phrase(plan) or plan.canonical() != canonical_plan:
        raise ConsentDeclined("erase plan was not approved")
    path = os.path.join(private_dir, "approval.json")
    _journal(path, canonical_plan, consumed=False)
    return _Approval(canonical_plan, path)
