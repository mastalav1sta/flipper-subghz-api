"""Serial adapter for the Flipper Zero USB CLI (stdlib only: os/termios/fcntl/select).

Grounding (read from source at the tested firmware, Momentum `mntm-012`, commit
e1784e74, file applications/main/subghz/subghz_cli.c):

* ``subghz tx_from_file <path> <repeat> <device>`` is a headless CLI command. For
  ``Protocol: RAW`` files it builds the RAW transmitter from the file and replays it
  ``repeat`` times on device ``0`` (internal CC1101). For key files (e.g. ``Protocol:
  Princeton``) ``repeat`` is the number of encoded frames sent in one TX; the per-action
  value comes from the validated allowlist (evidence in ``actions``).
* All validation errors print ``subghz tx_from_file: ... Error ...`` (or usage) and return
  BEFORE any transmission.
* On a valid file it prints ``Listening at <file>. Frequency=..., Protocol=<Protocol>``
  and then starts async TX. Only that exact line with the action's expected protocol
  counts as sent (see ``classify``). If TX start is refused it prints ``Transmission on
  this frequency is restricted ...`` (no RF). When TX finishes the shell prints a fresh
  ``>: `` prompt.
* Ctrl+C (ETX, 0x03) on an idle line clears the line editor *and* the history cursor and
  re-prints the prompt; during TX it aborts the transmission. We use it to get a clean
  line before typing, and to stop a transmission we lost track of. It never starts one.

Outcome policy: a request is reported as ``sent``, ``failed`` (certain that nothing was
transmitted) or ``unknown`` (the command may have run). There is NO retry anywhere.
"""

from __future__ import annotations

import errno
import fcntl
import logging
import os
import re
import select
import stat
import termios
import time
from dataclasses import dataclass
from typing import Callable, Optional

from .actions import Allowlist, is_safe_path

SENT = "sent"
FAILED = "failed"
UNKNOWN = "unknown"

PROMPT = b">: "
ETX = b"\x03"
_ANSI = re.compile(rb"\x1b\[[0-9;?]*[A-Za-z]")

log = logging.getLogger("flipper_subghz_api")


@dataclass(frozen=True)
class TxResult:
    status: str  # sent | failed | unknown
    reason: str  # short machine-readable reason, never raw device output


class DeviceNotFound(Exception):
    pass


class SerialPortBusy(Exception):
    """The port is (or may be) in use by someone else. ``reason`` is for logs only; the API
    always reports ``serial_port_busy``."""

    def __init__(self, path: str, reason: str = "exclusive_or_locked"):
        super().__init__(path, reason)
        self.path = path
        self.reason = reason


PROC_ROOT = "/proc"


def _list_dir(path: str):
    return os.listdir(path)  # indirection so tests can simulate an unreadable /proc


def find_other_holders(rdev: int, own_pid: int, own_fd: int) -> list:
    """Return ``[(pid, fd), ...]`` of open fds on the char device ``rdev``, except our own
    ``(own_pid, own_fd)``. Other fds of our own process DO count.

    Caveat: only processes whose ``/proc/<pid>/fd`` we may read are visible, i.e. our own
    user's (or all, if we run as root). A process of another non-root user, or root, holding
    the port is invisible here, and TIOCEXCL does not stop root either. Deployment must make
    sure nothing else (qFlipper, ModemManager, HA serial integrations) uses the port.

    Raises ``SerialPortBusy(reason="busy_check_unavailable")`` if ``/proc`` cannot be
    listed or our own fd is not visible in it (the scan would prove nothing): fail closed.
    """
    try:
        pids = [p for p in _list_dir(PROC_ROOT) if p.isdigit()]
    except OSError as exc:
        raise SerialPortBusy("", "busy_check_unavailable") from exc
    holders = []
    seen_own = False
    for pid in pids:
        fd_dir = os.path.join(PROC_ROOT, pid, "fd")
        try:
            fds = _list_dir(fd_dir)
        except (PermissionError, FileNotFoundError, ProcessLookupError, NotADirectoryError):
            continue  # other user's process, or it exited meanwhile
        for fd in fds:
            try:
                st = os.stat(os.path.join(fd_dir, fd))
            except (PermissionError, FileNotFoundError, ProcessLookupError, NotADirectoryError):
                continue
            if not (stat.S_ISCHR(st.st_mode) and st.st_rdev == rdev):
                continue
            if int(pid) == own_pid and fd == str(own_fd):
                seen_own = True
                continue
            holders.append((int(pid), int(fd)))
    if not seen_own:
        raise SerialPortBusy("", "busy_check_unavailable")
    return holders


class SerialTransport:
    """Exclusive raw access to one tty. Opened per request, closed afterwards."""

    def __init__(self, path: str):
        self.path = path
        self.fd: Optional[int] = None

    def open(self) -> None:
        if not os.path.exists(self.path):
            raise DeviceNotFound(self.path)
        try:
            fd = os.open(self.path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        except OSError as exc:
            if exc.errno == errno.EBUSY:  # another process holds it with TIOCEXCL
                raise SerialPortBusy(self.path) from exc
            raise
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                if exc.errno in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES):
                    raise SerialPortBusy(self.path) from exc
                raise
            # Refuse further opens by other non-root processes while we hold it.
            fcntl.ioctl(fd, termios.TIOCEXCL)
            # flock/TIOCEXCL do not see a client that opened the port BEFORE us (qFlipper,
            # screen, ...). Now that no new non-root opener can race in, scan /proc for any
            # other fd on this device; refuse before a single byte is written. Root and other
            # users' processes stay invisible (see find_other_holders).
            st = os.fstat(fd)
            if not stat.S_ISCHR(st.st_mode):
                raise SerialPortBusy(self.path, "not_a_char_device")
            holders = find_other_holders(st.st_rdev, os.getpid(), fd)
            if holders:
                log.warning("serial port held by other process(es): pids=%s",
                            sorted({pid for pid, _ in holders}))
                raise SerialPortBusy(self.path, "held_by_other_process")
            attrs = termios.tcgetattr(fd)
            attrs[0] = 0  # iflag
            attrs[1] = 0  # oflag
            attrs[2] = termios.CS8 | termios.CREAD | termios.CLOCAL  # cflag
            attrs[3] = 0  # lflag: raw, no echo, no signals
            attrs[6][termios.VMIN] = 0
            attrs[6][termios.VTIME] = 0
            termios.tcsetattr(fd, termios.TCSANOW, attrs)
            flags = fcntl.fcntl(fd, fcntl.F_GETFL)
            fcntl.fcntl(fd, fcntl.F_SETFL, flags & ~os.O_NONBLOCK)
        except BaseException:
            try:
                # Don't leave TIOCEXCL behind on a tty someone else still holds (it would
                # lock out e.g. that client's reconnect).
                fcntl.ioctl(fd, termios.TIOCNXCL)
            except OSError:
                pass
            os.close(fd)
            raise
        self.fd = fd

    def write(self, data: bytes) -> None:
        assert self.fd is not None
        view = memoryview(data)
        while view:
            n = os.write(self.fd, view)
            view = view[n:]

    def read(self, timeout: float) -> bytes:
        assert self.fd is not None
        ready, _, _ = select.select([self.fd], [], [], max(0.0, timeout))
        if not ready:
            return b""
        chunk = os.read(self.fd, 4096)
        if chunk == b"":
            raise OSError(errno.EIO, "serial device closed")
        return chunk

    def flush_input(self) -> None:
        assert self.fd is not None
        termios.tcflush(self.fd, termios.TCIFLUSH)

    def close(self) -> None:
        if self.fd is not None:
            try:
                try:
                    # TIOCEXCL outlives our fd if anything else keeps the tty open;
                    # release it so the next request can open the port again.
                    fcntl.ioctl(self.fd, termios.TIOCNXCL)
                except OSError:
                    pass
                os.close(self.fd)
            finally:
                self.fd = None


def _clean(buf: bytes) -> str:
    return _ANSI.sub(b"", buf).decode("utf-8", "replace")


# The line subghz_cli.c:774 (mntm-012) prints once a file is accepted, after ANSI codes are
# stripped: "Listening at <file>. Frequency=<hz>, Protocol=<name>\r\n". The shell prints "\r\n"
# right before running a command (lib/toolbox/cli/shell/cli_shell_line.c:195), so the line
# starts at column 0. Anchored to a whole line so a "Protocol=..." fragment inside some
# other line (e.g. an error) cannot match, and Protocol=RAWfoo is not Protocol=RAW.
_LISTENING = re.compile(
    r"^Listening at (?P<path>\S+)\. Frequency=(?P<freq>\d+), Protocol=(?P<proto>\S+)\r?$",
    re.MULTILINE,
)


def classify(output_after_echo: str, expected_protocol: str) -> TxResult:
    """Map firmware output (after our echoed command line) to an outcome.

    ``sent`` needs exactly one ``Listening at`` line whose protocol equals
    ``expected_protocol`` exactly. A ``Listening at`` line with any other protocol means
    the firmware DID start a transmission of something we did not expect, so it is
    ``unknown`` (may have transmitted), never ``sent`` and never ``failed``.
    """
    text = output_after_echo
    if "restricted in your" in text:
        return TxResult(FAILED, "tx_refused_by_firmware_region_or_settings")
    listening = _LISTENING.findall(text)
    if len(listening) == 1 and listening[0][2] == expected_protocol:
        return TxResult(SENT, "tx_from_file_completed")
    if listening or "Listening at" in text:
        return TxResult(UNKNOWN, "unexpected_protocol_or_listening_line")
    if "could not find command" in text:
        return TxResult(FAILED, "subghz_cli_command_unavailable")
    if "subghz tx_from_file:" in text:
        if "Error open file" in text:
            return TxResult(FAILED, "capture_file_not_found_on_flipper")
        if "Error device not found" in text:
            return TxResult(FAILED, "radio_device_not_found")
        return TxResult(FAILED, "tx_from_file_rejected_before_transmit")
    return TxResult(UNKNOWN, "unrecognized_cli_output")


class FlipperCli:
    def __init__(
        self,
        transport_factory: Callable[[], SerialTransport],
        allowlist: Allowlist,
        tx_timeout: float = 30.0,
        sync_timeout: float = 5.0,
        quiet_period: float = 0.3,
        clock: Callable[[], float] = time.monotonic,
    ):
        if not isinstance(allowlist, Allowlist):
            raise TypeError("allowlist must be a validated actions.Allowlist")
        self._factory = transport_factory
        self._allowlist = allowlist
        self.tx_timeout = tx_timeout
        self.sync_timeout = sync_timeout
        self.quiet_period = quiet_period
        self._clock = clock

    def _sync(self, t) -> bool:
        """Ctrl+C, then wait until output ends with a prompt and goes quiet."""
        t.write(ETX)
        buf = b""
        deadline = self._clock() + self.sync_timeout
        while self._clock() < deadline:
            chunk = t.read(min(self.quiet_period, max(0.0, deadline - self._clock())))
            if chunk:
                buf += chunk
                continue
            if _ANSI.sub(b"", buf).endswith(PROMPT):
                return True
        return False

    def _run(self, t, command: str):
        """Return (text_after_echo, completed). completed=False means no prompt seen."""
        echo = command.encode()
        buf = b""
        deadline = self._clock() + self.tx_timeout
        while self._clock() < deadline:
            chunk = t.read(min(0.5, max(0.0, deadline - self._clock())))
            if not chunk:
                continue
            buf += chunk
            clean = _ANSI.sub(b"", buf)
            idx = clean.find(echo)
            if idx >= 0:
                tail = clean[idx + len(echo):]
                if tail.endswith(b"\r\n" + PROMPT):
                    return tail.decode("utf-8", "replace"), True
        return "", False

    def transmit(self, sub_path: str) -> TxResult:
        # Re-checked before every send, independent of how the allowlist was built.
        if not is_safe_path(sub_path) or sub_path not in self._allowlist.paths:
            raise ValueError("path is not in the fixed allowlist")
        spec = self._allowlist.by_path[sub_path]
        command = f"subghz tx_from_file {spec.path} {spec.repeat} 0"
        try:
            t = self._factory()
            t.open()
        except DeviceNotFound:
            return TxResult(FAILED, "device_not_found")
        except SerialPortBusy as exc:
            log.warning("serial port busy (%s); nothing sent", exc.reason)
            return TxResult(FAILED, "serial_port_busy")
        except OSError:
            return TxResult(FAILED, "serial_open_error")

        command_written = False
        try:
            if not self._sync(t):
                return TxResult(FAILED, "cli_prompt_not_ready")
            t.flush_input()
            command_written = True  # set first: from here on, the command may have run
            t.write(command.encode() + b"\r")
            text, completed = self._run(t, command)
            if not completed:
                try:
                    t.write(ETX)  # stop a possibly running transmission; never re-send
                except OSError:
                    pass
                return TxResult(UNKNOWN, "timeout_waiting_for_prompt")
            return classify(text, spec.protocol)
        except OSError:
            if command_written:
                return TxResult(UNKNOWN, "serial_error_after_command_sent")
            return TxResult(FAILED, "serial_error_before_command_sent")
        finally:
            t.close()
