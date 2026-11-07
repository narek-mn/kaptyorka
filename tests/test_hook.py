import json
import socket
import time

from kaptyorka import hook, paths
from kaptyorka.identity import Identity


def join(hub_url):
    paths.config_path().write_text(json.dumps({"hub": hub_url, "token": "T", "machine": "pc"}))


def closed_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_hook_start_uses_the_new_brief(hub, home, monkeypatch, capsys):
    join(hub.url)
    monkeypatch.setattr(hook, "identify", lambda machine, workspace=None:
                        Identity("claude:1", machine, "claude-code", "proj-a"))
    assert hook.main("claude-code", "start") == 0
    assert capsys.readouterr().out.startswith("You are `pc/claude-code/proj-a`")


def test_hook_prompt_with_nothing_unread_prints_nothing(hub, home, monkeypatch, capsys):
    join(hub.url)
    monkeypatch.setattr(hook, "identify", lambda machine, workspace=None:
                        Identity("claude:1", machine, "claude-code", "proj-a"))
    assert hook.main("claude-code", "prompt") == 0
    assert capsys.readouterr().out == ""


def test_hook_hub_down_is_silent(home, monkeypatch, capsys):
    join(f"http://127.0.0.1:{closed_port()}")
    monkeypatch.setattr(hook, "identify", lambda machine, workspace=None:
                        Identity("claude:1", machine, "claude-code", "proj-a"))
    t = time.monotonic()
    assert hook.main("claude-code", "start") == 0
    assert time.monotonic() - t < 3.5
    assert capsys.readouterr() == ("", "")


def test_hook_not_joined_is_silent(home, capsys):
    assert hook.main("claude-code", "start") == 0
    assert capsys.readouterr() == ("", "")


def test_hook_cursor_workspace_from_stdin(home, monkeypatch):
    join("http://127.0.0.1:1")
    seen = []
    monkeypatch.setitem(hook.FORMATTERS, "cursor", lambda event, brief: brief)
    monkeypatch.setattr(hook, "identify", lambda machine, workspace=None: seen.append(workspace))
    hook.main("cursor", "start", json.dumps({"workspace_roots": ["/w/app", "/w/b"]}))
    assert seen == ["/w/app"]


def test_hook_hub_hanging_gives_up_within_budget(home, monkeypatch, capsys):
    with socket.socket() as s:  # accepts the TCP connection, never answers
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        join(f"http://127.0.0.1:{s.getsockname()[1]}")
        monkeypatch.setattr(hook, "identify", lambda machine, workspace=None:
                            Identity("claude:1", machine, "claude-code", "proj-a"))
        t = time.monotonic()
        assert hook.main("claude-code", "start") == 0
        assert time.monotonic() - t < 3.5
    assert capsys.readouterr() == ("", "")


def test_cli_hook_exits_0_silently_when_not_joined(home, monkeypatch, capsys):
    import io
    import sys

    import pytest

    from kaptyorka import cli

    monkeypatch.setattr(sys, "argv", ["kaptyorka", "hook", "claude-code", "start"])
    monkeypatch.setattr(sys, "stdin", io.StringIO('{"session_id": "x"}'))
    with pytest.raises(SystemExit) as e:
        cli.entry()
    assert e.value.code == 0
    assert capsys.readouterr() == ("", "")


def test_hook_codex_session_from_stdin(home, monkeypatch):
    join("http://127.0.0.1:1")
    seen = []
    monkeypatch.setattr(hook, "identify", lambda *a, **kw: pytest.fail("no lookup needed"))
    monkeypatch.setattr(hook.http, "request", lambda method, url, token, headers, timeout:
                        seen.append(headers) or (200, "B"))
    hook.main("codex", "start", json.dumps({"session_id": "th-1", "cwd": "/w/app/",
                                            "hook_event_name": "SessionStart"}))
    assert seen == [Identity("codex:th-1", "pc", "codex", "app").headers()]


# ---- the stop hook ----

def _reg(hub):
    hub.store.project_register("proj-a", by="admin")


def _signal(hub, key, tool="claude-code"):
    from helpers import call, run

    run(call(hub, {"key": key, "machine": "pc", "tool": tool, "project": "proj-a"}, "status", text="done"))


def _stdin(sid, **kw):
    return json.dumps({"session_id": sid, "cwd": "/w/proj-a", **kw})


def _no_identify(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("identify must not be called")
    monkeypatch.setattr(hook, "identify", boom)


def test_stop_claude_blocks_with_reason_from_stdin_session(hub, home, monkeypatch, capsys):
    join(hub.url)
    _reg(hub)
    _no_identify(monkeypatch)
    _signal(hub, "claude:s1")
    assert hook.main("claude-code", "stop", _stdin("s1", stop_hook_active=False)) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["decision"] == "block" and out["reason"].startswith("You did work in proj-a (done)")
    hook.main("claude-code", "stop", _stdin("s1"))  # blocks once only
    assert capsys.readouterr().out == ""


def test_stop_claude_ok_is_silent_and_stop_hook_active_never_blocks(hub, home, monkeypatch, capsys):
    join(hub.url)
    _reg(hub)
    _no_identify(monkeypatch)
    hook.main("claude-code", "stop", _stdin("s2"))
    assert capsys.readouterr().out == ""
    _signal(hub, "claude:s3")
    hook.main("claude-code", "stop", _stdin("s3", stop_hook_active=True))
    assert capsys.readouterr().out == ""
    hook.main("claude-code", "stop", _stdin("s3"))  # the active call did not use the one block
    assert json.loads(capsys.readouterr().out)["decision"] == "block"


def test_stop_codex_prints_json_always(hub, home, monkeypatch, capsys):
    join(hub.url)
    _reg(hub)
    hook.main("codex", "stop", _stdin("t1"))
    assert json.loads(capsys.readouterr().out) == {}
    _signal(hub, "codex:t2", "codex")
    hook.main("codex", "stop", _stdin("t2", stop_hook_active=True))
    assert json.loads(capsys.readouterr().out) == {}
    hook.main("codex", "stop", _stdin("t2"))
    out = json.loads(capsys.readouterr().out)
    assert out["decision"] == "block" and "state note" in out["reason"]


def test_stop_cursor_followup_is_reason_then_brief(hub, home, monkeypatch, capsys):
    join(hub.url)
    _reg(hub)
    key = "cursor:pc:/w/proj-a"
    monkeypatch.setattr(hook, "identify", lambda machine, workspace=None:
                        Identity(key, machine, "cursor", "proj-a"))
    hook.main("cursor", "stop", json.dumps({}))
    assert json.loads(capsys.readouterr().out) == {}
    _signal(hub, key, "cursor")
    _signal(hub, "codex:x1", "codex")  # a known sender
    hub.store.send("codex:x1", "pc/cursor/proj-a", "hello cursor")
    hook.main("cursor", "stop", json.dumps({}))
    followup = json.loads(capsys.readouterr().out)["followup_message"]
    reason, _, brief = followup.partition("\n\n")
    assert reason.startswith("You did work in proj-a (done)")
    assert "hello cursor" in brief


def test_opencode_prompt_brief_carries_the_reminder_without_blocking(hub, home, monkeypatch, capsys):
    join(hub.url)
    _reg(hub)
    ident = Identity("opencode:o1", "pc", "opencode", "proj-a")
    monkeypatch.setattr(hook, "identify", lambda machine, workspace=None: ident)
    hook.main("opencode", "prompt")
    assert capsys.readouterr().out == ""
    _signal(hub, "opencode:o1", "opencode")
    for _ in range(2):  # a reminder repeats until the note is written
        hook.main("opencode", "prompt")
        assert capsys.readouterr().out.startswith("You did work in proj-a (done)")
    with hub.store.lock:
        assert hub.store.db.execute("SELECT blocked_at FROM done_checks WHERE key='opencode:o1'"
                                    ).fetchone()[0] is None


def test_stop_hub_down_codex_prints_empty_json_claude_nothing(home, monkeypatch, capsys):
    join("http://127.0.0.1:1")
    assert hook.main("codex", "stop", _stdin("t1")) == 0
    assert capsys.readouterr().out.strip() == "{}"
    assert hook.main("claude-code", "stop", _stdin("s1")) == 0
    assert capsys.readouterr().out == ""
    assert hook.main("codex", "prompt", _stdin("t1")) == 0
    assert capsys.readouterr().out == ""


def test_stop_codex_not_joined_prints_empty_json(home, capsys):
    assert hook.main("codex", "stop", _stdin("t1")) == 0
    assert capsys.readouterr().out.strip() == "{}"


def test_stop_sends_the_folders_git_read_now(home, monkeypatch, capsys):
    import subprocess
    from types import SimpleNamespace
    from urllib.parse import parse_qs, urlsplit

    join("http://127.0.0.1:1")
    gets, runs = [], []

    def fake_request(method, url, token, headers=None, timeout=None):
        gets.append((url, timeout))
        return 200, {"decision": "ok"}

    def fake_run(args, **kw):
        runs.append((args, kw))
        out = {"rev-parse": "main\n", "status": " M a.py\n", "log": "fix the bug\n"}[args[3]]
        return SimpleNamespace(returncode=0, stdout=out)

    monkeypatch.setattr(hook.http, "request", fake_request)
    for tool in ("claude-code", "codex"):
        gets.clear()
        runs.clear()
        hook.main(tool, "stop", _stdin("s9"), run=fake_run)
        url, _ = gets[0]
        assert urlsplit(url).path == "/api/done-check"
        assert parse_qs(urlsplit(url).query) == {"cwd": ["/w/proj-a"], "branch": ["main"],
                                                 "subject": ["fix the bug"], "dirty": ["1"]}
        assert {a[2] for a, _ in runs} == {"/w/proj-a"}
        assert all(kw["env"]["GIT_OPTIONAL_LOCKS"] == "0" and kw["timeout"] <= hook.GIT_S
                   for _, kw in runs)
    capsys.readouterr()

    def no_repo(args, **kw):
        return SimpleNamespace(returncode=128, stdout="")

    gets.clear()
    hook.main("claude-code", "stop", _stdin("s9"), run=no_repo)
    assert gets[0][0].endswith("/api/done-check")  # not a repo: no parameters

    def slow(args, **kw):
        raise subprocess.TimeoutExpired(args, kw["timeout"])

    gets.clear()
    hook.main("codex", "stop", _stdin("t9"), run=slow)  # git too slow: the check still runs
    assert gets[0][0].endswith("/api/done-check")
    monkeypatch.setattr(hook, "identify", lambda machine, workspace=None:
                        Identity("cursor:pc:/w/proj-a", machine, "cursor", "proj-a"))
    gets.clear()
    runs.clear()
    hook.main("cursor", "stop", json.dumps({}), run=fake_run)  # Cursor's stop reads no git
    assert gets[0][0].endswith("/api/done-check") and runs == []


def _manager_config(hub):
    """The manager's account on the hub and its config file; (person id, token, folder)."""
    from kaptyorka import manager
    mid = hub.store.person_create("manager", "manager", "test")
    token = hub.store.device_add(mid, "manager", "test")
    (paths.home() / manager.CONFIG).write_text(json.dumps(
        {"hub": hub.url, "token": token, "machine": "manager@box-1", "person": "manager"}))
    folder = paths.home() / manager.DIR
    folder.mkdir()
    return mid, token, folder


def _tokens(monkeypatch):
    used = []
    real = hook.http.request

    def spy(method, url, token, *a, **kw):
        used.append(token)
        return real(method, url, token, *a, **kw)
    monkeypatch.setattr(hook.http, "request", spy)
    return used


def test_hook_in_the_manager_folder_uses_the_manager_config(hub, home, monkeypatch, capsys):
    join(hub.url)
    mid, token, folder = _manager_config(hub)
    monkeypatch.setattr(hook, "identify", lambda machine, workspace=None:
                        Identity("claude:m", machine, "claude-code", "manager"))
    used = _tokens(monkeypatch)
    hook.main("claude-code", "start", json.dumps({"cwd": str(folder)}))
    assert used and set(used) == {token}
    assert hub.store.agent("claude:m").person_id == mid
    assert hub.store.agent("claude:m").machine == "manager@box-1"
    used.clear()
    hook.main("claude-code", "start", json.dumps({"cwd": str(home / "proj-a")}))
    assert used and set(used) == {"T"}  # any other folder: the normal config
    used.clear()
    monkeypatch.chdir(folder)  # no cwd in the input: the hook's own folder
    hook.main("claude-code", "start", "")
    assert set(used) == {token}


def test_hook_manager_folder_without_its_config_uses_the_normal_one(hub, home, monkeypatch):
    from kaptyorka import manager
    join(hub.url)
    folder = paths.home() / manager.DIR
    folder.mkdir()
    monkeypatch.setattr(hook, "identify", lambda machine, workspace=None:
                        Identity("claude:m", machine, "claude-code", "manager"))
    used = _tokens(monkeypatch)
    hook.main("claude-code", "start", json.dumps({"cwd": str(folder)}))
    assert used and set(used) == {"T"}
