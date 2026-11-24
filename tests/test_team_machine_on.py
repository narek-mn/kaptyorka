"""A team machine (a shared team account) puts every session on the hub by itself, in any
folder and any tool: one in a folder that is no team project joins as a coordinator, as
`/hub team` would, with nothing of its folder sent. /hub off still takes one session off.
Personal machines are unchanged."""
import json
import time
from urllib.parse import unquote

import pytest

from kaptyorka import bridge as bridge_mod
from kaptyorka import guide, hook, http, hubstate, offhub, optin, paths
from kaptyorka.adapters.base import Session
from kaptyorka.bridge import Bridge
from kaptyorka.identity import Identity
from kaptyorka.store import session_id as handle_of
from test_bridge import FakeAdapter
from test_team_only import Answers, quiet  # noqa: F401  (quiet is a fixture)

pytestmark = pytest.mark.team_only
COORD = paths.COORDINATOR
CFG = {"hub": "http://h", "token": "t", "machine": "lab-1"}


@pytest.fixture
def lab(home):
    paths.save_config(dict(CFG))
    offhub.save_team({"proj-a"}, set(), shared=True)
    return home


@pytest.fixture
def laptop(home):
    paths.save_config(dict(CFG))
    offhub.save_team({"proj-a"}, set())
    return home


def ident(key, project, cwd, pid=0):
    return Identity(key, "lab-1", key.split(":")[0], project, cwd, pid)


def test_the_bridge_keeps_what_the_hub_says_about_the_account(home, quiet, monkeypatch):  # noqa: F811
    hub = Answers(monkeypatch, {"team": ["proj-a"], "keep": [], "projects": [], "shared": True})
    b = Bridge(dict(CFG), [FakeAdapter(sessions=[])], bridge_mod.log)
    b.heartbeat()
    assert offhub.shared_machine() is True
    hub.reply = {**hub.reply, "shared": False}
    b.heartbeat()
    assert offhub.shared_machine() is False


def test_every_session_on_a_team_machine_is_on_the_hub(lab):
    for key in ("claude:s1", "opencode:ses_a", "codex:t1", "cursor:lab-1:/w/diary"):
        got = optin.apply(ident(key, "diary", "/w/diary"))
        assert got.project == COORD and offhub.personal(got.project, got.cwd, key) == ""
    team = optin.apply(ident("codex:t2", "proj-a", "/w/proj-a"))
    assert team.project == "proj-a"  # a team project's folder: as before
    assert optin.get("codex:t1") is None  # nothing saved: the next session decides again


def test_a_personal_machine_is_unchanged(laptop):
    got = optin.apply(ident("codex:t1", "diary", "/w/diary"))
    assert got.project == "diary" and offhub.personal("diary", "/w/diary", "codex:t1") == paths.NOT_TEAM


def test_no_team_list_yet_or_the_manager_folder_never_auto_joins(home, monkeypatch):
    paths.save_config(dict(CFG))
    offhub.save_team(None, set(), shared=True)  # an older hub: no list
    assert optin.apply(ident("codex:t1", "diary", "/w/diary")).project == "diary"
    offhub.save_team({"proj-a"}, set(), shared=True)
    monkeypatch.setattr(paths, "in_manager_folder", lambda cwd: cwd == "/w/manager")
    assert optin.apply(ident("codex:t1", "manager", "/w/manager")).project == "manager"


def test_the_bridge_reports_them_as_coordinators_with_nothing_of_their_folder(
        lab, quiet, monkeypatch):  # noqa: F811
    hub = Answers(monkeypatch, {"team": ["proj-a"], "keep": [], "projects": ["proj-a"],
                                "shared": True})
    offhub.switch_key("opencode:ses_off", True)  # /hub off in this one session
    adapters = [FakeAdapter("opencode", "opencode", [
        Session("opencode:ses_a", "opencode", "diary", cwd="/w/diary"),
        Session("opencode:ses_off", "opencode", "diary", cwd="/w/diary"),
        Session("opencode:ses_t", "opencode", "proj-a", cwd="/w/proj-a")])]
    Bridge(dict(CFG), adapters, bridge_mod.log).heartbeat()
    body = hub.bodies[-1]
    got = {s["key"]: s for s in body["sessions"]}
    assert sorted(got) == ["opencode:ses_a", "opencode:ses_t"]
    assert (got["opencode:ses_a"]["project"], got["opencode:ses_a"]["cwd"],
            got["opencode:ses_a"]["label"]) == (COORD, "", optin.LABEL)
    assert got["opencode:ses_t"]["project"] == "proj-a"
    assert "diary" not in json.dumps(body)


def test_a_coordinator_sends_its_tasks_without_their_commands(lab, quiet, monkeypatch):  # noqa: F811
    hub = Answers(monkeypatch, {"team": ["proj-a"], "keep": [], "projects": ["proj-a"],
                                "shared": True})
    task = {"id": "bq1", "silent_s": 2100.0, "command": "pytest /w/diary/tests"}
    adapters = [FakeAdapter("claude-code", "claude", [
        Session("claude:c1", "claude-code", "diary", "shell", cwd="/w/diary", write_s=5.0,
                tasks=[dict(task)]),
        Session("claude:t1", "claude-code", "proj-a", "shell", cwd="/w/proj-a",
                tasks=[{**task, "command": "make test"}])])]
    Bridge(dict(CFG), adapters, bridge_mod.log).heartbeat()
    got = {s["key"]: s for s in hub.bodies[-1]["sessions"]}
    assert got["claude:c1"]["tasks"] == [{"id": "bq1", "silent_s": 2100.0, "command": ""}]
    assert got["claude:c1"]["write_s"] == 5.0
    assert got["claude:t1"]["tasks"][0]["command"] == "make test"  # a team folder: as before
    assert "/w/diary" not in json.dumps(hub.bodies[-1])


def test_hub_off_takes_one_session_off_and_on_brings_it_back(lab, monkeypatch):
    monkeypatch.setattr(http, "request", lambda *a, **k: (204, None))
    env = {offhub.OPENCODE_SESSION: "ses_a"}
    code, line = offhub.command([], env, cwd="/w/diary")
    assert code == 0 and "on the hub as a coordinator" in line and "team machine" in line
    code, line = offhub.command(["off"], env, cwd="/w/diary")
    assert code == 0 and "now off the hub" in line and offhub.kept_off("opencode:ses_a")
    assert offhub.command([], env, cwd="/w/diary")[1].startswith("this session is off the hub")
    assert not offhub.kept_off("opencode:ses_new")  # the next session starts on again
    code, line = offhub.command(["on"], env, cwd="/w/diary")
    assert code == 0 and "coordinator" in line


def test_hub_state_says_on_for_a_team_machine_session_in_any_folder(lab, monkeypatch):
    key = "opencode:ses_a"
    (paths.home() / hubstate.CACHE).write_text(json.dumps(
        {"at": time.time(), "ok": True, "ids": [handle_of(key)]}))
    assert hubstate.state(key, "/w/diary", spawn=lambda: None) == "on"
    offhub.save_team({"proj-a"}, set())  # a personal machine: the folder keeps it off
    assert hubstate.state(key, "/w/diary", spawn=lambda: None) == "off"


def test_the_start_text_says_it_is_on_and_how_to_go_off(lab, monkeypatch):
    calls = []

    def request(method, url, token, **kw):
        calls.append((url, kw.get("headers") or {}))
        return 200, "TEAM BRIEF"
    monkeypatch.setattr(http, "request", request)
    out = json.loads(hook._run("codex", "start", json.dumps({"session_id": "t1", "cwd": "/w/diary"}),
                               run=lambda *a, **k: type("R", (), {"stdout": ""})()))
    text = out["hookSpecificOutput"]["additionalContext"]
    assert text.startswith("TEAM BRIEF")
    assert "team machine" in text and "$hub off" in text and "this session" in text
    assert unquote(calls[0][1]["X-Kaptyorka-Project"]) == COORD  # never the folder's name
    line = guide.on_hub("claude:s1", "proj-a", False)
    assert "team machine" in line and "/hub off" in line  # a team project's folder too


def test_the_stop_hook_of_an_auto_coordinator_sends_no_git(lab, monkeypatch):
    calls = []
    monkeypatch.setattr(http, "request",
                        lambda *a, **k: calls.append((a, k)) or (200, {"decision": "ok"}))
    ran = []
    hook._run("codex", "stop", json.dumps({"session_id": "s1", "cwd": "/w/diary"}),
              run=lambda *a, **k: ran.append(a))
    assert calls and ran == []
    (_, url, *_), _ = calls[0]
    assert url == "http://h/api/done-check"
