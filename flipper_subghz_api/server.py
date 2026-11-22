"""HTTP layer (stdlib http.server). Every endpoint requires `Authorization: Bearer <token>`.

Routes (exact match, no query strings, no path parameters):
  GET  /health                 availability + last outcomes (no serial I/O)
  POST /api/send/<action>      send the allowlisted file for <action>; never retried
  POST /api/toggle/<action>    DEPRECATED alias of /api/send/<action> (same behavior,
                               adds a `Deprecation: true` response header)

<action> is only ever looked up in the allowlist loaded at startup; the request cannot
name a path, a repeat count or anything else.

One lock and one rate limit cover ALL actions together (one USB port, one radio): while
any send runs, another is refused with 409 ``busy``; within ``min_interval`` (default
3 s, FLIPPER_MIN_INTERVAL) after the previous send ended, ANY send -- also a different
action, e.g. Light_off right after Light_on -- is refused with 429 ``rate_limited`` and
nothing is sent.
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import __version__
from .flipper import FAILED, SENT, UNKNOWN, TxResult

log = logging.getLogger("flipper_subghz_api")

MAX_BODY = 1024
SEND_PREFIX = "/api/send/"
LEGACY_PREFIX = "/api/toggle/"  # deprecated alias

_HTTP_STATUS = {SENT: 200, FAILED: 502, UNKNOWN: 504}
_FAILED_STATUS = {
    "device_not_found": 503,
    "busy": 409,
    "rate_limited": 429,
    "serial_port_busy": 409,
}


class App:
    """Owns the single-USB-port lock. Concurrent sends are rejected, never queued."""

    def __init__(self, cli, allowlist, serial_port: str, min_interval: float = 3.0,
                 clock=time.monotonic):
        self.cli = cli
        self.allowlist = allowlist
        self.serial_port = serial_port
        self.min_interval = min_interval
        self._clock = clock
        self._lock = threading.Lock()
        self._last_tx_end = None
        self._last = {}
        routes = {}
        for name in allowlist.names:
            routes[SEND_PREFIX + name] = (name, False)
            routes[LEGACY_PREFIX + name] = (name, True)
        self.routes = routes

    def send(self, action: str) -> TxResult:
        path = self.allowlist.specs[action].path  # KeyError impossible: routes come from it
        if not self._lock.acquire(blocking=False):
            return TxResult(FAILED, "busy")
        try:
            now = self._clock()
            if self._last_tx_end is not None and now - self._last_tx_end < self.min_interval:
                return TxResult(FAILED, "rate_limited")
            try:
                result = self.cli.transmit(path)
            except Exception:  # adapter bug: we cannot know whether RF went out
                log.exception("adapter error for %s", action)
                result = TxResult(UNKNOWN, "adapter_exception")
            self._last_tx_end = self._clock()
            self._last[action] = {
                "status": result.status,
                "reason": result.reason,
                "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            }
            return result
        finally:
            self._lock.release()

    toggle = send  # backwards-compatible name

    def health(self) -> dict:
        present = os.path.exists(self.serial_port)
        return {
            "service": "flipper-subghz-api",
            "version": __version__,
            "device_present": present,
            "device_rw": present and os.access(self.serial_port, os.R_OK | os.W_OK),
            "busy": self._lock.locked(),
            "actions": list(self.allowlist.names),
            "last_results": self._last,
            "note": "fixed-code replay only; the state of the target device is not known",
        }


def make_handler(app: App, token: str):
    expected = ("Bearer " + token).encode()

    class Handler(BaseHTTPRequestHandler):
        server_version = "flipper-subghz-api"
        sys_version = ""

        def log_message(self, format, *args):  # request line only; headers never logged
            log.info("%s %s", self.client_address[0], format % args)

        def _send(self, code: int, body: dict, extra_headers=()):
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            for k, v in extra_headers:
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def _authorized(self) -> bool:
            got = self.headers.get("Authorization", "").encode()
            return hmac.compare_digest(got, expected)

        def _guard(self) -> bool:
            if not self._authorized():
                self._send(401, {"error": "unauthorized"})
                return False
            length = self.headers.get("Content-Length")
            try:
                n = int(length) if length else 0
            except ValueError:
                n = MAX_BODY + 1
            if n < 0 or n > MAX_BODY:
                self._send(413, {"error": "body too large"})
                return False
            if n:
                self.rfile.read(n)  # ignored: actions take no parameters
            return True

        def do_GET(self):
            if not self._guard():
                return
            if self.path == "/health":
                self._send(200, app.health())
            elif self.path in app.routes:
                self._send(405, {"error": "use POST"})
            else:
                self._send(404, {"error": "not found"})

        def do_POST(self):
            if not self._guard():
                return
            route = app.routes.get(self.path)
            if route is None:
                self._send(404, {"error": "unknown action"})
                return
            action, deprecated = route
            result = app.send(action)
            log.info("send %s -> %s (%s)", action, result.status, result.reason)
            code = _HTTP_STATUS[result.status]
            if result.status == FAILED:
                code = _FAILED_STATUS.get(result.reason, code)
            body = {"action": action, "status": result.status, "reason": result.reason}
            if result.status == UNKNOWN:
                body["note"] = ("may have transmitted; not retried. Check the device before "
                                "sending again.")
            headers = [("Deprecation", "true")] if deprecated else []
            self._send(code, body, headers)

        def _not_allowed(self):
            if not self._guard():
                return
            self._send(405, {"error": "method not allowed"})

        do_PUT = do_DELETE = do_PATCH = do_OPTIONS = _not_allowed

    return Handler


def build_server(app: App, token: str, host: str, port: int) -> ThreadingHTTPServer:
    if not token:
        raise ValueError("refusing to build a server without a token")
    srv = ThreadingHTTPServer((host, port), make_handler(app, token))
    srv.daemon_threads = True
    return srv
