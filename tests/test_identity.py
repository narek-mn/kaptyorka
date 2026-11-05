import json
import os

from kaptyorka import identity
from kaptyorka.identity import Identity, ancestors, identify
from helpers import write_session_file


def test_identify_claude_from_session_file(home, monkeypatch):
    write_session_file(home, pid=4242, cwd="/w/murmur")
    monkeypatch.setattr(identity, "ancestors", lambda pid: [(100, "sh"), (4242, "claude")])
    assert identify("pc", pid=1) == Identity("claude:sid-4242", "pc", "claude-code", "murmur")


def test_identify_uses_resolver(home, monkeypatch):
    monkeypatch.setattr(identity, "ancestors", lambda pid: [(7, "node"), (9, "opencode")])
    monkeypatch.setattr(identity, "proc_cwd", lambda pid: "/w/nav" if pid == 9 else "/x")
    monkeypatch.setitem(identity.RESOLVERS, "opencode",
                        lambda pid, cwd: "opencode:S1" if (pid, cwd) == (9, "/w/nav") else None)
    assert identify("pc", pid=1) == Identity("opencode:S1", "pc", "opencode", "nav")


def test_identify_cursor_workspace(home, monkeypatch):
    monkeypatch.setattr(identity, "ancestors", lambda pid: [(5, "Cursor")])
    assert identify("pc", pid=1, workspace="/w/app") == Identity("cursor:/w/app", "pc", "cursor", "app")


def test_identify_cursor_defaults_to_own_cwd(home, monkeypatch, tmp_path):
    monkeypatch.setattr(identity, "ancestors", lambda pid: [(5, "cursor-agent")])
    monkeypatch.chdir(tmp_path)
    assert identify("pc", pid=1) == Identity(f"cursor:{tmp_path}", "pc", "cursor", tmp_path.name)


def test_identify_none_outside_a_tool(home, monkeypatch):
    monkeypatch.setattr(identity, "ancestors", lambda pid: [(1, "systemd")])
    assert identify("pc", pid=1) is None


def test_identify_test_override(monkeypatch):
    ident = {"key": "k", "machine": "m", "tool": "claude-code", "project": "p"}
    monkeypatch.setenv("KAPTYORKA_TEST_IDENTITY", json.dumps(ident))
    assert identify("pc") == Identity(**ident)


def test_headers_are_percent_encoded_ascii():
    assert Identity("cursor:/w/мой app", "m", "t", "p").headers() == {
        "X-Kaptyorka-Key": "cursor%3A%2Fw%2F%D0%BC%D0%BE%D0%B9%20app", "X-Kaptyorka-Machine": "m",
        "X-Kaptyorka-Tool": "t", "X-Kaptyorka-Project": "p"}


def test_identify_corrupt_session_file_is_none(home, monkeypatch):
    d = home / ".claude" / "sessions"
    d.mkdir(parents=True)
    (d / "4242.json").write_text('{"pid": 42')  # half written
    monkeypatch.setattr(identity, "ancestors", lambda pid: [(4242, "claude")])
    assert identify("pc", pid=1) is None


def test_identify_proc_gone_is_none(home, monkeypatch):
    def gone(pid):
        raise FileNotFoundError(pid)
    monkeypatch.setattr(identity, "ancestors", lambda pid: [(9, "opencode")])
    monkeypatch.setattr(identity, "proc_cwd", gone)
    monkeypatch.setitem(identity.RESOLVERS, "opencode", lambda pid, cwd: "opencode:S1")
    assert identify("pc", pid=1) is None


def test_identify_claude_without_session_file_stops_the_walk(home, monkeypatch):
    monkeypatch.setattr(identity, "ancestors", lambda pid: [(4242, "claude"), (5, "Cursor")])
    assert identify("pc", pid=1, workspace="/w/app") is None


def test_identify_empty_project_is_none(home, monkeypatch):
    monkeypatch.setattr(identity, "ancestors", lambda pid: [(5, "Cursor")])
    assert identify("pc", pid=1, workspace="/") is None


def test_ancestors_real_process():
    assert ancestors(os.getpid())[0][0] == os.getppid()


def test_proc_cwd_real_process():
    assert identity.proc_cwd(os.getpid()) == os.getcwd()
