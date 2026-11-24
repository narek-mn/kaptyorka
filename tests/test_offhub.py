"""Keep a Claude Code session off the hub: `/hub off`, `/hub on`, KAPTYORKA_HUB=off at start."""
import json
import os
import subprocess
import sys
from pathlib import Path

import anyio
import pytest

from helpers import call, call_error, run, tool_names, write_session_file
from kaptyorka import bridge as bridge_mod
from kaptyorka import hook, http, offhub, paths
from kaptyorka.adapters.claude import COMMAND_MARK, ClaudeAdapter
from kaptyorka.bridge import Bridge, install_commands
from kaptyorka.identity import Identity
from test_bridge import FakeRun
from test_privacy import client, feed, me, team  # noqa: F401  (team is a fixture)
from test_shim import shim, text

pytestmark = pytest.mark.skipif(not Path("/proc/self/stat").exists(), reason="needs /proc")


def start_of(pid: int) -> str:
    stat = Path(f"/proc/{pid}/stat").read_text()
    return stat[stat.rindex(")") + 2:].split()[19]


@pytest.fixture
def procs():
    """Start stand-in session processes (`sleep`), with or without KAPTYORKA_HUB=off."""
    started = []

    def start(hub_env=None):
        env = {k: v for k, v in os.environ.items() if k != offhub.ENV}
        if hub_env is not None:
            env[offhub.ENV] = hub_env
        p = subprocess.Popen(["sleep", "60"], env=env)
        started.append(p)
        return p
    yield start
    for p in started:
        p.kill()
        p.wait()


def session(home, p, sid, cwd="/w/proj-a", **extra):
    write_session_file(home, p.pid, cwd=cwd, session_id=sid, name="secret-desk",
                       extra={"procStart": start_of(p.pid), **extra})
    return {"CLAUDE_CODE_SESSION_ID": sid}


class Sent:
    """Records every request the bridge and the command make, then makes it."""

    def __init__(self, monkeypatch):
        self.bodies = []
        real = http.request

        def request(method, url, token, body=None, **kw):
            self.bodies.append(json.dumps(body))
            return real(method, url, token, body, **kw)
        monkeypatch.setattr(http, "request", request)

    def mentions(self, *words) -> bool:
        return any(w in b for b in self.bodies for w in words)


@pytest.fixture
def alice_box(team, home, monkeypatch):  # noqa: F811
    """alice's bridge on box-1 with the real Claude Code adapter."""
    from kaptyorka import facts

    facts._cache.clear()
    monkeypatch.setattr(facts.shutil, "which", lambda n: None)
    monkeypatch.setattr(bridge_mod.subprocess, "run", FakeRun())
    cfg = {"hub": team.hub.url, "token": team.tokens["alice"], "machine": "box-1"}
    paths.save_config(cfg)
    return lambda: Bridge(dict(cfg), [ClaudeAdapter(home=home)], bridge_mod.log)


def visible(team, key) -> dict:  # noqa: F811
    """Where the session shows, per viewer: feed agents and who (with others)."""
    out = {}
    label = "secret-desk"
    for who in ("alice", "admin", "manager"):
        in_feed = any(a.get("label") == label for a in feed(team, who)["agents"])
        rows = run(call(client(team, who), me(team, who), "who"))
        out[who] = in_feed or any(r.get("label") == label for r in rows)
    out["store"] = team.store.agent(key) is not None
    return out


def test_off_hides_from_everyone_and_on_brings_it_back(team, alice_box, home, procs):  # noqa: F811
    env = session(home, procs(), "s1")
    bridge = alice_box()
    bridge.heartbeat()
    assert visible(team, "claude:s1") == {"alice": True, "admin": True, "manager": True,
                                          "store": True}
    name = team.store.agent("claude:s1").name
    code, line = offhub.command(["off"], env)
    assert code == 0 and "off the hub" in line
    # gone at once, removed rather than shown offline, before the bridge beats again
    assert visible(team, "claude:s1") == {"alice": False, "admin": False, "manager": False,
                                          "store": False}
    bridge.heartbeat()
    assert not any(a.key == "claude:s1" for a in team.store.online())
    assert team.store.agent("claude:s1") is None
    err = call_error(client(team, "admin"), me(team, "admin"), "send", to=name, text="hi")
    assert "no session named" in err
    assert offhub.command([], env) == (0, offhub.COME_BACK)
    assert offhub.command(["on"], env)[0] == 0
    bridge.heartbeat()
    assert visible(team, "claude:s1")["admin"] is True
    assert offhub.command([], env) == (0, "this session is on the hub. Project proj-a; /hub off takes it off; the agent reaches the team with who and send")


def test_off_session_sends_nothing_after_the_hub_dropped_it(team, alice_box, home, procs,  # noqa: F811
                                                           monkeypatch):
    env = session(home, procs(), "s1", cwd="/w/hidden-proj")
    bridge = alice_box()
    bridge.heartbeat()
    offhub.command(["off"], env)
    bridge.heartbeat()  # this one still asks the hub to drop it: the command's call may be lost
    sent = Sent(monkeypatch)
    bridge.heartbeat()
    bridge.heartbeat()
    assert sent.bodies and not sent.mentions("s1", "hidden-proj", "secret-desk")


def test_messages_are_not_pushed_and_hooks_are_silent(team, alice_box, home, procs,  # noqa: F811
                                                      monkeypatch, capsys):
    env = session(home, procs(), "s1")
    bridge = alice_box()
    bridge.heartbeat()
    offhub.command(["off"], env)
    adapter = bridge.adapters[0]
    adapter.sessions()
    with pytest.raises(Exception, match="off the hub"):
        adapter.deliver("claude:s1", "hi")

    def no_hub(*a, **k):
        raise AssertionError("an off session's hook must not call the hub")
    monkeypatch.setattr(hook.http, "request", no_hub)
    monkeypatch.setattr(hook, "identify", lambda machine, workspace=None:
                        Identity("claude:s1", machine, "claude-code", "proj-a"))
    stdin = json.dumps({"session_id": "s1", "cwd": "/w/proj-a"})
    for event in ("start", "prompt", "stop"):
        assert hook.main("claude-code", event, stdin) == 0
    # only the start hook says so, from this machine alone (guide.py)
    assert capsys.readouterr().out == "kaptyorka hub: this session is off the hub; /hub on brings it back.\n"


def test_started_with_env_off_is_never_reported(team, alice_box, home, procs,  # noqa: F811
                                                monkeypatch):
    sent = Sent(monkeypatch)
    env = session(home, procs("off"), "s2", cwd="/w/hidden-proj")
    bridge = alice_box()
    bridge.heartbeat()
    alice_box().heartbeat()  # a restarted bridge
    assert sent.bodies and not sent.mentions("s2", "hidden-proj", "secret-desk")
    assert team.store.agent("claude:s2") is None
    assert offhub.command([], {**env, offhub.ENV: "off"}) == (0, offhub.COME_BACK)
    offhub.command(["on"], env)  # the person's choice beats the variable
    bridge.heartbeat()
    assert team.store.agent("claude:s2") is not None


def test_background_session_ignores_the_variable(team, alice_box, home, procs):  # noqa: F811
    env = session(home, procs("off"), "s3", kind="bg")
    alice_box().heartbeat()
    assert team.store.agent("claude:s3") is not None
    code, line = offhub.command([], {**env, offhub.ENV: "off"})
    assert line.startswith("this session is on the hub (a background session ignores")


def test_off_survives_a_bridge_restart(team, alice_box, home, procs):  # noqa: F811
    env = session(home, procs(), "s1")
    alice_box().heartbeat()
    offhub.command(["off"], env)
    alice_box().heartbeat()
    alice_box().heartbeat()
    assert team.store.agent("claude:s1") is None


def test_ended_session_cleans_up_and_a_reused_id_shows(team, alice_box, home, procs):  # noqa: F811
    p = procs()
    env = session(home, p, "s1")
    offhub.command(["off"], env)
    assert (paths.home() / "off" / "s1").exists()
    p.kill()
    p.wait()
    alice_box().heartbeat()
    assert not (paths.home() / "off" / "s1").exists()
    session(home, procs(), "s1")  # the same id in a new process: `claude --resume`
    alice_box().heartbeat()
    assert team.store.agent("claude:s1") is not None


def test_off_follows_the_process_to_a_new_id_after_clear(home, procs):
    p = procs()
    env = session(home, p, "s1")
    offhub.command(["off"], env)
    (home / ".claude" / "sessions" / f"{p.pid}.json").unlink()
    session(home, p, "s1-new")
    assert offhub.off("claude:s1-new", {})


def test_worker_and_manager_refuse_off(home, procs):
    (paths.home() / "workers").mkdir(parents=True)
    (paths.home() / "workers" / "w1.log").write_text("")
    p = procs("off")
    write_session_file(home, p.pid, session_id="w", name="w1",
                       extra={"procStart": start_of(p.pid), "kind": "bg"})
    code, line = offhub.command(["off"], {"CLAUDE_CODE_SESSION_ID": "w"})
    assert code == 1 and line == "kaptyorka: a worker the hub started stays on the hub"
    mgr = paths.home() / paths.MANAGER_DIR
    mgr.mkdir(parents=True)
    env = session(home, procs("off"), "m", cwd=str(mgr))
    code, line = offhub.command(["off"], env)
    assert code == 1 and line == "kaptyorka: the manager session stays on the hub"
    assert not offhub.off("claude:m", {offhub.ENV: "off"})  # the variable does not hide it either
    assert not (paths.home() / "off" / "w").exists()


def test_command_outside_a_session_and_bad_words(home):
    assert offhub.command(["off"], {})[0] == 1
    assert offhub.command(["maybe"], {}) == (1, "use /hub, /hub on, /hub off, /hub team, /hub team <project>, /hub team off, /hub overnight or /hub overnight off")


def test_no_hub_tool_switches_a_session_off(hub):
    assert not {n for n in tool_names(hub) if "hub" in n or "off" in n}


def test_shim_refuses_while_off_and_sends_nothing(hub, home, procs):
    p = procs()
    env = session(home, p, "s9")
    offhub.switch("s9", p.pid, json.loads((home / ".claude" / "sessions" / f"{p.pid}.json")
                                          .read_text()), True)
    ident = {"key": "claude:s9", "machine": "pc", "tool": "claude-code", "project": "proj-a"}

    async def go():
        async with shim(home, hub.url, ident) as c:
            await c.list_tools()
            for tool in ("who", "send", "notes_write"):
                r = await c.call_tool(tool, {})
                assert r.is_error and text(r) == f"kaptyorka: {offhub.COME_BACK}"
    anyio.run(go)
    assert hub.store.agent("claude:s9") is None
    assert offhub.command(["on"], env)[0] == 0

    async def back():
        async with shim(home, hub.url, ident) as c:
            r = await c.call_tool("who", {})
            assert not r.is_error, text(r)
    anyio.run(back)
    assert hub.store.agent("claude:s9") is not None


def test_hub_drops_only_its_own_machines_sessions(hub):
    s = hub.store
    s.heartbeat("box-1", [{"key": "claude:a", "tool": "claude-code", "project": "p"}])
    s.heartbeat("box-2", [], gone=["claude:a"])
    assert s.agent("claude:a") is not None
    s.heartbeat("box-1", [], gone=["claude:a"])
    assert s.agent("claude:a") is None


def test_hub_command_is_installed_and_removed(home):
    calls = []
    a = ClaudeAdapter(home=home, run=lambda cmd, **kw: calls.append(cmd) or
                      subprocess.CompletedProcess(cmd, 0, "", ""))
    f = home / ".claude" / "commands" / "hub.md"
    lines = a.configure("/opt/kp/kaptyorka")
    assert any("/hub command" in line for line in lines)
    body = f.read_text()
    assert "!`/opt/kp/kaptyorka hub $ARGUMENTS`" in body and "disable-model-invocation: true" in body
    assert a.install_command("/opt/kp/kaptyorka") == []  # idempotent
    logged = []
    install_commands([ClaudeAdapter(home=home, log=logged.append)], "/new/kaptyorka", logged.append)
    assert "/new/kaptyorka hub" in f.read_text() and logged  # a moved install is rewritten
    a.unconfigure()
    assert not f.exists()
    f.write_text("my own /hub")
    assert ClaudeAdapter(home=home, log=logged.append).install_command("/x/kaptyorka") == []
    assert f.read_text() == "my own /hub"
    assert COMMAND_MARK not in f.read_text()


def test_cli_hub_prints_one_line(home, monkeypatch, capsys):
    from kaptyorka import cli

    monkeypatch.setattr(sys, "argv", ["kaptyorka", "hub", "sideways"])
    with pytest.raises(SystemExit) as e:
        cli.entry()
    assert e.value.code == 1 and capsys.readouterr().out == "use /hub, /hub on, /hub off, /hub team, /hub team <project>, /hub team off, /hub overnight or /hub overnight off\n"


def test_bridge_drops_it_when_the_commands_own_call_was_lost(team, alice_box, home, procs,  # noqa: F811
                                                             monkeypatch):
    env = session(home, procs(), "s1")
    bridge = alice_box()
    bridge.heartbeat()
    monkeypatch.setattr(offhub, "_tell_now", lambda data: None)  # the hub was down then
    offhub.command(["off"], env)
    assert team.store.agent("claude:s1") is not None
    bridge.heartbeat()
    assert team.store.agent("claude:s1") is None
    assert offhub.gone() == []  # asked once the hub answered


def test_start_hook_saves_the_variable_for_bridges_without_proc(home, procs, monkeypatch, capsys):
    session(home, procs(), "s4")  # its process shows no variable, as outside Linux
    monkeypatch.setenv(offhub.ENV, "off")
    monkeypatch.setattr(hook, "identify", lambda machine, workspace=None:
                        Identity("claude:s4", machine, "claude-code", "proj-a"))
    paths.save_config({"hub": "http://127.0.0.1:9", "token": "T", "machine": "pc"})
    hook.main("claude-code", "start", json.dumps({"session_id": "s4"}))
    assert json.loads((paths.home() / "off" / "s4").read_text())["off"] is True
    assert ClaudeAdapter(home=home).sessions() and offhub.off("claude:s4", {})
