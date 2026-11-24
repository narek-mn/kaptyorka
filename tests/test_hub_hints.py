"""The hub, without the person knowing its commands: the state said plainly where a tool has no
indicator, the indicator checked and reported, and hub commands suggested from the prompt
(suggest.py), with no model call and no network."""
import json
import socket
import subprocess
import time
from types import SimpleNamespace

import pytest

from kaptyorka import facts, hook, hubstate, offhub, optin, paths, suggest
from kaptyorka.adapters.claude import ClaudeAdapter
from kaptyorka.bridge import install_commands
from kaptyorka.identity import Identity

pytestmark = pytest.mark.team_only
PERSONAL, TEAM = "/w/diary", "/w/proj-a"


@pytest.fixture
def laptop(home):
    paths.save_config({"hub": "http://127.0.0.1:9", "token": "T", "machine": "box-1"})
    offhub.save_team({"proj-a", "proj-b"}, set(), mine=["proj-a"])
    (paths.home() / hubstate.CACHE).write_text(json.dumps({
        "at": time.time(), "ok": True, "ids": [], "people": ["bob", "al"],
        "machines": ["lab@jetson-1", "jetson-1"]}))
    return home


def no_network(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("no network in the prompt hook of a personal folder")
    monkeypatch.setattr(hook.http, "request", boom)
    monkeypatch.setattr(socket, "create_connection", boom)


def as_session(monkeypatch, key, tool, cwd):
    import os
    monkeypatch.setattr(hook, "identify", lambda machine, workspace=None:
                        optin.apply(Identity(key, machine, tool, os.path.basename(cwd), cwd)))


# ---- off the hub, in a personal folder ----

@pytest.mark.parametrize("prompt,target", [
    ("Fix the login bug in proj-a's API", "proj-a"),
    ("can you check PROJ-B for the same issue?", "proj-b"),
    ("copy the model weights to jetson-1", "jetson-1"),
    ("what did Bob change yesterday", "bob"),
])
def test_off_line_names_the_target(laptop, prompt, target):
    line = suggest.off_line("claude:s1", prompt)
    assert f"names {target}" in line and "suggest once" in line and "never switch" in line


@pytest.mark.parametrize("prompt", [
    "fix the typo in my notes",
    "rename proj-ab to proj-c",  # a longer word, not proj-a
    "add an alert when the disk is full",  # "al" is under 3 characters
    "the bobsled page needs a fix",
    "",
])
def test_off_line_stays_silent(laptop, prompt):
    assert suggest.off_line("claude:s1", prompt) == ""


def test_off_line_says_which_command(laptop):
    assert "`/hub team proj-a` (this session joins project proj-a)" in \
        suggest.off_line("claude:s1", "look at proj-a")
    # not their project, or a person or machine: as a coordinator
    assert "`/hub team` (this session joins as a coordinator)" in \
        suggest.off_line("claude:s2", "look at proj-b")
    assert "`$hub team`" in suggest.off_line("codex:t1", "ask bob")


def test_off_line_once_per_session_per_target(laptop):
    assert suggest.off_line("claude:s1", "proj-a is slow")
    assert suggest.off_line("claude:s1", "proj-a again") == ""
    assert suggest.off_line("claude:s1", "and jetson-1")  # another target
    assert suggest.off_line("claude:s2", "proj-a is slow")  # another session


def test_never_on_a_team_machine(laptop):
    offhub.save_team({"proj-a"}, set(), shared=True)
    assert suggest.off_line("claude:s1", "proj-a is slow") == ""


def test_claude_prompt_hook_in_a_personal_folder(laptop, monkeypatch, capsys):
    no_network(monkeypatch)
    as_session(monkeypatch, "claude:s1", "claude-code", PERSONAL)
    stdin = json.dumps({"session_id": "s1", "cwd": PERSONAL, "prompt": "merge it into proj-a"})
    hook.main("claude-code", "prompt", stdin)
    out = capsys.readouterr().out
    assert out.startswith("kaptyorka hub: the user's request names proj-a")
    hook.main("claude-code", "prompt", stdin)
    assert capsys.readouterr().out == ""  # once


def test_codex_prompt_hook_in_a_personal_folder(laptop, monkeypatch, capsys):
    no_network(monkeypatch)
    stdin = json.dumps({"session_id": "t1", "cwd": PERSONAL, "prompt": "ask bob about it"})
    hook.main("codex", "prompt", stdin)
    ctx = json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"]
    assert ctx.startswith("kaptyorka hub: the user's request names bob") and "$hub team" in ctx


def test_never_after_hub_off(laptop, monkeypatch, capsys):
    no_network(monkeypatch)
    offhub.switch_key("codex:t1", True)
    hook.main("codex", "prompt", json.dumps({"session_id": "t1", "cwd": PERSONAL,
                                             "prompt": "fix proj-a"}))
    assert "names proj-a" not in capsys.readouterr().out


# ---- on the hub ----

@pytest.mark.parametrize("prompt,want", [
    ("ask the session on jetson-1 whether the build passed", "who"),
    ("what is bob doing right now?", "who"),
    ("check with the other session before you push", "who"),
    ("run the training on jetson-1", "work"),
    ("continue the benchmark on the jetson-1 overnight", "work"),
])
def test_on_lines(laptop, prompt, want):
    text = suggest.on_lines("claude:s1", prompt)
    assert ("call who" in text) == (want == "who")
    assert ("work(project, task, machine=\"jetson-1\")" in text) == (want == "work")


@pytest.mark.parametrize("prompt", [
    "store the session id in a cookie",
    "the job ran on jetson-1 yesterday, read its log",
    "what is the capital of France",
    "run the tests on my laptop",
    "open another terminal",
])
def test_on_lines_stay_silent(laptop, prompt):
    assert suggest.on_lines("claude:s1", prompt) == ""


def test_on_lines_once_each(laptop):
    assert suggest.on_lines("claude:s1", "what is bob doing")
    assert suggest.on_lines("claude:s1", "ask bob again") == ""
    assert "work(" in suggest.on_lines("claude:s1", "run it on jetson-1")


def test_claude_prompt_on_the_hub_adds_the_hint_after_the_brief(laptop, monkeypatch, capsys):
    monkeypatch.setattr(hook.http, "request", lambda *a, **k: (200, "the brief"))
    as_session(monkeypatch, "claude:s1", "claude-code", TEAM)
    hook.main("claude-code", "prompt", json.dumps({"session_id": "s1", "cwd": TEAM,
                                                   "prompt": "what is bob doing?"}))
    out = capsys.readouterr().out
    assert out.startswith("the brief\n\nkaptyorka hub: to see what other sessions do, call who")


def test_refresh_keeps_the_names_the_person_can_see(laptop, monkeypatch):
    feed = {"agents": [
        {"id": "a", "person": "alice", "mine": True, "team": False, "machine": "box-1"},
        {"id": "b", "person": "bob", "mine": False, "team": False, "machine": "box-2"},
        {"id": "c", "person": "jet", "mine": False, "team": True, "machine": "lab@jetson-1",
         "where": "jetson-1"}]}
    monkeypatch.setattr("kaptyorka.http.request", lambda *a, **k: (200, feed))
    got = hubstate.refresh()
    assert got["people"] == ["bob"] and got["machines"] == ["jet", "jetson-1", "lab@jetson-1"]


# ---- where a tool has no indicator: the state first ----

def test_cursor_start_says_the_state_first(laptop, monkeypatch, capsys):
    no_network(monkeypatch)
    as_session(monkeypatch, f"cursor:box-1:{PERSONAL}", "cursor", PERSONAL)
    hook.main("cursor", "start", json.dumps({"workspace_roots": [PERSONAL]}))
    ctx = json.loads(capsys.readouterr().out)["additional_context"]
    assert ctx.splitlines()[0] == "Hub: OFF (personal folder)"
    as_session(monkeypatch, f"cursor:box-1:{TEAM}", "cursor", TEAM)
    hook.main("cursor", "start", json.dumps({"workspace_roots": [TEAM]}))
    assert json.loads(capsys.readouterr().out)["additional_context"].startswith(
        "Hub: ON for proj-a\n")


def test_claude_code_start_has_no_state_line(laptop, monkeypatch, capsys):
    monkeypatch.setattr(hook.http, "request", lambda *a, **k: (200, "the brief"))
    as_session(monkeypatch, "claude:s1", "claude-code", TEAM)
    hook.main("claude-code", "start", "")
    assert capsys.readouterr().out.startswith("the brief")  # its status line shows it


# ---- /hub ----

def test_hub_says_state_and_next_commands(laptop):
    env = {offhub.SESSION_KEY: "codex:t1"}
    assert offhub.command([], env, cwd=TEAM)[1] == (
        "this session is on the hub. Project proj-a; $hub off takes it off; the agent reaches"
        " the team with who and send")
    assert offhub.command([], env, cwd=PERSONAL)[1] == (
        f"this session stays off the hub: {paths.NOT_TEAM} $hub team <project> puts it on for"
        " one project (yours: proj-a); $hub team, as a coordinator across projects")


# ---- the indicator: reported, and put back at bridge start ----

def test_machine_facts_report_the_indicator(monkeypatch):
    monkeypatch.setattr(facts.doctor, "tool_configured", lambda a, home: a.tool != "cursor")
    monkeypatch.setattr(facts.doctor, "hub_installed", lambda a, home: a.tool == "opencode")
    monkeypatch.setattr(facts, "_tool_version", lambda tool, run: "")
    tools = [SimpleNamespace(tool=t, installed=lambda: True)
             for t in ("claude-code", "opencode", "codex", "cursor")]
    m = facts.machine(tools)
    assert m["indicator_ok"] == {"claude-code": False, "opencode": True}  # no place in the others


def test_a_missing_indicator_is_a_machine_warning(tmp_path):
    from helpers import person
    from kaptyorka.store import Store
    from kaptyorka.workers import WorkerStore
    from test_workers import device

    now = [1_000_000.0]
    store = Store(tmp_path / "h.db", clock=lambda: now[0])
    ws = WorkerStore(store)
    a = device(store, person(store, "alice")[0], "box-1", "alice")[0]
    ws.facts_seen(a, {"version": "0.3.0", "tmux": True, "hooks_ok": {"claude-code": True},
                      "indicator_ok": {"claude-code": False, "opencode": True}}, {})
    got = ws.warnings(now[0], "0.3.0")
    assert [w["short"] for w in got] == ["claude-code hub indicator missing"]
    assert "Run `kaptyorka doctor --fix` there" in got[0]["text"]
    store.close()


def test_bridge_start_wraps_a_status_line_set_after_join(home):
    settings = home / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps({"statusLine": {"type": "command", "command": "my-line"}}))
    logged = []
    install_commands([ClaudeAdapter(home=home, log=logged.append,
                                    run=lambda *a, **k: subprocess.CompletedProcess(a, 0, "", ""))],
                     "/opt/kp/kaptyorka", logged.append)
    line = json.loads(settings.read_text())["statusLine"]
    assert line["command"] == "/opt/kp/kaptyorka statusline -- my-line"
    assert any("added the hub field" in s for s in logged)
