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


def test_hook_start_prints_brief(hub, home, monkeypatch, capsys):
    join(hub.url)
    monkeypatch.setattr(hook, "identify", lambda machine, workspace=None:
                        Identity("claude:1", machine, "claude-code", "murmur"))
    assert hook.main("claude-code", "start") == 0
    assert capsys.readouterr().out.startswith("Kaptyorka: you are `")


def test_hook_prompt_with_nothing_unread_prints_nothing(hub, home, monkeypatch, capsys):
    join(hub.url)
    monkeypatch.setattr(hook, "identify", lambda machine, workspace=None:
                        Identity("claude:1", machine, "claude-code", "murmur"))
    assert hook.main("claude-code", "prompt") == 0
    assert capsys.readouterr().out == ""


def test_hook_hub_down_is_silent(home, monkeypatch, capsys):
    join(f"http://127.0.0.1:{closed_port()}")
    monkeypatch.setattr(hook, "identify", lambda machine, workspace=None:
                        Identity("claude:1", machine, "claude-code", "murmur"))
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
                            Identity("claude:1", machine, "claude-code", "murmur"))
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
