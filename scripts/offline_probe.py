"""Offline end-to-end probe: real HTTP server + real FlipperCli/SerialTransport, talking to
a FAKE Flipper shell on a pseudo-terminal. No real serial port, no RF.

    python3 scripts/offline_probe.py

Uses the test allowlist (tests/fakes.py). The token is generated in memory for this run
and never printed.
"""

import http.client
import json
import os
import pty
import secrets
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests import fakes  # noqa: E402,F401  (guard: real /dev/ttyACM*, /dev/serial/* refused)
from tests.fakes import TEST_ALLOWLIST  # noqa: E402
from tests.test_flipper import PtyFlipper  # noqa: E402

from flipper_subghz_api.flipper import FlipperCli, SerialTransport  # noqa: E402
from flipper_subghz_api.server import App, build_server  # noqa: E402


def main() -> int:
    master, slave = pty.openpty()
    pty_path = os.ttyname(slave)
    os.close(slave)  # any other open fd on the tty would (correctly) make the API refuse
    emu = PtyFlipper(master)
    emu.start()
    token = secrets.token_urlsafe(32)
    cli = FlipperCli(lambda: SerialTransport(pty_path), TEST_ALLOWLIST, tx_timeout=5,
                     sync_timeout=3)
    app = App(cli, TEST_ALLOWLIST, pty_path, min_interval=0)
    srv = build_server(app, token, "127.0.0.1", 0)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    print(f"fake flipper pty: {pty_path}  server: 127.0.0.1:{port}")

    def req(method, path, auth):
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        h = {"Authorization": auth} if auth else {}
        c.request(method, path, headers=h)
        r = c.getresponse()
        body = r.read().decode()
        c.close()
        return r.status, body

    good = "Bearer " + token
    steps = [
        ("POST", "/api/send/Gate", None, "no token"),
        ("POST", "/api/send/Gate", "Bearer not-the-token", "wrong token"),
        ("POST", "/api/send/Gate2", good, "unknown action"),
        ("GET", "/health", good, "health"),
    ] + [("POST", f"/api/send/{n}", good, "authorized") for n in TEST_ALLOWLIST.names] + [
        ("POST", "/api/toggle/Gate", good, "authorized"),  # deprecated alias
    ]
    ok = True
    expect = {"no token": 401, "wrong token": 401, "unknown action": 404, "health": 200,
              "authorized": 200}
    for method, path, auth, label in steps:
        code, body = req(method, path, auth)
        if label != "health":
            print(f"{label:15} {method} {path:24} -> {code} {body}")
        else:
            print(f"{label:15} {method} {path:24} -> {code} device_present="
                  f"{json.loads(body)['device_present']}")
        ok &= code == expect[label]
    print("fake flipper received command lines:", emu.lines)
    want = [f"subghz tx_from_file {s.path} {s.repeat} 0" for s in TEST_ALLOWLIST.specs.values()]
    want.append("subghz tx_from_file /ext/subghz/Gate.sub 1 0")
    ok &= emu.lines == want
    srv.shutdown()
    srv.server_close()
    emu.stop.set()
    os.close(master)
    print("PROBE", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
