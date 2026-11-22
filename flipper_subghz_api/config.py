"""Runtime configuration from a private env file (never committed).

The file is parsed by this module (not by systemd's EnvironmentFile=) so the token does
not end up in the process environment. Startup is refused when the file is missing,
readable by group/other, lacks a strong token, or asks to bind a wildcard/public address.
"""

from __future__ import annotations

import ipaddress
import os
import re
import stat
from dataclasses import dataclass, field

DEFAULT_ENV_FILE = "~/.config/flipper-subghz-api/config.env"
DEFAULT_ACTIONS_FILE = "~/.config/flipper-subghz-api/actions.json"
DEFAULT_SERIAL_PORT = "/dev/ttyACM0"
DEFAULT_PORT = 8787
MIN_TOKEN_LEN = 32
ACTIONS_FILE_ENV = "FLIPPER_ACTIONS_FILE"

_KEY = re.compile(r"^[A-Z_][A-Z0-9_]*$")
_TOKEN = re.compile(r"^[A-Za-z0-9._~+/=-]+$")
_SERIAL = re.compile(r"^/dev/(serial/by-id/[A-Za-z0-9._:-]+|ttyACM[0-9]+)$")


class ConfigError(Exception):
    """Message is safe to print: it never contains the token."""


@dataclass(frozen=True)
class Config:
    token: str = field(repr=False)
    bind: str
    port: int = DEFAULT_PORT
    serial_port: str = DEFAULT_SERIAL_PORT
    tx_timeout: float = 30.0
    min_interval: float = 3.0
    actions_file: str = DEFAULT_ACTIONS_FILE


def parse_env_file(path: str) -> dict:
    path = os.path.expanduser(path)
    try:
        st = os.stat(path)
    except FileNotFoundError:
        raise ConfigError(f"env file not found: {path}") from None
    if not stat.S_ISREG(st.st_mode):
        raise ConfigError(f"env file is not a regular file: {path}")
    if st.st_mode & 0o077:
        raise ConfigError(f"env file must not be group/other accessible (chmod 600): {path}")
    values = {}
    with open(path, encoding="utf-8") as fh:
        for lineno, raw in enumerate(fh, 1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            key, sep, value = line.partition("=")
            key, value = key.strip(), value.strip()
            if not sep or not _KEY.fullmatch(key):
                raise ConfigError(f"{path}:{lineno}: expected KEY=VALUE")
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
                value = value[1:-1]
            values[key] = value
    return values


def _validate_bind(bind: str) -> str:
    if not bind:
        raise ConfigError("FLIPPER_API_BIND is required (this host's LAN IP); there is no default")
    try:
        addr = ipaddress.ip_address(bind)
    except ValueError:
        raise ConfigError("FLIPPER_API_BIND must be a literal IP address") from None
    if addr.is_unspecified:
        raise ConfigError("refusing wildcard bind (0.0.0.0 / ::); use this host's LAN IP")
    if not (addr.is_loopback or (addr.is_private and not addr.is_global)):
        raise ConfigError("refusing non-private bind address; LAN or loopback only")
    return bind


def _number(values, key, default, cast, lo, hi):
    raw = values.get(key)
    if raw in (None, ""):
        return default
    try:
        val = cast(raw)
    except ValueError:
        raise ConfigError(f"{key} must be a number") from None
    if not (lo <= val <= hi):
        raise ConfigError(f"{key} out of range [{lo}, {hi}]")
    return val


def load_config(path: str = DEFAULT_ENV_FILE, actions_file: str | None = None,
                environ=None) -> Config:
    """``actions_file`` precedence: argument (CLI flag) > $FLIPPER_ACTIONS_FILE >
    FLIPPER_ACTIONS_FILE in the env file > ``DEFAULT_ACTIONS_FILE``."""
    environ = os.environ if environ is None else environ
    values = parse_env_file(path)
    token = values.get("FLIPPER_API_TOKEN", "")
    if not token:
        raise ConfigError("FLIPPER_API_TOKEN missing or empty; refusing to start")
    if len(token) < MIN_TOKEN_LEN or not _TOKEN.fullmatch(token):
        raise ConfigError(
            f"FLIPPER_API_TOKEN must be >= {MIN_TOKEN_LEN} chars of [A-Za-z0-9._~+/=-]"
        )
    serial_port = values.get("FLIPPER_SERIAL_PORT") or DEFAULT_SERIAL_PORT
    if not _SERIAL.fullmatch(serial_port):
        raise ConfigError("FLIPPER_SERIAL_PORT must be /dev/serial/by-id/... or /dev/ttyACMn")
    actions = (actions_file or environ.get(ACTIONS_FILE_ENV)
               or values.get(ACTIONS_FILE_ENV) or DEFAULT_ACTIONS_FILE)
    return Config(
        token=token,
        bind=_validate_bind(values.get("FLIPPER_API_BIND", "")),
        port=_number(values, "FLIPPER_API_PORT", DEFAULT_PORT, int, 1024, 65535),
        serial_port=serial_port,
        tx_timeout=_number(values, "FLIPPER_TX_TIMEOUT", 30.0, float, 5.0, 120.0),
        min_interval=_number(values, "FLIPPER_MIN_INTERVAL", 3.0, float, 0.0, 60.0),
        actions_file=actions,
    )
