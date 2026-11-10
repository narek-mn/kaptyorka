import json
import subprocess
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
    assert e.value.code == 0 and "0.7.1" in capsys.readouterr().out


def test_subcommands_listed(capsys):
    with pytest.raises(SystemExit):
        entry_with(["--help"])
    out = capsys.readouterr().out
    listed = out.split("positional arguments:")[1].split("options:")[0]
    names = [l.split()[0] for l in listed.splitlines()[2:] if l.startswith("    ") and l[4] != " "]
    assert names == ["start", "join", "leave", "stop", "update", "remove", "invite", "people", "invites", "grant",
                     "revoke", "project", "machine", "who", "doctor", "backup", "restore", "fleet", "login-copy", "rules"]
    for hidden in ("serve", "mcp", "bridge", "hook"):
        assert hidden not in names


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
    assert "was already used" in capsys.readouterr().err


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
    assert "only the admin can do that" in capsys.readouterr().err


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
    assert ("{start,join,leave,stop,update,remove,invite,people,invites,project,grant,revoke,machine,who,doctor,fleet,login-copy,rules,backup,restore}"
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


def test_login_copy_prints_and_exits_by_result(monkeypatch, capsys):
    from kaptyorka import login_copy

    seen = []

    def fake(target, force=False, **kw):
        seen.append((target, force))
        return fake.result

    monkeypatch.setattr(login_copy, "copy", fake)
    fake.result = (True, "alice@box-1: Claude Code is logged in there")
    assert entry_with_exit(["login-copy", "alice@box-1"]) == 0
    assert capsys.readouterr().out == "alice@box-1: Claude Code is logged in there\n"
    fake.result = (False, "x")
    assert entry_with_exit(["login-copy", "alice@box-1", "--force"]) == 1
    assert seen == [("alice@box-1", False), ("alice@box-1", True)]


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
    out = capsys.readouterr().out
    assert "installed t" in out
    pw = paths.home() / "backup-pass"  # made now, so a copy can be kept before the first night
    assert pw.read_text().strip() and f"backup passphrase in {pw}, keep a copy" in out
    assert pw.stat().st_mode & 0o777 == 0o600


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


def _team_folder(tmp_path):
    root = tmp_path / "team"
    (root / "skills").mkdir(parents=True)
    (root / "RULES.md").write_text("use tabs\n", encoding="utf-8")
    (root / "skills" / "review.md").write_text("review steps", encoding="utf-8")
    (root / "skills" / "deploy.md").write_text("deploy steps", encoding="utf-8")
    return root


def test_rules_push_uploads_rules_and_skills(monkeypatch, home, tmp_path, capsys):
    paths.save_config({"hub": "http://h:1", "token": "adm", "machine": "pc"})
    root = _team_folder(tmp_path)
    calls = []

    def fake(method, url, token, body=None, **kw):
        calls.append((method, url, token, body))
        if url.endswith("/api/rules"):
            return 200, {"version": 3}
        return 200, {"added": ["deploy", "review"], "changed": ["old"], "removed": []}

    monkeypatch.setattr(http, "request", fake)
    assert entry_with_exit(["rules", "push", str(root)]) == 0
    assert calls[0] == ("GET", "http://h:1/api/rules", "adm", None)
    assert calls[1] == ("POST", "http://h:1/api/rules", "adm", {"text": "use tabs\n"})
    assert calls[2] == ("PUT", "http://h:1/api/skills", "adm",
                        {"skills": {"deploy": "deploy steps", "review": "review steps"}})
    assert capsys.readouterr().out.strip() == "team rules v3; skills: 2 added, 1 changed, 0 removed"


def test_rules_push_missing_rules_md(monkeypatch, home, tmp_path, capsys):
    paths.save_config({"hub": "http://h:1", "token": "adm", "machine": "pc"})
    monkeypatch.setattr(http, "request", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no call")))
    assert entry_with_exit(["rules", "push", str(tmp_path)]) == 1
    assert f"no RULES.md in {tmp_path}" in capsys.readouterr().err


def test_rules_push_bad_skill_name_fails_before_upload(monkeypatch, home, tmp_path, capsys):
    paths.save_config({"hub": "http://h:1", "token": "adm", "machine": "pc"})
    root = _team_folder(tmp_path)
    (root / "skills" / "Bad_Name.md").write_text("x", encoding="utf-8")
    monkeypatch.setattr(http, "request", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no call")))
    assert entry_with_exit(["rules", "push", str(root)]) == 1
    assert "skill name must be 1-48 characters" in capsys.readouterr().err


def test_rules_push_forbidden(monkeypatch, home, tmp_path, capsys):
    paths.save_config({"hub": "http://h:1", "token": "t", "machine": "pc"})
    monkeypatch.setattr(http, "request", lambda *a, **k: (403, "admin only"))
    assert entry_with_exit(["rules", "push", str(_team_folder(tmp_path))]) == 1
    assert "only the admin's device may push rules" in capsys.readouterr().err


def test_rules_push_without_skills_folder_says_all_removed(monkeypatch, home, tmp_path, capsys):
    paths.save_config({"hub": "http://h:1", "token": "adm", "machine": "pc"})
    (tmp_path / "RULES.md").write_text("use tabs\n", encoding="utf-8")

    def fake(method, url, token, body=None, **kw):
        if url.endswith("/api/rules"):
            return 200, {"version": 4}
        return 200, {"added": [], "changed": [], "removed": ["review"]}

    monkeypatch.setattr(http, "request", fake)
    assert entry_with_exit(["rules", "push", str(tmp_path)]) == 0
    assert capsys.readouterr().out.splitlines()[-1] == (
        f"no skills/ folder in {tmp_path}: all team skills removed")


def _no_request(monkeypatch):
    monkeypatch.setattr(http, "request", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no call")))


def test_rules_push_non_utf8_skill_fails_before_upload(monkeypatch, home, tmp_path, capsys):
    paths.save_config({"hub": "http://h:1", "token": "adm", "machine": "pc"})
    root = _team_folder(tmp_path)
    (root / "skills" / "bad.md").write_bytes(b"\xff\xfe\x00")
    _no_request(monkeypatch)
    assert entry_with_exit(["rules", "push", str(root)]) == 1
    err = capsys.readouterr().err
    assert "cannot read" in err and "bad.md" in err and "UTF-8" in err


def test_rules_push_directory_named_md_is_skipped(monkeypatch, home, tmp_path, capsys):
    paths.save_config({"hub": "http://h:1", "token": "adm", "machine": "pc"})
    root = _team_folder(tmp_path)
    (root / "skills" / "foo.md").mkdir()
    calls = []

    def fake(method, url, token, body=None, **kw):
        calls.append((method, url, body))
        if url.endswith("/api/rules"):
            return 200, {"version": 2}
        return 200, {"added": ["deploy", "review"], "changed": [], "removed": []}

    monkeypatch.setattr(http, "request", fake)
    assert entry_with_exit(["rules", "push", str(root)]) == 0
    assert calls[2][2] == {"skills": {"deploy": "deploy steps", "review": "review steps"}}


def test_rules_push_oversized_skill_makes_no_request(monkeypatch, home, tmp_path, capsys):
    paths.save_config({"hub": "http://h:1", "token": "adm", "machine": "pc"})
    root = _team_folder(tmp_path)
    (root / "skills" / "big.md").write_text("a" * 20001, encoding="utf-8")
    _no_request(monkeypatch)
    assert entry_with_exit(["rules", "push", str(root)]) == 1
    assert "is too long: 20001 characters, limit 20000" in capsys.readouterr().err


def test_rules_push_put_failure_says_rules_were_pushed(monkeypatch, home, tmp_path, capsys):
    paths.save_config({"hub": "http://h:1", "token": "adm", "machine": "pc"})

    def fake(method, url, token, body=None, **kw):
        if url.endswith("/api/rules"):
            return 200, {"version": 5}
        return 500, "boom"

    monkeypatch.setattr(http, "request", fake)
    assert entry_with_exit(["rules", "push", str(_team_folder(tmp_path))]) == 1
    assert capsys.readouterr().err.strip() == (
        "skills upload failed (500): boom; the rules were already pushed as v5")


def _hub_with_rules(monkeypatch, hub_text):
    paths.save_config({"hub": "http://h:1", "token": "adm", "machine": "pc"})
    calls = []

    def fake(method, url, token, body=None, **kw):
        calls.append((method, url))
        if url.endswith("/api/rules"):
            return 200, ({"version": 5, "text": hub_text} if method == "GET" else {"version": 6})
        return 200, {"added": [], "changed": [], "removed": []}

    monkeypatch.setattr(http, "request", fake)
    return calls


def test_rules_push_refuses_when_hub_has_rules_not_in_file(monkeypatch, home, tmp_path, capsys):
    calls = _hub_with_rules(monkeypatch, "use tabs\n\nno force pushes\nsmall commits\n")
    assert entry_with_exit(["rules", "push", str(_team_folder(tmp_path))]) == 1
    assert calls == [("GET", "http://h:1/api/rules")]
    assert capsys.readouterr().err.splitlines() == [
        "the hub has rules not in RULES.md (edited on the page or accepted proposals):",
        "  no force pushes", "  small commits", "add them to RULES.md or push with --force"]


def test_rules_push_force_skips_the_check(monkeypatch, home, tmp_path, capsys):
    calls = _hub_with_rules(monkeypatch, "use tabs\nno force pushes\n")
    assert entry_with_exit(["rules", "push", "--force", str(_team_folder(tmp_path))]) == 0
    assert [c[0] for c in calls] == ["POST", "PUT"]
    assert "team rules v6" in capsys.readouterr().out


@pytest.mark.parametrize("hub_text", ["use tabs\n", "", "  use tabs  \n\n"])
def test_rules_push_same_or_subset_pushes(monkeypatch, home, tmp_path, hub_text):
    root = _team_folder(tmp_path)
    (root / "RULES.md").write_text("use tabs\nsmall commits\n", encoding="utf-8")
    calls = _hub_with_rules(monkeypatch, hub_text)
    assert entry_with_exit(["rules", "push", str(root)]) == 0
    assert [c[0] for c in calls] == ["GET", "POST", "PUT"]


@pytest.fixture
def admin_hub(monkeypatch, home):
    """A saved config and a fake hub: answers[(method, path)] -> (status, body); calls kept."""
    paths.save_config({"hub": "http://h:1", "token": "adm", "machine": "pc"})
    hub = {"calls": [], "answers": {}}

    def fake(method, url, token, body=None, **kw):
        path = url[len("http://h:1"):]
        hub["calls"].append((method, path, body))
        return hub["answers"].get((method, path), (204, None))

    monkeypatch.setattr(http, "request", fake)
    return hub


def test_cli_people_remove_asks_first(admin_hub, monkeypatch, capsys):
    asked = []
    monkeypatch.setattr("builtins.input", lambda q: asked.append(q) or "n")
    assert entry_with_exit(["people", "remove", "bob"]) == 1
    assert asked == ["Remove bob? Their devices stop working at once; their messages and notes"
                     " stay. [y/N] "]
    assert admin_hub["calls"] == []
    admin_hub["answers"][("DELETE", "/api/people/bob")] = (200, {
        "devices": 2, "workers": 1, "doors": 0, "grants": 3, "requests": 0, "work_requests": 0,
        "machines_unowned": ["box-2"], "still_running": [{"worker": "w4", "host": "box-2"}]})
    monkeypatch.setattr("builtins.input", lambda q: "y")
    assert entry_with_exit(["people", "remove", "bob"]) == 0
    out = capsys.readouterr().out
    assert "removed bob: 2 devices stopped, 1 workers closed" in out
    assert "w4 may still run on box-2; close it on that machine" in out


def test_cli_people_remove_yes_and_error(admin_hub, monkeypatch, capsys):
    monkeypatch.setattr("builtins.input", lambda q: pytest.fail("asked"))
    admin_hub["answers"][("DELETE", "/api/people/alice")] = (400, "you cannot remove yourself")
    assert entry_with_exit(["people", "remove", "alice", "--yes"]) == 1
    assert capsys.readouterr().err.strip() == "you cannot remove yourself"


def test_cli_people_role(admin_hub, capsys):
    assert entry_with_exit(["people", "role", "bob", "manager"]) == 0
    assert admin_hub["calls"] == [("POST", "/api/people/bob/role", {"role": "manager"})]
    assert capsys.readouterr().out.strip() == "bob is now a manager"
    admin_hub["answers"][("POST", "/api/people/bob/role")] = (404, "no person named bob")
    assert entry_with_exit(["people", "role", "bob", "member"]) == 1
    assert "no person named bob" in capsys.readouterr().err


def test_cli_people_still_lists(admin_hub, capsys):
    admin_hub["answers"][("GET", "/api/people")] = (200, [{"name": "alice", "role": "admin",
                                                           "devices": []}])
    assert entry_with_exit(["people"]) == 0
    assert capsys.readouterr().out.splitlines()[1].split() == ["alice", "admin", "0", "never"]


def test_cli_invites_table_and_cancel(admin_hub, monkeypatch, capsys):
    monkeypatch.setattr("time.time", lambda: 0.0)
    admin_hub["answers"][("GET", "/api/invites")] = (200, [
        {"code": "C" * 22, "person": "bob", "created": 0.0, "expires": 7 * 86400.0}])
    assert entry_with_exit(["invites"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0].split() == ["PERSON", "EXPIRES", "IN", "CODE"]
    assert out[1].split() == ["bob", "168h", "C" * 22]
    assert entry_with_exit(["invites", "--cancel", "C" * 22]) == 0
    assert admin_hub["calls"][-1] == ("DELETE", "/api/invites/" + "C" * 22, None)
    admin_hub["answers"][("DELETE", "/api/invites/x")] = (404, "no open invite with that code")
    assert entry_with_exit(["invites", "--cancel", "x"]) == 1
    assert "no open invite with that code" in capsys.readouterr().err


def test_cli_invites_empty(admin_hub, capsys):
    admin_hub["answers"][("GET", "/api/invites")] = (200, [])
    assert entry_with_exit(["invites"]) == 0
    assert capsys.readouterr().out.strip() == "no open invites"


def test_cli_project_edit_and_remove(admin_hub, monkeypatch, capsys):
    assert entry_with_exit(["project", "edit", "proj-a", "--description", "the app"]) == 0
    assert admin_hub["calls"][-1] == ("POST", "/api/projects/proj-a", {"description": "the app"})
    monkeypatch.setattr("builtins.input", lambda q: "")
    assert entry_with_exit(["project", "remove", "proj-a"]) == 1
    assert admin_hub["calls"][-1][0] == "POST"  # nothing sent
    admin_hub["answers"][("DELETE", "/api/projects/proj-a")] = (200, {"grants": 2, "requests": 0,
                                                                       "folders": 1})
    assert entry_with_exit(["project", "remove", "proj-a", "--yes"]) == 0
    assert "removed the project proj-a: 2 grants dropped" in capsys.readouterr().out
    admin_hub["answers"][("DELETE", "/api/projects/proj-a")] = (
        400, "workers are running in proj-a; close its workers first")
    assert entry_with_exit(["project", "remove", "proj-a", "--yes"]) == 1
    assert "close its workers first" in capsys.readouterr().err


def test_cli_admin_commands_hub_unreachable(monkeypatch, home, capsys):
    paths.save_config({"hub": "http://h:1", "token": "adm", "machine": "pc"})

    def down(*a, **k):
        raise http.HubUnreachable("http://h:1")

    monkeypatch.setattr(http, "request", down)
    for args in (["people", "remove", "bob", "--yes"], ["people", "role", "bob", "member"],
                 ["invites"], ["project", "edit", "p", "--description", "d"],
                 ["project", "remove", "p", "--yes"]):
        assert entry_with_exit(args) == 1
        assert capsys.readouterr().err.strip() == http.unreachable("http://h:1")


COMMON_COMMANDS = [["who"], ["invite", "bob"], ["people"], ["people", "remove", "bob", "--yes"],
                   ["people", "role", "bob", "member"], ["invites"], ["invites", "--cancel", "c1"],
                   ["grant", "bob", "proj-a:read"], ["revoke", "bob", "proj-a"],
                   ["project", "add", "proj-a"], ["project", "edit", "proj-a", "--description", "d"],
                   ["project", "remove", "proj-a", "--yes"], ["machine", "owner", "box-1", "bob"],
                   ["fleet", "list"]]


def _one_sentence(err):
    lines = err.strip().splitlines()
    assert len(lines) == 1 and "Traceback" not in err and not lines[0].startswith("<")
    return lines[0]


@pytest.mark.parametrize("args", COMMON_COMMANDS, ids=lambda a: " ".join(a))
def test_cli_not_joined_says_how_to_join(args, home, capsys):
    assert entry_with_exit(args) == 1
    out = capsys.readouterr()
    assert out.out == ""
    assert _one_sentence(out.err) == cli.NOT_JOINED


@pytest.mark.parametrize("args", COMMON_COMMANDS, ids=lambda a: " ".join(a))
def test_cli_unreachable_hub_is_one_sentence(args, monkeypatch, home, capsys):
    paths.save_config({"hub": "http://h:1", "token": "adm", "machine": "pc"})

    def down(*a, **k):
        raise http.HubUnreachable("http://h:1")

    monkeypatch.setattr(http, "request", down)
    assert entry_with_exit(args) == 1
    out = capsys.readouterr()
    assert out.out == ""
    assert _one_sentence(out.err) == "cannot reach the hub at http://h:1; is it running, and is the address right?"


@pytest.mark.parametrize("args", COMMON_COMMANDS, ids=lambda a: " ".join(a))
@pytest.mark.parametrize("status,body,said", [
    (401, "", "the hub does not accept this machine's token; run kaptyorka join again"),
    (403, "admin only", "only the admin can do that; this machine is not signed in as the admin"),
    (403, None, "only the admin can do that; this machine is not signed in as the admin"),
    (404, "no person named bob", "no person named bob"),
    (404, None, "the hub has no such thing"),
    (409, "bob already exists", "bob already exists"),
    (409, None, "that conflicts with what the hub already has"),
    (500, "<html>boom</html>", "the hub answered 500 and gave no reason; look at the hub's log"),
])
def test_cli_hub_refusals_are_one_plain_sentence(args, status, body, said, monkeypatch, home, capsys):
    paths.save_config({"hub": "http://h:1", "token": "adm", "machine": "pc"})
    monkeypatch.setattr(http, "request", lambda *a, **k: (status, body))
    if args[0] == "invite" and status == 409:
        pytest.skip("a 409 from adding the person is expected: invite goes on")
    assert entry_with_exit(args) == 1
    out = capsys.readouterr()
    assert out.out == ""
    assert _one_sentence(out.err) == said


def test_cli_missing_argument_prints_usage_and_one_sentence(capsys):
    for args in (["invite"], ["people", "remove"], ["project"], ["grant", "bob"], ["fleet"]):
        with pytest.raises(SystemExit) as e:
            entry_with(args)
        assert e.value.code == 2
        err = capsys.readouterr().err.strip().splitlines()
        assert err[0].startswith("usage: kaptyorka") and "error: the following arguments are required" in err[-1]
        assert "_cmd" not in err[-1]


def _start_with(monkeypatch, home, active, restart):
    paths.home().mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(cli, "_serve", lambda args: 0)
    monkeypatch.setattr(cli.service, "is_active", lambda name, **kw: active)
    monkeypatch.setattr(cli.service, "restart", restart)
    return entry_with_exit(["start"])


def test_start_turns_the_bridge_back_on(monkeypatch, home, capsys):
    calls = []

    def restart(name, **kw):
        calls.append(name)
        return ["bridge started"]

    assert _start_with(monkeypatch, home, False, restart) in (0, None)
    assert calls == ["kaptyorka-bridge"]
    assert "bridge started" in capsys.readouterr().out


def test_start_leaves_a_running_bridge(monkeypatch, home):
    calls = []
    assert _start_with(monkeypatch, home, True, lambda name, **kw: calls.append(name) or []) in (0, None)
    assert calls == []


def test_start_without_bridge(monkeypatch, home):
    calls = []
    assert _start_with(monkeypatch, home, None, lambda name, **kw: calls.append(name) or []) in (0, None)
    assert calls == []


def test_start_reports_a_bridge_that_will_not_start(monkeypatch, home, capsys):
    def restart(name, **kw):
        raise cli.service.ServiceError("bridge broke")

    assert _start_with(monkeypatch, home, False, restart) == 1
    assert "bridge broke" in capsys.readouterr().err
    assert (paths.home() / "hub.json").exists()


# --- start, stop, update: once, and say so ---

def _units(monkeypatch, **active):
    """service.is_active from a dict {unit: True/False/None}; everything else is recorded."""
    calls = []
    monkeypatch.setattr(cli.service, "is_active",
                        lambda name, **kw: active.get(name.replace("kaptyorka-", ""), None))
    monkeypatch.setattr(cli.service, "restart", lambda name, **kw: calls.append(("restart", name)) or [f"{name} up"])
    monkeypatch.setattr(cli.service, "stop", lambda name, **kw: calls.append(("stop", name)) or [f"{name}: stopped"])
    monkeypatch.setattr(cli.service, "install", lambda name, argv, **kw: calls.append(("install", name)) or
                        [f"installed /x/{name}.service"])
    monkeypatch.setattr(cli, "_outbound_ip", lambda: "10.0.0.1")
    return calls


def _hub_json(port=8765, host=None, manager=True):
    paths.home().mkdir(parents=True, exist_ok=True)
    (paths.home() / "hub.json").write_text(json.dumps({"port": port, "host": host, "manager": manager}))


def test_start_twice_does_not_restart(monkeypatch, home, capsys):
    calls = _units(monkeypatch, hub=True, bridge=True)
    _hub_json()
    assert entry_with_exit(["start"]) in (0, None)
    assert calls == []
    out = capsys.readouterr().out.splitlines()
    assert out[0] == "the hub is already running: http://10.0.0.1:8765"
    assert out[1].startswith("open the page: http://10.0.0.1:8765/#t=") and "keep this link private" in out[1]
    assert len(out) == 2


def test_start_already_running_still_turns_the_bridge_on(monkeypatch, home):
    calls = _units(monkeypatch, hub=True, bridge=False)
    _hub_json()
    assert entry_with_exit(["start"]) in (0, None)
    assert calls == [("restart", "kaptyorka-bridge")]


def test_start_manager_flag_on_running_hub_unblocks(monkeypatch, home, capsys):
    from kaptyorka.manager import ManagerStore
    from kaptyorka.store import Store

    calls = _units(monkeypatch, hub=True, bridge=True)
    _hub_json()
    s = Store(paths.home() / "hub.db", legacy_token=cli._token())
    mm = ManagerStore(s)
    mm.set_blocked("trust")
    assert mm.blocked()
    s.close()
    assert entry_with_exit(["start", "--manager"]) in (0, None)
    assert calls == [] and "already running" in capsys.readouterr().out
    s = Store(paths.home() / "hub.db", legacy_token=cli._token())
    assert not ManagerStore(s).blocked()
    s.close()


def test_start_on_a_member_machine_refused(monkeypatch, home, capsys):
    calls = _units(monkeypatch, bridge=False)
    paths.save_config({"hub": "http://other:1", "token": "t", "machine": "pc"})
    assert entry_with_exit(["start"]) == 1
    assert calls == []
    assert capsys.readouterr().err.strip() == (
        "this machine is joined to http://other:1; it does not run a hub. "
        "To turn its bridge back on: kaptyorka join")


def test_start_new_port_moves_this_machine(monkeypatch, home, capsys):
    calls = _units(monkeypatch, hub=True, bridge=True)
    _hub_json(port=8765)
    paths.save_config({"hub": "http://10.0.0.1:8765", "token": "t", "machine": "pc"})
    assert entry_with_exit(["start", "--port", "9000"]) in (0, None)
    assert paths.load_config() == {"hub": "http://10.0.0.1:9000", "token": "t", "machine": "pc"}
    assert ("install", "kaptyorka-hub") in calls and ("restart", "kaptyorka-bridge") in calls
    assert capsys.readouterr().out.splitlines()[0] == "hub restarted: http://10.0.0.1:9000"


def test_first_start_output(monkeypatch, home, capsys):
    calls = _units(monkeypatch)
    assert entry_with_exit(["start"]) in (0, None)
    out, err = capsys.readouterr()
    lines = out.splitlines()
    assert lines[0] == "hub running: http://10.0.0.1:8765"
    assert lines[1].startswith("open the page: http://10.0.0.1:8765/#t=") and "keep this link private" in lines[1]
    assert lines[2] == "invite a teammate: kaptyorka invite <name>"
    assert lines[3] == cli.MANAGER_NOTE
    assert "installed" not in out + err and "join --hub" not in out
    assert calls == [("install", "kaptyorka-hub")]


def test_restart_output_is_one_line(monkeypatch, home, capsys):
    _units(monkeypatch, hub=True, bridge=True)
    _hub_json(port=8765)
    assert entry_with_exit(["start", "--port", "9001"]) in (0, None)
    out = capsys.readouterr().out.splitlines()
    assert out[0] == "hub restarted: http://10.0.0.1:9001" and len(out) == 2
    assert "MANAGER" not in "".join(out) and "manager:" not in "".join(out)


def test_stop_already_stopped(monkeypatch, capsys):
    calls = _units(monkeypatch, hub=False, bridge=False)
    assert entry_with_exit(["stop"]) in (0, None)
    assert capsys.readouterr().out.splitlines() == ["bridge already stopped", "hub already stopped"]
    assert calls == []


def test_stop_hub_machine_warns_the_team(monkeypatch, capsys):
    calls = _units(monkeypatch, hub=True, bridge=True)
    assert entry_with_exit(["stop"]) in (0, None)
    assert capsys.readouterr().out.splitlines() == [
        "bridge stopped", "hub stopped", "teammates cannot reach the hub until: kaptyorka start"]
    assert calls == [("stop", "kaptyorka-bridge"), ("stop", "kaptyorka-hub")]


def test_stop_member_machine(monkeypatch, capsys):
    _units(monkeypatch, bridge=True)  # no hub unit here
    assert entry_with_exit(["stop"]) in (0, None)
    assert capsys.readouterr().out.splitlines() == ["bridge stopped"]


def _pipx(monkeypatch, tmp_path, new_version):
    venv = tmp_path / "venv"
    venv.mkdir()
    (venv / "pipx_metadata.json").write_text("{}")
    monkeypatch.setattr(sys, "prefix", str(venv))

    def run(cmd, **kw):
        out = f"kaptyorka {new_version}\n" if cmd[-1] == "--version" else ""
        return subprocess.CompletedProcess(cmd, 0, out, "")
    return run


def test_update_already_latest(monkeypatch, tmp_path, capsys):
    calls = _units(monkeypatch, hub=True, bridge=True)
    assert cli._update(None, run=_pipx(monkeypatch, tmp_path, cli.__version__)) == 0
    assert capsys.readouterr().out.strip() == f"kaptyorka {cli.__version__} is already the latest"
    assert calls == []


def test_update_on_a_member_machine_is_silent_about_the_hub(monkeypatch, tmp_path, capsys):
    calls = _units(monkeypatch, bridge=True)
    assert cli._update(None, run=_pipx(monkeypatch, tmp_path, "99.0.0")) == 0
    out = capsys.readouterr().out
    assert out.splitlines()[0] == f"kaptyorka {cli.__version__} -> 99.0.0"
    assert "hub" not in out and "not installed" not in out
