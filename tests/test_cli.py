import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest import mock

import pytest

from kaptyorka import cli, http, paths


def entry_with(args):
    with mock.patch.object(sys, "argv", ["kaptyorka", *args]):
        cli.entry()


@pytest.fixture
def stub_401():
    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(401)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"error": "no"}')

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}/x"
    srv.shutdown()
    srv.server_close()


def test_version_flag(capsys):
    with pytest.raises(SystemExit) as e:
        entry_with(["--version"])
    assert e.value.code == 0 and "0.1.0" in capsys.readouterr().out


def test_subcommands_listed(capsys):
    with pytest.raises(SystemExit):
        entry_with(["--help"])
    out = capsys.readouterr().out
    for cmd in ["serve", "join", "who", "mcp", "hook", "bridge"]:
        assert cmd in out


def test_config_roundtrip_is_private(home):
    paths.save_config({"hub": "http://h:8765", "token": "t", "machine": "pc"})
    assert paths.load_config()["machine"] == "pc"
    assert oct(paths.config_path().stat().st_mode & 0o777) == "0o600"
    assert str(paths.home()).startswith(str(home))


def test_request_returns_http_errors(stub_401):
    assert http.request("GET", stub_401, "t")[0] == 401


def test_request_unreachable_raises():
    with pytest.raises(http.HubUnreachable):
        http.request("GET", "http://127.0.0.1:9/x", "t", timeout=1)
