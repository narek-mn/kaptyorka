"""/hub on, /hub off and the hub indicator in every tool: OpenCode, Codex and Cursor beside
Claude Code. The choice is per session key, as the tools' identities are keyed."""
import json
import os
import shutil
import subprocess
import time

import pytest

from kaptyorka import bridge as bridge_mod
from kaptyorka import doctor, hook, http, offhub, paths
from kaptyorka.adapters.base import Session
from kaptyorka.adapters.claude import ClaudeAdapter
from kaptyorka.adapters.codex import SKILL, SKILL_MARK, CodexAdapter
from kaptyorka.adapters.cursor import CursorAdapter
from kaptyorka.adapters.cursor import SKILL as CURSOR_SKILL
from kaptyorka.adapters.opencode import COMMAND, OpenCodeAdapter
from kaptyorka.bridge import Bridge, install_commands
from kaptyorka.identity import Identity
from test_bridge import FakeRun
from test_privacy import team  # noqa: F401  (a fixture)

OC = {offhub.OPENCODE_SESSION: "ses_1"}


def key_env(key):
    return {offhub.SESSION_KEY: key}


# --- which session runs /hub ---------------------------------------------------------------

def test_session_key_comes_from_each_tool():
    assert offhub.session_key({"CLAUDE_CODE_SESSION_ID": "c1"}) == "claude:c1"
    assert offhub.session_key(OC) == "opencode:ses_1"  # the OpenCode plugin's variable
    assert offhub.session_key(key_env("codex:t-1")) == "codex:t-1"
    assert offhub.session_key(key_env("cursor:box-1:/w/proj-a")) == "cursor:box-1:/w/proj-a"
    assert offhub.session_key({}) == ""
    assert offhub.session_key(key_env("claude:c1")) == ""  # Claude Code names itself
    assert offhub.session_key(key_env("nonsense")) == ""


# --- the command, for the tools without a Claude Code session file --------------------------

def test_opencode_off_and_on(home):
    assert offhub.command([], OC, cwd="/w/proj-a") == (0, "this session is on the hub")
    code, line = offhub.command(["off"], OC, cwd="/w/proj-a")
    assert code == 0 and line.startswith("this session is now off the hub") and "/hub on" in line
    assert offhub.off("opencode:ses_1") and not offhub.off("opencode:ses_2")
    assert (paths.home() / "off" / "opencode%3Ases_1").exists()
    assert offhub.command([], OC, cwd="/w/proj-a") == (
        0, "this session is off the hub; type /hub on to come back")
    assert offhub.command(["on"], OC, cwd="/w/proj-a")[0] == 0
    assert not offhub.off("opencode:ses_1")
    assert not (paths.home() / "off" / "opencode%3Ases_1").exists()


def test_codex_lines_name_its_dollar_command(home):
    env = key_env("codex:t-1")
    assert "$hub on brings it back" in offhub.command(["off"], env, cwd="/w/proj-a")[1]
    assert offhub.command([], env, cwd="/w/proj-a")[1].endswith("type $hub on to come back")
    assert offhub.command(["maybe"], env) == (
        1, "use $hub, $hub on, $hub off, $hub team, $hub team <project> or $hub team off")


def test_cursor_switches_its_folder(home):
    env = key_env("cursor:box-1:/w/proj-a")
    assert offhub.command(["off"], env, cwd="/w/proj-a")[1].startswith("this folder is now off")
    assert offhub.off("cursor:box-1:/w/proj-a") and not offhub.off("cursor:box-1:/w/proj-b")


def test_bad_words_and_no_session(home):
    assert offhub.command(["sideways"], OC) == (
        1, "use /hub, /hub on, /hub off, /hub team, /hub team <project> or /hub team off")
    assert offhub.command(["off"], {})[0] == 1


def test_manager_stays_on(home, monkeypatch):
    monkeypatch.setattr(paths, "in_manager_folder", lambda cwd: True)
    code, line = offhub.command(["off"], OC, cwd="/m")
    assert code == 1 and "manager" in line and not offhub.off("opencode:ses_1")


@pytest.mark.team_only
def test_a_personal_folder_says_it_stays_off(home):
    offhub.save_team({"proj-a"}, set())
    assert offhub.command([], OC, cwd="/w/proj-a") == (0, "this session is on the hub")
    code, line = offhub.command([], OC, cwd="/w/mine")
    assert line == f"this session stays off the hub: {paths.NOT_TEAM}"


def test_gone_told_and_clean_for_keyed_files(home, monkeypatch):
    monkeypatch.setattr(offhub, "_tell_now", lambda data: None)
    offhub.command(["off"], OC, cwd="/w/proj-a")
    offhub.command(["off"], key_env("cursor:box-1:/w/proj-a"), cwd="/w/proj-a")
    assert sorted(offhub.gone()) == ["cursor:box-1:/w/proj-a", "opencode:ses_1"]
    offhub.told(["opencode:ses_1"])
    assert offhub.gone() == ["cursor:box-1:/w/proj-a"]
    offhub.command(["off"], OC, cwd="/w/proj-a")  # off again: the hub already dropped it
    assert offhub.gone() == ["cursor:box-1:/w/proj-a"]
    offhub.clean()  # no process to check: kept
    assert offhub.off("opencode:ses_1")
    f = paths.home() / "off" / "opencode%3Ases_1"
    st = json.loads(f.read_text())
    f.write_text(json.dumps({**st, "at": time.time() - offhub.KEEP_S - 1}))
    offhub.clean()  # a session long finished
    assert not f.exists()


# --- the bridge and the shim honour it ------------------------------------------------------

class FakeOpenCode:
    tool, prefix, restart_note = "opencode", "opencode", None

    def __init__(self):
        self.sent = []

    def installed(self):
        return True

    def sessions(self):
        return [Session("opencode:ses_1", "opencode", "proj-a", "", cwd="/w/proj-a")]

    def deliver(self, key, text):
        self.sent.append(key)


def test_bridge_never_reports_or_pushes_an_off_opencode_session(team, home, monkeypatch):  # noqa: F811
    from kaptyorka import facts

    facts._cache.clear()
    monkeypatch.setattr(facts.shutil, "which", lambda n: None)
    monkeypatch.setattr(bridge_mod.subprocess, "run", FakeRun())
    cfg = {"hub": team.hub.url, "token": team.tokens["alice"], "machine": "box-1"}
    paths.save_config(cfg)
    oc = FakeOpenCode()
    b = Bridge(dict(cfg), [oc], bridge_mod.log)
    b.heartbeat()
    assert team.store.agent("opencode:ses_1") is not None
    offhub.command(["off"], OC, cwd="/w/proj-a")
    assert team.store.agent("opencode:ses_1") is None  # the command told the hub at once
    b.heartbeat()
    assert team.store.agent("opencode:ses_1") is None
    acks = []
    real = http.request

    def request(method, url, token, body=None, **kw):
        if "/bridge/poll" in url:
            return 200, [{"id": 7, "key": "opencode:ses_1", "text": "hi"}]
        if url.endswith("/bridge/ack"):
            acks.append(body)
            return 204, None
        return real(method, url, token, body, **kw)
    monkeypatch.setattr(http, "request", request)
    assert b.poll_once(0) == 0 and oc.sent == [] and acks == [
        {"id": 7, "ok": False, "error": "the session is off the hub"}]
    monkeypatch.setattr(http, "request", real)
    offhub.command(["on"], OC, cwd="/w/proj-a")
    b.heartbeat()
    assert team.store.agent("opencode:ses_1") is not None


# --- the hooks answer /hub in Codex and Cursor ----------------------------------------------

def test_hub_words():
    assert hook.hub_words("/hub") == []
    assert hook.hub_words("  $hub off ") == ["off"]
    assert hook.hub_words("/hub team proj-a") == ["team", "proj-a"]  # passed on to the CLI
    assert hook.hub_words("/hubble") is None
    assert hook.hub_words("please /hub off") is None
    assert hook.hub_words(None) is None


class HubCli:
    """Runs `kaptyorka hub` in-process, as the real CLI would in its own process."""

    def __init__(self):
        self.calls = []

    def __call__(self, argv, **kw):
        assert os.path.basename(argv[0]) == "kaptyorka" and argv[1] == "hub"
        self.calls.append((argv, kw["env"].get(offhub.SESSION_KEY), kw.get("cwd")))
        code, line = offhub.command(argv[2:], kw["env"], cwd=kw.get("cwd") or "")
        return subprocess.CompletedProcess(argv, code, line + "\n", "")


@pytest.fixture
def joined(home, monkeypatch):
    paths.save_config({"hub": "http://127.0.0.1:9", "token": "T", "machine": "box-1"})
    monkeypatch.setattr(offhub, "_tell_now", lambda data: None)  # /hub off tells the hub at once


def test_codex_prompt_hook_answers_dollar_hub(joined, monkeypatch, capsys):
    monkeypatch.setattr(hook.http, "request", lambda *a, **k: pytest.fail("no hub call"))
    cli = HubCli()
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "not-this-one")  # never wins over the hook's key
    stdin = json.dumps({"session_id": "t-1", "cwd": "/w/proj-a", "prompt": "$hub off"})
    assert hook.main("codex", "prompt", stdin, run=cli) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["decision"] == "block" and out["reason"].startswith("this session is now off")
    assert out["systemMessage"] == out["reason"]
    assert cli.calls[0][1:] == ("codex:t-1", "/w/proj-a")
    assert offhub.off("codex:t-1")
    # off now: an ordinary prompt sends nothing to the hub and gets no brief
    stdin = json.dumps({"session_id": "t-1", "cwd": "/w/proj-a", "prompt": "fix the bug"})
    assert hook.main("codex", "prompt", stdin, run=cli) == 0
    assert capsys.readouterr().out == ""


def test_codex_start_says_the_state(joined, monkeypatch, capsys):
    cli = HubCli()
    monkeypatch.setattr(hook.http, "request", lambda *a, **k: (200, "the brief"))
    stdin = json.dumps({"session_id": "t-1", "cwd": "/w/proj-a"})
    hook.main("codex", "start", stdin, run=cli)
    out = json.loads(capsys.readouterr().out)
    assert out["systemMessage"] == "this session is on the hub"
    assert out["hookSpecificOutput"]["additionalContext"] == "the brief"
    offhub.switch_key("codex:t-1", True)
    monkeypatch.setattr(hook.http, "request", lambda *a, **k: pytest.fail("no hub call"))
    hook.main("codex", "start", stdin, run=cli)
    assert json.loads(capsys.readouterr().out) == {
        "systemMessage": "this session is off the hub; type $hub on to come back"}


def test_cursor_prompt_hook_answers_slash_hub(joined, monkeypatch, capsys):
    monkeypatch.setattr(hook.http, "request", lambda *a, **k: pytest.fail("no hub call"))
    cli = HubCli()
    stdin = {"workspace_roots": ["/w/proj-a"], "prompt": "/hub off"}
    hook.main("cursor", "prompt", json.dumps(stdin), run=cli)
    out = json.loads(capsys.readouterr().out)
    assert out["continue"] is False and out["user_message"].startswith("this folder is now off")
    assert offhub.off("cursor:box-1:/w/proj-a")  # the key identify() gives the shim and hooks
    hook.main("cursor", "prompt", json.dumps({**stdin, "prompt": "hello"}), run=cli)
    assert json.loads(capsys.readouterr().out) == {"continue": True}


def test_hook_hub_says_so_when_the_cli_fails(joined, monkeypatch, capsys):
    def broken(argv, **kw):
        raise OSError("gone")
    stdin = json.dumps({"session_id": "t-1", "cwd": "/w/proj-a", "prompt": "$hub"})
    hook.main("codex", "prompt", stdin, run=broken)
    assert json.loads(capsys.readouterr().out)["reason"] == "kaptyorka: $hub did not run"  # Codex's own word


def test_codex_dollar_hub_team_gets_the_join_time(joined, monkeypatch, capsys):
    seen = []

    def cli(argv, **kw):
        seen.append((argv[1:], kw["timeout"], kw["env"].get(offhub.SESSION_KEY)))
        return subprocess.CompletedProcess(argv, 0, "this session joined proj-a\n", "")
    stdin = json.dumps({"session_id": "t-1", "cwd": "/w/notes", "prompt": "$hub team proj-a"})
    monkeypatch.setattr(hook.http, "request", lambda *a, **k: pytest.fail("no hub call"))
    hook.main("codex", "prompt", stdin, run=cli)
    out = json.loads(capsys.readouterr().out)
    assert out["decision"] == "block" and out["reason"] == "this session joined proj-a"
    assert seen == [(["hub", "team", "proj-a"], hook.HUB_TEAM_S, "codex:t-1")]


def test_an_off_opencode_session_hook_is_silent(joined, monkeypatch, capsys):
    monkeypatch.setattr(hook, "identify", lambda machine, workspace=None:
                        Identity("opencode:ses_1", machine, "opencode", "proj-a", "/w/proj-a"))
    monkeypatch.setattr(hook.http, "request", lambda *a, **k: pytest.fail("no hub call"))
    offhub.switch_key("opencode:ses_1", True)
    for event in ("start", "prompt"):
        hook.main("opencode", event, "")
    assert capsys.readouterr().out == ""


# --- what each tool gets installed ----------------------------------------------------------

def test_opencode_installs_command_and_plugin_and_removes_them(home):
    a = OpenCodeAdapter(home)
    lines = a.install_hub("/opt/kp/kaptyorka", v2=False)
    conf = home / ".config/opencode"
    assert (conf / "commands/hub.md").read_text() == COMMAND
    plugin = (conf / "plugins/kaptyorka.js").read_text()
    assert '"command.execute.before"' in plugin and '"hub", ...words' in plugin
    assert "showToast" in plugin and "session.created" in plugin
    assert len(lines) == 2 and a.install_hub("/opt/kp/kaptyorka", v2=False) == []
    a.unconfigure()
    assert not (conf / "commands/hub.md").exists() and not (conf / "plugins/kaptyorka.js").exists()


def test_opencode_leaves_a_foreign_hub_command_alone(home):
    f = home / ".config/opencode/commands/hub.md"
    f.parent.mkdir(parents=True)
    f.write_text("my own")
    logged = []
    OpenCodeAdapter(home, log=logged.append).install_hub("/k", v2=False)
    OpenCodeAdapter(home).unconfigure()
    assert f.read_text() == "my own" and logged


def test_codex_installs_a_dollar_hub_skill(home, monkeypatch, tmp_path):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "cx"))
    a = CodexAdapter(home)
    assert a.install_hub("/k") and a.install_hub("/k") == []
    folder = tmp_path / "cx/skills/hub"
    assert (folder / "SKILL.md").read_text() == SKILL and "name: hub" in SKILL
    assert "allow_implicit_invocation: false" in (folder / "agents/openai.yaml").read_text()
    a.unconfigure()
    assert not folder.exists()
    a.install_hub("/k")
    (folder / "notes.txt").write_text("someone's own file")
    (folder / "agents" / "other.yaml").write_text("theirs")
    a.unconfigure()  # leave removes only what kaptyorka wrote
    assert sorted(str(f.relative_to(folder)) for f in folder.rglob("*")) == [
        "agents", "agents/other.yaml", "notes.txt"]
    shutil.rmtree(folder)
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text("theirs")
    assert a.install_hub("/k") == [] and SKILL_MARK not in (folder / "SKILL.md").read_text()


def test_cursor_installs_skill_and_prompt_hook(home):
    a = CursorAdapter(home)
    a.install_hub("/bin/kaptyorka")
    d = home / ".cursor"
    assert (d / "skills/hub/SKILL.md").read_text() == CURSOR_SKILL
    assert "disable-model-invocation: true" in CURSOR_SKILL
    hooks = json.loads((d / "hooks.json").read_text())["hooks"]
    assert hooks["beforeSubmitPrompt"] == [{"command": "/bin/kaptyorka hook cursor prompt"}]
    assert not (d / "mcp.json").exists()  # bridge start adds /hub only, never the MCP entry
    a.configure("/bin/kaptyorka")
    a.unconfigure()
    assert not (d / "skills/hub").exists()
    assert json.loads((d / "hooks.json").read_text()).get("hooks", {}) == {}


def test_bridge_start_installs_hub_for_every_tool(home, monkeypatch, tmp_path):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "cx"))
    monkeypatch.setattr(ClaudeAdapter, "installed", lambda self: True)
    logged = []
    adapters = [ClaudeAdapter(home=home), OpenCodeAdapter(home), CodexAdapter(home),
                CursorAdapter(home)]
    install_commands(adapters, "/opt/kp/kaptyorka", logged.append)
    assert (home / ".claude/commands/hub.md").exists()
    status = json.loads((home / ".claude/settings.json").read_text())["statusLine"]
    assert status["command"] == "/opt/kp/kaptyorka statusline"
    assert (home / ".config/opencode/commands/hub.md").exists()
    assert (tmp_path / "cx/skills/hub/SKILL.md").exists()
    assert (home / ".cursor/skills/hub/SKILL.md").exists()
    for a in adapters:
        assert doctor.hub_installed(a, home), a.tool
    logged.clear()
    install_commands(adapters, "/opt/kp/kaptyorka", logged.append)
    assert logged == []  # nothing new the second time


def test_claude_wraps_a_teammates_own_status_line(home):
    settings = home / ".claude/settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps({"statusLine": {"type": "command", "command": "my-line"}}))
    ClaudeAdapter(home=home).install_hub("/k")
    assert json.loads(settings.read_text())["statusLine"]["command"] == "/k statusline -- my-line"
    assert doctor.hub_installed(ClaudeAdapter(home=home), home)
    ClaudeAdapter(home=home, run=lambda *a, **k: subprocess.CompletedProcess(a, 0, "", "")).unconfigure()
    assert json.loads(settings.read_text())["statusLine"] == {"type": "command", "command": "my-line"}
    assert not doctor.hub_installed(ClaudeAdapter(home=home), home)


def test_doctor_flags_a_missing_hub_and_fix_installs_it(home, monkeypatch, capsys):
    from types import SimpleNamespace

    from test_doctor import CFG, Run

    monkeypatch.setattr(http, "request", lambda *a, **k: (200, {}))
    d = home / ".cursor"
    d.mkdir()
    (d / "mcp.json").write_text(json.dumps({"mcpServers": {"kaptyorka": {}}}))
    (d / "hooks.json").write_text(json.dumps({"version": 1, "hooks": {
        "stop": [{"command": "/opt/kaptyorka hook cursor stop"}], "postToolUse": [{"command": "/opt/kaptyorka hook cursor tool"}]}}))
    a = CursorAdapter(home)
    res = {c.name: c for c in doctor.checks(CFG, [a], home, Run(), "linux")}
    assert res["tool: cursor"].ok and not res["tool: cursor /hub"].ok
    assert res["tool: cursor /hub"].fix == "kaptyorka doctor --fix"
    doctor.run(CFG, fix=True, adapters=[a], home=home, run=Run(), platform="linux", exe="/opt/kaptyorka")
    assert "ok   tool: cursor /hub" in capsys.readouterr().out
    assert doctor.hub_installed(SimpleNamespace(tool="other"), home)  # nothing to check


# --- the OpenCode plugin itself, run in node with a stand-in kaptyorka ----------------------

PLUGIN_DRIVER = """
import { Kaptyorka } from "%s";
const toasts = [];
const client = { tui: { showToast: async (o) => { toasts.push(o.body); } } };
const hooks = await Kaptyorka({ directory: %s, client });
const output = { parts: [{ type: "text", text: "template" }, { type: "file", url: "x" }] };
await hooks["command.execute.before"]({ command: "hub", sessionID: "ses_9", arguments: " off " }, output);
await hooks["command.execute.before"]({ command: "other", sessionID: "ses_9", arguments: "" }, output);
await hooks.event({ event: { type: "session.created", properties: { info: { id: "ses_8" } } } });
await hooks.event({ event: { type: "session.created", properties: { info: { id: "c", parentID: "p" } } } });
console.log(JSON.stringify({ toasts, parts: output.parts }));
"""


def test_opencode_plugin_runs_hub_for_its_session(home, tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    fake = tmp_path / "kaptyorka"
    fake.write_text('#!/bin/sh\nif [ "$2" = off ]; then echo "now off ($KAPTYORKA_OPENCODE_SESSION)";'
                    ' else echo "on ($KAPTYORKA_OPENCODE_SESSION) $(pwd)"; fi\n')
    fake.chmod(0o755)
    OpenCodeAdapter(home).install_hub(str(fake), v2=False)
    plugin = tmp_path / "plugin.mjs"
    plugin.write_text((home / ".config/opencode/plugins/kaptyorka.js").read_text())
    driver = tmp_path / "driver.mjs"
    driver.write_text(PLUGIN_DRIVER % (plugin, json.dumps(str(tmp_path))))
    r = subprocess.run([node, str(driver)], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    got = json.loads(r.stdout)
    assert got["parts"][0]["text"].startswith("now off (ses_9)\n\nTell the user the line above")
    assert got["parts"][1] == {"type": "file", "url": "x"}
    assert [t["message"] for t in got["toasts"]] == ["now off (ses_9)", f"on (ses_8) {tmp_path}"]
    assert all(t["title"] == "kaptyorka hub" for t in got["toasts"])
