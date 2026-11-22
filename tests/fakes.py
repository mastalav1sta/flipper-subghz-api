"""Test-only fakes. Importing this module installs a hard guard: any attempt to open a real
Flipper serial device (/dev/ttyACM*, /dev/ttyUSB*, /dev/serial/*) during tests raises
immediately. All action names and paths below are made up."""

from __future__ import annotations

import os
import threading
import time

from flipper_subghz_api.actions import parse_actions
from flipper_subghz_api.flipper import ETX, TxResult

_real_open = os.open


def _guarded_open(path, *args, **kwargs):
    p = os.fspath(path)
    if isinstance(p, bytes):
        p = p.decode(errors="replace")
    if p.startswith("/dev/ttyACM") or p.startswith("/dev/serial/") or p.startswith("/dev/ttyUSB"):
        raise AssertionError(f"TEST GUARD: refusing to open real serial device {p}")
    return _real_open(path, *args, **kwargs)


os.open = _guarded_open

PROMPT = b"\r\n>: "

# The allowlist every test uses, built through the real validator.
TEST_ACTIONS_DATA = {
    "actions": [
        {"name": "Gate", "path": "/ext/subghz/Gate.sub", "protocol": "RAW", "repeat": 1},
        {"name": "Garage_door", "path": "/ext/subghz/Garage_door.sub", "protocol": "RAW",
         "repeat": 1},
        {"name": "Light_on", "path": "/ext/subghz/Light_on.sub", "protocol": "Princeton",
         "repeat": 10},
        {"name": "Light_off", "path": "/ext/subghz/Light_off.sub", "protocol": "Princeton",
         "repeat": 10},
    ]
}
TEST_ALLOWLIST = parse_actions(TEST_ACTIONS_DATA)
ACTIONS = {name: spec.path for name, spec in TEST_ALLOWLIST.specs.items()}


def listening(path: str, protocol: str = "RAW", frequency: int = 868350000) -> bytes:
    """What subghz_cli.c:774 (mntm-012) prints once a file is accepted, then TX dots."""
    return (
        b"\r\nListening at \x1b[0;33m" + path.encode() + b"\x1b[0m. Frequency="
        + str(frequency).encode() + b", Protocol=" + protocol.encode()
        + b"\r\n\r\nPress CTRL+C to stop\r\n\r\n...."
    )


class FakeTransport:
    """Scripted CLI. `responses` maps a written command (str, without CR) to the bytes the
    fake Flipper sends back after echoing it. ETX always yields a fresh prompt unless
    `sync_ok=False`. `hang=True` swallows the command (simulates no prompt / timeout)."""

    def __init__(self, responses=None, sync_ok=True, hang=False, raise_after_write=False,
                 raise_on_open=None):
        self.responses = responses or {}
        self.sync_ok = sync_ok
        self.hang = hang
        self.raise_after_write = raise_after_write
        self.raise_on_open = raise_on_open
        self.writes = []
        self.opened = 0
        self.closed = 0
        self._out = b""

    # transport API
    def open(self):
        self.opened += 1
        if self.raise_on_open:
            raise self.raise_on_open

    def write(self, data: bytes):
        self.writes.append(data)
        if data == ETX:
            if self.sync_ok:
                self._out += b"^C" + PROMPT
            return
        if self.raise_after_write:
            raise OSError(5, "I/O error (fake)")
        cmd = data.rstrip(b"\r").decode()
        self._out += data.rstrip(b"\r")  # echo
        if self.hang:
            return
        self._out += self.responses.get(cmd, b"\r\ncould not find command") + PROMPT

    def read(self, timeout: float) -> bytes:
        if self._out:
            out, self._out = self._out, b""
            return out
        time.sleep(min(timeout, 0.01))
        return b""

    def flush_input(self):
        self._out = b""

    def close(self):
        self.closed += 1

    # helpers
    def commands(self):
        return [w for w in self.writes if w != ETX]


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        self.t += 0.05  # every look at the clock advances time -> fast timeouts
        return self.t


class FakeCli:
    """Stands in for FlipperCli in HTTP tests. Never touches any device."""

    def __init__(self, result=None, block: threading.Event = None):
        self.result = result or TxResult("sent", "tx_from_file_completed")
        self.block = block
        self.started = threading.Event()
        self.calls = []
        self._lock = threading.Lock()

    def transmit(self, path):
        with self._lock:
            self.calls.append(path)
        self.started.set()
        if self.block is not None:
            self.block.wait(5)
        return self.result
