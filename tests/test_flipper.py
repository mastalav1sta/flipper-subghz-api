import errno
import os
import pty
import select
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from tests import fakes  # noqa: F401  (installs the real-device open guard)
from tests.fakes import ACTIONS, TEST_ALLOWLIST, FakeClock, FakeTransport, listening

from flipper_subghz_api import flipper as flipper_mod
from flipper_subghz_api.flipper import (
    ETX, FAILED, SENT, UNKNOWN, DeviceNotFound, FlipperCli, SerialPortBusy,
    SerialTransport, classify,
)

ALLOWED_PATHS = TEST_ALLOWLIST.paths


def cmd(path, repeat=1):
    return f"subghz tx_from_file {path} {repeat} 0"


# Expected wire commands, written out by hand (NOT derived from the allowlist) so a change
# to how path/repeat reach the wire shows up here as a test failure.
RAW_WIRE = {
    "Gate": "subghz tx_from_file /ext/subghz/Gate.sub 1 0",
    "Garage_door": "subghz tx_from_file /ext/subghz/Garage_door.sub 1 0",
}
KEY_WIRE = {
    "Light_on": "subghz tx_from_file /ext/subghz/Light_on.sub 10 0",
    "Light_off": "subghz tx_from_file /ext/subghz/Light_off.sub 10 0",
}


def princeton(path):
    return listening(path, protocol="Princeton", frequency=433920000)


def make_cli(factory, **kw):
    return FlipperCli(factory, TEST_ALLOWLIST, **kw)


class ActionMappingTests(unittest.TestCase):
    def test_exactly_the_configured_mappings(self):
        self.assertEqual(
            ACTIONS,
            {
                "Gate": "/ext/subghz/Gate.sub",
                "Garage_door": "/ext/subghz/Garage_door.sub",
                "Light_on": "/ext/subghz/Light_on.sub",
                "Light_off": "/ext/subghz/Light_off.sub",
            },
        )
        self.assertEqual(ALLOWED_PATHS, frozenset(ACTIONS.values()))

    def test_protocol_and_repeat_per_action(self):
        self.assertEqual(
            {n: (s.protocol, s.repeat) for n, s in TEST_ALLOWLIST.specs.items()},
            {
                "Gate": ("RAW", 1),
                "Garage_door": ("RAW", 1),
                "Light_on": ("Princeton", 10),
                "Light_off": ("Princeton", 10),
            },
        )
        for name, spec in TEST_ALLOWLIST.specs.items():
            self.assertEqual(spec.path, ACTIONS[name])
            self.assertIs(TEST_ALLOWLIST.by_path[spec.path], spec)

    def test_mapping_is_immutable(self):
        with self.assertRaises(TypeError):
            TEST_ALLOWLIST.specs["Evil"] = None  # type: ignore[index]
        with self.assertRaises(TypeError):
            TEST_ALLOWLIST.by_path["/ext/subghz/x.sub"] = None  # type: ignore[index]
        with self.assertRaises(AttributeError):
            TEST_ALLOWLIST.paths = frozenset({"/ext/subghz/x.sub"})  # type: ignore[misc]
        with self.assertRaises(AttributeError):
            del TEST_ALLOWLIST.specs
        with self.assertRaises(Exception):  # frozen dataclass
            TEST_ALLOWLIST.specs["Light_on"].repeat = 99  # type: ignore[misc]

    def test_lookalike_files_not_mapped(self):
        for p in ("/ext/subghz/Gate2.sub", "/ext/subghz/Light.sub",
                  "/ext/subghz/light_on.sub", "/ext/subghz/Gate.sub "):
            self.assertNotIn(p, ALLOWED_PATHS)


class TransmitTests(unittest.TestCase):
    def make(self, transport, **kw):
        return make_cli(lambda: transport, clock=FakeClock(), quiet_period=0.0, **kw)

    def test_each_action_sends_its_own_file_once(self):
        """RAW actions: wire command with repeat 1, RAW output -> sent."""
        for name, wire in RAW_WIRE.items():
            with self.subTest(name=name):
                path = ACTIONS[name]
                t = FakeTransport({wire: listening(path)})
                r = self.make(t).transmit(path)
                self.assertEqual((r.status, r.reason), (SENT, "tx_from_file_completed"))
                self.assertEqual(t.commands(), [wire.encode() + b"\r"])
                self.assertEqual(t.writes[0], ETX)  # line cleared before typing
                self.assertEqual(t.closed, 1)

    def test_key_file_actions_send_with_configured_repeat(self):
        for name, wire in KEY_WIRE.items():
            with self.subTest(name=name):
                path = ACTIONS[name]
                t = FakeTransport({wire: princeton(path)})
                r = self.make(t).transmit(path)
                self.assertEqual((r.status, r.reason), (SENT, "tx_from_file_completed"))
                self.assertEqual(t.commands(), [wire.encode() + b"\r"])
                self.assertEqual(t.writes[0], ETX)
                self.assertEqual(t.opened, 1)
                self.assertEqual(t.closed, 1)

    def test_key_file_with_raw_output_is_not_sent(self):
        for name, wire in KEY_WIRE.items():
            with self.subTest(name=name):
                path = ACTIONS[name]
                t = FakeTransport({wire: listening(path, protocol="RAW")})
                r = self.make(t).transmit(path)
                self.assertNotEqual(r.status, SENT)
                self.assertEqual((r.status, r.reason),
                                 (UNKNOWN, "unexpected_protocol_or_listening_line"))
                self.assertEqual(len(t.commands()), 1, "never retried")

    def test_raw_action_with_princeton_output_is_not_sent(self):
        for name, wire in RAW_WIRE.items():  # fake transport only, no RF
            with self.subTest(name=name):
                path = ACTIONS[name]
                t = FakeTransport({wire: princeton(path)})
                r = self.make(t).transmit(path)
                self.assertNotEqual(r.status, SENT)
                self.assertEqual((r.status, r.reason),
                                 (UNKNOWN, "unexpected_protocol_or_listening_line"))
                self.assertEqual(len(t.commands()), 1, "never retried")

    def test_non_allowlisted_path_rejected_before_open(self):
        t = FakeTransport()
        for bad in ("/ext/subghz/Gate2.sub", "/ext/subghz/../x.sub", "Gate",
                    "/ext/subghz/Gate.sub 1 0\rstorage remove /ext", "", None):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.make(t).transmit(bad)
        self.assertEqual(t.opened, 0)
        self.assertEqual(t.writes, [])

    def test_missing_device_fails_without_writing(self):
        with tempfile.TemporaryDirectory() as d:
            missing = os.path.join(d, "no-flipper-here")
            cli = make_cli(lambda: SerialTransport(missing), clock=FakeClock())
            r = cli.transmit(ACTIONS["Gate"])
        self.assertEqual((r.status, r.reason), (FAILED, "device_not_found"))

    def test_port_busy_fails(self):
        t = FakeTransport(raise_on_open=SerialPortBusy("x"))
        r = self.make(t).transmit(ACTIONS["Garage_door"])
        self.assertEqual((r.status, r.reason), (FAILED, "serial_port_busy"))
        self.assertEqual(t.writes, [])

    def test_open_device_not_found_exception(self):
        t = FakeTransport(raise_on_open=DeviceNotFound("x"))
        r = self.make(t).transmit(ACTIONS["Garage_door"])
        self.assertEqual(r.reason, "device_not_found")

    def test_no_prompt_on_sync_fails_without_command(self):
        t = FakeTransport(sync_ok=False)
        r = self.make(t).transmit(ACTIONS["Garage_door"])
        self.assertEqual((r.status, r.reason), (FAILED, "cli_prompt_not_ready"))
        self.assertEqual(t.commands(), [])

    def test_timeout_is_unknown_sends_ctrl_c_and_never_retries(self):
        t = FakeTransport(hang=True)
        r = self.make(t, tx_timeout=1.0).transmit(ACTIONS["Garage_door"])
        self.assertEqual((r.status, r.reason), (UNKNOWN, "timeout_waiting_for_prompt"))
        self.assertEqual(len(t.commands()), 1, "command must be written exactly once")
        self.assertEqual(t.writes[-1], ETX, "abort possibly-running TX, nothing else")
        self.assertEqual(t.opened, 1)

    def test_serial_error_after_command_is_unknown_no_retry(self):
        t = FakeTransport(raise_after_write=True)
        r = self.make(t).transmit(ACTIONS["Gate"])
        self.assertEqual((r.status, r.reason), (UNKNOWN, "serial_error_after_command_sent"))
        self.assertEqual(len(t.commands()), 1)
        self.assertEqual(t.opened, 1)

    def test_firmware_errors_are_failed(self):
        path = ACTIONS["Gate"]
        out = b"\r\nsubghz tx_from_file: \x1b[0;31mError open file\x1b[0m " + path.encode()
        t = FakeTransport({cmd(path): out})
        r = self.make(t).transmit(path)
        self.assertEqual((r.status, r.reason), (FAILED, "capture_file_not_found_on_flipper"))

    def test_classify(self):
        cases = {
            "\r\nListening at x. Frequency=868350000, Protocol=RAW\r\n"
            "Transmission on this frequency is restricted in your settings/region\r\n":
                (FAILED, "tx_refused_by_firmware_region_or_settings"),
            "\r\nListening at x. Frequency=1, Protocol=RAW\r\n..": (SENT, "tx_from_file_completed"),
            "\r\ncould not find command `subghz`": (FAILED, "subghz_cli_command_unavailable"),
            "\r\nsubghz tx_from_file: Error device not found": (FAILED, "radio_device_not_found"),
            "\r\nsubghz tx_from_file: Missing protocol": (FAILED, "tx_from_file_rejected_before_transmit"),
            "\r\nsomething else": (UNKNOWN, "unrecognized_cli_output"),
            "": (UNKNOWN, "unrecognized_cli_output"),
        }
        for text, want in cases.items():
            with self.subTest(text=text):
                r = classify(text, "RAW")
                self.assertEqual((r.status, r.reason), want)

    def test_classify_is_protocol_exact(self):
        L = "\r\nListening at /ext/subghz/A.sub. Frequency=433920000, Protocol="
        mismatch = (UNKNOWN, "unexpected_protocol_or_listening_line")
        cases = [
            (L + "Princeton\r\n\r\nPress CTRL+C to stop\r\n..", "Princeton",
             (SENT, "tx_from_file_completed")),
            (L + "Princeton\r\n..", "RAW", mismatch),
            (L + "RAW\r\n..", "Princeton", mismatch),
            (L + "RAWfoo\r\n..", "RAW", mismatch),
            (L + "Princeton2\r\n..", "Princeton", mismatch),
            (L + "princeton\r\n..", "Princeton", mismatch),
            (L + "\r\n..", "RAW", mismatch),
            (L + "RAW\r\n" + L + "RAW\r\n", "RAW", mismatch),  # two TX starts: not "sent"
            # a Protocol=... fragment outside a real Listening line never counts
            ("\r\nsubghz tx_from_file: Error deserialize protocol 3 Protocol=Princeton",
             "Princeton", (FAILED, "tx_from_file_rejected_before_transmit")),
            ("\r\nfoo Listening at x. Frequency=1, Protocol=Princeton\r\n", "Princeton",
             mismatch),
            ("\r\nProtocol=Princeton\r\n", "Princeton", (UNKNOWN, "unrecognized_cli_output")),
            (L + "Princeton\r\nTransmission on this frequency is restricted in your "
             "settings/region\r\n", "Princeton",
             (FAILED, "tx_refused_by_firmware_region_or_settings")),
        ]
        for i, (text, proto, want) in enumerate(cases):
            with self.subTest(case=i, proto=proto):
                r = classify(text, proto)
                self.assertEqual((r.status, r.reason), want)


class PtyFlipper(threading.Thread):
    """Minimal Flipper shell emulator on a pseudo-terminal (no hardware)."""

    def __init__(self, master_fd, allowlist=TEST_ALLOWLIST):
        super().__init__(daemon=True)
        self.fd = master_fd
        self.allowlist = allowlist
        self.lines = []
        self.stop = threading.Event()

    def run(self):
        line = b""
        while not self.stop.is_set():
            try:
                data = os.read(self.fd, 1024)
            except OSError as exc:
                if exc.errno == errno.EIO:  # no slave fd open right now (between requests)
                    time.sleep(0.01)
                    continue
                return
            for b in data:
                ch = bytes([b])
                if ch == ETX:
                    line = b""
                    os.write(self.fd, b"^C\r\n>: ")
                elif ch == b"\r":
                    self.lines.append(line.decode())
                    out = b"\r\n"
                    parts = line.decode().split(" ")
                    spec = (self.allowlist.by_path.get(parts[2])
                            if parts[:2] == ["subghz", "tx_from_file"] and len(parts) > 2
                            else None)
                    if spec is not None and spec.protocol == "RAW":
                        out += listening(parts[2])[2:]
                    elif spec is not None:
                        out += listening(parts[2], spec.protocol, 433920000)[2:]
                    else:
                        out += b"could not find command"
                    os.write(self.fd, out + b"\r\n>: ")
                    line = b""
                else:
                    line += ch
                    os.write(self.fd, ch)  # echo


class PtySerialTransportTest(unittest.TestCase):
    """Exercises the real SerialTransport (termios/flock/TIOCEXCL/select) on a pty."""

    def test_real_transport_against_pty_emulator(self):
        master, slave = pty.openpty()
        slave_path = os.ttyname(slave)
        # Close our slave fd: any other open fd on the tty is now a "holder" and refused.
        os.close(slave)
        emu = PtyFlipper(master)
        emu.start()
        try:
            cli = make_cli(lambda: SerialTransport(slave_path), tx_timeout=5, sync_timeout=3)
            r = cli.transmit(ACTIONS["Garage_door"])
            self.assertEqual((r.status, r.reason), (SENT, "tx_from_file_completed"))
            self.assertEqual(emu.lines, [cmd(ACTIONS["Garage_door"])])
            # regression (found by offline probe): a 2nd request must be able to reopen
            r = cli.transmit(ACTIONS["Gate"])
            self.assertEqual((r.status, r.reason), (SENT, "tx_from_file_completed"))
            self.assertEqual(emu.lines, [cmd(ACTIONS["Garage_door"]), cmd(ACTIONS["Gate"])])
            r = cli.transmit(ACTIONS["Light_off"])
            self.assertEqual((r.status, r.reason), (SENT, "tx_from_file_completed"))
            self.assertEqual(emu.lines[-1], KEY_WIRE["Light_off"])
            self.assertEqual(len(emu.lines), 3)
        finally:
            emu.stop.set()
            os.close(master)

    def test_second_open_while_held_is_busy(self):
        master, slave = pty.openpty()
        slave_path = os.ttyname(slave)
        os.close(slave)
        try:
            a = SerialTransport(slave_path)
            a.open()
            try:
                with self.assertRaises(SerialPortBusy):
                    SerialTransport(slave_path).open()
            finally:
                a.close()
        finally:
            os.close(master)


PEER_FLAGS = os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK
# Child-process peer: opens the tty WITHOUT flock/TIOCEXCL (like qFlipper/screen), reports
# ready, then holds the fd until its stdin is closed.
_CHILD_PEER = (
    "import os, sys\n"
    "fd = os.open(sys.argv[1], os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)\n"
    "sys.stdout.write('ready\\n'); sys.stdout.flush()\n"
    "sys.stdin.read()\n"
)


def master_received(master, wait=0.3) -> bytes:
    """Everything that reached the pty master (i.e. was written to the 'device')."""
    got = b""
    while True:
        ready, _, _ = select.select([master], [], [], wait)
        if not ready:
            return got
        try:
            chunk = os.read(master, 4096)
        except OSError:  # EIO: no slave open any more
            return got
        if not chunk:
            return got
        got += chunk
        wait = 0.05


class ExistingPeerTest(unittest.TestCase):
    """A client that opened the port BEFORE us (no flock, no TIOCEXCL) must make open()
    refuse, and nothing may be written to the port."""

    def setUp(self):
        self.master, slave = pty.openpty()
        self.path = os.ttyname(slave)
        os.close(slave)

    def tearDown(self):
        os.close(self.master)

    def assert_refused_and_silent(self):
        t = SerialTransport(self.path)
        try:
            t.open()
        except SerialPortBusy as exc:
            self.assertEqual(exc.reason, "held_by_other_process")
        else:
            t.close()  # keep later subtests independent of this failure
            self.fail("SerialTransport.open() succeeded despite an existing peer")
        cli = make_cli(lambda: SerialTransport(self.path), tx_timeout=1, sync_timeout=1)
        r = cli.transmit(ACTIONS["Gate"])
        self.assertEqual((r.status, r.reason), (FAILED, "serial_port_busy"))
        self.assertEqual(master_received(self.master), b"")  # zero bytes reached the device

    def test_existing_nonexclusive_peer_is_refused(self):
        with self.subTest(peer="same process"):
            peer = os.open(self.path, PEER_FLAGS)
            try:
                self.assert_refused_and_silent()
                # we must not leave TIOCEXCL behind: the peer can still (re)open the port
                os.close(os.open(self.path, PEER_FLAGS))
            finally:
                os.close(peer)

        with self.subTest(peer="child process"):
            child = subprocess.Popen(
                [sys.executable, "-c", _CHILD_PEER, self.path],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            )
            child_pid = child.pid  # the only process this test ever signals
            try:
                self.assertEqual(child.stdout.readline(), b"ready\n")
                self.assert_refused_and_silent()
            finally:
                child.stdin.close()  # child exits on EOF
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.kill(child_pid, signal.SIGKILL)
                    child.wait(timeout=5)
                child.stdout.close()

    def test_open_succeeds_without_peer_and_after_peer_left(self):
        t = SerialTransport(self.path)
        t.open()
        self.assertIsNotNone(t.fd)
        t.close()
        peer = os.open(self.path, PEER_FLAGS)
        os.close(peer)
        t = SerialTransport(self.path)
        t.open()  # a closed peer is not a holder
        t.close()

    def test_proc_unlistable_fails_closed(self):
        real = flipper_mod._list_dir

        def no_proc(path):
            if path == flipper_mod.PROC_ROOT:
                raise PermissionError(13, "simulated: /proc not listable")
            return real(path)

        with mock.patch.object(flipper_mod, "_list_dir", side_effect=no_proc):
            with self.assertRaises(SerialPortBusy) as ctx:
                SerialTransport(self.path).open()
            self.assertEqual(ctx.exception.reason, "busy_check_unavailable")
            cli = make_cli(lambda: SerialTransport(self.path), tx_timeout=1, sync_timeout=1)
            r = cli.transmit(ACTIONS["Garage_door"])
        self.assertEqual((r.status, r.reason), (FAILED, "serial_port_busy"))
        self.assertEqual(master_received(self.master), b"")
        os.close(os.open(self.path, PEER_FLAGS))  # no TIOCEXCL left behind

    def test_own_fd_invisible_fails_closed(self):
        # a /proc that lists nothing (e.g. hidepid, not procfs) proves nothing -> refuse
        with mock.patch.object(flipper_mod, "_list_dir", return_value=[]):
            with self.assertRaises(SerialPortBusy) as ctx:
                SerialTransport(self.path).open()
        self.assertEqual(ctx.exception.reason, "busy_check_unavailable")

    def test_unreadable_or_vanished_pid_is_skipped(self):
        real = flipper_mod._list_dir

        def flaky(path):
            if path == flipper_mod.PROC_ROOT:
                return ["999999999", "999999998", "999999997"] + real(path)
            if path.endswith("/999999999/fd"):
                raise PermissionError(13, "simulated: other user's process")
            if path.endswith("/999999998/fd"):
                raise ProcessLookupError(3, "simulated: exited")
            if path.endswith("/999999997/fd"):
                raise FileNotFoundError(2, "simulated: exited")
            return real(path)

        with mock.patch.object(flipper_mod, "_list_dir", side_effect=flaky):
            t = SerialTransport(self.path)
            t.open()
            t.close()


class GuardTest(unittest.TestCase):
    def test_tests_cannot_open_real_flipper(self):
        with self.assertRaises(AssertionError):
            os.open("/dev/ttyACM0", os.O_RDWR)

    def test_cli_requires_a_validated_allowlist(self):
        for bad in (None, {"Gate": "/ext/subghz/Gate.sub"}, ACTIONS):
            with self.subTest(bad=type(bad).__name__), self.assertRaises(TypeError):
                FlipperCli(lambda: FakeTransport(), bad)


if __name__ == "__main__":
    unittest.main()
