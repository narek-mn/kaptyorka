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
    assert e.value.code == 0 and "0.3.0" in capsys.readouterr().out


def test_subcommands_listed(capsys):
    with pytest.raises(SystemExit):
        entry_with(["--help"])
    out = capsys.readouterr().out
    listed = out.split("positional arguments:")[1].split("options:")[0]
    names = [l.split()[0] for l in listed.splitlines()[2:] if l.startswith("    ") and l[4] != " "]
    assert names == ["start", "join", "leave", "stop", "update", "remove", "doctor", "backup", "restore"]


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


def test_cli_grant_parses_shapes(monkeypatch, home, capsys):
    paths.save_config({"hub": "http://h:1", "token": "adm", "machine": "pc"})
    calls = []

    def fake(method, url, token, body=None, **kw):
        calls.append((method, url, body))
        return 204, None

    monkeypatch.setattr(http, "request", fake)
    assert entry_with_exit(["grant", "ann", "shop:write"]) == 0
    assert entry_with_exit(["grant", "ann", "machine:my host"]) == 0
    assert entry_with_exit(["revoke", "ann", "shop"]) == 0
    assert entry_with_exit(["revoke", "ann", "machine:box"]) == 0
    assert entry_with_exit(["project", "add", "shop", "--repo", "https://g/shop",
                            "--desc", "the shop"]) == 0
    assert entry_with_exit(["project", "add", "lab"]) == 0
    assert calls == [
        ("POST", "http://h:1/api/projects/shop/grant", {"person": "ann", "role": "write"}),
        ("POST", "http://h:1/api/machines/my%20host/grant", {"person": "ann"}),
        ("POST", "http://h:1/api/projects/shop/revoke", {"person": "ann"}),
        ("POST", "http://h:1/api/machines/box/revoke", {"person": "ann"}),
        ("POST", "http://h:1/api/projects",
         {"name": "shop", "repo": "https://g/shop", "description": "the shop"}),
        ("POST", "http://h:1/api/projects", {"name": "lab", "repo": "", "description": ""}),
    ]
    calls.clear()
    assert entry_with_exit(["grant", "ann", "shop"]) == 1  # no role
    assert entry_with_exit(["revoke", "ann", "shop:read"]) == 1
    assert calls == []
    assert "project>:<role" in capsys.readouterr().err


def test_cli_grant_prints_hub_error(monkeypatch, home, capsys):
    paths.save_config({"hub": "http://h:1", "token": "t", "machine": "pc"})
    monkeypatch.setattr(http, "request", lambda *a, **k: (400, "no project named shop"))
    assert entry_with_exit(["grant", "ann", "shop:read"]) == 1
    assert "no project named shop" in capsys.readouterr().err


def test_cli_machine_owner(monkeypatch, home, capsys):
    paths.save_config({"hub": "http://h:1", "token": "adm", "machine": "pc"})
    calls = []
    monkeypatch.setattr(http, "request",
                        lambda method, url, token, body=None, **kw: calls.append((method, url, body))
                        or (204, None))
    assert entry_with_exit(["machine", "owner", "box 1", "ann"]) == 0
    assert calls == [("POST", "http://h:1/api/machines/box%201/owner", {"person": "ann"})]
    assert capsys.readouterr().out == "box 1 owned by ann\n"


def test_metavar_lists_user_commands(capsys):
    with pytest.raises(SystemExit):
        entry_with(["--help"])
    assert ("{start,join,leave,stop,update,remove,invite,people,project,grant,revoke,machine,who,doctor,fleet,backup,restore}"
            in capsys.readouterr().out)


def test_doctor_prints_one_line_per_check(monkeypatch, home, capsys):
    from kaptyorka import doctor

    seen = []
    monkeypatch.setattr(doctor, "checks", lambda *a, **k: [doctor.Check("config", True),
                                                          doctor.Check("tmux", False, "not installed", "brew install tmux")])
    monkeypatch.setattr("kaptyorka.adapters.all_adapters", lambda log=print: seen.append(1) or [])
    assert entry_with_exit(["doctor"]) == 1
    assert capsys.readouterr().out == "ok   config\nFAIL tmux: not installed -> brew install tmux\n"


def test_fleet_add_smoke_with_fake_run(monkeypatch, home, tmp_path, capsys):
    from types import SimpleNamespace

    paths.save_config({"hub": "http://h:1", "token": "adm", "machine": "pc"})
    monkeypatch.setattr(http, "request", lambda m, u, t, b=None, **k: (
        200, {"url": "http://h:1/i/" + "C" * 22}))
    seen = []

    def fake_run(argv, **kw):
        seen.append(argv)
        return SimpleNamespace(returncode=0, stdout="/bin/claude\n", stderr="")

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    assert entry_with_exit(["fleet", "add", "alice@box-1", "--person", "alice"]) == 0
    assert capsys.readouterr().out == "alice@box-1: joined as alice@box-1; tools: claude-code\n"
    assert all(a[:3] == ["ssh", "-o", "BatchMode=yes"] for a in seen) and len(seen) == 4
    # every target rejected: a failure, not a success
    assert entry_with_exit(["fleet", "add", "a b@box-2", "--person", "alice"]) == 1
    assert capsys.readouterr().out == "a b@box-2: not a user@host\n" and len(seen) == 4


def test_fleet_list_prints_devices(monkeypatch, home, capsys):
    paths.save_config({"hub": "http://h:1", "token": "adm", "machine": "pc"})
    monkeypatch.setattr(http, "request", lambda *a, **k: (200, [{"host": "box-1", "devices": [
        {"os_user": "alice", "person": "alice", "online": True, "version": "1.0"}]}]))
    assert entry_with_exit(["fleet", "list"]) == 0
    assert capsys.readouterr().out == "box-1 alice        alice      online 1.0\n"


def test_backup_and_restore_are_wired(monkeypatch):
    from kaptyorka import backup

    seen = []
    monkeypatch.setattr(backup, "run_backup", lambda home, dest, run: seen.append(("b", dest)) or 0)
    monkeypatch.setattr(backup, "restore", lambda home, f, run: seen.append(("r", str(f))) or 0)
    with pytest.raises(SystemExit) as e:
        entry_with(["backup", "alice@box-1:/srv/b"])
    assert e.value.code == 0
    with pytest.raises(SystemExit) as e:
        entry_with(["restore", "x.tar"])
    assert e.value.code == 0
    assert seen == [("b", "alice@box-1:/srv/b"), ("r", "x.tar")]


def test_backup_nightly_installs_timer(home, monkeypatch, capsys):
    from kaptyorka import service

    got = []
    monkeypatch.setattr(service, "install_timer",
                        lambda name, argv, **kw: got.append((name, argv)) or ["installed t"])
    with pytest.raises(SystemExit) as e:
        entry_with(["backup", "alice@box-1:/srv/b", "--nightly"])
    assert e.value.code == 0
    assert got[0][0] == "kaptyorka-backup" and got[0][1][1:] == ["backup", "alice@box-1:/srv/b"]
    assert "installed t" in capsys.readouterr().out


def test_backup_nightly_rejects_bad_dest(home, capsys):
    with pytest.raises(SystemExit) as e:
        entry_with(["backup", "-oProxy=x", "--nightly"])
    assert e.value.code != 0


def test_backup_nightly_warns_without_gpg(home, monkeypatch, capsys):
    import shutil
    from kaptyorka import service

    monkeypatch.setattr(shutil, "which", lambda n: None)
    monkeypatch.setattr(service, "install_timer", lambda *a, **kw: ["installed t"])
    with pytest.raises(SystemExit):
        entry_with(["backup", "alice@box-1:/srv/b", "--nightly"])
    assert "gpg not found: nightly backups will be unencrypted" in capsys.readouterr().out
