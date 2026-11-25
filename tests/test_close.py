"""Closing a session from the page (left.py Closing, hub /api/sessions/<id>/close, bridge.py
_close_asked, ClaudeAdapter.end): who may, what is refused, the order reaching only that
session's own bridge once, and what the row then says."""
import os
import signal
import subprocess
from types import SimpleNamespace

import pytest

from helpers import person, write_session_file
from test_hub_manager import clock, hub  # noqa: F401 (fixtures)
from test_hub_workers import bridge, heartbeat
from kaptyorka import bridge as bridge_mod
from kaptyorka import http, left
from kaptyorka.adapters.base import Session
from kaptyorka.adapters.claude import ClaudeAdapter
from kaptyorka.store import session_id

DAY = 86400


def row(key, idle_s=2 * DAY, status="idle", tool="claude-code"):
    return {"key": key, "tool": tool, "project": "proj-a", "cwd": "/w/proj-a", "status": status,
            "idle_s": idle_s}


def close(hub, token, key, confirm=None):
    body = {} if confirm is None else {"confirm": confirm}
    return http.request("POST", f"{hub.url}/api/sessions/{session_id(key).replace('#', '%23')}/close",
                        token, body)


def feed_row(hub, token, key):
    rows = http.request("GET", hub.url + "/api/feed", token)[1]["agents"]
    return next((r for r in rows if r["id"] == session_id(key)), None)


def asked(hub, b, sessions, extra=None):
    status, body = heartbeat(hub, b.token, b.host, sessions, extra=extra)
    assert status == 200
    return body.get("close_asked", [])


def setup(hub):
    hub.store.project_register("proj-a", by="test")
    alice, alice_t = person(hub.store, "alice")
    bob, bob_t = person(hub.store, "bob")
    _, root_t = person(hub.store, "root", "admin")
    for p in (alice, bob):
        hub.store.grant_project("proj-a", p, "write", "test")
    b = bridge(hub, alice, sessions=[row("claude:i1"), row("claude:w1", idle_s=60)])
    return SimpleNamespace(alice=alice_t, bob=bob_t, root=root_t, b=b)


def test_the_owner_closes_an_idle_one_and_only_its_bridge_hears_it_once(hub):
    s = setup(hub)
    _, other = person(hub.store, "carol")
    carol_b = bridge(hub, hub.store.authenticate(other).person_id, host="box-2", sessions=[])
    assert feed_row(hub, s.alice, "claude:i1")["close"] == "now"
    assert feed_row(hub, s.alice, "claude:w1")["close"] == "ask"
    status, body = close(hub, s.alice, "claude:i1")
    assert status == 202 and body == {"closing": True}
    assert feed_row(hub, s.alice, "claude:i1")["closing"] is True
    assert asked(hub, carol_b, []) == []  # another device: never
    sessions = [row("claude:i1"), row("claude:w1", idle_s=60)]
    assert asked(hub, s.b, sessions) == [{"key": "claude:i1", "confirmed": False}]
    assert asked(hub, s.b, sessions) == []  # once
    with hub.store.lock:
        logged = [(r["person"], r["detail"]) for r in hub.store.db.execute(
            "SELECT * FROM log WHERE kind='session_close'")]
    assert logged == [("alice", f"{hub.store.agent('claude:i1').name} on box-1")]
    # another device's answer is ignored; its own bridge's "" ends it
    heartbeat(hub, carol_b.token, "box-2", [], extra={"close_done": {"claude:i1": ""}})
    assert feed_row(hub, s.alice, "claude:i1")["closing"] is True
    heartbeat(hub, s.b.token, "box-1", [row("claude:w1", idle_s=60)],
              extra={"close_done": {"claude:i1": ""}})
    assert feed_row(hub, s.alice, "claude:i1") is None


def test_a_session_in_use_needs_the_confirm(hub):
    s = setup(hub)
    status, text = close(hub, s.alice, "claude:w1")
    assert status == 409 and "confirm" in text
    assert close(hub, s.alice, "claude:w1", confirm=True)[0] == 202
    assert asked(hub, s.b, [row("claude:i1"), row("claude:w1", idle_s=60)]) == [
        {"key": "claude:w1", "confirmed": True}]


def test_the_admin_closes_anyones_and_another_member_gets_no_such_session(hub):
    s = setup(hub)
    assert feed_row(hub, s.bob, "claude:i1") is None  # a member never sees it
    status, text = close(hub, s.bob, "claude:i1")
    assert (status, text) == (404, "no such session")
    assert close(hub, s.bob, "claude:nope")[0] == 404  # the same answer as for an unknown one
    assert feed_row(hub, s.root, "claude:i1")["close"] == "now"
    assert close(hub, s.root, "claude:i1")[0] == 202


def test_manager_worker_overnight_and_other_tools_are_refused(hub):
    s = setup(hub)
    now = hub.store.clock()
    with hub.store.lock:
        hub.store.db.execute(
            "INSERT INTO workers (name, project, task, boss_key, boss_person, device_id, host, tool,"
            " key, state, created) VALUES ('w-1','proj-a','t','web:alice',1,?,'box-1','claude-code',"
            "'claude:k1','up',?)", (s.b.device_id, now))
    heartbeat(hub, s.b.token, "box-1", [row("claude:i1"), row("claude:k1"), row("claude:n1"),
                                        row("codex:c1", tool="codex")],
              extra={"overnight": ["claude:n1"]})
    for key, why in (("claude:k1", "a hub worker"), ("claude:n1", "overnight mode"),
                     ("codex:c1", "cannot be closed")):
        status, text = close(hub, s.root, key)
        assert status == 409 and why in text, (key, text)
        assert "close" not in feed_row(hub, s.root, key)
    mgr, mgr_t = person(hub.store, "manager", "manager")
    mb = SimpleNamespace(token=mgr_t, host="hub-box")
    assert asked(hub, mb, [row("claude:m1")]) == []
    status, text = close(hub, s.root, "claude:m1")
    assert status == 409 and "manager" in text
    assert asked(hub, mb, [row("claude:m1")]) == []


def test_a_refusal_shows_on_the_row_and_close_comes_back(hub, clock):
    s = setup(hub)
    close(hub, s.alice, "claude:i1")
    sessions = [row("claude:i1"), row("claude:w1", idle_s=60)]
    asked(hub, s.b, sessions)
    heartbeat(hub, s.b.token, "box-1", sessions,
              extra={"close_done": {"claude:i1": "in use again, not closed"}})
    r = feed_row(hub, s.alice, "claude:i1")
    assert r["close_error"] == "in use again, not closed" and "closing" not in r and r["close"]
    assert close(hub, s.alice, "claude:i1")[0] == 202  # tried again
    assert feed_row(hub, s.alice, "claude:i1")["closing"] is True
    assert asked(hub, s.b, sessions) == [{"key": "claude:i1", "confirmed": False}]


def test_no_answer_from_the_bridge_is_said_after_a_while():
    c = left.Closing()
    a = SimpleNamespace(key="claude:x", device_id=1)
    c.ask(a, "alice", False, 100.0)
    assert c.state("claude:x", 100.0) == {"closing": True}
    assert c.state("claude:x", 100 + left.CLOSE_WAIT_S + 1) == {
        "close_error": "its machine did not answer; try again later"}
    assert c.for_device(1) == []  # given up: not sent any more
    assert c.state("claude:x", 100 + left.CLOSE_WAIT_S + left.SHOWN_S + 10) == {}


# ---------- the bridge ----------

class Ender:
    prefix, tool = "claude", "claude-code"

    def __init__(self):
        self.ended, self.launched = [], set()

    def end(self, key):
        self.ended.append(key)


def make_bridge(tmp_path, monkeypatch, claude):
    monkeypatch.setenv("KAPTYORKA_HOME", str(tmp_path / "kh"))
    (tmp_path / "kh").mkdir(exist_ok=True)
    return bridge_mod.Bridge({"hub": "http://127.0.0.1:9", "token": "t", "machine": "box-1"},
                             [claude], lambda line: None)


def sess(key, idle_s=2 * DAY, activity="idle", tasks=None, cwd="/w/proj-a"):
    return Session(key, "claude-code", "proj-a", activity, "", cwd, idle_s=idle_s, tasks=tasks)


def test_the_bridge_checks_again_then_ends_it(tmp_path, monkeypatch):
    claude = Ender()
    b = make_bridge(tmp_path, monkeypatch, claude)
    monkeypatch.setattr(b, "_team", lambda: (None, {"claude:k1"}))
    monkeypatch.setattr(b, "_overnight", staticmethod(lambda k: k == "claude:n1"))
    monkeypatch.setattr(bridge_mod.paths, "in_manager_folder", lambda c: c == "/mgr")
    b.hub = {"close_asked": [{"key": k, "confirmed": c} for k, c in (
        ("claude:i1", False), ("claude:busy", False), ("claude:task", False), ("claude:new", False),
        ("claude:conf", True), ("claude:k1", True), ("claude:n1", True), ("claude:m1", True),
        ("claude:gone", False))]}
    b._close_asked([sess("claude:i1"), sess("claude:busy", activity="busy"),
                    sess("claude:task", tasks=[{"id": "t"}]), sess("claude:new", idle_s=60),
                    sess("claude:conf", idle_s=5, activity="busy"), sess("claude:k1"),
                    sess("claude:n1"), sess("claude:m1", cwd="/mgr")])
    assert claude.ended == ["claude:i1", "claude:conf"]
    assert b._close_done == {
        "claude:i1": "", "claude:busy": "in use again, not closed",
        "claude:task": "in use again, not closed", "claude:new": "in use again, not closed",
        "claude:conf": "", "claude:k1": "a hub worker: close it with its own close",
        "claude:n1": "overnight mode is on in that session",
        "claude:m1": "the manager is never closed from here", "claude:gone": ""}
    b._close_asked([sess("claude:i1")])
    assert claude.ended == ["claude:i1", "claude:conf"]  # once, until the hub has the answer


def start_of(pid):
    stat = open(f"/proc/{pid}/stat").read()
    return stat[stat.rindex(")") + 2:].split()[19]


def test_end_stops_a_background_job_and_terminates_only_a_proven_session(tmp_path):
    calls = []

    def run(cmd, **kw):
        calls.append(cmd)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    a = ClaudeAdapter(home=tmp_path, run=run)
    write_session_file(tmp_path, os.getpid(), session_id="abcdef12-bg", extra={"kind": "bg"})
    a.end("claude:abcdef12-bg")
    assert calls == [["claude", "stop", "abcdef12"]]
    p = subprocess.Popen(["sleep", "60"])
    try:
        # no start time to compare: never killed, the reason comes back to the row
        write_session_file(tmp_path, p.pid, session_id="fg-1", extra={"kind": "interactive"})
        with pytest.raises(ValueError, match="cannot check that its process is still that session"):
            a.end("claude:fg-1")
        # a wrong start time: a file left behind, its pid is someone else's now
        write_session_file(tmp_path, p.pid, session_id="fg-1",
                           extra={"kind": "interactive", "procStart": "1"})
        with pytest.raises(ValueError, match="no running session"):
            a.end("claude:fg-1")
        assert p.poll() is None
        # the start time matches: that session, ended
        write_session_file(tmp_path, p.pid, session_id="fg-1",
                           extra={"kind": "interactive", "procStart": start_of(p.pid)})
        a.end("claude:fg-1")
        assert p.wait(timeout=10) == -signal.SIGTERM
    finally:
        if p.poll() is None:
            p.kill()
    with pytest.raises(ValueError, match="no running session"):
        a.end("claude:not-here")  # nothing ended is never reported as closed
    assert len(calls) == 1


def test_a_stale_file_never_hides_the_live_one(tmp_path):
    p = subprocess.Popen(["sleep", "60"])
    try:
        a = ClaudeAdapter(home=tmp_path, run=None)
        write_session_file(tmp_path, 1, session_id="fg-2", extra={"procStart": "1"})  # sorts first
        write_session_file(tmp_path, p.pid, session_id="fg-2", extra={"procStart": start_of(p.pid)})
        a.end("claude:fg-2")
        assert p.wait(timeout=10) == -signal.SIGTERM
    finally:
        if p.poll() is None:
            p.kill()


def test_a_dead_pids_file_never_hides_the_live_one(tmp_path):
    p = subprocess.Popen(["sleep", "60"])
    try:
        dead = next(c for c in range(100000, 4000000, 7919)
                    if str(c) < str(p.pid) and not os.path.exists(f"/proc/{c}"))
        a = ClaudeAdapter(home=tmp_path, run=None)
        write_session_file(tmp_path, dead, session_id="fg-4")  # sorts first, no process
        write_session_file(tmp_path, p.pid, session_id="fg-4", extra={"procStart": start_of(p.pid)})
        a.end("claude:fg-4")
        assert p.wait(timeout=10) == -signal.SIGTERM
    finally:
        if p.poll() is None:
            p.kill()


@pytest.mark.parametrize("out,ok", [
    ("grep -rn claude .\n", False), ("vim claude\n", False), ("", False),
    ("/usr/local/bin/claude --resume x\n", True), ("node /x/bin/claude\n", True)])
def test_is_claude_reads_the_program_not_any_word(tmp_path, out, ok):
    a = ClaudeAdapter(home=tmp_path, run=lambda cmd, **kw: SimpleNamespace(
        returncode=0, stdout=out, stderr=""))
    assert a._is_claude(1) is ok


def test_without_proc_ps_must_name_claude(tmp_path, monkeypatch):
    real = os.path.isdir
    monkeypatch.setattr(os.path, "isdir", lambda d: False if d == "/proc" else real(d))
    p = subprocess.Popen(["sleep", "60"])
    try:
        said = {"out": "sleep 60\n"}
        a = ClaudeAdapter(home=tmp_path, run=lambda cmd, **kw: SimpleNamespace(
            returncode=0, stdout=said["out"], stderr=""))
        write_session_file(tmp_path, p.pid, session_id="fg-3")
        with pytest.raises(ValueError, match="cannot check"):
            a.end("claude:fg-3")
        assert p.poll() is None
        said["out"] = "/opt/homebrew/bin/claude --model haiku\n"
        a.end("claude:fg-3")
        assert p.wait(timeout=10) == -signal.SIGTERM
    finally:
        if p.poll() is None:
            p.kill()


def test_the_bridge_puts_the_reason_on_the_row(tmp_path, monkeypatch):
    class Refuses(Ender):
        def end(self, key):
            raise ValueError("cannot check that its process is still that session")

    b = make_bridge(tmp_path, monkeypatch, Refuses())
    b.hub = {"close_asked": [{"key": "claude:i1", "confirmed": True}]}
    b._close_asked([sess("claude:i1")])
    assert b._close_done == {"claude:i1": "cannot check that its process is still that session"}


def test_a_failed_claude_stop_says_why(tmp_path):
    a = ClaudeAdapter(home=tmp_path, run=lambda cmd, **kw: SimpleNamespace(
        returncode=1, stdout="", stderr="No session matching 'abcdef12'."))
    write_session_file(tmp_path, os.getpid(), session_id="abcdef12-bg", extra={"kind": "bg"})
    with pytest.raises(ValueError, match="No session matching"):
        a.end("claude:abcdef12-bg")
