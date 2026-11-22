# flipper-subghz-api

Control any device that has a **fixed-code Sub-GHz remote** from Home Assistant, locally,
using a Flipper Zero as the transmitter: gates, garage doors, lights, sockets, blinds,
fans, and so on. You record the remote once with the Flipper, then this small HTTP API
replays that saved `.sub` file when Home Assistant asks for it.

**Fixed codes only.** Rolling-code remotes (most modern garage door openers and car keys)
cannot be controlled by replaying a recording, and this project will not add anything
for them. It only ever runs `subghz tx_from_file` on files you already saved; there is
no IR, NFC or any other Flipper feature here.

## Why

My gates are from Brix, and Brix doesn't offer a smart-home integration for them, so the
original remote was the only way in. I didn't want to add a proprietary smart-home
controller just to press a button, so I used a Flipper Zero I already had and Home
Assistant: local, open, no cloud.

## How it works

```
Home Assistant ──HTTP (LAN, Bearer token)──▶ flipper-subghz-api ──USB serial──▶ Flipper Zero
  rest_command      POST /api/send/Gate       looks up "Gate" in       CLI: subghz tx_from_file
  + script                                    the allowlist             /ext/subghz/Gate.sub 1 0
                                                                               │
                                                                               ▼
                                                                    RF (e.g. 433.92 / 868.35 MHz)
```

* The request names an **action**, never a file. The action is looked up in an allowlist
  file you write once (name → `.sub` path on the Flipper's SD card, expected protocol,
  repeat count). Anything not in the allowlist is a 404.
* Per request the API opens the serial port exclusively, presses Ctrl+C for a clean
  prompt, types one `subghz tx_from_file <path> <repeat> 0` line, waits for the prompt and
  closes the port.
* `sent` is only reported if the firmware printed exactly one
  `Listening at <file>. Frequency=…, Protocol=<expected>` line with the protocol the
  allowlist expects. If the outcome is unclear, it reports `unknown` (it may have
  transmitted) and **never retries**.
* The API knows nothing about the device it controls (open/closed, on/off). It only
  reports whether a transmission request went out.

## Hardware

* A Flipper Zero, with the remote's signal saved under `/ext/subghz/` (Sub-GHz app →
  Read or Read RAW → Save).
* Any Linux machine with USB and Python ≥ 3.11 (stdlib only, nothing to `pip install`).
* Tested on a Raspberry Pi Zero 2 W with **Momentum firmware `mntm-012`**. The stock
  firmware's source (`applications/main/subghz/subghz_cli.c`) has the same
  `subghz tx_from_file` command and prints the same `Listening at …` line, but this
  project has only been tested on Momentum.

### Tested with

| Device | Signal | Allowlist entry | Status |
|---|---|---|---|
| Belfox 868 MHz remotes (two gate drives + a garage door), gates by Brix | 868.35 MHz, OOK, RAW capture | `protocol: RAW`, `repeat: 1` | confirmed live on one gate channel; the other two are configured the same way but not yet live-tested |
| 433.92 MHz light remote | Princeton 24-bit key file | `protocol: Princeton`, `repeat: 10` (a single frame was not enough for the receiver to latch) | confirmed live |

Flipper Zero on Momentum `mntm-012`, Raspberry Pi Zero 2 W, Home Assistant.

## Quick start

```sh
git clone <this repo> ~/flipper-subghz-api && cd ~/flipper-subghz-api
python3 -m unittest discover -s tests -t .          # fakes only, no hardware

install -d -m 700 ~/.config/flipper-subghz-api
install -m 600 config.env.example  ~/.config/flipper-subghz-api/config.env
install -m 644 actions.example.json ~/.config/flipper-subghz-api/actions.json
# edit config.env: token, FLIPPER_API_BIND (this host's LAN IP), serial port
# edit actions.json: your action names and the .sub files saved on your Flipper

python3 -m flipper_subghz_api --check-config        # prints "config ok: ..." or exits 2
python3 -m flipper_subghz_api                       # or install the systemd unit

curl -X POST -H "Authorization: Bearer $TOKEN" http://192.0.2.10:8787/api/send/Light_on
```

`192.0.2.10` is a documentation placeholder (RFC 5737); use your host's LAN IP. Run it as
a service with [`deploy/flipper-subghz-api.service.example`](deploy/flipper-subghz-api.service.example);
details in [`docs/DEPLOY.md`](docs/DEPLOY.md).

## Configuration

**`config.env`** (`chmod 600`, parsed by the app, so the token never lands in the process
environment): `FLIPPER_API_TOKEN` (required, ≥ 32 chars), `FLIPPER_API_BIND` (required, a
literal LAN or loopback IP), and optionally `FLIPPER_API_PORT` (8787),
`FLIPPER_SERIAL_PORT` (`/dev/ttyACM0`; prefer
`/dev/serial/by-id/usb-Flipper_Devices_Inc._Flipper_<name>_<name>-if00`),
`FLIPPER_TX_TIMEOUT` (30 s), `FLIPPER_MIN_INTERVAL` (3 s) and `FLIPPER_ACTIONS_FILE`.

**Actions file** (JSON, or TOML if the name ends in `.toml`). Location, first match wins:
`--actions-file`, `$FLIPPER_ACTIONS_FILE`, `FLIPPER_ACTIONS_FILE` in `config.env`,
`~/.config/flipper-subghz-api/actions.json`.

```json
{"actions": [
  {"name": "Gate",      "path": "/ext/subghz/Gate.sub",      "protocol": "RAW",       "repeat": 1},
  {"name": "Light_on",  "path": "/ext/subghz/Light_on.sub",  "protocol": "Princeton", "repeat": 10},
  {"name": "Light_off", "path": "/ext/subghz/Light_off.sub", "protocol": "Princeton", "repeat": 10}
]}
```

The service refuses to start if anything in it is off:

* `name`: `^[A-Za-z0-9_]{1,32}$`, unique (case-insensitive). It becomes `/api/send/<name>`.
* `path`: `^/ext/subghz/[A-Za-z0-9_]+\.sub$`, so no sub-directories, `..`, spaces, quotes
  or control characters. Unique (case-insensitive, the SD card is FAT). Rename files on
  the Flipper if needed.
* `protocol`: `^[A-Za-z0-9_]{1,32}$`, exactly what the firmware prints after `Protocol=`
  (it's the `Protocol:` line in the `.sub` file, e.g. `RAW`, `Princeton`).
* `repeat`: an integer 1–50. For RAW captures it is the number of times the whole capture
  is replayed (use 1). For key files it is the number of frames in one transmission (10
  is the firmware default).
* 1–64 entries, no unknown keys, no duplicate JSON keys; the file must not be group- or
  world-writable.

Note: for key files (not RAW) the firmware writes to the `.sub` on the SD card during
every send (it adds and removes a `Repeat:` line).

## API

Every endpoint requires `Authorization: Bearer <token>`; anything else is 401.

| Method | Path | |
|---|---|---|
| GET | `/health` | `device_present`, `device_rw`, `busy`, action names, last outcome per action (no serial I/O) |
| POST | `/api/send/<action>` | send the allowlisted file for `<action>` |
| POST | `/api/toggle/<action>` | **deprecated** alias of `/api/send/<action>`; responds with `Deprecation: true` |

| HTTP | `status` | Meaning |
|---|---|---|
| 200 | `sent` | the firmware accepted the file and finished the transmission |
| 404 | – | unknown action |
| 409 | `failed` | `busy` (another send is running) or `serial_port_busy`: nothing sent |
| 429 | `failed` | `rate_limited`: nothing sent |
| 502 | `failed` | the firmware rejected it before transmitting (file missing, region, …) |
| 503 | `failed` | Flipper not connected |
| 504 | `unknown` | timeout, serial error after the command was typed, or an unexpected protocol: it **may** have transmitted. Never retried. |

## Security model

* **LAN only.** `FLIPPER_API_BIND` has no default and must be a literal private or
  loopback IP; `0.0.0.0`, `::`, hostnames and public addresses are refused at startup.
* **Token required** on every endpoint, compared in constant time. No token, a short
  token or a group/other-readable `config.env` refuses startup.
* **No dynamic paths.** A request can only name an action. The allowlist is validated
  at startup and frozen; the serial adapter re-checks the path before every send. Request
  bodies and query strings are ignored or rejected.
* **Rate limit and single sender.** One send at a time (a concurrent request gets 409,
  it is not queued) and, by default, nothing within 3 s of the previous send, across all
  actions.
* **Single serial user.** The port is opened with `flock` + `TIOCEXCL`, and the API scans
  `/proc` for any other process holding it; if one does (qFlipper, `screen`, …) the
  request is refused with 409 before a byte is written. Processes of other users or root
  are not visible to that scan, so make sure nothing else uses the port.
* **ModemManager** probes new `ttyACM` devices and can interfere. Remove it, or tell it to
  ignore the Flipper (check the IDs with `lsusb`), e.g. in
  `/etc/udev/rules.d/99-flipper-mm-ignore.rules`:
  `ATTRS{idVendor}=="0483", ATTRS{idProduct}=="5740", ENV{ID_MM_DEVICE_IGNORE}="1"`

## Home Assistant

Two packages in [`examples/homeassistant/`](examples/homeassistant/):

* [`flipper_buttons_package.yaml`](examples/homeassistant/flipper_buttons_package.yaml):
  `rest_command` + script + template button for toggle remotes (gate, garage door), plus
  an availability sensor. On anything but HTTP 200 it **notifies and never retries**: a
  retry could undo a press that did go out.
* [`flipper_light_package.yaml`](examples/homeassistant/flipper_light_package.yaml): a
  light with separate on/off codes. Its state is an `input_boolean` that only changes
  after a confirmed 200, with one retry after 3 s. Don't use `optimistic: true`: when the
  API refuses a send (e.g. 429), an optimistic light shows the wrong state.

Both use `!secret flipper_subghz_api_auth` (`"Bearer <token>"`) for the header.

## Legal and safety

* **Only replay your own remotes and devices.** Don't transmit to anything you don't own
  or aren't allowed to operate.
* **Fixed codes only.** This project does not and will not support rolling-code attacks,
  code grabbing, jamming or brute forcing.
* **Follow your local radio regulations.** The Flipper firmware's region settings apply;
  unlocking extended frequencies or other region limits is your responsibility.
* **No warranty.** This is provided as is (see [LICENSE](LICENSE)). A gate or garage door
  that moves when you don't expect it can hurt people: keep the manufacturer's safety
  features in place and don't automate what you can't see.
* **Never expose this API to the internet.** No port forwarding, no reverse proxy. Reach
  it through Home Assistant, or a VPN into your network.

## Development

```sh
python3 -m unittest discover -s tests -t . -v
python3 scripts/offline_probe.py     # real HTTP + serial code against a fake Flipper on a pty
```

Tests use fakes only; `tests/fakes.py` makes any attempt to open `/dev/ttyACM*`,
`/dev/ttyUSB*` or `/dev/serial/*` fail. See [CONTRIBUTING.md](CONTRIBUTING.md) and
[SECURITY.md](SECURITY.md).

## License

MIT, see [LICENSE](LICENSE).
