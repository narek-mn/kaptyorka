"""Only sessions in a team project's folder reach the hub; every other folder is personal."""
import json
import os

import anyio
import pytest

from helpers import call, call_error, run
from kaptyorka import admin_ops, hook, http, offhub, paths
from kaptyorka import bridge as bridge_mod
from kaptyorka.adapters.base import Session
from kaptyorka.bridge import Bridge
from test_bridge import FakeAdapter, FakeRun
from test_privacy import as_person, client, ident, me, team  # noqa: F401  (team is a fixture)
from test_shim import shim, text

NOT_TEAM = paths.NOT_TEAM
pytestmark = pytest.mark.team_only


def admin(team):  # noqa: F811
    return team.store.authenticate(team.tokens["admin"])


def register(team, *names):  # noqa: F811
    for n in names:
        team.store.project_register(n, by="admin")


def beat(team, who, body):  # noqa: F811
    status, answer = http.request("POST", team.hub.url + "/bridge/heartbeat",
                                  team.tokens[who], {"machine": "box-1", **body})
    assert status == 200, answer
    return answer


# the hub


def test_tools_refuse_a_personal_folder_and_store_nothing(team):  # noqa: F811
    diary = ident("k:diary", "box-1", project="diary")
    for tool, args in (("who", {}), ("status", {"text": "writing my diary"}),
                       ("notes_write", {"topic": "diary", "text": "secret"}),
                       ("notes_list", {}), ("decide", {"project": "", "text": "x"}),
                       ("send", {"to": team.names["bob"], "text": "hi"}),
                       ("state_write", {"project": "diary", "text": "x"}), ("inbox", {})):
        assert call_error(client(team, "alice"), diary, tool, **args).endswith(NOT_TEAM), tool
    assert team.store.agent("k:diary") is None
    assert all(n["topic"] != "diary" for n in team.store.notes_list())
    assert team.store.inbox(team.keys["bob"]) == []


def test_a_registered_folder_and_its_worktree_work(team):  # noqa: F811
    register(team, "proj-a")
    run(call(client(team, "alice"), me(team, "alice"), "notes_write", topic="t", text="x"))
    tree = ident("k:tree", "box-1", project="proj-a--2")
    run(call(client(team, "alice"), tree, "status", text="on a worktree"))
    assert team.store.agent("k:tree").status == "on a worktree"


def test_the_manager_and_a_hub_worker_are_exempt(team):  # noqa: F811
    # the manager's own account, in its folder (no project of that name)
    run(call(client(team, "manager"), ident("k:mgr", "box-1", project="manager"), "who"))
    # a live worker the hub started, even in a folder that is no team project
    with team.store.lock:
        team.store.db.execute(
            "INSERT INTO workers (name, project, task, boss_key, boss_person, device_id, host,"
            " tool, folder, key, state, created) VALUES"
            " ('w-1','old','t','k:alice',?,1,'box-1','codex','/w/old','k:w1','up',0)",
            (team.ids["alice"],))
    run(call(client(team, "alice"), ident("k:w1", "box-1", project="old"), "status", text="ok"))
    assert team.store.agent("k:w1").status == "ok"


def test_heartbeat_drops_personal_sessions_folders_and_git(team):  # noqa: F811
    register(team, "proj-a")
    answer = beat(team, "alice", {
        "sessions": [{"key": "k:a1", "tool": "codex", "project": "proj-a", "cwd": "/w/proj-a"},
                     {"key": "k:d1", "tool": "codex", "project": "diary", "cwd": "/w/diary"}],
        "folders": {"proj-a": "/w/proj-a", "diary": "/w/diary"},
        "facts": {"host": "box-1", "os_user": "alice", "git": {
            "/w/proj-a": {"branch": "main", "subject": "ok"},
            "/w/diary": {"branch": "main", "subject": "dear diary"}}}})
    assert answer["team"] == ["proj-a"] and answer["keep"] == []
    assert team.store.agent("k:a1") is not None and team.store.agent("k:d1") is None
    with team.store.lock:
        row = team.store.db.execute("SELECT facts, folders FROM device_facts").fetchone()
        git = team.store.db.execute("SELECT subject FROM git_facts").fetchall()
    assert "diary" not in row["folders"] and "diary" not in row["facts"]
    assert [g["subject"] for g in git] == ["ok"]


def test_brief_and_upload_refuse_a_personal_folder(team):  # noqa: F811
    h = {f"X-Kaptyorka-{k.capitalize()}": v
         for k, v in ident("k:diary", "box-1", project="diary").items()}
    status, body = http.request("GET", team.hub.url + "/api/brief?event=start",
                                team.tokens["alice"], headers=h)
    assert status == 400 and body == NOT_TEAM
    status, body, _ = http.request_raw("POST", team.hub.url + "/api/files", team.tokens["alice"],
                                       b"secret", {**h, "X-File-Name": "a.txt"})
    assert status == 403
    assert team.store.agent("k:diary") is None


def test_removing_a_project_takes_its_sessions_off(team):  # noqa: F811
    register(team, "proj-b")
    team.store.grant_project("proj-b", team.ids["alice"], "write", "admin")
    beat(team, "alice", {"sessions": [{"key": "k:b1", "tool": "codex", "project": "proj-b"}]})
    run(call(client(team, "alice"), ident("k:b1", "box-1", project="proj-b"),
             "notes_write", topic="plan", text="kept", project="proj-b"))
    status, done = http.request("DELETE", team.hub.url + "/api/projects/proj-b",
                                team.tokens["admin"])
    assert status == 200, done
    assert done["personal"]["agents"] >= 1
    assert team.store.agent("k:b1") is None
    assert team.store.notes_read("plan", admin(team), "proj-b").text == "kept"  # archived: kept
    answer = beat(team, "alice", {"sessions": [{"key": "k:b1", "tool": "codex",
                                                "project": "proj-b"}]})
    assert "proj-b" not in answer["team"] and team.store.agent("k:b1") is None


# existing data


def test_purge_removes_what_personal_sessions_left(team):  # noqa: F811
    s = team.store
    register(team, "proj-a", "old")
    s.grant_project("old", team.ids["alice"], "write", "admin")
    s.heartbeat("box-1", [{"key": "k:d1", "tool": "codex", "project": "diary"},
                          {"key": "k:o1", "tool": "codex", "project": "old"},
                          {"key": "k:a2", "tool": "codex", "project": "proj-a"}],
                principal=s.authenticate(team.tokens["alice"]))
    s.notes_write("k:o1", "plan", "old plan", "old")
    s.send("k:d1", s.agent("k:a2").name, "from my diary")
    s.send(team.keys["alice"], s.agent("k:d1").name, "to the diary")
    s.send(team.keys["alice"], s.agent("k:a2").name, "team talk")
    with s.lock:
        s.db.execute("UPDATE projects SET archived_at=1 WHERE name='old'")
    done = admin_ops.purge_personal(s)
    assert done["messages"] == 2 and done["agents"] == 2
    keys = {a.key for a in s.online()}
    assert "k:d1" not in keys and "k:o1" not in keys  # an archived project's sessions go too
    assert team.keys["manager"] in keys  # the manager's own session row stays
    assert s.notes_read("plan", admin(team), "old").text == "old plan"
    assert [m.text for m in s.inbox("k:a2")] == ["team talk"]
    assert all(v == 0 for v in admin_ops.purge_personal(s).values())  # nothing left to do


# the bridge


class Answers:
    """Stands in for the hub: records each heartbeat body, answers with `reply`."""

    def __init__(self, monkeypatch, reply):
        self.bodies, self.reply = [], reply

        def request(method, url, token, body=None, **kw):
            if url.endswith("/bridge/heartbeat"):
                self.bodies.append(body)
                return 200, self.reply
            return 404, ""
        monkeypatch.setattr(http, "request", request)


@pytest.fixture
def quiet(monkeypatch):
    from kaptyorka import facts

    facts._cache.clear()
    monkeypatch.setattr(facts.shutil, "which", lambda n: None)
    monkeypatch.setattr(bridge_mod.subprocess, "run", FakeRun())


def sessions():
    return FakeAdapter(sessions=[
        Session("codex:a", "codex", "proj-a", cwd="/w/proj-a"),
        Session("codex:t", "codex", "proj-a--2", cwd="/w/proj-a--2"),
        Session("codex:d", "codex", "diary", cwd="/w/diary"),
        Session("codex:w", "codex", "scratch", cwd="/w/scratch"),
        Session("codex:m", "codex", "manager", cwd=str(paths.home() / paths.MANAGER_DIR))])


def reported(body):
    return sorted(s["key"] for s in body["sessions"])


def test_bridge_reports_nothing_personal_without_a_list(home, quiet, monkeypatch):
    hub = Answers(monkeypatch, {"version": "0"})  # an older hub: no team list
    Bridge({"hub": "http://h", "token": "t", "machine": "box-1"}, [sessions()],
           bridge_mod.log).heartbeat()
    assert len(hub.bodies) == 1 and reported(hub.bodies[0]) == ["codex:m"]  # only the manager
    assert hub.bodies[0]["folders"] == {} and "diary" not in json.dumps(hub.bodies[0])
    assert offhub.personal("proj-a", "/w/proj-a")  # shims and hooks refuse too


def test_bridge_reports_team_folders_workers_and_the_manager(home, quiet, monkeypatch):
    hub = Answers(monkeypatch, {"team": ["proj-a"], "keep": ["codex:w"]})
    Bridge({"hub": "http://h", "token": "t", "machine": "box-1"}, [sessions()],
           bridge_mod.log).heartbeat()
    # the first beat had no list yet; the second, right after, has it
    assert reported(hub.bodies[0]) == ["codex:m"]
    assert reported(hub.bodies[1]) == ["codex:a", "codex:m", "codex:t", "codex:w"]
    assert "diary" not in json.dumps(hub.bodies)
    assert not offhub.personal("proj-a--2", "/w/proj-a--2")
    assert offhub.personal("diary", "/w/diary")
    assert not offhub.personal("scratch", "/w/scratch", "codex:w")


def test_bridge_against_a_hub(team, home, quiet):  # noqa: F811
    register(team, "proj-a")
    cfg = {"hub": team.hub.url, "token": team.tokens["alice"], "machine": "box-1"}
    Bridge(cfg, [FakeAdapter(sessions=[Session("codex:a", "codex", "proj-a", cwd="/w/proj-a"),
                                       Session("codex:d", "codex", "diary", cwd="/w/diary")])],
           bridge_mod.log).heartbeat()
    assert team.store.agent("codex:a") is not None and team.store.agent("codex:d") is None


# this machine's shim and hooks


def test_shim_refuses_a_personal_folder_locally(hub, home, monkeypatch):
    offhub.save_team({"proj-a"}, set())
    diary = {"key": "claude:9", "machine": "pc", "tool": "claude-code", "project": "diary"}

    async def go():
        async with shim(home, hub.url, diary) as c:
            tools = (await c.list_tools()).tools
            assert tools and all(t.description == NOT_TEAM for t in tools)
            r = await c.call_tool("notes_write", {"topic": "t", "text": "x"})
            assert r.is_error and NOT_TEAM in text(r)
    anyio.run(go)
    assert hub.store.agent("claude:9") is None


def test_hooks_are_silent_in_a_personal_folder(home, monkeypatch):
    paths.save_config({"hub": "http://h", "token": "t", "machine": "pc"})
    offhub.save_team({"proj-a"}, set())
    calls = []
    monkeypatch.setattr(http, "request", lambda *a, **k: calls.append(a) or (200, "brief"))
    stdin = json.dumps({"session_id": "s1", "cwd": "/w/diary"})
    assert hook._run("codex", "start", stdin) == ""
    assert calls == []
    stdin = json.dumps({"session_id": "s1", "cwd": "/w/proj-a"})
    assert "brief" in hook._run("codex", "start", stdin)
