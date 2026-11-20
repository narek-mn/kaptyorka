"""The hub keeps the manager session alive through the admin's own bridge on its machine."""
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import kaptyorka
from conftest import start_hub
from helpers import call, call_error, old_knock, person, run
from test_hub_workers import BOSS, as_token, bridge, heartbeat, ident, member, poll
from kaptyorka import admin_ops, http, identity, manager, paths
from kaptyorka.hub import MANAGER_FAILED, MANAGER_FIRST
from kaptyorka.hub_feed import feed
from kaptyorka.manager import ManagerStore
from kaptyorka.rules import RulesStore
from kaptyorka.store import ONLINE_S, Store
from kaptyorka.workers import GONE_S, WorkerStore

HOST = "box-1"
URL = "http://127.0.0.1:9999"
NO_REPORT = ["no fresh use report from this machine"]


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
    # its session goes quiet: closed as "session gone"; a message starts it again
    clock[0] = t0 + ONLINE_S + GONE_S + 130
    beat(hub, b)
    hub.tick()
    assert orders(hub, b) == [] and state(hub) == "closed"
    hub.ws.notify("claude:m1", "wake up")
    hub.tick()
    [o2] = orders(hub, b)
    assert o2["id"] != o1["id"] and state(hub) == "starting"
    ack(hub, b, o2)  # the new session links under its new key
    hub.store.seen("claude:m2", f"manager@{HOST}", "claude-code", "manager",
                   principal=hub.store.authenticate(mtoken))
    assert http.request("POST", hub.url + "/bridge/worker", b.token,
                        {"worker": "manager", "key": "claude:m2"})[0] == 204
    assert state(hub) == "up" and hub.ws.by_name("manager")["key"] == "claude:m2"
    # gone again after a normal run: two wake-ups in an hour never hold the third
    clock[0] += ONLINE_S + GONE_S + 130
    beat(hub, b)
    hub.ws.notify("claude:m2", "wake up")
    hub.tick()
    [o3] = orders(hub, b)
    assert hub.mm.crashes() == []
    # two failures in an hour: the page hears it once
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


def test_settings_carry_the_manager_reason_and_last_error(hub, clock):
    _, token = person(hub.store, "root", "admin")  # a web admin; no bridge on the hub machine

    def settings():
        status, body = http.request("GET", hub.url + "/api/settings", token)
        assert status == 200
        return body
    s = settings()
    assert s["manager_state"] == "off" and s["manager_reason"] == "the manager is switched off"
    assert s["manager_error"] == ""
    hub.mm.set_enabled(True, "test")
    s = settings()
    assert s["manager_state"] == "blocked" and s["manager_error"] == ""
    assert s["manager_reason"].startswith("the admin's bridge on the hub machine is offline")
    with hub.store.lock:
        hub.store.db.execute(
            "INSERT INTO workers (name, project, task, boss_key, boss_person, device_id, host,"
            " tool, state, created, closed_at, error) VALUES ('manager','p','t','k',1,1,?,"
            "'claude','failed',?,?,'manager did not show up')", (HOST, clock[0], clock[0]))
    s = settings()
    assert s["manager_state"] == "blocked" and s["manager_error"] == "manager did not show up"
    with hub.store.lock:
        hub.store.db.execute("UPDATE workers SET state='starting', error='', closed_at=NULL"
                             " WHERE name='manager'")
    s = settings()
    assert s == {**s, "manager_state": "starting", "manager_reason": "", "manager_error": ""}
    for kept in ("unattended", "abandoned"):  # the session is there, so it is up
        with hub.store.lock:
            hub.store.db.execute("UPDATE workers SET state=? WHERE name='manager'", (kept,))
        s = settings()
        assert s == {**s, "manager_state": "up", "manager_reason": "", "manager_error": ""}


def test_no_launch_while_claude_is_logged_out(hub, clock):
    b = admin_bridge(hub)
    with hub.store.lock:
        hub.store.db.execute("UPDATE device_facts SET facts=? WHERE device_id=?",
                             (json.dumps({"logins": {"claude-code": False}}), b.device_id))
    hub.mm.set_enabled(True, "test")
    for _ in range(3):
        hub.tick()
        clock[0] += 130
        with hub.store.lock:
            hub.store.db.execute("UPDATE devices SET last_used=? WHERE id=?",
                                 (clock[0], b.device_id))
    assert orders(hub, b) == [] and state(hub) is None
    assert hub.mm.may_launch(clock[0]) == (False, "Claude Code is not logged in on the hub"
                                                  " machine; run claude auth login there")
    assert hub.mm.launches_last_hour(clock[0]) == 0
    with hub.store.lock:  # logged in again (or no login fact at all): it starts
        hub.store.db.execute("UPDATE device_facts SET facts='{}' WHERE device_id=?",
                             (b.device_id,))
    hub.tick()
    assert len(orders(hub, b)) == 1


def test_manager_waits_for_the_admin_bridge(hub, clock):
    b = admin_bridge(hub)
    hub.mm.set_enabled(True, "test")
    clock[0] += ONLINE_S + 1  # the bridge went quiet
    hub.tick()
    assert orders(hub, b) == [] and state(hub) is None
    beat(hub, b)
    hub.tick()
    assert len(orders(hub, b)) == 1


def bridge_as(hub, name, role, os_user, team=False):
    """A person with one live bridge on HOST running as `os_user`."""
    pid, _ = person(hub.store, name, role)
    if team:
        with hub.store.lock:
            hub.store.db.execute("UPDATE people SET team=1, role='member' WHERE id=?", (pid,))
    code = hub.store.invite_create(pid)
    token, _ = hub.store.invite_redeem(code, HOST, host=HOST, os_user=os_user)
    b = SimpleNamespace(token=token, device_id=hub.store.authenticate(token).device_id)
    body = {"machine": HOST, "sessions": [], "tools": [],
            "facts": {"host": HOST, "os_user": os_user, "version": kaptyorka.__version__}}
    assert http.request("POST", hub.url + "/bridge/heartbeat", token, body)[0] == 200
    return b


def test_manager_launches_through_the_hub_accounts_bridge(hub, monkeypatch):
    monkeypatch.setattr(manager.getpass, "getuser", lambda: "svc")
    svc = bridge_as(hub, "svc", "admin", "svc", team=True)  # the install-time person, now a team machine
    write_hub_key(hub, svc)
    human = bridge_as(hub, "carol", "admin", "carol")  # joined later from their own account
    hub.mm.set_enabled(True, "test")
    hub.tick()
    assert orders(hub, human) == []
    [o] = orders(hub, svc)
    assert o["worker"] == "manager" and o["person"] == "svc"  # the device owner: its bridge takes no other
    assert o["boss"] in admin_ops.admins(hub.store) and o["boss"] != "svc"  # a human admin
    with hub.store.lock:
        row = hub.store.db.execute("SELECT device_id, boss_key FROM workers WHERE name='manager'").fetchone()
    assert row["device_id"] == svc.device_id and row["boss_key"] == "web:" + o["boss"]


def write_hub_key(hub, b):
    paths.home().mkdir(parents=True, exist_ok=True)
    (paths.home() / "admin-token").write_text(b.token + "\n")


def test_manager_launches_through_the_hub_key_persons_bridge(hub, monkeypatch):
    monkeypatch.setattr(manager.getpass, "getuser", lambda: "svc")
    svc = bridge_as(hub, "svc", "member", "svc", team=True)
    write_hub_key(hub, svc)
    hub.mm.set_enabled(True, "test")
    other = bridge_as(hub, "carol", "admin", "carol")
    assert hub.mm.admin_device(HOST)["id"] == svc.device_id
    hub.tick()
    assert orders(hub, other) == [] and len(orders(hub, svc)) == 1


def test_untrusted_devices_reporting_the_hub_user_are_never_picked(hub, monkeypatch):
    monkeypatch.setattr(manager.getpass, "getuser", lambda: "svc")
    svc = bridge_as(hub, "svc", "member", "svc", team=True)
    write_hub_key(hub, svc)
    bridge_as(hub, "mallory", "member", "svc")  # a member claims the hub's host and OS user
    bridge_as(hub, "shared", "member", "svc", team=True)  # another team machine does the same
    got = hub.mm.admin_device(HOST)
    assert got["id"] == svc.device_id
    with hub.store.lock:  # with the hub key's bridge gone, nobody trusted is left
        hub.store.db.execute("UPDATE devices SET revoked_at=1 WHERE id=?", (svc.device_id,))
    assert hub.mm.admin_device(HOST) is None


def test_old_setup_admin_owns_the_hub_account(hub, monkeypatch):
    monkeypatch.setattr(manager.getpass, "getuser", lambda: "u")
    b = admin_bridge(hub)  # os_user "u", the admin
    hub.mm.set_enabled(True, "test")
    hub.tick()
    [o] = orders(hub, b)
    assert o["person"] == "root"


def test_no_hub_account_bridge_falls_back_and_says_so(hub, monkeypatch, capsys):
    monkeypatch.setattr(manager.getpass, "getuser", lambda: "svc")
    manager._FALLBACK_LOGGED.clear()
    b = bridge_as(hub, "carol", "admin", "carol")
    hub.mm.set_enabled(True, "test")
    hub.tick()
    [o] = orders(hub, b)
    assert o["person"] == "carol"
    assert "no bridge runs as the hub's OS user 'svc'" in capsys.readouterr().out


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
    assert inbox(mgr, me) == [manager.text_busy("bob", "proj-a", "box-1", "alice", "/w/proj-a", 1, NO_REPORT)]
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
    # a third person cannot reach bob
    _, _, cat = plain(hub, "cat")
    C = ident("k:cat", project="proj-b", machine="pc-3")
    run(call(cat, C, "inbox"))
    assert "that you can reach" in call_error(cat, C, "send", to="pc-2/codex/proj-b", text="hi")


def test_manager_cannot_answer_others_knock(hub):
    mgr, me = manager_online(hub)
    _, _, ann = plain(hub, "ann")
    _, _, ben = plain(hub, "ben")
    A, B = ident("k:ann", machine="pc-1"), ident("k:ben", machine="pc-2")
    run(call(ann, A, "inbox"))
    run(call(ben, B, "inbox"))
    kid = old_knock(hub.store, "k:ann", hub.store.person_by_name("ben")["id"], "review?")
    assert call_error(mgr, me, "answer", knock_id=kid, answer="yes").endswith(
        "only the admin opens doors")
    assert call_error(mgr, me, "knock", to="ben", about="a door?").endswith(
        "ask the manager: send it a message saying who you need and why")
    assert hub.store.doors() == []


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
    [only] = admin_ops.admins(hub.store)  # the hub's first admin (from the shared token)
    them = f"the admin ({only})"
    assert "worker `w-old` of alice is abandoned (unattended a day) on box-1;" \
           f" tell {them} in one line" in texts
    assert f"bridge on box-1 (u), a shared machine, has been offline 11 minutes;" \
           f" tell {them}" in texts
    assert f"alice's workers failed to start twice (no tmux); tell {them}" in texts
    assert f"proposal from alice has waited a day: always lint; remind {them}" in texts
    assert "access request from alice has waited a day: project proj-x: need it;" \
           f" remind {them}" in texts
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


def knowledge_texts(hub, mgr, me, start="knowledge"):
    return [t for t in drain(hub, mgr, me) if t.startswith(start)]


def unread_to_manager(hub):
    with hub.store.lock:
        return hub.store.db.execute("SELECT COUNT(*) FROM messages WHERE to_key='k:mgr' AND"
                                    " read_at IS NULL").fetchone()[0]


def test_knowledge_digest_daily_and_only_to_an_awake_manager(hub, clock):
    from kaptyorka import knowledge
    mgr, me = manager_online(hub)
    hub.tick()  # the first tick starts the count: no digest of the past
    assert knowledge_texts(hub, mgr, me) == []
    assert hub.mm.stamp(knowledge.DIGEST_KEY) == clock[0]
    clock[0] += 1
    hub.store.project_register("proj-a", by="test")
    clock[0] = knowledge.quiet_mark(clock[0]) + 86400 + 60  # past the next quiet hour
    hub.tick()  # the manager is asleep: nothing is queued, so nothing wakes it
    assert unread_to_manager(hub) == 0 and not hub.mm.has_work()
    clock[0] += 5 * 3600  # it wakes later in the day
    run(call(mgr, me, "inbox"))
    hub.tick()
    [digest] = knowledge_texts(hub, mgr, me, "knowledge digest")
    assert "- proj-a: new" in digest
    hub.tick()
    assert knowledge_texts(hub, mgr, me) == []
    clock[0] += 12 * 3600  # the same quiet hour has not come again yet
    run(call(mgr, me, "inbox"))
    hub.store.project_register("proj-b", by="test")
    hub.tick()
    assert knowledge_texts(hub, mgr, me, "knowledge digest") == []
    clock[0] += 12 * 3600
    run(call(mgr, me, "inbox"))
    hub.tick()
    [digest] = knowledge_texts(hub, mgr, me, "knowledge digest")
    assert "- proj-b: new" in digest and "proj-a" not in digest


def test_knowledge_light_notice_on_a_project_note_and_a_finished_worker(hub, clock):
    mgr, me = manager_online(hub)
    hub.tick()
    hub.store.project_register("proj-a", by="test")
    alice, _, _ = plain(hub, "alice")
    hub.store.grant_project("proj-a", alice, "write", "test")
    hub.store.seen("k:alice", "box-1", "claude-code", "proj-a",
                   principal=hub.store._person_principal(alice))
    clock[0] += 1
    hub.store.notes_write("k:alice", "state", "where it stands", "proj-a")
    run(call(mgr, me, "inbox"))
    hub.tick()
    assert knowledge_texts(hub, mgr, me) == [
        "knowledge: proj-a/state written by alice. If it changes what the team knows, update"
        " the team notes (read first, merge)."]
    clock[0] += 1
    insert_worker(hub, "w-1", "closed", alice, clock[0])
    with hub.store.lock:
        hub.store.db.execute("UPDATE workers SET closed_at=? WHERE name='w-1'", (clock[0],))
    hub.tick()  # at most one light notice per 10 minutes: this one waits
    assert knowledge_texts(hub, mgr, me) == []
    clock[0] += 600
    run(call(mgr, me, "inbox"))
    hub.tick()
    assert knowledge_texts(hub, mgr, me) == [
        "knowledge: worker `w-1` in proj-a finished. If it changes what the team knows,"
        " update the team notes (read first, merge)."]
    hub.store.notes_write("k:alice", "team", "a team note waits for the digest")
    clock[0] += 601
    run(call(mgr, me, "inbox"))
    hub.tick()
    assert knowledge_texts(hub, mgr, me) == []


def test_manager_writes_project_notes_through_the_tool(hub):
    mgr, me = manager_online(hub)
    hub.store.project_register("proj-a", by="test")
    assert run(call(mgr, me, "notes_write", topic="api", text="x", project="proj-a")) \
        == "saved note api"
    assert run(call(mgr, me, "notes_write", topic="projects", text="- proj-a: x")) \
        == "saved note projects"
    assert [p["name"] for p in run(call(mgr, me, "projects"))] == ["proj-a"]
    assert "not registered" in call_error(mgr, me, "notes_write", topic="api", text="x",
                                          project="home-stuff")


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
    assert status == 400 and "that you can reach" in body


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
    assert [m["text"] for m in run(call(mgr, me, "inbox"))] == [
        f"see this\n[attached: a.txt (5 bytes) \u2014 get it with fetch_file({fid})]"]
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
    assert close["manager"] is True  # the bridge ends its tmux session, not a --bg job
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


# a launch the bridge refused ran no session: it does not use up the hourly launches

SCOPE_TAKEN = ("Failed to start transient scope unit: Unit kaptyorka-worker-manager.scope was"
               " already loaded or has a fragment file.")


def relaunch(hub, b, clock):
    """Past the two minutes after a failure: the next order, or None."""
    clock[0] += 121
    beat(hub, b)
    hub.tick()
    items = orders(hub, b)
    return items[0] if items else None


def no_show(hub, b, item):
    """The bridge took the order, the session never came: a real failed start."""
    ack(hub, b, item)
    assert http.request("POST", hub.url + "/bridge/worker", b.token,
                        {"worker": "manager", "error": "manager did not show up"})[0] == 204


def test_a_refused_launch_does_not_use_up_the_hourly_launches(hub, clock):
    b = admin_bridge(hub)
    hub.mm.set_enabled(True, "test")
    hub.tick()
    [o1] = orders(hub, b)
    no_show(hub, b, o1)
    o2 = relaunch(hub, b, clock)
    ack(hub, b, o2, ok=False, error=SCOPE_TAKEN)
    assert hub.mm.launches_last_hour(clock[0]) == 2 and hub.mm.refusals_last_hour(clock[0]) == 1
    o3 = relaunch(hub, b, clock)  # counted as two launches, it would wait an hour here
    assert o3 is not None and o3["id"] not in (o1["id"], o2["id"])


def test_refusals_stop_after_three_in_an_hour(hub, clock):
    b = admin_bridge(hub)
    hub.mm.set_enabled(True, "test")
    hub.tick()
    [o] = orders(hub, b)
    for _ in range(3):
        ack(hub, b, o, ok=False, error="claude: not found")
        o = relaunch(hub, b, clock)
    assert o is None
    ok, reason = hub.mm.may_launch(clock[0])
    assert not ok and reason.startswith("the hub machine's bridge refused to start the manager"
                                        " 3 times in the last hour")
    clock[0] += 3600
    assert relaunch(hub, b, clock) is not None


def test_a_crash_loop_backs_off_and_says_why_when_it_gives_up(hub, clock):
    b = admin_bridge(hub)
    hub.mm.set_enabled(True, "test")
    hub.tick()
    [o] = orders(hub, b)
    for n, wait in enumerate((120, 240, 480, 960), start=1):
        no_show(hub, b, o)
        hub.tick()
        assert len(hub.mm.crashes()) == n
        clock[0] += wait - 10
        beat(hub, b)
        hub.tick()
        assert orders(hub, b) == []
        assert hub.mm.may_launch(clock[0])[1].startswith(
            f"the manager crashed {n} time{'s' if n > 1 else ''} in a row; it tries again in 1"
            " minute by")
        clock[0] += 10
        beat(hub, b)
        hub.tick()
        [o] = orders(hub, b)
    no_show(hub, b, o)
    hub.tick()
    clock[0] += 3000
    beat(hub, b)
    hub.tick()
    assert orders(hub, b) == []
    assert hub.mm.may_launch(clock[0]) == (
        False, "the manager crashed 5 times in a row (last: manager did not show up); it tries"
               " again once an hour by itself; see ~/.kaptyorka/workers/manager.log on the hub"
               " machine")
    assert hub.mm.refusals_last_hour(clock[0]) == 0
    clock[0] += 600
    beat(hub, b)
    hub.tick()
    assert len(orders(hub, b)) == 1


def test_a_session_that_ends_soon_after_its_start_is_a_crash(hub, clock):
    b = admin_bridge(hub)
    hub.mm.set_enabled(True, "test")
    hub.tick()
    [o] = orders(hub, b)
    ack(hub, b, o)
    assert http.request("POST", hub.url + "/bridge/worker", b.token,
                        {"worker": "manager", "key": "claude:m1"})[0] == 204
    clock[0] += 200  # its session went quiet within minutes of the start
    beat(hub, b)
    hub.tick()
    assert state(hub) == "closed" and len(hub.mm.crashes()) == 1
    hub.ws.notify("claude:m1", "wake up")
    hub.tick()
    assert orders(hub, b) == []  # the backoff holds the wake-up
    assert hub.mm.may_launch(clock[0])[1].startswith("the manager crashed 1 time in a row")
    clock[0] += 120
    beat(hub, b)
    hub.tick()
    assert len(orders(hub, b)) == 1


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


MGR_NAME = f"manager@{HOST}/claude-code/manager"


def asleep_manager(hub, clock):
    """A running manager whose session Claude retired: closed as gone, not started again."""
    b = running_manager(hub)
    clock[0] += 3600  # a fresh launch budget
    beat(hub, b)
    hub.tick()
    assert state(hub) == "closed" and orders(hub, b) == []
    assert hub.mm.asleep(clock[0])
    return b


def woken(hub, b):
    """The next tick launches the manager; its new session links under a new key. Returns
    the texts the admin's bridge is handed for that new session."""
    hub.tick()
    [o] = orders(hub, b)
    ack(hub, b, o)
    mtoken = json.loads(hub.config.read_text())["token"]
    hub.store.seen("claude:m2", f"manager@{HOST}", "claude-code", "manager",
                   principal=hub.store.authenticate(mtoken))
    assert http.request("POST", hub.url + "/bridge/worker", b.token,
                        {"worker": "manager", "key": "claude:m2"})[0] == 204
    status, items = http.request("GET", f"{hub.url}/bridge/poll?machine={HOST}", b.token)
    assert status == 200
    return [i["text"] for i in items if i.get("kind") == "message" and i["key"] == "claude:m2"]


def test_sleeping_manager_wakes_for_the_admin_page(hub, clock):
    b = asleep_manager(hub, clock)
    assert http.request("POST", hub.url + "/api/send", b.token,
                        {"to": MGR_NAME, "text": "from root"})[0] == 200
    assert any("\n\nfrom root\n" in t for t in woken(hub, b))


def test_sleeping_manager_wakes_for_a_member_page(hub, clock):
    b = asleep_manager(hub, clock)
    _, btok, _ = plain(hub, "bob")
    assert http.request("POST", hub.url + "/api/send", btok,
                        {"to": MGR_NAME, "text": "from bob"})[0] == 200
    assert any("\n\nfrom bob\n" in t for t in woken(hub, b))


def test_sleeping_manager_wakes_for_a_member_session(hub, clock):
    b = asleep_manager(hub, clock)
    _, _, bob = plain(hub, "bob")
    B = ident("k:bob", project="proj-b", machine="pc-2")
    run(call(bob, B, "inbox"))
    assert run(call(bob, B, "send", to=MGR_NAME, text="from bob's session")) \
        == "sent to 1 session"
    assert any("\n\nfrom bob's session\n" in t for t in woken(hub, b))


def test_sleeping_manager_wakes_for_a_hub_notice(hub, clock):
    b = asleep_manager(hub, clock)
    assert hub.ws.notify("claude:m1", "a hub notice")
    assert any(t.endswith("a hub notice") for t in woken(hub, b))


def test_busy_work_while_the_manager_sleeps_wakes_it(hub, clock):
    b = asleep_manager(hub, clock)
    _, bob_id, bob = busy_setup(hub)
    assert ask(hub, bob).startswith("busy: box-1 has alice's session in that folder; the"
                                    " manager is asking them (request 1)")
    texts = woken(hub, b)
    busy = manager.text_busy("bob", "proj-a", "box-1", "alice", "/w/proj-a", 1, NO_REPORT)
    assert any(t.endswith(busy) for t in texts)


def test_sleeping_manager_shown_asleep(hub, clock):
    b = asleep_manager(hub, clock)
    status, s = http.request("GET", hub.url + "/api/settings", b.token)
    assert status == 200 and s["manager_state"] == "asleep"
    status, rows = http.request("GET", hub.url + "/api/workers", b.token)
    [w] = [w for w in rows if w["manager"]]
    assert w["state"] == "asleep" and w["error"] == ""


def test_retired_but_switched_off_manager_not_shown_asleep(hub, clock):
    b = asleep_manager(hub, clock)
    hub.mm.set_enabled(False, "test")
    status, rows = http.request("GET", hub.url + "/api/workers", b.token)
    [w] = [w for w in rows if w["manager"]]
    assert w["state"] != "asleep"


def test_manager_row_never_marked_background(hub):
    """The manager runs as a background job (claude --bg); the page keeps its manager look."""
    b = admin_bridge(hub)
    hub.mm.set_enabled(True, "test")
    hub.tick()
    [o1] = orders(hub, b)
    ack(hub, b, o1)
    mtoken = json.loads(hub.config.read_text())["token"]
    hub.store.seen("claude:m1", f"manager@{HOST}", "claude-code", "manager",
                   principal=hub.store.authenticate(mtoken))
    assert http.request("POST", hub.url + "/bridge/worker", b.token,
                        {"worker": "manager", "key": "claude:m1"})[0] == 204
    beat(hub, b, [{"key": "claude:m1", "tool": "claude-code", "project": "manager", "background": True},
                  {"key": "claude:j1", "tool": "claude-code", "project": "proj-a", "background": True}])
    assert hub.store.agent("claude:m1").background is True  # stored as sent
    admin = hub.store.authenticate(b.token)
    rows = {a["person"] + ":" + a["project"]: a for a in feed(hub.store, admin)["agents"]}
    assert rows["manager:manager"]["role"] == "manager"
    assert rows["manager:manager"]["background"] is False
    assert rows["root:proj-a"]["background"] is True


# a machine in use by another person (any folder), and sessions opened directly there

HEAVY = {"age": 5, "cpus": 32, "mem_mb": 65536,
         "gpus": [{"index": i, "util": 90, "mem": 30000, "total": 32000} for i in range(4)],
         "users": {"u": {"cpu": 4.0, "mem": 8192, "gpus": {"0": 29000, "1": 29000, "2": 29000}}}}
ALICE_LINE = "alice is using 3 of 4 GPUs (90% busy at most), 4.0 of 32 CPU cores and 8 of 64 GB memory"


def device(hub, pid, os_user):
    code = hub.store.invite_create(pid)
    token, _ = hub.store.invite_redeem(code, HOST, host=HOST, os_user=os_user)
    return SimpleNamespace(token=token, host=HOST, os_user=os_user,
                           device_id=hub.store.authenticate(token).device_id)


def use_beat(hub, d, use=None, sessions=()):
    facts = {"host": HOST, "os_user": d.os_user, "version": kaptyorka.__version__,
             "tmux": True, "load": 0.1, "cpus": 4}
    if use is not None:
        facts["use"] = use
    body = {"machine": HOST, "sessions": list(sessions), "tools": [], "facts": facts,
            "folders": {"proj-a": "/w/proj-a"}}
    assert http.request("POST", hub.url + "/bridge/heartbeat", d.token, body)[0] == 200


def in_use_setup(hub, sessions=(), use=None):
    """alice's shared account on box-1 (os user u) with `sessions` and `use`; bob may use it."""
    hub.store.project_register("proj-a", by="test")
    a_id, _ = member(hub, "alice")
    b = device(hub, a_id, "u")
    use_beat(hub, b, use, sessions)
    hub.store.device_set_shared(b.device_id, True)
    bob_id, bob = member(hub, "bob")
    hub.store.grant_machine(HOST, bob_id, by="test")
    run(call(bob, BOSS, "inbox"))
    return b, bob_id, bob


OTHER_FOLDER = [{"key": "claude:a1", "tool": "claude-code", "project": "proj-b",
                 "cwd": "/w/proj-b"}]


def test_other_folder_with_no_report_starts_and_tells_the_manager(hub):
    mgr, me = manager_online(hub)
    b, _, bob = in_use_setup(hub, OTHER_FOLDER)
    assert ask(hub, bob).startswith("starting `proj-a-w1`") and hub.mm.request(1) is None
    assert inbox(mgr, me) == [manager.text_unchecked("bob", "proj-a", HOST, ["alice"])]


def test_other_folder_with_light_use_starts_at_once(hub):
    mgr, me = manager_online(hub)
    light = {**HEAVY, "users": {"u": {"cpu": 0.5, "mem": 900, "gpus": {"0": 2000}}},
             "gpus": [{**g, "util": 5, "mem": 2000} for g in HEAVY["gpus"]]}
    _, _, bob = in_use_setup(hub, OTHER_FOLDER, light)
    assert ask(hub, bob).startswith("starting `proj-a-w1`") and hub.mm.request(1) is None
    assert inbox(mgr, me) == []


def test_heavy_use_held_and_decided(hub):
    mgr, me = manager_online(hub)
    b, _, bob = in_use_setup(hub, OTHER_FOLDER, HEAVY)
    assert ask(hub, bob) == ("busy: box-1 is in use by alice; the manager is deciding"
                             " (request 1); you will hear back here")
    assert poll(hub, b) == []
    out = run(call(mgr, me, "work_decide", request_id=1, machine=HOST, reason="it fits"))
    assert "proj-a-w1" in out and not hub.ws.by_name("proj-a-w1")["worktree"]


def test_choose_prefers_a_machine_nobody_uses_heavily(hub):
    manager_online(hub)
    _, bob_id, bob = in_use_setup(hub, use=HEAVY)
    a_id = next(p["id"] for p in hub.store.people() if p["name"] == "alice")
    code = hub.store.invite_create(a_id)
    token, _ = hub.store.invite_redeem(code, "box-2", host="box-2", os_user="u")
    hub.store.device_set_shared(hub.store.authenticate(token).device_id, True)
    facts = {"host": "box-2", "os_user": "u", "version": kaptyorka.__version__,
             "tmux": True, "load": 3.0, "cpus": 4,  # busier by load than box-1
             "use": {**HEAVY, "users": {}}}
    assert http.request("POST", hub.url + "/bridge/heartbeat", token,
                        {"machine": "box-2", "sessions": [], "tools": [], "facts": facts,
                         "folders": {"proj-a": "/w/proj-a"}})[0] == 200
    hub.store.grant_machine("box-2", bob_id, by="test")
    assert ask(hub, bob).startswith("starting `proj-a-w1` on box-2")


def test_heavy_use_holds_with_numbers(hub):
    mgr, me = manager_online(hub)
    _, _, bob = in_use_setup(hub, use=HEAVY)
    assert ask(hub, bob).startswith("busy: box-1 is in use by alice;")
    text = inbox(mgr, me)[0]
    assert text == manager.text_in_use("bob", "proj-a", HOST, ["alice"], [ALICE_LINE], 1)
    assert "Rule of thumb: heavy means" in text


def test_light_use_without_a_session_starts(hub):
    manager_online(hub)
    light = {**HEAVY, "users": {"u": {"cpu": 0.2, "mem": 500, "gpus": {}}}}
    _, _, bob = in_use_setup(hub, use=light)
    assert ask(hub, bob).startswith("starting `proj-a-w1`")


def test_stale_heavy_use_is_ignored(hub):
    manager_online(hub)
    _, _, bob = in_use_setup(hub, use={**HEAVY, "age": 400})
    assert ask(hub, bob).startswith("starting `proj-a-w1`")


def test_in_use_without_manager_starts_as_before(hub):
    _, _, bob = in_use_setup(hub, OTHER_FOLDER, HEAVY)
    assert ask(hub, bob).startswith("starting `proj-a-w1`") and hub.mm.request(1) is None


def test_own_use_never_holds_own_work(hub):
    manager_online(hub)
    hub.store.project_register("proj-a", by="test")
    bob_id, bob = member(hub, "bob")
    d = device(hub, bob_id, "u")
    d2 = device(hub, bob_id, "u2")  # the same person in a second OS user
    use_beat(hub, d2, {**HEAVY, "users": {"u2": HEAVY["users"]["u"]}},
             [{"key": "claude:b1", "tool": "claude-code", "project": "proj-b", "cwd": "/w/b"}])
    use_beat(hub, d)
    run(call(bob, BOSS, "inbox"))
    assert ask(hub, bob).startswith("starting `proj-a-w1`")


def test_the_manager_is_nobody_on_a_machine(hub):
    mgr, me = manager_online(hub)
    _, _, bob = in_use_setup(hub)
    with hub.store.lock:  # the manager's own session runs on box-1
        hub.store.db.execute("UPDATE agents SET host=? WHERE key='k:mgr'", (HOST,))
    assert ask(hub, bob).startswith("starting `proj-a-w1`")


def arrival_setup(hub, clock, use=HEAVY):
    clock[0] += 200  # past the hub's start-up grace
    mgr, me = manager_online(hub)
    hub.store.project_register("proj-a", by="test")
    a_id, _ = member(hub, "alice")
    a = device(hub, a_id, "u")
    use_beat(hub, a, use)
    bob_id, _ = member(hub, "bob")
    d = device(hub, bob_id, "bob")
    return mgr, me, a, d


BOB_SESSION = [{"key": "claude:b1", "tool": "claude-code", "project": "proj-a", "cwd": "/w/proj-a"}]


def arrival_texts(mgr, me):
    return [t for t in inbox(mgr, me) if "opened session" in t]


def test_direct_session_on_heavy_machine_warned_once(hub, clock):
    mgr, me, a, d = arrival_setup(hub, clock)
    use_beat(hub, d, sessions=BOB_SESSION)
    hub.tick()
    name = hub.store.agent("claude:b1").name
    assert arrival_texts(mgr, me) == [manager.text_arrival("bob", name, HOST, [ALICE_LINE])]
    for _ in range(3):  # every heartbeat and tick after: no second warning
        clock[0] += 15
        use_beat(hub, a, HEAVY)
        use_beat(hub, d, sessions=BOB_SESSION)
        hub.tick()
    assert arrival_texts(mgr, me) == []
    with hub.store.lock:
        kinds = [r["kind"] for r in hub.store.db.execute("SELECT kind FROM log")]
    assert kinds.count("machine_in_use") == 1
    clock[0] += 900  # bob was gone over 10 minutes: coming back is a new arrival
    run(call(mgr, me, "inbox"))
    use_beat(hub, a, HEAVY)
    use_beat(hub, d, sessions=BOB_SESSION)
    hub.tick()
    assert len(arrival_texts(mgr, me)) == 1


def test_direct_session_on_quiet_machine_not_warned(hub, clock):
    mgr, me, a, d = arrival_setup(hub, clock, use={**HEAVY, "users": {}})
    use_beat(hub, d, sessions=BOB_SESSION)
    hub.tick()
    assert arrival_texts(mgr, me) == []


def test_hub_worker_session_never_warned(hub, clock):
    mgr, me, a, d = arrival_setup(hub, clock)
    with hub.store.lock:
        hub.store.db.execute(
            "INSERT INTO workers (name, project, task, boss_key, boss_person, device_id, host,"
            " tool, key, state, created) VALUES ('proj-a-w1','proj-a','t','k:boss',1,?,?,"
            "'claude-code','claude:b1','up',?)", (d.device_id, HOST, clock[0]))
    use_beat(hub, d, sessions=BOB_SESSION)
    hub.tick()
    assert arrival_texts(mgr, me) == []


def test_use_numbers_only_for_the_admin(hub):
    in_use_setup(hub, use=HEAVY)
    _, admin_token = person(hub.store, "root", "admin")
    alice = next(p for p in hub.store.people() if p["name"] == "alice")
    with hub.store.lock:
        hub.store.db.execute("UPDATE machines SET owner_person_id=? WHERE host=?",
                             (alice["id"], HOST))
    _, alice_token = member(hub, "alice2")
    hub.store.grant_machine(HOST, next(p["id"] for p in hub.store.people()
                                       if p["name"] == "alice2"), by="test")

    def uses(token):
        status, rows = http.request("GET", hub.url + "/api/machines", token)
        assert status == 200
        return [d["facts"].get("use") for m in rows for d in m["devices"]]
    assert any(u and u["users"] for u in uses(admin_token))
    assert all(u is None for u in uses(alice_token.token))


def test_a_real_person_with_the_manager_role_counts(hub, clock):
    """Only the hub's own manager account is nobody; a person with the role is a person."""
    mgr, me, a, d = arrival_setup(hub, clock, use={**HEAVY, "users": {}})
    carol_id, _ = person(hub.store, "carol", "manager")
    c = device(hub, carol_id, "carol")
    carol_use = {**HEAVY, "users": {"carol": HEAVY["users"]["u"]}}
    use_beat(hub, a, carol_use)  # every bridge on a machine reports the same numbers
    use_beat(hub, c, carol_use)
    use_beat(hub, d, sessions=BOB_SESSION)
    hub.tick()
    line = ALICE_LINE.replace("alice", "carol", 1)
    name = hub.store.agent("claude:b1").name
    assert arrival_texts(mgr, me) == [manager.text_arrival("bob", name, HOST, [line])]
    use_beat(hub, c, {**HEAVY, "users": {"carol": HEAVY["users"]["u"]}},
             [{"key": "claude:c1", "tool": "claude-code", "project": "proj-a", "cwd": "/w/c"}])
    assert hub.ws.host_people(HOST, clock[0]).keys() >= {"carol", "bob"}
    assert any(s["person"] == "carol" for s in hub.ws.direct_sessions(clock[0]))


def test_switching_the_manager_on_warns_no_one_already_there(hub, clock):
    mgr, me, a, d = arrival_setup(hub, clock)
    hub.mm.set_enabled(False, "test")
    use_beat(hub, d, sessions=BOB_SESSION)
    hub.tick()  # recorded while off
    hub.mm.set_enabled(True, "test")
    clock[0] += 15
    run(call(mgr, me, "inbox"))
    use_beat(hub, a, HEAVY)
    use_beat(hub, d, sessions=BOB_SESSION)
    hub.tick()
    assert arrival_texts(mgr, me) == []


def test_machine_in_use_log_sentence():
    from kaptyorka import admin_ops

    row = {"person": "hub", "kind": "machine_in_use", "detail": f"bob on box-1: {ALICE_LINE}"}
    assert admin_ops.log_sentence(row) == (
        f"bob opened a session on box-1, which someone else uses heavily: {ALICE_LINE}")
    assert "machine_in_use" in dict(admin_ops.LOG_GROUPS)["machines"]


def test_notices_name_every_admin(hub, clock):
    now = clock[0]
    alice, _, _ = plain(hub, "alice")
    insert_worker(hub, "w-old", "abandoned", alice, now)
    person(hub.store, "root", "admin")
    first = admin_ops.admins(hub.store)[0]
    mgr, me = manager_online(hub)
    hub.tick()
    texts = drain(hub, mgr, me)
    assert "worker `w-old` of alice is abandoned (unattended a day) on box-1;" \
           f" tell each admin ({first}, root) in one line" in texts


def test_manager_down_notice_reaches_every_admin_page(hub, clock):
    from kaptyorka.hub import MANAGER_UNTRUSTED
    b = admin_bridge(hub)
    person(hub.store, "bob", "admin")
    hub.mm.set_enabled(True, "test")
    hub.tick()
    [o] = orders(hub, b)
    ack(hub, b, o, ok=False, error=TRUST)
    hub.tick()
    assert MANAGER_UNTRUSTED in page_texts(hub, "root")
    assert MANAGER_UNTRUSTED in page_texts(hub, "bob")


# the GPU share queue: a full queue or a waiting job is heavy for that computer

FULL_QUEUE = {"age": 1, "shares": 10, "holders": {"u": 10}, "whole": "", "waiting": 0, "waiters": {}}


def queue_beat(hub, d, q):
    facts = {"host": HOST, "os_user": d.os_user, "version": kaptyorka.__version__,
             "tmux": True, "load": 0.1, "cpus": 4, "gpu_queue": q}
    body = {"machine": HOST, "sessions": [], "tools": [], "facts": facts,
            "folders": {"proj-a": "/w/proj-a"}}
    status, answer = http.request("POST", hub.url + "/bridge/heartbeat", d.token, body)
    assert status == 200
    return answer


def test_full_gpu_queue_holds_hub_work(hub):
    mgr, me = manager_online(hub)
    b, _, bob = in_use_setup(hub)
    hub.ws.set_gpu_queue(HOST, "/srv/gpu.lock", 10)
    assert queue_beat(hub, b, FULL_QUEUE)["gpu_queue"] == {"lock": "/srv/gpu.lock", "shares": 10}
    # bob hears counts only; the manager's notice names who holds the shares
    assert ask(hub, bob) == ("busy: box-1 is in use by the GPU queue (10/10 in use); the manager"
                             " is deciding (request 1); you will hear back here")
    text = inbox(mgr, me)[0]
    assert "GPU queue: alice 10/10" in text and hub.mm.request(1) is not None


def test_a_waiting_job_holds_and_room_starts(hub):
    mgr, me = manager_online(hub)
    b, _, bob = in_use_setup(hub)
    hub.ws.set_gpu_queue(HOST, "/srv/gpu.lock", 10)
    queue_beat(hub, b, {**FULL_QUEUE, "holders": {"u": 2}})
    assert ask(hub, bob).startswith("starting `proj-a-w1`")  # room left, nobody waits
    queue_beat(hub, b, {**FULL_QUEUE, "holders": {"u": 2}, "waiting": 1, "waiters": {"u": 1}})
    assert ask(hub, bob).startswith("busy: box-1 is in use by the GPU queue (2/10 in use · 1 waiting)")
    hub.ws.set_gpu_queue(HOST, None)  # switched off: the old report no longer counts
    queue_beat(hub, b, FULL_QUEUE)
    assert ask(hub, bob).startswith("starting `proj-a-w")



def test_a_queue_report_going_stale_mid_request_does_not_crash(hub, monkeypatch):
    """The queue is read once per request: a report that goes stale right after is no crash."""
    mgr, me = manager_online(hub)
    b, _, bob = in_use_setup(hub)
    hub.ws.set_gpu_queue(HOST, "/srv/gpu.lock", 10)
    queue_beat(hub, b, FULL_QUEUE)
    real, calls = WorkerStore.host_queue, []

    def once(self, host, now):
        calls.append(host)
        return real(self, host, now) if len(calls) == 1 else None
    monkeypatch.setattr(WorkerStore, "host_queue", once)
    assert ask(hub, bob).startswith("busy: box-1 is in use by the GPU queue (10/10 in use)")
    text = inbox(mgr, me)[0]
    assert "GPU queue: alice 10/10 (names for admins only; to anyone else say: 10/10 in use)" in text


def test_the_managers_row_attaches_to_its_tmux_session(hub):
    running_manager(hub)
    assert hub.ws.by_name("manager")["attach"].endswith("tmux attach -t =kaptyorka-manager")


def test_never_a_second_manager_launch_while_one_is_live(hub, clock):
    b = running_manager(hub)
    for _ in range(3):
        clock[0] += 3600
        beat(hub, b, [{"key": "claude:m1", "tool": "claude-code", "project": "manager"}])
        hub.ws.notify("claude:m1", "wake up")
        hub.tick()
    assert [x for x in orders(hub, b) if x.get("kind") == "launch"] == []
    assert hub.mm.may_launch(clock[0]) == (False, "the manager is already running")


def test_the_managers_state_note_is_its_own(hub):
    """Its open tasks per person: the manager reads and writes it; nobody else reads it."""
    running_manager(hub)
    text = ("## What it is\nthe manager\n## Where it stands\nalice: reach bob\n## Decisions\n-\n"
            "## Open problems\n-\n## How to run and test\n-\n")
    hub.store.notes_write("claude:m1", "state", text, "manager")
    mp = hub.store._person_principal(hub.mm.person_id())
    assert hub.store.notes_read("state", mp, "manager").text == text
    pid, _ = person(hub.store, "alice", "member")
    with pytest.raises(Exception, match="not registered"):
        hub.store.notes_read("state", hub.store._person_principal(pid), "manager")
