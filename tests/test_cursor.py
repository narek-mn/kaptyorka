import json
import os

import pytest

from kaptyorka import hook, http, identity, paths
from kaptyorka.adapters.base import DeliveryError
from kaptyorka.adapters.cursor import CursorAdapter
from kaptyorka.hook import FORMATTERS


def test_cursor_is_pull_only():
    a = CursorAdapter()
    assert a.sessions() == []
    with pytest.raises(DeliveryError, match="pull only"):
        a.deliver("cursor:/w", "x")


def test_installed_when_dir_exists(home, monkeypatch):
    monkeypatch.setenv("PATH", "")
    assert not CursorAdapter(home).installed()
    (home / ".cursor").mkdir()
    assert CursorAdapter(home).installed()


def test_formatter_keys():
    f = FORMATTERS["cursor"]
    assert json.loads(f("stop", "msg")) == {"followup_message": "msg"}
    assert json.loads(f("tool", "msg")) == {"additional_context": "msg"}
    assert json.loads(f("tool", "")) == {}
    assert json.loads(f("stop", "")) == {}


def test_configure_merges_mcp_and_hooks(home):
    d = home / ".cursor"
    d.mkdir()
    (d / "mcp.json").write_text(json.dumps({"mcpServers": {"other": {"command": "x"}}}))
    (d / "hooks.json").write_text(json.dumps(
        {"version": 1, "hooks": {"stop": [{"command": "other stop"}]}}))
    a = CursorAdapter(home)
    assert len(a.configure("/bin/kap")) == 2
    mcp = json.loads((d / "mcp.json").read_text())
    assert mcp["mcpServers"]["other"] == {"command": "x"}
    assert mcp["mcpServers"]["kaptyorka"] == {"type": "stdio", "command": "/bin/kap", "args": ["mcp"]}
    hooks = json.loads((d / "hooks.json").read_text())
    assert hooks["version"] == 1
    assert hooks["hooks"]["stop"] == [
        {"command": "other stop"},
        {"command": "/bin/kap hook cursor stop", "loop_limit": 5}]
    assert hooks["hooks"]["postToolUse"] == [{"command": "/bin/kap hook cursor tool"}]
    before = (d / "mcp.json").read_text(), (d / "hooks.json").read_text()
    assert a.configure("/bin/kap") == []
    assert before == ((d / "mcp.json").read_text(), (d / "hooks.json").read_text())


def test_configure_symlink_and_broken(home):
    d = home / ".cursor"
    d.mkdir()
    real = home / "real.json"
    real.write_text("{}")
    (d / "mcp.json").symlink_to(real)
    (d / "hooks.json").write_text("{nope")
    logs = []
    CursorAdapter(home, log=logs.append).configure("/bin/kap")
    assert (d / "mcp.json").is_symlink() and "kaptyorka" in real.read_text()
    assert (d / "hooks.json").read_text() == "{nope"
    assert logs == [f"left alone, not valid JSON: {d / 'hooks.json'}"]


def test_cursor_key_matches_shim_and_hook(home, monkeypatch, capsys):
    paths.save_config({"hub": "http://h", "token": "t", "machine": "pc"})
    monkeypatch.delenv("KAPTYORKA_TEST_IDENTITY", raising=False)
    monkeypatch.setattr(identity, "ancestors", lambda pid: [(5, "Cursor")])
    seen = {}

    def fake(method, url, token, headers=None, timeout=None):
        seen.update(headers)
        return 200, "hello"

    monkeypatch.setattr(http, "request", fake)
    assert hook.main("cursor", "stop", '{"workspace_roots":["/w/app"]}') == 0
    assert json.loads(capsys.readouterr().out) == {"followup_message": "hello"}
    ident = identity.identify("pc", workspace="/w/app")
    assert ident.key == "cursor:pc:/w/app"
    assert seen == ident.headers()


def test_hook_cursor_prints_empty_object_on_failure(home, capsys):
    assert hook.main("cursor", "stop", "") == 0  # not joined
    assert capsys.readouterr().out == "{}\n"


def test_hook_commands_quote_the_exe(tmp_path):
    CursorAdapter(tmp_path, log=print).configure("/my dir/kap")
    hooks = json.loads((tmp_path / ".cursor/hooks.json").read_text())
    assert hooks["hooks"]["postToolUse"] == [{"command": "'/my dir/kap' hook cursor tool"}]
