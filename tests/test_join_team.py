"""/hub team [project]: a session in a folder that is no team project joins the team, one
session at a time, after the hub checked the person's role."""
import json
import os
import subprocess
import sys

import anyio
import pytest
from mcp import Client, StdioServerParameters

from helpers import call, call_error, person, run
from kaptyorka import hook, http, offhub, optin, paths
from kaptyorka import bridge as bridge_mod
from kaptyorka.adapters.base import Session
from kaptyorka.bridge import Bridge
from kaptyorka.store import session_id
from test_bridge import FakeAdapter
from test_privacy import as_person, client, ident, me, team  # noqa: F401  (team is a fixture)
from test_shim import text
from test_team_only import Answers, quiet, register  # noqa: F401  (quiet is a fixture)

pytestmark = pytest.mark.team_only
COORD = paths.COORDINATOR


def join(team, who, project):  # noqa: F811
    return http.request("POST", team.hub.url + "/bridge/join-team", team.tokens[who],
                        {"project": project})


def team_machine(team):  # noqa: F811
    """A team machine with one session in proj-a and one in proj-b."""
    s = team.store
    _, code = s.team_machine_add("jet-1", "admin")
    token, _ = s.invite_redeem(code, "jet-1", host="jet-1", os_user="lab")
    p = s.authenticate(token)
    s.seen("t-a", "jet-1", "opencode", "proj-a", principal=p)
    s.seen("t-b", "jet-1", "claude", "proj-b", principal=p)
    return as_person(team.hub, token)


# the hub


def test_the_hub_checks_the_role_before_a_join(team):  # noqa: F811
    register(team, "proj-a", "proj-b")
    team.store.grant_project("proj-a", team.ids["alice"], "write", "admin")
    assert join(team, "alice", "proj-a") == (200, {"project": "proj-a", "projects": ["proj-a"]})
    status, why = join(team, "alice", "proj-b")
    assert status == 403 and why == "you have no role in proj-b; your projects: proj-a"
    status, why = join(team, "alice", "diary")
    assert status == 403 and why.startswith("diary is not a team project")
    assert join(team, "alice", "")[0] == 200  # a coordinator needs no role
    assert join(team, "bob", "")[0] == 200
    assert join(team, "admin", "proj-b")[0] == 200  # an admin has every project
    status, _ = http.request("POST", team.hub.url + "/bridge/join-team", "T", {"project": ""})
    assert status == 403  # the old shared token speaks for nobody


def test_the_refusal_guides_with_only_the_persons_projects(team):  # noqa: F811
    register(team, "proj-a", "proj-b")
    team.store.grant_project("proj-a", team.ids["alice"], "write", "admin")
    err = call_error(client(team, "alice"), ident("k:diary", "box-1", project="diary"), "who")
    assert paths.NOT_TEAM in err
    assert "/hub team (to coordinate across projects) or /hub team <project> (you have:" \
        " proj-a)" in err and "proj-b" not in err
    assert "never switch on your own" in err
    err = call_error(client(team, "bob"), ident("k:d2", "box-1", project="diary"), "who")
    assert "you have no role in a team project yet" in err
    assert team.store.agent("k:diary") is None and team.store.agent("k:d2") is None


def test_a_coordinator_reaches_team_sessions_and_gets_answers(team):  # noqa: F811
    register(team, "proj-a", "proj-b")
    for p in ("proj-a", "proj-b"):
        team.store.grant_project(p, team.ids["alice"], "write", "admin")
    jet = team_machine(team)
    coord = ident("k:coord", "box-1", project=COORD)
    rows = run(call(client(team, "alice"), coord, "who"))
    keys = {r["id"] for r in rows}
    assert {session_id("t-a"), session_id("t-b"), session_id(team.keys["alice"])} <= keys
    assert session_id(team.keys["bob"]) not in keys
    for t in ("t-a", "t-b"):
        run(call(client(team, "alice"), coord, "send", to=session_id(t), text="run the tests"))
    # the team sessions answer it by its id (the one hop back), though they do not list it
    for t, project, tool in (("t-a", "proj-a", "opencode"), ("t-b", "proj-b", "claude")):
        run(call(jet, {"key": t, "machine": "jet-1", "tool": tool, "project": project},
                 "send", to=session_id("k:coord"), text=f"done in {project}"))
    assert sorted(m.text for m in team.store.inbox("k:coord")) == ["done in proj-a",
                                                                    "done in proj-b"]
    assert team.store.agent("k:coord").cwd == ""


def test_a_coordinator_names_the_project_for_notes_and_work(team):  # noqa: F811
    register(team, "proj-a")
    team.store.grant_project("proj-a", team.ids["alice"], "write", "admin")
    coord, alice = ident("k:coord", "box-1", project=COORD), client(team, "alice")
    run(call(alice, coord, "notes_write", topic="plan", text="team-wide"))
    assert run(call(alice, coord, "notes_read", topic="plan"))["text"] == "team-wide"
    run(call(alice, coord, "notes_write", topic="plan", text="for a", project="proj-a"))
    assert run(call(alice, coord, "notes_read", topic="plan", project="proj-a"))["text"] == "for a"
    assert "not registered" in call_error(alice, coord, "notes_write", topic="x", text="y",
                                          project=COORD)
    assert "not registered" in call_error(alice, coord, "state", project=COORD)
    for project in ("", COORD):
        err = call_error(alice, coord, "work", project=project, task="t")
        assert "name the project" in err
    err = call_error(alice, coord, "work", project="proj-b", task="t")
    assert "you may not start workers in proj-b" in err  # the role check stays


def test_a_coordinator_without_roles_reaches_only_its_own(team):  # noqa: F811
    register(team, "proj-a")
    team_machine(team)
    coord = ident("k:bc", "box-1", project=COORD)
    rows = run(call(client(team, "bob"), coord, "who"))
    assert {r["person"] for r in rows} <= {"bob", "manager"}
    err = call_error(client(team, "bob"), coord, "send", to=session_id("t-a"), text="hi")
    assert "that you can reach" in err


# this machine: the shim, the bridge, the hooks and /hub


def shim(home, hub_url, token, who):
    paths.save_config({"hub": hub_url, "token": token, "machine": "box-1"})
    env = {"HOME": str(home), "KAPTYORKA_HOME": os.environ["KAPTYORKA_HOME"],
           "KAPTYORKA_TEST_IDENTITY": json.dumps(who)}
    return Client(StdioServerParameters(
        command=sys.executable, args=["-c", "from kaptyorka.cli import entry; entry()", "mcp"],
        env=env))


def test_the_shim_joins_lists_messages_notes_and_leaves(hub, home):
    s = hub.store
    alice, token = person(s, "alice")
    s.project_register("proj-a", by="admin")
    s.project_register("proj-b", by="admin")
    s.grant_project("proj-a", alice, "write", "admin")
    offhub.save_team({"proj-a", "proj-b"}, set(), ["proj-a"])
    s.seen("k:alice2", "box-2", "codex", "proj-a", principal=s.authenticate(
        s.device_add(alice, "box-2")))  # her own session in the project, elsewhere
    diary = {"key": "claude:7", "machine": "box-1", "tool": "claude-code", "project": "diary",
             "cwd": "/w/diary", "pid": os.getpid()}
    twin = {**diary, "key": "claude:8"}  # another session in the same folder

    async def go():
        async with shim(home, hub.url, token, diary) as c:
            assert "join_team" in {t.name for t in (await c.list_tools()).tools}
            r = await c.call_tool("notes_list", {})
            assert r.is_error and "(you have: proj-a)" in text(r) and "proj-b" not in text(r)
            r = await c.call_tool("join_team", {"project": "proj-b"})
            assert r.is_error and "no role in proj-b" in text(r)
            r = await c.call_tool("join_team", {"project": "proj-a"})
            assert not r.is_error and "project proj-a" in text(r), text(r)
            r = await c.call_tool("notes_write", {"topic": "t", "text": "x",
                                                  "project": "proj-a"})
            assert not r.is_error, text(r)
            r = await c.call_tool("who", {})
            assert any(row["project"] == "proj-a" and row["you"]
                       for row in r.structured_content["result"])
            r = await c.call_tool("send", {"to": s.agent("k:alice2").name,
                                           "text": "hello proj-a"})
            assert not r.is_error, text(r)
        assert s.agent("claude:7").project == "proj-a" and s.agent("claude:7").cwd == ""
        async with shim(home, hub.url, token, twin) as c:  # never the other session
            r = await c.call_tool("status", {"text": "x"})
            assert r.is_error and paths.NOT_TEAM in text(r)
        async with shim(home, hub.url, token, diary) as c:  # a restarted shim keeps it
            r = await c.call_tool("status", {"text": "still here"})
            assert not r.is_error, text(r)
            r = await c.call_tool("join_team", {"project": "off"})
            assert not r.is_error and "left the team" in text(r)
            r = await c.call_tool("status", {"text": "x"})
            assert r.is_error and paths.NOT_TEAM in text(r)
    anyio.run(go)
    assert s.agent("claude:8") is None
    assert s.agent("claude:7") is None  # dropped from the hub at once
    assert [m.text for m in s.inbox("k:alice2")] == ["hello proj-a"]


def test_the_bridge_reports_only_the_joined_session_and_nothing_of_its_folder(
        home, quiet, monkeypatch):  # noqa: F811
    hub = Answers(monkeypatch, {"team": ["proj-a"], "keep": [], "projects": ["proj-a", "old"]})
    offhub.save_team({"proj-a"}, set())
    optin.save("codex:j", "proj-a", os.getpid())
    optin.save("claude:c", "", os.getpid())
    adapters = [FakeAdapter(sessions=[Session("codex:j", "codex", "diary", cwd="/w/diary"),
                                      Session("codex:d", "codex", "diary", cwd="/w/diary")]),
                FakeAdapter("claude-code", "claude", [
                    Session("claude:c", "claude-code", "notes", cwd="/w/notes",
                            title="Plan the week")])]
    Bridge({"hub": "http://h", "token": "t", "machine": "box-1"}, adapters,
           bridge_mod.log).heartbeat()
    body = hub.bodies[-1]
    got = {s["key"]: s for s in body["sessions"]}
    assert sorted(got) == ["claude:c", "codex:j"]
    assert (got["codex:j"]["project"], got["codex:j"]["cwd"]) == ("proj-a", "")
    assert (got["claude:c"]["project"], got["claude:c"]["label"], got["claude:c"]["title"]) == \
        (COORD, "coordinator", "Plan the week")
    sent = json.dumps(body)
    assert "/w/diary" not in sent and "/w/notes" not in sent and "diary" not in sent
    assert body["facts"]["git"] == {} and body["folders"] == {}
    assert offhub.load_mine() == ["proj-a"]  # only registered ones, for the help text
    optin.clear("codex:j")
    Bridge({"hub": "http://h", "token": "t", "machine": "box-1"}, adapters,
           bridge_mod.log).heartbeat()
    assert sorted(s["key"] for s in hub.bodies[-1]["sessions"]) == ["claude:c"]  # off reverts


def test_a_join_ends_with_its_process(home):
    p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        optin.save("codex:x", "proj-a", p.pid)
        assert optin.get("codex:x") == "proj-a"
    finally:
        p.kill()
        p.wait()
    assert optin.get("codex:x") is None and optin.joined() == []
    assert optin.get("codex:never") is None


def test_hub_team_command(home, monkeypatch):
    paths.save_config({"hub": "http://h", "token": "t", "machine": "box-1"})
    sessions = home / ".claude" / "sessions"
    sessions.mkdir(parents=True)
    (sessions / f"{os.getpid()}.json").write_text(json.dumps(
        {"sessionId": "s1", "cwd": "/w/diary", "procStart": optin.proc_start(os.getpid())}))
    calls = []

    def request(method, url, token, body=None, **kw):
        calls.append((url, body))
        if url.endswith("/bridge/join-team"):
            if body["project"] in ("", "proj-a"):
                return 200, {"project": body["project"], "projects": ["proj-a"]}
            return 403, "you have no role in proj-b; your projects: proj-a"
        return 204, None
    monkeypatch.setattr(http, "request", request)
    env = {"CLAUDE_CODE_SESSION_ID": "s1"}
    assert offhub.command(["team", "proj-b"], env) == (
        1, "kaptyorka: you have no role in proj-b; your projects: proj-a")
    code, line = offhub.command(["team", "proj-a"], env)
    assert code == 0 and "project proj-a" in line and optin.get("claude:s1") == "proj-a"
    assert offhub.command([], env) == (0, "this session is on the hub for project proj-a."
                                       " /hub team off leaves the team; the agent reaches the"
                                       " team with who and send")
    code, line = offhub.command(["team"], env)
    assert code == 0 and "coordinator" in line and optin.get("claude:s1") == COORD
    code, line = offhub.command(["team", "off"], env)
    assert code == 0 and "left the team" in line and optin.get("claude:s1") is None
    assert calls[-1] == ("http://h/bridge/heartbeat", {
        "machine": "box-1", "sessions": [], "tools": [], "gone": ["claude:s1"]})
    assert offhub.command(["team", "a", "b"], env)[0] == 1


def _join_hub(monkeypatch):
    def request(method, url, token, body=None, **kw):
        if url.endswith("/bridge/join-team"):
            if body["project"] in ("", "proj-a"):
                return 200, {"project": body["project"], "projects": ["proj-a"]}
            return 403, "you have no role in proj-b; your projects: proj-a"
        return 204, None
    monkeypatch.setattr(http, "request", request)


def test_hub_team_in_opencode_codex_and_cursor(home, monkeypatch):
    paths.save_config({"hub": "http://h", "token": "t", "machine": "box-1"})
    _join_hub(monkeypatch)
    monkeypatch.setattr(offhub, "_tool_pid", lambda key: os.getpid())  # the tool's process
    for env, key, cmd in (({offhub.OPENCODE_SESSION: "ses_1"}, "opencode:ses_1", "/hub"),
                          ({offhub.SESSION_KEY: "codex:t-1"}, "codex:t-1", "$hub"),
                          ({offhub.SESSION_KEY: "cursor:box-1:/w/diary"}, "cursor:box-1:/w/diary",
                           "/hub")):
        assert offhub.command(["team", "proj-b"], env, cwd="/w/diary")[0] == 1
        code, line = offhub.command(["team", "proj-a"], env, cwd="/w/diary")
        assert code == 0 and "project proj-a" in line and optin.get(key) == "proj-a"
        assert offhub.command([], env, cwd="/w/diary")[1].startswith(
            "this session is on the hub for project proj-a. ")
        assert offhub.command(["team"], env, cwd="/w/diary")[0] == 0 and optin.get(key) == COORD
        code, line = offhub.command(["team", "off"], env, cwd="/w/diary")
        assert code == 0 and "left the team" in line and optin.get(key) is None
        offhub.switch_key(key, True)  # an off session must come back first
        assert offhub.command(["team"], env, cwd="/w/diary") == (
            1, f"kaptyorka: {'this folder' if key.startswith('cursor') else 'this session'}"
               f" is off the hub; type {cmd} on to come back")
        offhub.switch_key(key, False)


def test_hub_team_gets_more_time_than_on_and_off(home, monkeypatch):
    assert hook.hub_timeout(["team", "proj-a"]) == hook.hub_timeout(["team"]) == hook.HUB_TEAM_S
    assert hook.hub_timeout([]) == hook.hub_timeout(["off"]) == hook.TIMEOUT_S
    assert optin.JOIN_S < hook.HUB_TEAM_S  # the hub's answer comes before the hook gives up
    seen = []

    def run(argv, **kw):
        seen.append((argv[2:], kw["timeout"]))
        return subprocess.CompletedProcess(argv, 0, "ok\n", "")
    who = hook.Identity("codex:t-1", "box-1", "codex", "diary", "/w/diary")
    hook._hub_line(who, ["team", "proj-a"], run)
    hook._hub_line(who, ["on"], run)
    assert seen == [(["team", "proj-a"], hook.HUB_TEAM_S), (["on"], hook.TIMEOUT_S)]
    timeouts = []
    monkeypatch.setattr(http, "request", lambda *a, **kw: timeouts.append(kw["timeout"]) or (
        200, {"project": "", "projects": []}))
    optin.join({"hub": "http://h", "token": "t"}, "codex:t-1", os.getpid(), "")
    assert timeouts == [optin.JOIN_S]


def test_codex_lines_say_dollar_hub_team(home, monkeypatch):
    paths.save_config({"hub": "http://h", "token": "t", "machine": "box-1"})
    _join_hub(monkeypatch)
    monkeypatch.setattr(offhub, "_tool_pid", lambda key: os.getpid())
    code, line = offhub.command(["team"], {offhub.SESSION_KEY: "codex:t-1"}, cwd="/w/diary")
    assert code == 0 and line.endswith("$hub team off (or join_team(\"off\")) leaves it")
    code, line = offhub.command(["team"], {offhub.OPENCODE_SESSION: "ses_1"}, cwd="/w/diary")
    assert code == 0 and line.endswith("/hub team off (or join_team(\"off\")) leaves it")
    assert "run $hub team (to coordinate" in paths.not_team_help(["proj-a"], "$hub")
    assert offhub.come_back("codex:t-1").endswith("type $hub on to come back")
    assert offhub.come_back("claude:c1") == offhub.COME_BACK


def test_a_join_needs_the_tools_process(home, monkeypatch):
    paths.save_config({"hub": "http://h", "token": "t", "machine": "box-1"})
    _join_hub(monkeypatch)
    monkeypatch.setattr(offhub, "_tool_pid", lambda key: 0)
    assert offhub.command(["team"], {offhub.SESSION_KEY: "codex:t-1"}, cwd="/w/diary") == (
        1, "kaptyorka: could not find this session's process; try again")


def test_hub_team_through_the_codex_and_cursor_prompt_hooks(home, monkeypatch, capsys):
    from test_hub_toggle import HubCli

    paths.save_config({"hub": "http://h", "token": "t", "machine": "box-1"})
    _join_hub(monkeypatch)
    monkeypatch.setattr(offhub, "_tool_pid", lambda key: os.getpid())
    stdin = json.dumps({"session_id": "t-1", "cwd": "/w/diary", "prompt": "$hub team proj-a"})
    assert hook.main("codex", "prompt", stdin, run=HubCli()) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["decision"] == "block" and "project proj-a" in out["reason"]
    assert optin.get("codex:t-1") == "proj-a"
    stdin = {"workspace_roots": ["/w/diary"], "prompt": "/hub team"}
    hook.main("cursor", "prompt", json.dumps(stdin), run=HubCli())
    out = json.loads(capsys.readouterr().out)
    assert out["continue"] is False and "coordinator" in out["user_message"]
    assert optin.get("cursor:box-1:/w/diary") == COORD


def test_a_joined_sessions_stop_hook_sends_no_git(home, monkeypatch):
    paths.save_config({"hub": "http://h", "token": "t", "machine": "pc"})
    offhub.save_team({"proj-a"}, set())
    optin.save("codex:s1", "proj-a", os.getpid())
    calls = []
    monkeypatch.setattr(http, "request",
                        lambda *a, **k: calls.append((a, k)) or (200, {"decision": "ok"}))
    ran = []
    stdin = json.dumps({"session_id": "s1", "cwd": "/w/diary"})
    hook._run("codex", "stop", stdin, run=lambda *a, **k: ran.append(a))
    assert calls and ran == []  # never read git, never sent the folder
    (_, url, *_), kw = calls[0]
    assert url == "http://h/api/done-check"
    assert kw["headers"]["X-Kaptyorka-Project"] == "proj-a"


def test_a_hub_restart_keeps_a_coordinator(team):  # noqa: F811
    from kaptyorka import admin_ops

    run(call(client(team, "alice"), ident("k:coord", "box-1", project=COORD), "status",
             text="planning"))
    admin_ops.purge_personal(team.store)
    assert team.store.agent("k:coord") is not None
