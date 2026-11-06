import socket
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
    assert e.value.code == 0 and "0.1.9" in capsys.readouterr().out


def test_subcommands_listed(capsys):
    with pytest.raises(SystemExit):
        entry_with(["--help"])
    out = capsys.readouterr().out
    listed = out.split("positional arguments:")[1].split("options:")[0]
    names = [l.split()[0] for l in listed.splitlines()[2:] if l.startswith("    ") and l[4] != " "]
    assert names == ["start", "join", "leave", "stop", "update", "remove"]


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


def test_join_name_defaults_to_user_at_host(monkeypatch):
    import getpass
    monkeypatch.setattr(getpass, "getuser", lambda: "alice")
    host = socket.gethostname().split(".")[0]
    assert cli._default_name() == f"alice@{host}"
    monkeypatch.setattr(getpass, "getuser", lambda: (_ for _ in ()).throw(OSError()))
    assert cli._default_name() == host


def test_cli_join_invite_url(monkeypatch, home):
    from kaptyorka import join

    calls = []

    def fake(method, url, token, body=None, **kw):
        calls.append((method, url, body))
        return 200, {"token": "tok", "secret": "sec", "hub": "http://h:1"}

    monkeypatch.setattr(http, "request", fake)
    got = {}
    monkeypatch.setattr(join, "main", lambda hub, token, name, **kw: got.update(
        hub=hub, token=token, name=name, **kw) or 0)
    entry_with_exit(["join", "http://h:1/i/CODE123", "--name", "pc"])
    assert calls[0][:2] == ("POST", "http://h:1/i/CODE123")
    assert (got["hub"], got["token"], got["name"], got["secret"]) == ("http://h:1", "tok", "pc", "sec")


def test_cli_join_invite_failure_exits_1(monkeypatch, home, capsys):
    monkeypatch.setattr(http, "request", lambda *a, **k: (400, "invite already used"))
    assert entry_with_exit(["join", "http://h:1/i/CODE123"]) == 1
    assert "invite already used" in capsys.readouterr().err


def entry_with_exit(args):
    try:
        entry_with(args)
    except SystemExit as e:
        return e.code


def test_cli_invite_prints_url(monkeypatch, home, capsys):
    paths.save_config({"hub": "http://h:1", "token": "adm", "machine": "pc"})
    calls = []

    def fake(method, url, token, body=None, **kw):
        calls.append((method, url, token, body))
        if url.endswith("/api/people"):
            return 409, "a person named ann already exists"
        return 200, {"code": "C" * 22, "url": "http://h:1/i/" + "C" * 22}

    monkeypatch.setattr(http, "request", fake)
    assert entry_with_exit(["invite", "ann"]) == 0
    assert capsys.readouterr().out.strip() == "http://h:1/i/" + "C" * 22
    assert calls[1] == ("POST", "http://h:1/api/invites", "adm", {"person": "ann"})


def test_cli_invite_not_admin(monkeypatch, home, capsys):
    paths.save_config({"hub": "http://h:1", "token": "t", "machine": "pc"})
    monkeypatch.setattr(http, "request", lambda *a, **k: (403, "admin only"))
    assert entry_with_exit(["invite", "ann"]) == 1
    assert "admin only" in capsys.readouterr().err


def test_cli_people_table(monkeypatch, home, capsys):
    paths.save_config({"hub": "http://h:1", "token": "adm", "machine": "pc"})
    rows = [{"name": "ann", "role": "admin", "devices": [{"last_used": 100.0}, {"last_used": 200.0}]},
            {"name": "ben", "role": "member", "devices": []}]
    monkeypatch.setattr(http, "request", lambda *a, **k: (200, rows))
    monkeypatch.setattr("time.time", lambda: 260.0)
    assert entry_with_exit(["people"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0].split() == ["NAME", "ROLE", "DEVICES", "LAST", "SEEN"]
    assert out[1].split()[:3] == ["ann", "admin", "2"] and "60s ago" in out[1]
    assert out[2].split() == ["ben", "member", "0", "never"]


def test_cli_join_invite_keeps_token_when_join_fails(monkeypatch, home):
    from kaptyorka import join

    monkeypatch.setattr(http, "request", lambda *a, **k: (
        200, {"token": "tok", "secret": "sec", "hub": "http://h:1"}))
    monkeypatch.setattr(join, "main", lambda *a, **k: 1)
    assert entry_with_exit(["join", "http://h:1/i/CODE123", "--name", "pc"]) == 1
    cfg = paths.load_config()
    assert (cfg["hub"], cfg["token"], cfg["secret"], cfg["machine"]) == ("http://h:1", "tok", "sec", "pc")
