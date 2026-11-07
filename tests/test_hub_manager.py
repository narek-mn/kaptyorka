"""The hub keeps the manager session alive through the admin's own bridge on its machine."""
import json
import os
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
    mm.set_enabled(False, "test")  # the start-up tick must not race the test
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
    hub.tick()
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
    ask(hub, bob)
    err = call_error(mgr, me, "work_decide", request_id=1, machine="box-9", reason="try")
    assert "no machine named box-9" in err
    assert hub.ws.by_name("proj-a-w1") is None and hub.mm.request(1)["decided_at"] is None


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
