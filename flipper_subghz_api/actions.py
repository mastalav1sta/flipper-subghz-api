"""The action allowlist: action name -> Sub-GHz file already saved on the Flipper, the
protocol the firmware must report for it, and the ``<repeat>`` argument we pass.

The allowlist is loaded ONCE at startup from a JSON or TOML file (``FLIPPER_ACTIONS_FILE``
or ``--actions-file``), validated strictly, and frozen. Any validation error refuses
startup. There is no dynamic path input anywhere in this service: an HTTP request can
only name one of these actions, and the CLI adapter re-checks the path before sending.

File format (JSON)::

    {"actions": [
      {"name": "Gate", "path": "/ext/subghz/Gate.sub", "protocol": "RAW", "repeat": 1},
      {"name": "Light_on", "path": "/ext/subghz/Light_on.sub",
       "protocol": "Princeton", "repeat": 10}
    ]}

or the TOML equivalent (``[[actions]]`` tables), chosen by the ``.toml`` suffix.

What ``subghz tx_from_file <path> <repeat> <device>`` does with ``<repeat>``, read from
Momentum firmware tag ``mntm-012`` (commit e1784e7418d8); the stock firmware's
applications/main/subghz/subghz_cli.c has the same logic at the same lines:

* applications/main/subghz/subghz_cli.c:585 -- ``repeat`` defaults to 10 when omitted
  (we always pass it explicitly); :618 parses it.
* RAW files (:714, :795-803): the capture is replayed as a whole, and the outer do/while
  replays it again while ``repeat`` is non-zero -> ``repeat=1`` = one replay of the whole
  capture. ``1`` is the recommended value for RAW captures of toggle remotes.
* Key files such as Princeton (:735-755): ``repeat`` is stored as a ``Repeat`` key in the
  file data (:736) before the protocol encoder deserialises it; the outer loop runs only
  once for non-RAW (:802-803 requires ``Protocol == "RAW"``). The Princeton encoder reads
  ``Repeat`` (lib/subghz/protocols/princeton.c:372-373) and emits the whole frame once per
  repeat (princeton.c:406-421). One 24-bit frame at TE 488 us is ~62 ms, so ``repeat=1``
  puts exactly ONE short frame on air; many receivers need several. The firmware's own
  defaults are 10 (subghz_cli.c:585, :180; princeton.c:100).
* Both paths print ``Listening at <file>. Frequency=<hz>, Protocol=<Protocol>`` (:774)
  once before TX starts; success detection keys on it (see ``flipper.classify``).

Side effect (same source): for key files the firmware opens the .sub read/write
(lib/flipper_format/flipper_format.c:52-54), appends ``Repeat`` to it (subghz_cli.c:736),
the encoder may rewrite ``Key`` and the CLI deletes ``Repeat`` again (subghz_cli.c:754).
So every key-file send writes to the Flipper's SD card; RAW sends do not (they use an
in-memory copy, subghz_cli.c:715-716).
"""

from __future__ import annotations

import json
import os
import re
import stat
import tomllib
from dataclasses import dataclass
from types import MappingProxyType

from .config import ConfigError

# Paths are interpolated into a CLI line, so they must be boring: no spaces, quotes,
# control characters, sub-directories or `..` (tx_from_file does not parse quotes).
_SAFE_PATH = re.compile(r"^/ext/subghz/[A-Za-z0-9_]+\.sub$")
_NAME = re.compile(r"^[A-Za-z0-9_]{1,32}$")
_PROTOCOL = re.compile(r"^[A-Za-z0-9_]{1,32}$")
REPEAT_MIN, REPEAT_MAX = 1, 50
MAX_ACTIONS = 64
MAX_FILE_BYTES = 64 * 1024
_KEYS = frozenset({"name", "path", "protocol", "repeat"})


class ActionsError(ConfigError):
    """Message is safe to print."""


def is_safe_path(path) -> bool:
    return isinstance(path, str) and _SAFE_PATH.fullmatch(path) is not None


@dataclass(frozen=True)
class ActionSpec:
    name: str
    path: str
    protocol: str  # exact value the firmware prints after ``Protocol=``
    repeat: int  # ``<repeat>`` argument of ``subghz tx_from_file``


class Allowlist:
    """Immutable, validated set of actions. Build it with ``parse_actions``/``load_actions``."""

    __slots__ = ("specs", "by_path", "paths")

    def __init__(self, specs):
        object.__setattr__(self, "specs", MappingProxyType({s.name: s for s in specs}))
        object.__setattr__(self, "by_path", MappingProxyType({s.path: s for s in specs}))
        object.__setattr__(self, "paths", frozenset(self.by_path))

    def __setattr__(self, key, value):
        raise AttributeError("Allowlist is immutable")

    def __delattr__(self, key):
        raise AttributeError("Allowlist is immutable")

    @property
    def names(self) -> tuple:
        return tuple(self.specs)

    def __len__(self):
        return len(self.specs)


def _entry(i: int, raw) -> ActionSpec:
    where = f"actions[{i}]"
    if not isinstance(raw, dict):
        raise ActionsError(f"{where}: must be an object/table")
    missing = _KEYS - raw.keys()
    if missing:
        raise ActionsError(f"{where}: missing {', '.join(sorted(missing))}")
    extra = raw.keys() - _KEYS
    if extra:
        raise ActionsError(f"{where}: unknown key(s) {', '.join(sorted(map(str, extra)))}")
    name, path, protocol, repeat = raw["name"], raw["path"], raw["protocol"], raw["repeat"]
    if not isinstance(name, str) or not _NAME.fullmatch(name):
        raise ActionsError(f"{where}: name must match {_NAME.pattern}")
    where = f"action {name!r}"
    if not is_safe_path(path):
        raise ActionsError(f"{where}: path must match {_SAFE_PATH.pattern}")
    if not isinstance(protocol, str) or not _PROTOCOL.fullmatch(protocol):
        raise ActionsError(f"{where}: protocol must match {_PROTOCOL.pattern}")
    if type(repeat) is not int or not REPEAT_MIN <= repeat <= REPEAT_MAX:
        raise ActionsError(f"{where}: repeat must be an integer {REPEAT_MIN}..{REPEAT_MAX}")
    return ActionSpec(name, path, protocol, repeat)


def parse_actions(data) -> Allowlist:
    """Validate already-decoded JSON/TOML data. Raises ``ActionsError`` on any problem."""
    if not isinstance(data, dict) or set(data) != {"actions"}:
        raise ActionsError('top level must contain exactly one key: "actions"')
    entries = data["actions"]
    if not isinstance(entries, list):
        raise ActionsError('"actions" must be a list')
    if not 1 <= len(entries) <= MAX_ACTIONS:
        raise ActionsError(f"between 1 and {MAX_ACTIONS} actions required")
    specs = [_entry(i, raw) for i, raw in enumerate(entries)]
    # Case-insensitive: the Flipper's SD card (FAT) treats A.sub and a.sub as one file,
    # and two routes differing only in case would be a footgun.
    seen_names, seen_paths = set(), set()
    for s in specs:
        if s.name.lower() in seen_names:
            raise ActionsError(f"duplicate action name {s.name!r}")
        if s.path.lower() in seen_paths:
            raise ActionsError(f"duplicate path for action {s.name!r}")
        seen_names.add(s.name.lower())
        seen_paths.add(s.path.lower())
    return Allowlist(specs)


def _no_duplicate_keys(pairs):
    obj = {}
    for key, value in pairs:
        if key in obj:  # json would silently keep the last one
            raise ValueError(f"duplicate key {key!r}")
        obj[key] = value
    return obj


def load_actions(path: str) -> Allowlist:
    """Read and validate the actions file (``.toml`` -> TOML, anything else -> JSON)."""
    path = os.path.expanduser(path)
    try:
        st = os.stat(path)
    except FileNotFoundError:
        raise ActionsError(f"actions file not found: {path}") from None
    if not stat.S_ISREG(st.st_mode):
        raise ActionsError(f"actions file is not a regular file: {path}")
    if st.st_mode & 0o022:
        raise ActionsError(f"actions file must not be group/other writable (chmod 644): {path}")
    if st.st_size > MAX_FILE_BYTES:
        raise ActionsError(f"actions file larger than {MAX_FILE_BYTES} bytes: {path}")
    with open(path, "rb") as fh:
        raw = fh.read(MAX_FILE_BYTES + 1)
    try:
        if path.endswith(".toml"):
            data = tomllib.loads(raw.decode("utf-8"))
        else:
            data = json.loads(raw.decode("utf-8"), object_pairs_hook=_no_duplicate_keys)
    except (UnicodeDecodeError, ValueError) as exc:  # JSONDecodeError/TOMLDecodeError
        raise ActionsError(f"cannot parse actions file {path}: {exc}") from None
    return parse_actions(data)
