"""The hub keeps the manager session alive through the admin's own bridge on its machine."""
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import kaptyorka
from conftest import start_hub
from helpers import call, call_error, person, run
from test_hub_workers import BOSS, as_token, bridge, heartbeat, ident, member, poll
from kaptyorka import http, identity, manager, paths
from kaptyorka.hub import MANAGER_FAILED, MANAGER_FIRST
from kaptyorka.hub_feed import feed
from kaptyorka.manager import ManagerStore
from kaptyorka.rules import RulesStore
from kaptyorka.store import ONLINE_S, Store
from kaptyorka.workers import GONE_S, WorkerStore

HOST = "box-1"
URL = "http://127.0.0.1:9999"


@pytest.fixture
def clock():
    return [1_000_000.0]


@pytest.fixture
def hub(tmp_path, clock, monkeypatch):
    monkeypatch.setenv("KAPTYORKA_HOME", str(tmp_path / "kh"))
    monkeypatch.setattr(identity, "facts", lambda: {"host": HOST, "os_user": "u",
                                                    "version": kaptyorka.__version__})
    store = Store(tmp_path / "hub.db", clock=lambda: clock[0], legacy_token="T")
    mm = ManagerStore(store)
    mm.set_enabled(False, "test")  # the start-up tick must not race the test; tests switch it on
    stop = start_hub(store, manager_url=URL)
    yield SimpleNamespace(url=f"http://127.0.0.1:{stop.port}", store=store, mm=mm,
                          ws=WorkerStore(store), tick=stop.app.state.tick,
                          folder=tmp_path / "kh" / manager.DIR,
                          config=tmp_path / "kh" / manager.CONFIG)
    stop()
    store.close()


def admin_bridge(hub):
    pid, _ = person(hub.store, "root", "admin")
    code = hub.store.invite_create(pid)
    token, _ = hub.store.invite_redeem(code, HOST, host=HOST, os_user="u")
    b = SimpleNamespace(token=token, device_id=hub.store.authenticate(token).device_id)
    beat(hub, b)
    return b


def beat(hub, b, sessions=()):
    body = {"machine": HOST, "sessions": list(sessions), "tools": [],
            "facts": {"host": HOST, "os_user": "u", "version": kaptyorka.__version__}}
    assert http.request("POST", hub.url + "/bridge/heartbeat", b.token, body)[0] == 200


def orders(hub, b):
    status, items = http.request("GET", f"{hub.url}/bridge/poll?machine={HOST}", b.token)
    assert status == 200
    return [i for i in items if i.get("launch")]


def ack(hub, b, item, ok=True, error=""):
    assert http.request("POST", hub.url + "/bridge/ack", b.token,
                        {"id": item["id"], "ok": ok, "launch": True, "error": error})[0] == 204


def page_texts(hub, admin="root"):
    with hub.store.lock:
        return [r["text"] for r in hub.store.db.execute(
            "SELECT text FROM messages WHERE to_key=? ORDER BY id", (f"web:{admin}",))]


def state(hub):
    w = hub.ws.by_name("manager")
    return w and w["state"]


def test_disabled_manager_tick_does_nothing(hub):
    admin_bridge(hub)
    hub.tick()
    assert hub.mm.person_id() is None and not hub.folder.exists() and state(hub) is None


def test_manager_tick_writes_files_and_rewrites_on_rules_change(hub):
    hub.mm.set_enabled(True, "test")
    hub.tick()  # no admin bridge yet: the account and files, but no launch
    assert hub.mm.person_id() is not None and state(hub) is None
    assert os.stat(hub.folder).st_mode & 0o777 == 0o700
    assert not (hub.folder / "config.json").exists()  # outside the session's folder
    assert os.stat(hub.config).st_mode & 0o777 == 0o600
    cfg = json.loads(hub.config.read_text())
    assert cfg["hub"] == URL and cfg["person"] == "manager"
    assert cfg["machine"] == f"manager@{HOST}"
    assert hub.store.authenticate(cfg["token"]).role == "manager"
    assert "No team rules yet." in (hub.folder / "CLAUDE.md").read_text()
    exe = str(Path(sys.argv[0]).resolve())  # the hub's own executable runs the shim
    settings, mcp = hub.folder / "settings.json", hub.folder / "mcp.json"
    assert json.loads(settings.read_text()) == manager.settings(exe)
    assert json.loads(mcp.read_text()) == manager.mcp_config(exe)
    assert not (hub.folder / ".claude").exists()
    settings.unlink()
    mcp.write_text("{}")
    hub.tick()
    assert json.loads(settings.read_text()) == manager.settings(exe)  # put back on the next tick
    assert json.loads(mcp.read_text()) == manager.mcp_config(exe)
    assert json.loads(hub.config.read_text()) == cfg
    RulesStore(hub.store).set_rules("always run the tests", "root")
    hub.tick()
    assert "always run the tests" in (hub.folder / "CLAUDE.md").read_text()
    assert json.loads(hub.config.read_text()) == cfg  # same token
    (hub.folder / "CLAUDE.md").unlink()
    hub.tick()
    assert "always run the tests" in (hub.folder / "CLAUDE.md").read_text()
    hub.config.unlink()  # a lost token: a new one, the old one revoked
    hub.tick()
    new = json.loads(hub.config.read_text())
    assert new["token"] != cfg["token"] and hub.store.authenticate(cfg["token"]) is None
    assert hub.store.authenticate(new["token"]).role == "manager"


def test_lost_config_rotates_at_most_once_an_hour(hub, clock):
    hub.mm.set_enabled(True, "test")
    hub.tick()
    first = json.loads(hub.config.read_text())["token"]
    hub.config.unlink()
    hub.tick()
    second = json.loads(hub.config.read_text())["token"]
    hub.config.unlink()
    clock[0] += 60
    hub.tick()  # within the hour: no new token, the file stays missing
    assert not hub.config.exists() and hub.store.authenticate(second).role == "manager"
    assert hub.store.authenticate(first) is None
    clock[0] += 3600
    hub.tick()
    assert json.loads(hub.config.read_text())["token"] != second
    assert hub.store.authenticate(second) is None


def test_manager_relaunch_limits(hub, clock):
    b = admin_bridge(hub)
    hub.mm.set_enabled(True, "test")
    t0 = clock[0]
    hub.tick()
    [o1] = orders(hub, b)
    assert o1["worker"] == "manager" and o1["manager"] is True and o1["person"] == "root"
    assert o1["folder"] == str(hub.folder) and o1["first_message"] == MANAGER_FIRST
    assert "env" not in o1
    hub.tick()
    assert orders(hub, b) == [] and state(hub) == "starting"  # one at a time
    ack(hub, b, o1)
    # its hook reaches the hub (manager token) before the bridge links it
    mtoken = json.loads(hub.config.read_text())["token"]
    hub.store.seen("claude:m1", f"manager@{HOST}", "claude-code", "manager",
                   principal=hub.store.authenticate(mtoken))
    assert http.request("POST", hub.url + "/bridge/worker", b.token,
                        {"worker": "manager", "key": "claude:m1"})[0] == 204
    assert state(hub) == "up" and hub.store.agent("claude:m1").person_id == hub.mm.person_id()
    assert any("worker `manager` is up" in t for t in page_texts(hub))
    # the page shows the manager's session with its role
    admin = hub.store.authenticate(b.token)
    rows = {a["person"]: a for a in feed(hub.store, admin)["agents"]}
    assert rows["manager"]["role"] == "manager"
    # its session goes quiet: closed as "session gone", started again in the same tick
    clock[0] = t0 + ONLINE_S + GONE_S + 130
    beat(hub, b)
    hub.tick()
    [o2] = orders(hub, b)
    assert o2["id"] != o1["id"] and state(hub) == "starting"
    ack(hub, b, o2)  # the new session links under its new key
    hub.store.seen("claude:m2", f"manager@{HOST}", "claude-code", "manager",
                   principal=hub.store.authenticate(mtoken))
    assert http.request("POST", hub.url + "/bridge/worker", b.token,
                        {"worker": "manager", "key": "claude:m2"})[0] == 204
    assert state(hub) == "up" and hub.ws.by_name("manager")["key"] == "claude:m2"
    # gone again: two launches within the hour, so no third
    clock[0] += ONLINE_S + GONE_S + 130
    beat(hub, b)
    hub.tick()
    assert orders(hub, b) == [] and state(hub) == "closed"
    assert hub.mm.may_launch(clock[0]) == (False, "the manager was launched twice in the"
                                                  " last hour")
    # an hour later it may start again; two failures in an hour: the page hears it once
    clock[0] += 3600
    beat(hub, b)
    hub.tick()
    [o3] = orders(hub, b)
    ack(hub, b, o3, ok=False, error="claude: not found")
    hub.tick()
    assert "worker `manager` failed to start: claude: not found" in page_texts(hub)
    assert MANAGER_FAILED not in page_texts(hub)  # one failure so far
    clock[0] += 121
    beat(hub, b)
    hub.tick()
    [o4] = orders(hub, b)
    ack(hub, b, o4, ok=False, error="boom")
    hub.tick()
    hub.tick()
    clock[0] += 30
    beat(hub, b)
    hub.tick()
    assert page_texts(hub).count(MANAGER_FAILED) == 1


def test_manager_waits_for_the_admin_bridge(hub, clock):
    b = admin_bridge(hub)
    hub.mm.set_enabled(True, "test")
    clock[0] += ONLINE_S + 1  # the bridge went quiet
    hub.tick()
    assert orders(hub, b) == [] and state(hub) is None
    beat(hub, b)
    hub.tick()
    assert len(orders(hub, b)) == 1


def test_manager_failure_never_stops_the_worker_tick(hub, monkeypatch, capsys):
    hub.mm.set_enabled(True, "test")
    seen = []
    monkeypatch.setattr(hub.ws.__class__, "tick", lambda self, now: seen.append(now) or [])

    def boom():
        raise RuntimeError("no host")
    monkeypatch.setattr(identity, "facts", boom)
    hub.tick()
    assert seen and "manager tick failed: no host" in capsys.readouterr().out


# busy machines go to the manager

def manager_online(hub):
    """The manager's session online, with the switch on (as a running hub has it)."""
    hub.mm.set_enabled(True, "test")
    pid, token = person(hub.store, "manager", "manager")
    client = SimpleNamespace(url=hub.url, token=token)
    me = ident("k:mgr", project="manager")
    run(call(client, me, "inbox"))
    return client, me


def busy_setup(hub):
    """alice's session holds proj-a on shared box-1; bob (write, may use box-1) asks."""
    hub.store.project_register("proj-a", by="test")
    a_id, _ = member(hub, "alice")
    b = bridge(hub, a_id, sessions=[{"key": "claude:a1", "tool": "claude-code",
                                     "project": "proj-a", "cwd": "/w/proj-a"}])
    hub.store.device_set_shared(b.device_id, True)
    bob_id, bob = member(hub, "bob")
    hub.store.grant_machine("box-1", bob_id, by="test")
    run(call(bob, BOSS, "inbox"))  # bob's boss session is online
    return b, bob_id, bob


def ask(hub, bob):
    return run(call(bob, BOSS, "work", project="proj-a", task="fix it"))


def inbox(client, who):
    return [m["text"] for m in run(call(client, who, "inbox"))]


def test_busy_goes_to_manager_when_online(hub):
    mgr, me = manager_online(hub)
    b, _, bob = busy_setup(hub)
    assert ask(hub, bob) == ("busy: box-1 has alice's session in that folder; the manager is"
                             " asking them (request 1); you will hear back here")
    assert inbox(mgr, me) == [manager.text_busy("bob", "proj-a", "box-1", "alice", "/w/proj-a", 1)]
    assert hub.mm.request(1)["boss_key"] == "k:boss" and poll(hub, b) == []


def test_decide_refuses_a_removed_project(hub):
    from test_hub_workers import remove_project

    mgr, me = manager_online(hub)
    b, _, bob = busy_setup(hub)
    ask(hub, bob)
    remove_project(hub)  # bob's boss session still runs in folder proj-a
    out = run(call(mgr, me, "work_decide", request_id=1, machine="box-1", reason="ok"))
    assert out == "proj-a was removed; ask the admin to register it again"
    assert out in inbox(bob, BOSS)
    assert hub.mm.request(1)["decision"] == "refused" and hub.ws.by_name("proj-a-w1") is None
    assert poll(hub, b) == []


def test_busy_without_manager_unchanged(hub):
    _, _, bob = busy_setup(hub)
    assert ask(hub, bob) == ("busy: box-1 has alice's session in that folder; ask again later or"
                             " name another machine")
    assert hub.mm.request(1) is None


def test_decide_wait_notifies_asker(hub):
    mgr, me = manager_online(hub)
    _, _, bob = busy_setup(hub)
    ask(hub, bob)
    run(call(mgr, me, "work_decide", request_id=1, machine="wait", reason="alice is mid-run"))
    assert "the manager says wait on proj-a: alice is mid-run" in inbox(bob, BOSS)
    assert hub.mm.request(1)["decision"] == "wait"


def test_decide_machine_starts_worker_for_asker(hub):
    mgr, me = manager_online(hub)
    b, bob_id, bob = busy_setup(hub)
    ask(hub, bob)
    out = run(call(mgr, me, "work_decide", request_id=1, machine="box-1", reason="alice agreed"))
    assert "proj-a-w1" in out
    w = hub.ws.by_name("proj-a-w1")
    assert w["worktree"]
    with hub.store.lock:
        row = hub.store.db.execute("SELECT boss_key, boss_person FROM workers WHERE"
                                   " name='proj-a-w1'").fetchone()
    assert (row[0], row[1]) == ("k:boss", bob_id)
    assert any("the manager started `proj-a-w1` on box-1 for proj-a: alice agreed" == t
               for t in inbox(bob, BOSS))
    assert hub.mm.request(1)["worker"] == "proj-a-w1"
    with hub.store.lock:
        details = [r["detail"] for r in hub.store.db.execute("SELECT * FROM log")]
    assert "request 1: start box-1 — alice agreed" in details


def test_decide_refuses_machine_without_use(hub):
    mgr, me = manager_online(hub)
    b, _, bob = busy_setup(hub)
    a_id = next(p["id"] for p in hub.store.people() if p["name"] == "alice")
    b2 = bridge(hub, a_id, host="box-2")  # alice's own, not shared, not granted to bob
    ask(hub, bob)
    err = call_error(mgr, me, "work_decide", request_id=1, machine="box-2", reason="try")
    assert "no machine named box-2 that you may use" in err
    assert hub.ws.by_name("proj-a-w1") is None and poll(hub, b2) == []
    assert hub.mm.request(1)["decided_at"] is None


def test_decide_refused_when_asker_lost_write(hub):
    mgr, me = manager_online(hub)
    _, bob_id, bob = busy_setup(hub)
    ask(hub, bob)
    hub.store.grant_project("proj-a", bob_id, "read", "test")
    out = run(call(mgr, me, "work_decide", request_id=1, machine="box-1", reason="ok"))
    note = "the manager could not start proj-a for you: you no longer have the write role"
    assert out == note and hub.ws.by_name("proj-a-w1") is None
    assert hub.mm.request(1)["decision"] == "refused" and note in inbox(bob, BOSS)


def test_decide_after_asker_left(hub, clock):
    mgr, me = manager_online(hub)
    b, _, bob = busy_setup(hub)
    ask(hub, bob)
    clock[0] += ONLINE_S + 1  # bob's session went quiet; the bridge is still there
    heartbeat(hub, b.token, "box-1")
    run(call(mgr, me, "work_decide", request_id=1, machine="box-1", reason="ok"))
    assert hub.ws.by_name("proj-a-w1") is None
    assert hub.mm.request(1)["decision"] == "asker gone"
    with hub.store.lock:
        texts = [r["text"] for r in hub.store.db.execute(
            "SELECT text FROM messages WHERE to_key='web:bob'")]
    assert texts == ["the manager picked box-1 for proj-a but your session is gone; ask again"]


def test_decide_twice_and_by_member_refused(hub):
    mgr, me = manager_online(hub)
    _, _, bob = busy_setup(hub)
    ask(hub, bob)
    assert "only the manager" in call_error(bob, BOSS, "work_decide", request_id=1,
                                            machine="wait", reason="x")
    run(call(mgr, me, "work_decide", request_id=1, machine="wait", reason="x"))
    assert "already decided: wait" in call_error(mgr, me, "work_decide", request_id=1,
                                                 machine="wait", reason="y")
    assert "reason" in call_error(mgr, me, "work_decide", request_id=1, machine="wait",
                                  reason="")


def test_request_expires_after_an_hour(hub, clock):
    mgr, me = manager_online(hub)
    _, _, bob = busy_setup(hub)
    ask(hub, bob)
    clock[0] += 3601
    run(call(bob, BOSS, "inbox"))  # keeps bob's session online
    hub.tick()
    assert hub.mm.request(1)["decision"] == "expired"
    assert "no decision on your proj-a request within an hour; ask again" in inbox(bob, BOSS)


# what the manager may do, and what the hub tells it

def plain(hub, name):
    pid, token = person(hub.store, name)
    return pid, token, SimpleNamespace(url=hub.url, token=token)


def test_manager_messages_anyone_and_is_answered_without_door(hub):
    mgr, me = manager_online(hub)
    _, _, bob = plain(hub, "bob")
    B = ident("k:bob", project="proj-b", machine="pc-2")
    run(call(bob, B, "inbox"))
    # bob has no door to the manager and the manager has none to bob
    assert run(call(mgr, me, "send", to="pc-2/codex/proj-b", text="how is it going")) \
        == "sent to 1 session"
    assert [m["text"] for m in run(call(bob, B, "inbox"))] == ["how is it going"]
    assert run(call(bob, B, "send", to="laptop/codex/manager", text="fine")) \
        == "sent to 1 session"
    assert [m["text"] for m in run(call(mgr, me, "inbox"))] == ["fine"]
    # a third person still needs a door to bob
    _, _, cat = plain(hub, "cat")
    C = ident("k:cat", project="proj-b", machine="pc-3")
    run(call(cat, C, "inbox"))
    assert "no door" in call_error(cat, C, "send", to="pc-2/codex/proj-b", text="hi")


def test_manager_cannot_answer_others_knock(hub):
    mgr, me = manager_online(hub)
    _, _, ann = plain(hub, "ann")
    _, _, ben = plain(hub, "ben")
    A, B = ident("k:ann", machine="pc-1"), ident("k:ben", machine="pc-2")
    run(call(ben, B, "inbox"))
    out = run(call(ann, A, "knock", to="ben", about="review?"))
    kid = int(out.split("#")[1].split(")")[0])
    assert call_error(mgr, me, "answer", knock_id=kid, answer="yes")
    run(call(ben, B, "answer", knock_id=kid, answer="yes"))


def test_manager_refused_by_admin_only_routes(hub):
    _, token = person(hub.store, "manager", "manager")
    assert http.request("POST", hub.url + "/api/rules", token, {"text": "x"})[0] == 403
    assert http.request("POST", hub.url + "/api/proposals/1", token,
                        {"accept": True, "reason": "x"})[0] == 403


def test_log_tool_roles(hub):
    mgr, me = manager_online(hub)
    hub.store.log_add("alice", "x", "one")
    rows = run(call(mgr, me, "log", limit=5))
    assert rows[0]["detail"] == "one"
    _, _, bob = plain(hub, "bob")
    B = ident("k:bob")
    assert "the log is for the admin and the manager" in call_error(bob, B, "log")
    _, tok = person(hub.store, "root", "admin")
    assert run(call(SimpleNamespace(url=hub.url, token=tok), ident("k:root"), "log"))


def drain(hub, mgr, me):
    return [m["text"] for m in run(call(mgr, me, "inbox"))]


def insert_worker(hub, name, state, boss, now, error=""):
    with hub.store.lock:
        hub.store.db.execute(
            "INSERT INTO workers (name, project, task, boss_key, boss_person, device_id, host,"
            " tool, state, error, created) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (name, "proj-a", "t", "k:boss", boss, 0, "box-1", "claude-code", state, error, now))


def test_pushes_once_each(hub, clock):
    now = clock[0]
    alice, atok, _ = plain(hub, "alice")
    insert_worker(hub, "w-old", "abandoned", alice, now)
    insert_worker(hub, "w-f1", "failed", alice, now, "no tmux")
    insert_worker(hub, "w-f2", "failed", alice, now, "no tmux")
    b = bridge(hub, alice, sessions=[])
    hub.store.device_set_shared(b.device_id, True)
    hub.store.project_register("proj-x", by="test")
    hub.store.request_access(alice, "project", "proj-x", "need it")
    with hub.store.lock:
        hub.store.db.execute("UPDATE devices SET last_used=? WHERE id=?",
                             (now - 700, b.device_id))
        hub.store.db.execute(
            "INSERT INTO proposals (person_id, session_key, text, evidence, status, time)"
            " VALUES (?,?,?,?,?,?)", (alice, "k", "always lint", "ev", "open", now - 90000))
        hub.store.db.execute("UPDATE requests SET created=?", (now - 90000,))
    hub.tick()  # no manager online: nothing sent, nothing consumed
    with hub.store.lock:
        assert hub.store.db.execute("SELECT COUNT(*) FROM manager_pushes").fetchone()[0] == 0
    mgr, me = manager_online(hub)
    hub.tick()
    texts = drain(hub, mgr, me)
    assert "worker `w-old` of alice is abandoned (unattended a day) on box-1;" \
           " tell the admin in one line" in texts
    assert f"bridge on box-1 (u), a shared machine, has been offline 11 minutes;" \
           " tell the admin" in texts
    assert "alice's workers failed to start twice (no tmux); tell the admin" in texts
    assert "proposal from alice has waited a day: always lint; remind the admin" in texts
    assert "access request from alice has waited a day: project proj-x: need it;" \
           " remind the admin" in texts
    assert any(t.startswith("weekly review: read log(500)") for t in texts)
    assert len(texts) == 6
    hub.tick()
    assert drain(hub, mgr, me) == []


def test_weekly_push_repeats_after_a_week(hub, clock):
    mgr, me = manager_online(hub)
    hub.tick()
    assert sum(t.startswith("weekly review") for t in drain(hub, mgr, me)) == 1
    clock[0] += manager.WEEKLY_S + 1
    run(call(mgr, me, "inbox"))
    hub.tick()
    assert sum(t.startswith("weekly review") for t in drain(hub, mgr, me)) == 1


def test_page_answers_the_manager_without_a_door(hub):
    mgr, me = manager_online(hub)
    _, btok, _ = plain(hub, "bob")
    _, atok, alice = plain(hub, "alice")
    A = ident("k:alice", machine="pc-1", project="proj-b")
    run(call(alice, A, "inbox"))
    assert http.request("POST", hub.url + "/api/send", btok,
                        {"to": "laptop/codex/manager", "text": "here is my answer"})[0] == 200
    assert [m["text"] for m in run(call(mgr, me, "inbox"))] == ["here is my answer"]
    status, body = http.request("POST", hub.url + "/api/send", btok,
                                {"to": "pc-1/codex/proj-b", "text": "hi"})
    assert status == 400 and "no door" in body


def test_page_sends_a_file_to_the_manager_without_a_door(hub):
    mgr, me = manager_online(hub)
    bob_id, btok, _ = plain(hub, "bob")
    fid = hub.store.file_add("a.txt", b"hello", bob_id)
    status, body = http.request("POST", hub.url + "/api/send", btok,
                                {"to": "laptop/codex/manager", "text": "see this",
                                 "file_id": fid})
    assert (status, body) == (200, {"sent": 1})
    [m] = [m for m in hub.store.messages() if m.file]
    assert (m.text, m.file["id"]) == ("see this", fid)
    assert [m["text"] for m in run(call(mgr, me, "inbox"))] == ["see this"]
    status, text = http.request("POST", hub.url + "/api/send", btok,
                                {"to": "laptop/codex/manager", "text": "again", "file_id": fid})
    assert (status, text) == (400, "that file is not yours to send")


def test_admin_closing_the_manager_switches_it_off(hub, clock):
    b = admin_bridge(hub)
    hub.mm.set_enabled(True, "test")
    hub.tick()
    [o] = orders(hub, b)
    ack(hub, b, o)
    mtoken = json.loads(hub.config.read_text())["token"]
    hub.store.seen("claude:m1", f"manager@{HOST}", "claude-code", "manager",
                   principal=hub.store.authenticate(mtoken))
    http.request("POST", hub.url + "/bridge/worker", b.token,
                 {"worker": "manager", "key": "claude:m1"})
    # a member cannot close it
    _, bob = member(hub, "bob", role=None)
    assert "no worker manager of yours" in call_error(bob, ident("k:bob"), "close_worker",
                                                      name="manager")
    assert hub.mm.enabled() is True
    answer = run(call(as_token(hub, b.token), ident("k:root"), "close_worker", name="manager"))
    assert answer.endswith("; the manager is switched off until kaptyorka start --manager")
    assert hub.mm.enabled() is False
    clock[0] += 120
    beat(hub, b)
    hub.tick()
    assert [x for x in orders(hub, b) if x.get("kind") != "close"] == []


def test_admin_closing_the_manager_on_the_page_switches_it_off(hub, clock):
    """The page's Close does what the close_worker tool does: the manager stays off."""
    b = admin_bridge(hub)
    hub.mm.set_enabled(True, "test")
    hub.tick()
    [o] = orders(hub, b)
    ack(hub, b, o)
    status, w = http.request("POST", hub.url + "/api/workers/manager/close", b.token)
    assert status == 200 and w["manager"] is True
    assert hub.mm.enabled() is False
    clock[0] += 120
    beat(hub, b)
    hub.tick()
    assert [x for x in orders(hub, b) if x.get("kind") != "close"] == []
    assert state(hub) not in ("starting", "up")


def test_admin_closing_another_worker_on_the_page_leaves_the_switch(hub):
    b = admin_bridge(hub)
    hub.mm.set_enabled(True, "test")
    hub.store.project_register("proj-a", by="test")
    pid, bob = member(hub, "bob")
    bridge(hub, pid)
    run(call(bob, ident("k:bob"), "work", project="proj-a", task="x"))
    status, w = http.request("POST", hub.url + "/api/workers/proj-a-w1/close", b.token)
    assert status == 200 and w["manager"] is False
    assert hub.mm.enabled() is True


def test_non_admin_close_leaves_the_switch(hub):
    """The switch-off keys on the manager row and the admin; a member never reaches it."""
    admin_bridge(hub)
    hub.mm.set_enabled(True, "test")
    hub.store.project_register("proj-a", by="test")
    pid, bob = member(hub, "bob")
    bridge(hub, pid)
    run(call(bob, ident("k:bob"), "work", project="proj-a", task="x"))
    answer = run(call(bob, ident("k:bob"), "close_worker", name="proj-a-w1"))
    assert "switched off" not in answer and hub.mm.enabled() is True
    assert hub.ws.by_name("proj-a-w1")["manager"] is False


# the switch binds a manager that is already running

def test_switched_off_manager_gets_no_pushes(hub, clock):
    mgr, me = manager_online(hub)
    hub.mm.set_enabled(False, "test")
    hub.tick()
    assert drain(hub, mgr, me) == []
    with hub.store.lock:
        assert hub.store.db.execute("SELECT COUNT(*) FROM manager_pushes").fetchone()[0] == 0


def test_switched_off_busy_work_answers_as_without_manager(hub):
    mgr, me = manager_online(hub)
    _, _, bob = busy_setup(hub)
    hub.mm.set_enabled(False, "test")
    assert ask(hub, bob) == ("busy: box-1 has alice's session in that folder; ask again later or"
                             " name another machine")
    assert hub.mm.request(1) is None and drain(hub, mgr, me) == []


def test_switched_off_work_decide_refused(hub):
    mgr, me = manager_online(hub)
    _, _, bob = busy_setup(hub)
    ask(hub, bob)
    hub.mm.set_enabled(False, "test")
    assert "the manager is switched off" in call_error(mgr, me, "work_decide", request_id=1,
                                                       machine="wait", reason="x")
    assert hub.mm.request(1)["decided_at"] is None


def running_manager(hub):
    b = admin_bridge(hub)
    hub.mm.set_enabled(True, "test")
    hub.tick()
    [o] = orders(hub, b)
    ack(hub, b, o)
    mtoken = json.loads(hub.config.read_text())["token"]
    hub.store.seen("claude:m1", f"manager@{HOST}", "claude-code", "manager",
                   principal=hub.store.authenticate(mtoken))
    assert http.request("POST", hub.url + "/bridge/worker", b.token,
                        {"worker": "manager", "key": "claude:m1"})[0] == 204
    assert state(hub) == "up"
    return b


def test_hub_started_off_closes_a_running_manager(hub):
    from kaptyorka.hub import build_app
    b = running_manager(hub)
    hub.mm.set_enabled(False, "hub")  # kaptyorka start --no-manager, then the hub restarts
    restarted = build_app(hub.store, manager_url=URL).state.tick
    restarted()
    [close] = [x for x in orders(hub, b) if x.get("kind") == "close"]
    assert close["worker"] == "manager" and close["key"] == "claude:m1"
    with hub.store.lock:
        rows = [(r["person"], r["kind"], r["detail"]) for r in hub.store.db.execute(
            "SELECT * FROM log WHERE kind='manager' ORDER BY id")]
    assert rows[-1] == ("hub", "manager", "closed: switched off")
    restarted()  # only the first tick closes; nothing new is queued
    assert [x for x in orders(hub, b) if x.get("kind") == "close"] == []


def test_hub_started_on_leaves_a_running_manager(hub):
    from kaptyorka.hub import build_app
    b = running_manager(hub)
    build_app(hub.store, manager_url=URL).state.tick()
    assert [x for x in orders(hub, b) if x.get("kind") == "close"] == [] and state(hub) == "up"


def test_two_racing_decisions_start_one_worker(hub, monkeypatch):
    import asyncio
    import time
    from helpers import call_result
    mgr, me = manager_online(hub)
    _, _, bob = busy_setup(hub)
    ask(hub, bob)
    real = WorkerStore.create

    def slow(self, *a, **kw):
        time.sleep(0.5)  # both calls are inside work_decide at once
        return real(self, *a, **kw)
    monkeypatch.setattr(WorkerStore, "create", slow)

    async def both():
        return await asyncio.gather(*(call_result(mgr, me, "work_decide", request_id=1,
                                                  machine="box-1", reason=f"go {i}")
                                      for i in range(2)))
    results = run(both())
    errors = [r.content[0].text for r in results if r.is_error]
    assert len(errors) == 1 and "already decided" in errors[0]
    assert hub.ws.by_name("proj-a-w1") is not None and hub.ws.by_name("proj-a-w2") is None


# a folder Claude Code does not trust: one notice, no relaunch loop

TRUST = "Claude Code does not trust /h/.kaptyorka/manager; open claude there once and accept"


def test_trust_failure_blocks_relaunch_until_start_manager(hub, clock):
    from kaptyorka.cli import _apply_manager
    from kaptyorka.hub import MANAGER_UNTRUSTED
    b = admin_bridge(hub)
    hub.mm.set_enabled(True, "test")
    hub.tick()
    [o] = orders(hub, b)
    ack(hub, b, o, ok=False, error=TRUST)
    for _ in range(2 * 60 * 4):  # two hours of 15 s ticks, the admin's bridge online
        clock[0] += 15
        with hub.store.lock:
            hub.store.db.execute("UPDATE devices SET last_used=? WHERE id=?",
                                 (clock[0], b.device_id))
        hub.tick()
    assert orders(hub, b) == [] and hub.mm.may_launch(clock[0])[0] is False
    assert hub.mm.blocked() == "trust"
    texts = page_texts(hub)
    assert texts.count(MANAGER_UNTRUSTED) == 1
    assert not any("failed to start" in t for t in texts) and MANAGER_FAILED not in texts
    _apply_manager(hub.store, SimpleNamespace(manager=True, no_manager=False))
    assert hub.mm.blocked() is None
    beat(hub, b)
    hub.tick()
    assert len(orders(hub, b)) == 1  # launched again; the old failure does not block it


def test_other_launch_failure_does_not_block(hub):
    b = admin_bridge(hub)
    hub.mm.set_enabled(True, "test")
    hub.tick()
    [o] = orders(hub, b)
    ack(hub, b, o, ok=False, error="claude: not found")
    hub.tick()
    assert hub.mm.blocked() is None


# a person already named manager

def test_person_named_manager_reported_once_a_day(hub, clock, capsys):
    admin_bridge(hub)
    person(hub.store, "manager", "member")
    hub.mm.set_enabled(True, "test")
    for _ in range(10):
        clock[0] += 15
        hub.tick()
    out = capsys.readouterr().out
    line = "a person named manager already exists with role member; the manager cannot start"
    assert out.count(line) == 1 and "manager tick failed" not in out
    assert state(hub) is None
    clock[0] += 86400
    hub.tick()
    assert capsys.readouterr().out.count(line) == 1
