"""Unit tests for scripts/http_ok.py.

Verifies that the health-check helper exits 0 when the server returns
{"ok": true} and exits non-zero for any other response or connection failure.
These run without any server — a tiny stdlib HTTP server is spun up in-process.
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from tests import REPO_ROOT

HTTP_OK_PY = str(REPO_ROOT / "scripts" / "http_ok.py")


def _run(url: str) -> int:
    r = subprocess.run([sys.executable, HTTP_OK_PY, url], capture_output=True)
    return r.returncode


class _JsonHandler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_GET(self):
        body = self.server.response_body.encode()
        self.send_response(self.server.response_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class _Server:
    def __init__(self, code: int, body: str):
        self._srv = HTTPServer(("127.0.0.1", 0), _JsonHandler)
        self._srv.response_code = code
        self._srv.response_body = body
        self._thread = threading.Thread(target=self._srv.serve_forever, daemon=True)
        self._thread.start()

    @property
    def url(self) -> str:
        host, port = self._srv.server_address
        return f"http://{host}:{port}/"

    def stop(self):
        self._srv.shutdown()


class TestHttpOk(unittest.TestCase):

    def test_ok_true_exits_zero(self):
        srv = _Server(200, '{"ok": true}')
        try:
            self.assertEqual(_run(srv.url), 0)
        finally:
            srv.stop()

    def test_ok_false_exits_nonzero(self):
        srv = _Server(200, '{"ok": false}')
        try:
            self.assertNotEqual(_run(srv.url), 0)
        finally:
            srv.stop()

    def test_missing_ok_field_exits_nonzero(self):
        srv = _Server(200, '{"status": "healthy"}')
        try:
            self.assertNotEqual(_run(srv.url), 0)
        finally:
            srv.stop()

    def test_non_200_exits_nonzero(self):
        srv = _Server(503, '{"ok": true}')
        try:
            self.assertNotEqual(_run(srv.url), 0)
        finally:
            srv.stop()

    def test_invalid_json_exits_nonzero(self):
        srv = _Server(200, "not json")
        try:
            self.assertNotEqual(_run(srv.url), 0)
        finally:
            srv.stop()

    def test_connection_refused_exits_nonzero(self):
        # Port 1 is reserved and never listening
        self.assertNotEqual(_run("http://127.0.0.1:1/health"), 0)

    def test_script_exists(self):
        self.assertTrue(
            os.path.isfile(HTTP_OK_PY),
            f"http_ok.py not found at expected path: {HTTP_OK_PY}",
        )


class TestEntrypointUsesCorrectPath(unittest.TestCase):
    """Guard against the bug where $(dirname $0)/http_ok.py was used in
    entrypoint.sh — when the script runs as /entrypoint.sh inside Docker,
    dirname resolves to / and http_ok.py is never found."""

    def test_entrypoint_does_not_use_dirname_0_for_http_ok(self):
        entrypoint = str(REPO_ROOT / "scripts" / "entrypoint.sh")
        with open(entrypoint) as f:
            source = f.read()
        bad_pattern = '$(dirname "$0")/http_ok.py'
        self.assertNotIn(
            bad_pattern,
            source,
            "entrypoint.sh uses $(dirname \"$0\")/http_ok.py — this resolves to "
            "/http_ok.py when the script runs as /entrypoint.sh inside Docker. "
            "Use ${APP_ROOT}/scripts/http_ok.py instead.",
        )

    def test_entrypoint_uses_app_root_for_http_ok(self):
        entrypoint = str(REPO_ROOT / "scripts" / "entrypoint.sh")
        with open(entrypoint) as f:
            source = f.read()
        self.assertIn(
            "${APP_ROOT}/scripts/http_ok.py",
            source,
            "entrypoint.sh should reference http_ok.py via ${APP_ROOT}/scripts/http_ok.py",
        )
