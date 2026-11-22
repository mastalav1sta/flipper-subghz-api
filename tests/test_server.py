import http.client
import json
import os
import pty
import secrets
import select
import tempfile
import threading
import unittest

from tests import fakes  # noqa: F401  (installs the real-device open guard)
from tests.fakes import (
    ACTIONS, TEST_ACTIONS_DATA, TEST_ALLOWLIST, FakeCli, FakeClock, FakeTransport, listening,
)

from flipper_subghz_api.__main__ import main
from flipper_subghz_api.config import ConfigError, load_config
from flipper_subghz_api.flipper import FlipperCli, SerialTransport, TxResult
from flipper_subghz_api.server import App, build_server


class ServerHarness:
    def __init__(self, cli, min_interval=0.0, serial_port="/nonexistent/flipper"):
        self.token = secrets.token_urlsafe(32)  # runtime-only; never printed or stored
        self.cli = cli
        self.app = App(cli, TEST_ALLOWLIST, serial_port, min_interval=min_interval)
        self.srv = build_server(self.app, self.token, "127.0.0.1", 0)
        self.port = self.srv.server_address[1]
        self.thread = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.thread.start()

    def raw(self, method, path, auth="good", body=None, headers=None):
        h = dict(headers or {})
        if auth == "good":
            h["Authorization"] = "Bearer " + self.token
        elif auth is not None:
            h["Authorization"] = auth
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            c.request(method, path, body=body, headers=h)
            r = c.getresponse()
            return r.status, json.loads(r.read() or b"{}"), dict(r.getheaders())
        finally:
            c.close()

    def request(self, *a, **kw):
        code, body, _ = self.raw(*a, **kw)
        return code, body

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


def cli_for(transport):
    return FlipperCli(lambda: transport, TEST_ALLOWLIST, clock=FakeClock(), quiet_period=0.0)


class HttpTests(unittest.TestCase):
    def setUp(self):
        self.cli = FakeCli()
        self.h = ServerHarness(self.cli)

    def tearDown(self):
        self.h.close()

    def test_send_routes_map_to_their_files(self):
        for name, path in ACTIONS.items():
            with self.subTest(name=name):
                code, body, headers = self.h.raw("POST", f"/api/send/{name}")
                self.assertEqual(code, 200)
                self.assertEqual(body, {"action": name, "status": "sent",
                                        "reason": "tx_from_file_completed"})
                self.assertNotIn("Deprecation", headers)
                self.assertEqual(self.cli.calls[-1], path)
        self.assertEqual(self.cli.calls, list(ACTIONS.values()))

    def test_missing_token_rejected(self):
        code, body = self.h.request("POST", "/api/send/Gate", auth=None)
        self.assertEqual((code, body), (401, {"error": "unauthorized"}))
        self.assertEqual(self.cli.calls, [])

    def test_wrong_token_rejected(self):
        for i, bad in enumerate(("Bearer wrong", "Bearer " + self.h.token + "x", self.h.token,
                                 "Basic " + self.h.token, "bearer " + self.h.token, "Bearer")):
            with self.subTest(case=i):  # index only: never echo even a throwaway token
                for route in ("/api/send/Garage_door", "/api/toggle/Garage_door"):
                    code, _ = self.h.request("POST", route, auth=bad)
                    self.assertEqual(code, 401)
        self.assertEqual(self.cli.calls, [])

    def test_health_requires_auth(self):
        self.assertEqual(self.h.request("GET", "/health", auth=None)[0], 401)
        code, body = self.h.request("GET", "/health")
        self.assertEqual(code, 200)
        self.assertFalse(body["device_present"])
        self.assertEqual(body["actions"], ["Gate", "Garage_door", "Light_on", "Light_off"])
        self.assertEqual(body["service"], "flipper-subghz-api")
        self.assertNotIn("/ext/", json.dumps(body))  # no paths leak via health

    def test_unknown_action_and_path_tricks_rejected(self):
        for path in ("/api/send/Gate2", "/api/send/gate", "/api/send/", "/api/send",
                     "/api/send/Gate/../Garage_door", "/api/send/..%2fGarage_door",
                     "/api/send/Gate?file=/ext/subghz/x.sub", "/api/cli",
                     "/api/send/%2Fext%2Fsubghz%2FGate.sub", "/api/send/light_on",
                     "/api/send/Light", "/api/send/Light_on.sub", "/api/send/Light_on/",
                     "/api/send/Light_toggle", "/api/send/Light_on?repeat=50",
                     "/api/send//ext/subghz/Gate.sub", "/api/toggle/Gate2",
                     "/api/toggle/", "/api/toggle/Gate?x=1", "/api/sendGate",
                     "/API/send/Gate"):
            with self.subTest(path=path):
                code, body = self.h.request("POST", path)
                self.assertEqual((code, body), (404, {"error": "unknown action"}))
        self.assertEqual(self.cli.calls, [])

    def test_deprecated_toggle_alias_still_works(self):
        for name, path in (("Light_on", "/ext/subghz/Light_on.sub"),
                           ("Light_off", "/ext/subghz/Light_off.sub")):
            with self.subTest(name=name):
                code, body, headers = self.h.raw("POST", f"/api/toggle/{name}")
                self.assertEqual((code, body), (200, {"action": name, "status": "sent",
                                                      "reason": "tx_from_file_completed"}))
                self.assertEqual(headers.get("Deprecation"), "true")
                self.assertEqual(self.cli.calls[-1], path)
        self.assertEqual(len(self.cli.calls), 2)

    def test_unauthenticated_unknown_route_is_401_not_404(self):
        self.assertEqual(self.h.request("POST", "/api/whatever", auth=None)[0], 401)

    def test_get_on_send_does_not_transmit(self):
        for route in ("/api/send/Gate", "/api/toggle/Gate"):
            self.assertEqual(self.h.request("GET", route)[0], 405)
            self.assertEqual(self.h.request("PUT", route)[0], 405)
        self.assertEqual(self.cli.calls, [])

    def test_body_is_ignored_and_bounded(self):
        code, _ = self.h.request("POST", "/api/send/Gate",
                                 body=json.dumps({"path": "/ext/subghz/Gate2.sub",
                                                  "repeat": 50}))
        self.assertEqual(code, 200)
        self.assertEqual(self.cli.calls, ["/ext/subghz/Gate.sub"])
        code, _ = self.h.request("POST", "/api/send/Gate", body=b"x" * 5000)
        self.assertEqual(code, 413)
        self.assertEqual(len(self.cli.calls), 1)


class OutcomeTests(unittest.TestCase):
    def run_with(self, result):
        cli = FakeCli(result=result)
        h = ServerHarness(cli)
        try:
            return h.request("POST", "/api/send/Garage_door"), cli
        finally:
            h.close()

    def test_unknown_is_504_and_called_once(self):
        (code, body), cli = self.run_with(TxResult("unknown", "timeout_waiting_for_prompt"))
        self.assertEqual(code, 504)
        self.assertEqual(body["status"], "unknown")
        self.assertIn("not retried", body["note"])
        self.assertEqual(len(cli.calls), 1)

    def test_device_missing_is_503_failed(self):
        (code, body), _ = self.run_with(TxResult("failed", "device_not_found"))
        self.assertEqual((code, body["status"]), (503, "failed"))

    def test_adapter_exception_is_unknown(self):
        class Boom(FakeCli):
            def transmit(self, path):
                super().transmit(path)
                raise RuntimeError("bug")
        cli = Boom()
        h = ServerHarness(cli)
        try:
            code, body = h.request("POST", "/api/send/Gate")
        finally:
            h.close()
        self.assertEqual((code, body["status"]), (504, "unknown"))
        self.assertEqual(len(cli.calls), 1)


class SerializationTests(unittest.TestCase):
    def test_concurrent_send_rejected_not_queued(self):
        gate = threading.Event()
        cli = FakeCli(block=gate)
        h = ServerHarness(cli)
        try:
            first = {}
            t = threading.Thread(
                target=lambda: first.update(r=h.request("POST", "/api/send/Gate")))
            t.start()
            self.assertTrue(cli.started.wait(5))
            code, body = h.request("POST", "/api/send/Garage_door")
            self.assertEqual((code, body["status"], body["reason"]), (409, "failed", "busy"))
            self.assertTrue(h.request("GET", "/health")[1]["busy"])
            gate.set()
            t.join(5)
            self.assertEqual(first["r"][0], 200)
            self.assertEqual(cli.calls, ["/ext/subghz/Gate.sub"])
        finally:
            gate.set()
            h.close()

    def test_min_interval_rate_limit(self):
        cli = FakeCli()
        h = ServerHarness(cli, min_interval=60)
        try:
            self.assertEqual(h.request("POST", "/api/send/Gate")[0], 200)
            code, body = h.request("POST", "/api/send/Gate")
            self.assertEqual((code, body["reason"]), (429, "rate_limited"))
            code, body = h.request("POST", "/api/toggle/Gate")  # the alias shares it
            self.assertEqual((code, body["reason"]), (429, "rate_limited"))
            self.assertEqual(len(cli.calls), 1)
        finally:
            h.close()

    def test_rate_limit_is_global_across_actions(self):
        """Documented behavior: Light_off right after Light_on is refused (429)."""
        cli = FakeCli()
        h = ServerHarness(cli, min_interval=60)
        try:
            self.assertEqual(h.request("POST", "/api/send/Light_on")[0], 200)
            code, body = h.request("POST", "/api/send/Light_off")
            self.assertEqual((code, body["status"], body["reason"]),
                             (429, "failed", "rate_limited"))
            self.assertEqual(cli.calls, ["/ext/subghz/Light_on.sub"])
        finally:
            h.close()


class KeyFileEndToEndTests(unittest.TestCase):
    """HTTP -> real FlipperCli -> FakeTransport (scripted CLI, never a device)."""

    def run_with(self, action, output):
        wire = {"Light_on": "subghz tx_from_file /ext/subghz/Light_on.sub 10 0",
                "Garage_door": "subghz tx_from_file /ext/subghz/Garage_door.sub 1 0"}[action]
        t = FakeTransport({wire: output})
        h = ServerHarness(cli_for(t))
        try:
            return h.request("POST", f"/api/send/{action}"), t, wire
        finally:
            h.close()

    def test_princeton_output_is_200_sent(self):
        (code, body), t, wire = self.run_with(
            "Light_on", listening("/ext/subghz/Light_on.sub", "Princeton", 433920000))
        self.assertEqual((code, body["status"]), (200, "sent"))
        self.assertEqual(t.commands(), [wire.encode() + b"\r"])

    def test_raw_output_for_key_file_is_504_not_sent(self):
        (code, body), t, _ = self.run_with(
            "Light_on", listening("/ext/subghz/Light_on.sub", "RAW"))
        self.assertEqual((code, body["status"], body["reason"]),
                         (504, "unknown", "unexpected_protocol_or_listening_line"))
        self.assertEqual(len(t.commands()), 1)

    def test_princeton_output_for_raw_action_is_504_not_sent(self):
        (code, body), t, _ = self.run_with(
            "Garage_door", listening("/ext/subghz/Garage_door.sub", "Princeton", 433920000))
        self.assertEqual((code, body["status"]), (504, "unknown"))
        self.assertEqual(len(t.commands()), 1)


class ExistingPeerHttpTest(unittest.TestCase):
    """Real FlipperCli/SerialTransport on a pty that another fd already holds -> 409."""

    def test_port_held_by_other_fd_is_409_and_silent(self):
        master, slave = pty.openpty()
        path = os.ttyname(slave)  # `slave` stays open: it IS the foreign holder
        h = ServerHarness(FlipperCli(lambda: SerialTransport(path), TEST_ALLOWLIST,
                                     tx_timeout=1, sync_timeout=1), serial_port=path)
        try:
            code, body = h.request("POST", "/api/send/Gate")
            self.assertEqual((code, body["status"], body["reason"]),
                             (409, "failed", "serial_port_busy"))
            ready, _, _ = select.select([master], [], [], 0.3)
            self.assertEqual(ready, [], "bytes reached the device despite refusal")
        finally:
            h.close()
            os.close(slave)
            os.close(master)


class ConfigAndStartupTests(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.d.name, "config.env")
        self.actions = os.path.join(self.d.name, "actions.json")
        with open(self.actions, "w") as fh:
            json.dump(TEST_ACTIONS_DATA, fh)
        os.chmod(self.actions, 0o644)

    def tearDown(self):
        self.d.cleanup()

    def write(self, text, mode=0o600):
        with open(self.path, "w") as fh:
            fh.write(text)
        os.chmod(self.path, mode)

    def good(self, **over):
        vals = {"FLIPPER_API_TOKEN": secrets.token_urlsafe(32), "FLIPPER_API_BIND": "192.0.2.10"}
        vals.update(over)
        return "\n".join(f"{k}={v}" for k, v in vals.items() if v is not None) + "\n"

    def test_valid_config(self):
        self.write("# comment\n" + self.good())
        cfg = load_config(self.path, environ={})
        self.assertEqual((cfg.bind, cfg.port), ("192.0.2.10", 8787))
        self.assertNotIn(cfg.token, repr(cfg))

    def test_refusals(self):
        cases = {
            "no token": self.good(FLIPPER_API_TOKEN=None),
            "empty token": self.good(FLIPPER_API_TOKEN=""),
            "short token": self.good(FLIPPER_API_TOKEN="abc123"),
            "no bind": self.good(FLIPPER_API_BIND=None),
            "wildcard v4": self.good(FLIPPER_API_BIND="0.0.0.0"),
            "wildcard v6": self.good(FLIPPER_API_BIND="::"),
            "public": self.good(FLIPPER_API_BIND="8.8.8.8"),
            "hostname": self.good(FLIPPER_API_BIND="flipper.local"),
            "bad serial": self.good(FLIPPER_SERIAL_PORT="/etc/passwd"),
        }
        for name, text in cases.items():
            with self.subTest(name):
                self.write(text)
                with self.assertRaises(ConfigError) as cm:
                    load_config(self.path, environ={})
                self.assertNotIn("FLIPPER_API_TOKEN=", str(cm.exception))

    def test_world_readable_env_file_refused(self):
        self.write(self.good(), mode=0o644)
        with self.assertRaises(ConfigError):
            load_config(self.path, environ={})

    def test_main_refuses_start_without_token(self):
        self.write(self.good(FLIPPER_API_TOKEN=None))
        self.assertEqual(main(["--env-file", self.path, "--actions-file", self.actions]), 2)
        missing = os.path.join(self.d.name, "missing.env")
        self.assertEqual(main(["--env-file", missing, "--actions-file", self.actions]), 2)

    def test_main_check_config_ok(self):
        self.write(self.good(FLIPPER_API_BIND="127.0.0.1"))
        self.assertEqual(main(["--env-file", self.path, "--actions-file", self.actions,
                               "--check-config"]), 0)

    def test_build_server_refuses_empty_token(self):
        with self.assertRaises(ValueError):
            build_server(App(FakeCli(), TEST_ALLOWLIST, "/nonexistent"), "", "127.0.0.1", 0)


if __name__ == "__main__":
    unittest.main()
