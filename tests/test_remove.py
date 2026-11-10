import json
import os
import subprocess
import sys

import pytest

from kaptyorka import paths, remove
from kaptyorka.adapters.claude import ClaudeAdapter
from kaptyorka.adapters.codex import CodexAdapter
from kaptyorka.adapters.cursor import CursorAdapter
from kaptyorka.adapters.opencode import OpenCodeAdapter
from test_claude import fake_claude_cli  # noqa: F401


def fake_run(calls):
    def run(cmd, **kw):
        calls.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, "", "")
    return run


def test_claude_unconfigure_removes_only_ours(home, fake_claude_cli):
    settings = home / ".claude/settings.json"
    settings.parent.mkdir()
    a = ClaudeAdapter(home, run=fake_claude_cli.run)
    a.configure("/opt/x/kaptyorka")
    data = json.loads(settings.read_text())
    data["hooks"]["SessionStart"].append({"hooks": [{"type": "command", "command": "mine"}]})
    data["hooks"]["SessionStart"][0]["hooks"].append({"type": "command", "command": "also mine"})
    settings.write_text(json.dumps(data))
    lines = a.unconfigure()
    left = json.loads(settings.read_text())
    assert left["hooks"] == {"SessionStart": [{"hooks": [{"type": "command", "command": "also mine"}]},
                                              {"hooks": [{"type": "command", "command": "mine"}]}]}
    assert ["claude", "mcp", "remove", "--scope", "user", "kaptyorka"] in fake_claude_cli.calls[2:]
    assert any("hooks removed" in l for l in lines) and any("MCP server removed" in l for l in lines)


def test_claude_unconfigure_drops_empty_hooks_and_ignores_broken(home, fake_claude_cli):
    a = ClaudeAdapter(home, run=fake_claude_cli.run)
    (home / ".claude").mkdir()
    a.configure("/opt/x/kaptyorka")
    a.unconfigure()
    assert json.loads((home / ".claude/settings.json").read_text()) == {}
    (home / ".claude/settings.json").write_text("{not json")
    a.unconfigure()
    assert (home / ".claude/settings.json").read_text() == "{not json"


def test_claude_unconfigure_follows_symlink(home, tmp_path, fake_claude_cli):
    real = tmp_path / "setup/settings.json"
    real.parent.mkdir()
    real.write_text("{}")
    (home / ".claude").mkdir()
    (home / ".claude/settings.json").symlink_to(real)
    a = ClaudeAdapter(home, run=fake_claude_cli.run)
    a.configure("/opt/x/kaptyorka")
    a.unconfigure()
    assert (home / ".claude/settings.json").is_symlink() and json.loads(real.read_text()) == {}


def test_cursor_unconfigure_keeps_foreign(home):
    d = home / ".cursor"
    d.mkdir()
    (d / "mcp.json").write_text(json.dumps({"mcpServers": {"other": {"command": "x"}}}))
    (d / "hooks.json").write_text(json.dumps({"version": 1, "hooks": {"stop": [{"command": "mine"}]}}))
    a = CursorAdapter(home)
    a.configure("/opt/x/kaptyorka")
    a.unconfigure()
    assert json.loads((d / "mcp.json").read_text()) == {"mcpServers": {"other": {"command": "x"}}}
    assert json.loads((d / "hooks.json").read_text()) == {"version": 1, "hooks": {"stop": [{"command": "mine"}]}}
    assert a.unconfigure() == []


def test_opencode_unconfigure(home, monkeypatch):
    monkeypatch.setenv("SHELL", "/bin/bash")
    conf = home / ".config/opencode"
    conf.mkdir(parents=True)
    (conf / "opencode.json").write_text(json.dumps({"mcp": {"other": {}}, "theme": "x"}))
    (home / ".bashrc").write_text("export A=1\n")
    a = OpenCodeAdapter(home=home)
    a.configure("/opt/x/kaptyorka")
    lines = a.unconfigure()
    assert json.loads((conf / "opencode.json").read_text()) == {"mcp": {"other": {}}, "theme": "x"}
    assert not (conf / "plugins/kaptyorka.js").exists()
    assert (home / ".bashrc").read_text() == "export A=1\n"
    assert len(lines) == 3


def test_codex_unconfigure_removes_block_hooks_and_alias(home, monkeypatch):
    monkeypatch.setenv("SHELL", "/bin/zsh")
    monkeypatch.setenv("CODEX_HOME", str(home / ".codex"))
    conf = home / ".codex"
    conf.mkdir()
    (conf / "config.toml").write_text('model = "x"\n')
    (conf / "hooks.json").write_text(json.dumps(
        {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "mine"}]}]}}))
    a = CodexAdapter(home=home, socket_path=conf / "none.sock")
    a.configure("/opt/x/kaptyorka")
    assert "kaptyorka" in (conf / "config.toml").read_text()
    a.unconfigure()
    assert (conf / "config.toml").read_text().strip() == 'model = "x"'
    assert json.loads((conf / "hooks.json").read_text()) == {
        "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "mine"}]}]}}
    assert "codex" not in (home / ".zshrc").read_text()


class Spy:
    tool = "spy"

    def __init__(self, order):
        self.order = order

    def unconfigure(self):
        self.order.append("unconfigure")
        return ["spy: undone"]


def test_remove_everything_and_pipx_last(home, tmp_path, monkeypatch, capsys):
    unit = home / ".config/systemd/user/kaptyorka-bridge.service"
    unit.parent.mkdir(parents=True)
    unit.write_text("x")
    paths.save_config({"hub": "h", "token": "t"})
    venv = tmp_path / "venv"
    venv.mkdir()
    (venv / "pipx_metadata.json").write_text("{}")
    monkeypatch.setattr(sys, "prefix", str(venv))
    calls = []
    assert remove.main([Spy(calls)], run=fake_run(calls), platform="linux") == 0
    assert not unit.exists() and not (home / ".kaptyorka").exists()
    assert calls[0][:3] == ["systemctl", "--user", "disable"] and calls[-1] == ["pipx", "uninstall", "kaptyorka"]
    assert "unconfigure" in calls
    out = capsys.readouterr().out
    assert "kaptyorka-bridge: service removed" in out and "spy: undone" in out and "uninstalled" in out


def test_leave_undoes_join_and_keeps_hub_and_install(home, tmp_path, monkeypatch, capsys):
    units = home / ".config/systemd/user"
    units.mkdir(parents=True)
    (units / "kaptyorka-bridge.service").write_text("x")
    (units / "kaptyorka-hub.service").write_text("x")
    paths.save_config({"hub": "h", "token": "t"})
    (paths.home() / "hub-token").write_text("t\n")
    venv = tmp_path / "venv"
    venv.mkdir()
    (venv / "pipx_metadata.json").write_text("{}")
    monkeypatch.setattr(sys, "prefix", str(venv))
    calls = []
    assert remove.leave([Spy(calls)], run=fake_run(calls), platform="linux") == 0
    assert not (units / "kaptyorka-bridge.service").exists()
    assert (units / "kaptyorka-hub.service").exists() and (paths.home() / "hub-token").exists()
    assert paths.load_config() == {"hub": "h", "token": "t"}  # plain `join` brings it back
    assert "unconfigure" in calls and not any(c[0] == "pipx" for c in calls if isinstance(c, list))
    assert not any("kaptyorka-hub" in " ".join(c) for c in calls if isinstance(c, list))
    out = capsys.readouterr().out
    assert "spy: undone" in out and "kaptyorka join brings this machine back" in out


def test_leave_removes_team_skills(home, capsys):
    from kaptyorka import skills_sync

    skills_sync.sync(home, "v1", [{"name": "a", "text": "A"}], print)
    mine = home / ".claude/skills/team-mine/SKILL.md"
    mine.parent.mkdir(parents=True)
    mine.write_text("mine")
    assert remove.leave([], run=fake_run([]), platform="linux") == 0
    assert not (home / ".claude/skills/team-a").exists() and mine.exists()
    assert "removed team skill" in capsys.readouterr().out


def test_remove_removes_team_skills(home, tmp_path, monkeypatch, capsys):
    from kaptyorka import skills_sync

    monkeypatch.setattr(sys, "prefix", str(tmp_path))
    skills_sync.sync(home, "v1", [{"name": "a", "text": "A"}], print)
    assert remove.main([], run=fake_run([]), platform="linux") == 0
    assert not (home / ".claude/skills/team-a").exists() and not (home / ".kaptyorka").exists()
    assert "removed team skill" in capsys.readouterr().out


@pytest.mark.parametrize("which", ["leave", "main"])
def test_skill_removal_failure_does_not_crash(home, tmp_path, monkeypatch, capsys, which):
    from kaptyorka import skills_sync

    def boom(h, log):
        raise PermissionError("busy")
    monkeypatch.setattr(sys, "prefix", str(tmp_path))
    monkeypatch.setattr(skills_sync, "remove_all", boom)
    assert getattr(remove, which)([], run=fake_run([]), platform="linux") == 1
    out = capsys.readouterr()
    assert "team skills: failed: busy" in out.err
    assert "kaptyorka join brings this machine back" in out.out or "removed " in out.out


def test_remove_without_pipx_does_not_call_it(home, tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "prefix", str(tmp_path))
    calls = []
    assert remove.main([], run=fake_run(calls), platform="linux") == 0
    assert ["pipx", "uninstall", "kaptyorka"] not in calls


def test_remove_darwin_deletes_plist(home, monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "prefix", str(tmp_path))
    plist = home / "Library/LaunchAgents/io.kaptyorka.kaptyorka-hub.plist"
    plist.parent.mkdir(parents=True)
    plist.write_text("x")
    calls = []
    remove.main([], run=fake_run(calls), platform="darwin")
    assert not plist.exists()
    assert calls == [["launchctl", "bootout", f"gui/{os.getuid()}/io.kaptyorka.kaptyorka-hub"]]


def test_remove_on_hub_asks_and_stops_unless_y(home, monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(sys, "prefix", str(tmp_path))
    (paths.home() / "hub-token").write_text("T")
    assert remove.main([], run=fake_run([]), isatty=True, ask=lambda q: "n") == 1
    assert (paths.home() / "hub-token").exists()
    assert remove.main([], run=fake_run([]), isatty=True, ask=lambda q: "y") == 0
    assert not (home / ".kaptyorka").exists()


def test_remove_on_hub_refuses_without_terminal(home, capsys):
    (paths.home() / "hub-token").write_text("T")
    assert remove.main([], run=fake_run([]), isatty=False) == 1
    assert "run kaptyorka remove in a terminal to confirm" in capsys.readouterr().err
    assert (paths.home() / "hub-token").exists()


def test_remove_pipx_failure_exits_1(home, tmp_path, monkeypatch):
    (tmp_path / "pipx_metadata.json").write_text("{}")
    monkeypatch.setattr(sys, "prefix", str(tmp_path))
    run = lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, "", "nope")
    assert remove.main([], run=run, platform="linux") == 1


def test_hook_match_is_precise():
    from kaptyorka.adapters.base import is_our_hook
    ev = ("start", "prompt")
    assert is_our_hook("/a/kaptyorka hook claude-code start", "claude-code", ev)
    assert is_our_hook("'/my dir/kaptyorka' hook claude-code prompt", "claude-code", ev)
    assert is_our_hook("/my dir/kaptyorka hook claude-code prompt", "claude-code", ev)  # older, unquoted
    assert not is_our_hook("/usr/bin/other hook claude-code start", "claude-code", ev)
    assert is_our_hook("[ -x ~/.local/bin/kaptyorka ] && ~/.local/bin/kaptyorka hook claude-code start || true", "claude-code", ev)
    assert not is_our_hook("mykaptyorka hook claude-code start", "claude-code", ev)
    assert not is_our_hook("/a/kaptyorka hook claude-code stop", "claude-code", ev)
    assert not is_our_hook(None, "claude-code", ev)


def test_claude_unconfigure_keeps_lookalike_hook(home, fake_claude_cli):
    settings = home / ".claude/settings.json"
    settings.parent.mkdir()
    foreign = {"hooks": [{"type": "command", "command": "/usr/bin/other hook claude-code start"}]}
    settings.write_text(json.dumps({"hooks": {"SessionStart": [foreign]}}))
    ClaudeAdapter(home, run=fake_claude_cli.run).unconfigure()
    assert json.loads(settings.read_text()) == {"hooks": {"SessionStart": [foreign]}}


def test_claude_unconfigure_without_claude_still_drops_hooks(home, monkeypatch):
    monkeypatch.setenv("PATH", "")
    settings = home / ".claude/settings.json"
    settings.parent.mkdir()
    settings.write_text(json.dumps({"hooks": {"SessionStart": [
        {"hooks": [{"type": "command", "command": "/a/kaptyorka hook claude-code start"}]}]}}))
    log = []
    lines = ClaudeAdapter(home, log=log.append, run=fake_run(calls := [])).unconfigure()
    assert calls == [] and any("claude mcp remove --scope user kaptyorka" in l for l in log)
    assert json.loads(settings.read_text()) == {} and any("hooks removed" in l for l in lines)


def test_remove_on_hub_end_of_input_is_no(home, monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "prefix", str(tmp_path))
    (paths.home() / "hub-token").write_text("T")

    def eof(q):
        raise EOFError
    assert remove.main([], run=fake_run([]), isatty=True, ask=eof) == 1
    assert (paths.home() / "hub-token").exists()


def test_remove_pipx_missing_says_what_to_run(home, tmp_path, monkeypatch, capsys):
    (tmp_path / "pipx_metadata.json").write_text("{}")
    monkeypatch.setattr(sys, "prefix", str(tmp_path))

    def run(cmd, **kw):
        if cmd[0] == "pipx":
            raise FileNotFoundError(cmd[0])
        return subprocess.CompletedProcess(cmd, 0, "", "")
    assert remove.main([], run=run, platform="linux") == 1
    assert "pipx uninstall kaptyorka" in capsys.readouterr().err
