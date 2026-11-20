"""The hub guide at session start (guide.py): built on this machine alone, per tool."""
import json
import os
import socket
import subprocess

import pytest

from kaptyorka import guide, hook, offhub, optin, paths
from kaptyorka.adapters.cursor import CursorAdapter
from kaptyorka.identity import Identity

pytestmark = pytest.mark.team_only  # the real gate: a folder is a team project or personal

PERSONAL = "/w/diary"
TEAM = "/w/proj-a"


@pytest.fixture
def joined(home, monkeypatch):
    paths.save_config({"hub": "http://127.0.0.1:9", "token": "T", "machine": "box-1"})
    offhub.save_team({"proj-a", "proj-b"}, set(), mine=["proj-a", "proj-b"])


def no_network(monkeypatch):
    """Any request, connection or subprocess fails the test."""
    def boom(*a, **k):
        raise AssertionError("no network at session start in a personal folder")
    monkeypatch.setattr(hook.http, "request", boom)
    monkeypatch.setattr(socket.socket, "connect", boom)
    monkeypatch.setattr(socket, "create_connection", boom)


def cli_line(line):
    """`kaptyorka hub` as Codex's start hook runs it, answering with a fixed line."""
    return lambda argv, **kw: subprocess.CompletedProcess(argv, 0, line + "\n", "")


def as_session(monkeypatch, key, tool, cwd):
    monkeypatch.setattr(hook, "identify", lambda machine, workspace=None:  # as identify() does
                        optin.apply(Identity(key, machine, tool, os.path.basename(cwd), cwd)))


# ---- the texts ----

def test_personal_guide_names_their_projects_and_who_switches():
    text = guide.personal("claude:s1", ["proj-a", "proj-b"])
    assert "/hub team <project> for one project (theirs: proj-a, proj-b)" in text
    assert "/hub team to coordinate across projects" in text
    assert "touches a team project, a teammate, a team machine or the hub" in text
    assert "suggest once (not every turn)" in text
    assert "join_team only after they say yes" in text
    assert "Say nothing of this for personal work" in text


def test_personal_guide_without_a_known_list_or_any_role():
    assert "(theirs" not in guide.personal("claude:s1", None)
    assert "no team project role yet; the admin gives roles" in guide.personal("claude:s1", [])


def test_codex_says_dollar_hub_and_cursor_says_folder():
    assert "$hub team <project>" in guide.personal("codex:t1", ["proj-a"])
    assert "/hub" not in guide.personal("codex:t1", ["proj-a"])
    assert "$hub on" in guide.off("codex:t1")
    assert "this folder is off the hub" in guide.off("cursor:box-1:/w/diary")
    assert "switch this folder yourself" in guide.personal("cursor:box-1:/w/diary", ["proj-a"])


def test_off_coordinator_and_team_lines():
    assert guide.off("claude:s1") == "kaptyorka hub: this session is off the hub; /hub on brings it back."
    assert "name a project for notes and work" in guide.on_hub("claude:s1", paths.COORDINATOR, True)
    assert "suggest /hub off" in guide.on_hub("claude:s1", "proj-a", False)
    assert "suggest /hub team off" in guide.on_hub("claude:s1", "proj-a", True)


@pytest.mark.parametrize("key", ["claude:s1", "codex:t1", "opencode:ses_1",
                                 "cursor:box-1:/w/a-very-long-folder-name/" + "x" * 80])
def test_every_text_stays_under_the_cap(key):
    many = [f"proj-{i:03d}-with-a-long-name" for i in range(60)]
    texts = [guide.personal(key, many), guide.personal(key, []), guide.personal(key, None),
             guide.off(key)] + [guide.on_hub(key, p, j) for p in ("proj-a", paths.COORDINATOR)
                                for j in (True, False)]
    assert all(0 < len(t) <= guide.CAP for t in texts), [len(t) for t in texts]
    assert "proj-000-with-a-long-name" in texts[0] and texts[0].count(", ...)") == 1


# ---- per tool, personal folder: the guide, and nothing goes out ----

def test_claude_code_personal_folder_start_gets_the_guide_and_nothing_else(joined, monkeypatch,
                                                                           capsys):
    no_network(monkeypatch)
    as_session(monkeypatch, "claude:s1", "claude-code", PERSONAL)
    hook.main("claude-code", "start", json.dumps({"session_id": "s1", "cwd": PERSONAL}))
    out = capsys.readouterr().out  # SessionStart: plain stdout is added to the context
    assert out.startswith("kaptyorka hub: this folder is personal") and "(theirs: proj-a, proj-b)" in out
    for event in ("prompt", "stop"):  # once per session start, not every turn
        hook.main("claude-code", event, json.dumps({"session_id": "s1", "cwd": PERSONAL}))
        assert capsys.readouterr().out == ""


def test_codex_personal_folder_start_gets_the_guide_with_its_state_line(joined, monkeypatch,
                                                                         capsys):
    no_network(monkeypatch)
    line = "this session stays off the hub: " + paths.NOT_TEAM
    hook.main("codex", "start", json.dumps({"session_id": "t1", "cwd": PERSONAL}),
              run=cli_line(line))
    out = json.loads(capsys.readouterr().out)
    assert out["systemMessage"] == line
    assert out["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    assert out["hookSpecificOutput"]["additionalContext"].startswith(
        "kaptyorka hub: this folder is personal")
    assert "$hub team <project>" in out["hookSpecificOutput"]["additionalContext"]


def test_opencode_personal_folder_gets_the_guide_with_each_request(joined, monkeypatch, capsys):
    no_network(monkeypatch)
    as_session(monkeypatch, "opencode:ses_1", "opencode", PERSONAL)
    for event in ("start", "prompt"):  # the plugin pushes the hook's text into system each time
        hook.main("opencode", event, "")
        assert capsys.readouterr().out.startswith("kaptyorka hub: this folder is personal")


def test_cursor_personal_folder_session_start_gets_additional_context(joined, monkeypatch,
                                                                       capsys):
    no_network(monkeypatch)
    as_session(monkeypatch, f"cursor:box-1:{PERSONAL}", "cursor", PERSONAL)
    hook.main("cursor", "start", json.dumps({"workspace_roots": [PERSONAL]}))
    out = json.loads(capsys.readouterr().out)
    assert out["additional_context"].startswith("kaptyorka hub: this folder is personal")
    hook.main("cursor", "stop", json.dumps({"workspace_roots": [PERSONAL]}))
    assert json.loads(capsys.readouterr().out) == {}


def test_off_in_a_personal_folder_says_only_its_line(joined, monkeypatch, capsys):
    no_network(monkeypatch)
    as_session(monkeypatch, "opencode:ses_1", "opencode", PERSONAL)
    offhub.switch_key("opencode:ses_1", True)
    hook.main("opencode", "start", "")
    assert capsys.readouterr().out == "kaptyorka hub: this session is off the hub; /hub on brings it back.\n"


def test_no_team_list_from_an_older_hub_is_silent(home, monkeypatch, capsys):
    paths.save_config({"hub": "http://127.0.0.1:9", "token": "T", "machine": "box-1"})
    offhub.save_team(None, set())
    no_network(monkeypatch)
    as_session(monkeypatch, "claude:s1", "claude-code", PERSONAL)
    hook.main("claude-code", "start", "")
    assert capsys.readouterr().out == ""


# ---- on the hub: one local line after the hub's own brief ----

def test_team_folder_start_adds_one_line_to_the_hubs_brief(joined, monkeypatch, capsys):
    monkeypatch.setattr(hook.http, "request", lambda *a, **k: (200, "the brief"))
    as_session(monkeypatch, "claude:s1", "claude-code", TEAM)
    hook.main("claude-code", "start", "")
    assert capsys.readouterr().out == ("the brief\n\n" + guide.on_hub("claude:s1", "proj-a", False)
                                       + "\n")
    hook.main("claude-code", "prompt", "")  # a prompt's brief gets no line
    assert capsys.readouterr().out == "the brief\n"


def test_team_folder_with_the_hub_down_stays_silent(joined, monkeypatch, capsys):
    monkeypatch.setattr(hook.http, "request", lambda *a, **k: (502, ""))
    as_session(monkeypatch, "claude:s1", "claude-code", TEAM)
    hook.main("claude-code", "start", "")
    assert capsys.readouterr().out == ""


def test_coordinator_start_gets_the_name_a_project_line(joined, monkeypatch, capsys):
    monkeypatch.setattr(hook.http, "request", lambda *a, **k: (200, "the brief"))
    optin.save("opencode:ses_1", "", os.getpid())
    as_session(monkeypatch, "opencode:ses_1", "opencode", PERSONAL)
    hook.main("opencode", "start", "")
    out = capsys.readouterr().out
    assert out.startswith("the brief\n\n") and "name a project for notes and work" in out
    assert "this folder is personal" not in out


def test_cursor_team_folder_start_says_the_line_without_a_hub_call(joined, monkeypatch, capsys):
    no_network(monkeypatch)
    as_session(monkeypatch, f"cursor:box-1:{TEAM}", "cursor", TEAM)
    hook.main("cursor", "start", json.dumps({"workspace_roots": [TEAM]}))
    assert json.loads(capsys.readouterr().out) == {
        "additional_context": guide.on_hub(f"cursor:box-1:{TEAM}", "proj-a", False)}


# ---- Cursor's user-level sessionStart hook ----

def test_cursor_installs_and_removes_the_session_start_hook(home):
    (home / ".cursor").mkdir()
    a = CursorAdapter(home=home)
    a.install_hub("/bin/kaptyorka")
    hooks = json.loads((home / ".cursor" / "hooks.json").read_text())["hooks"]
    assert hooks["sessionStart"] == [{"command": "/bin/kaptyorka hook cursor start"}]
    a.unconfigure()
    assert "sessionStart" not in json.loads((home / ".cursor" / "hooks.json").read_text()).get(
        "hooks", {})
