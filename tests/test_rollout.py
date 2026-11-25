"""Staged updates on a fake clock: the hub's computer and one calm team machine update first;
the rest wait SOAK_S and follow only if those are healthy; a failure holds the rest and is told
once; the admin's go and hold; a machine offline during the window updates when it returns."""
import json
import time
from types import SimpleNamespace

import pytest

from conftest import start_hub
from helpers import person
from kaptyorka import http, paths, rollout
from kaptyorka.manager import ManagerStore
from kaptyorka.rollout import SOAK_S
from kaptyorka.store import Store

OLD, NEW = "0.9.34", "0.9.35"


def facts(store, device_id, **f):
    with store.lock:
        store.db.execute("INSERT OR REPLACE INTO device_facts (device_id, facts, folders, updated)"
                         " VALUES (?,?,?,?)", (device_id, json.dumps(f), "{}", store.clock()))


def device(store, token, host, user, version=OLD, sessions=(), activity=""):
    p = store.authenticate(token)
    store.heartbeat(host, [{"key": k, "tool": "codex", "project": "proj-a", "activity": activity or None}
                           for k in sessions], principal=p, facts={"host": host, "os_user": user})
    with store.lock:
        store.db.execute("UPDATE devices SET host=?, os_user=?, last_used=? WHERE id=?",
                         (host, user, store.clock(), p.device_id))
    facts(store, p.device_id, host=host, os_user=user, version=version)
    return p.device_id


@pytest.fixture
def fleet(tmp_path, monkeypatch):
    """alice (admin) on the hub's computer box-0 (her token is the hub key), two team machines
    (box-1 idle, box-2 with a session waiting on a background task), bob's laptop pc-b."""
    s = Store(tmp_path / "hub.db")
    clock = [time.time()]
    s.clock = lambda: clock[0]
    ManagerStore(s).set_enabled(False, "test")
    alice, atok = person(s, "alice", "admin")
    bob, btok = person(s, "bob")
    (paths.home()).mkdir(parents=True, exist_ok=True)
    (paths.home() / "admin-token").write_text(atok)
    ids = {"hub": device(s, atok, "box-0", "alice", sessions=["a1"])}
    for name in ("box-1", "box-2"):
        _, code = s.team_machine_add(name, "alice")
        t, _ = s.invite_redeem(code, name, host=name, os_user="u")
        ids[name] = device(s, t, name, "u", sessions=[f"s-{name}"],
                           activity="shell" if name == "box-2" else "")
    ids["laptop"] = device(s, btok, "pc-b", "bob", sessions=["b1"])
    yield SimpleNamespace(store=s, ids=ids, clock=clock, atok=atok)
    s.close()


def later(f, seconds, *alive):
    """Time passes; these devices' bridges keep reporting (and their sessions)."""
    f.clock[0] += seconds
    with f.store.lock:
        for d in alive:
            f.store.db.execute("UPDATE devices SET last_used=? WHERE id=?", (f.clock[0], f.ids[d]))
            f.store.db.execute("UPDATE agents SET last_seen=? WHERE device_id=?", (f.clock[0], f.ids[d]))


def updated(f, *names, version=NEW):
    for n in names:
        row = f.store.db.execute("SELECT host, os_user FROM devices WHERE id=?", (f.ids[n],)).fetchone()
        facts(f.store, f.ids[n], host=row["host"], os_user=row["os_user"], version=version)


def may(f, name):
    return rollout.allows(f.store, f.ids[name], NEW)


ALL = ("hub", "box-1", "box-2", "laptop")


def test_first_group_is_the_hub_computer_and_the_calm_team_machine(fleet):
    assert [may(fleet, n) for n in ALL] == [True, True, False, False]
    s = rollout.status(fleet.store, NEW)
    assert s["state"] == "soak" and s["first"] == ["alice@box-0", "u@box-1"]
    assert rollout.line(s) == "update 0.9.35: first alice@box-0, u@box-1; the rest follow in 5 min if they are healthy"


def test_a_busy_team_machine_never_goes_first(fleet):
    with fleet.store.lock:  # box-1 is gone: only box-2 (waiting on a task) is online
        fleet.store.db.execute("UPDATE devices SET last_used=? WHERE id=?", (fleet.clock[0] - 3600, fleet.ids["box-1"]))
    assert rollout.pick_first(fleet.store, fleet.clock[0]) == [fleet.ids["hub"]]  # the hub alone


@pytest.mark.parametrize("busy", ["working", "night", "worker"])
def test_a_team_machine_with_a_busy_session_or_worker_is_not_picked(fleet, busy):
    s, box1 = fleet.store, fleet.ids["box-1"]
    key = "s-box-1"
    night = frozenset()
    with s.lock:
        if busy == "working":
            s.db.execute("UPDATE agents SET activity='running tests' WHERE key=?", (key,))
        elif busy == "worker":
            s.db.execute("INSERT INTO workers (name, project, task, boss_key, boss_person,"
                         " device_id, host, tool, state, created) VALUES ('w1','proj-a','t',"
                         "'k',1,?,'box-1','claude-code','up',?)", (box1, fleet.clock[0]))
    if busy == "night":
        night = frozenset({key})
    assert rollout.pick_first(s, fleet.clock[0], night) == [fleet.ids["hub"]]


def test_a_team_machine_with_no_session_goes_first(fleet):
    s = fleet.store
    _, code = s.team_machine_add("box-3", "alice")
    t, _ = s.invite_redeem(code, "box-3", host="box-3", os_user="u")
    fleet.ids["box-3"] = device(s, t, "box-3", "u")  # no session at all
    assert rollout.pick_first(s, fleet.clock[0]) == sorted([fleet.ids["hub"], fleet.ids["box-3"]])


def test_the_first_group_waits_for_fresh_reports(fleet):
    st = rollout.state(fleet.store, NEW, fleet.clock[0], ready=False)
    assert st["state"] == "pending" and not rollout.allows(fleet.store, fleet.ids["hub"], NEW, ready=False)
    assert rollout.line(rollout.status(fleet.store, NEW, ready=False)) \
        == "update 0.9.35: choosing the first machines once they report"
    assert rollout._get(fleet.store) is None  # nothing decided yet
    assert rollout.state(fleet.store, NEW, fleet.clock[0], ready=True)["state"] == "soak"


def test_healthy_first_group_releases_everyone(fleet):
    rollout.state(fleet.store, NEW)
    later(fleet, 60, *ALL)
    updated(fleet, "hub", "box-1")
    assert rollout.tick(fleet.store, NEW, fleet.clock[0]) == []  # still inside the window
    assert not may(fleet, "laptop")
    later(fleet, SOAK_S, *ALL)
    assert rollout.tick(fleet.store, NEW, fleet.clock[0]) == []
    assert [may(fleet, n) for n in ALL] == [True] * 4
    assert rollout.status(fleet.store, NEW)["state"] == "passed"


def test_a_failed_update_holds_the_rest_and_is_told_once(fleet):
    rollout.state(fleet.store, NEW)
    later(fleet, SOAK_S + 1, *ALL)
    updated(fleet, "hub")
    facts(fleet.store, fleet.ids["box-1"], host="box-1", os_user="u", version=OLD, update_failed="auto-update failed")
    told = rollout.tick(fleet.store, NEW, fleet.clock[0])
    assert len(told) == 1 and "held" in told[0] and "u@box-1: auto-update failed" in told[0]
    assert "kaptyorka fleet rollout go" in told[0]
    assert not may(fleet, "laptop") and not may(fleet, "box-2")
    later(fleet, 600, *ALL)
    assert rollout.tick(fleet.store, NEW, fleet.clock[0]) == []  # once
    assert rollout.status(fleet.store, NEW)["state"] == "held"


def test_lost_sessions_or_still_old_hold(fleet):
    rollout.state(fleet.store, NEW)
    later(fleet, SOAK_S + 1, "hub", "box-1", "laptop")  # box-2 silent: not judged
    updated(fleet, "box-1")
    with fleet.store.lock:  # the hub computer's session vanished after its update
        fleet.store.db.execute("UPDATE agents SET last_seen=0 WHERE key='a1'")
    updated(fleet, "hub")
    told = rollout.tick(fleet.store, NEW, fleet.clock[0])
    assert "alice@box-0 lists no sessions since its update" in told[0]


def test_admin_go_and_hold(fleet):
    rollout.decide(fleet.store, NEW, go=False, by="alice")
    assert not may(fleet, "laptop") and may(fleet, "hub")
    later(fleet, SOAK_S + 1, *ALL)
    updated(fleet, "hub", "box-1")
    assert rollout.tick(fleet.store, NEW, fleet.clock[0]) == []  # held stays held
    assert not may(fleet, "laptop")
    s = rollout.decide(fleet.store, NEW, go=True, by="alice")
    assert s["state"] == "passed" and may(fleet, "laptop")


def test_offline_during_the_window_updates_when_back(fleet):
    rollout.state(fleet.store, NEW)
    later(fleet, SOAK_S + 1, "hub", "box-1")  # the laptop is closed the whole time
    updated(fleet, "hub", "box-1")
    rollout.tick(fleet.store, NEW, fleet.clock[0])
    later(fleet, 7200, "laptop")  # it comes back hours later
    assert may(fleet, "laptop")


def test_no_first_machine_online_waits(fleet):
    rollout.state(fleet.store, NEW)
    later(fleet, SOAK_S + rollout.ONLINE_S + 1, "laptop")  # neither the hub computer nor box-1 reports
    assert rollout.tick(fleet.store, NEW, fleet.clock[0]) == []
    assert rollout.status(fleet.store, NEW)["state"] == "soak"


def test_a_new_hub_version_starts_a_new_rollout(fleet):
    rollout.decide(fleet.store, NEW, go=True, by="alice")
    assert may(fleet, "laptop")
    assert not rollout.allows(fleet.store, fleet.ids["laptop"], "0.9.36")


def test_heartbeat_routes_and_cli(fleet, monkeypatch, capsys):
    """The bridge hears auto_update only when it may; the admin's route; the CLI."""
    from kaptyorka import __version__, cli

    tokens = {}
    _, tokens["laptop2"] = person(fleet.store, "carol")
    stop = start_hub(fleet.store)
    url = f"http://127.0.0.1:{stop.port}"
    try:
        beat0 = lambda: http.request("POST", url + "/bridge/heartbeat", fleet.atok, {  # noqa: E731
            "machine": "box-0", "sessions": [], "tools": [], "folders": {},
            "facts": {"host": "box-0", "os_user": "u", "version": OLD}})[1]["auto_update"]
        assert beat0() is False  # just started: the team machines have not reported yet
        fleet.clock[0] += 121  # the hub waits for fresh reports two minutes at most

        def beat(token, host):
            return http.request("POST", url + "/bridge/heartbeat", token, {
                "machine": host, "sessions": [], "tools": [], "folders": {},
                "facts": {"host": host, "os_user": "u", "version": OLD}})[1]["auto_update"]
        assert beat(fleet.atok, "box-0") is True  # the hub's computer goes first
        assert beat(tokens["laptop2"], "pc-c") is False  # a laptop waits
        status, s = http.request("GET", url + "/api/rollout", fleet.atok)
        assert status == 200 and s["version"] == __version__ and s["state"] == "soak"
        assert http.request("GET", url + "/api/rollout", tokens["laptop2"])[0] == 403  # the admin only
        assert http.request("POST", url + "/api/rollout", fleet.atok, {"go": "yes"})[0] == 400
        monkeypatch.setattr(cli, "_admin_call", lambda m, p, b=None: http.request(m, url + p, fleet.atok, b))
        from test_cli import entry_with_exit
        assert entry_with_exit(["fleet", "rollout", "go"]) in (0, None)
        assert capsys.readouterr().out.startswith(f"update {__version__}: released to every machine")
        assert beat(tokens["laptop2"], "pc-c") is True  # released
        assert entry_with_exit(["fleet", "rollout", "hold"]) in (0, None)
        assert beat(tokens["laptop2"], "pc-c") is False
        assert entry_with_exit(["fleet", "rollout"]) in (0, None)
        assert "held: held by alice" in capsys.readouterr().out
    finally:
        stop()


def test_a_bridge_one_version_behind_still_reports(fleet):
    """During the window most bridges run the old version: a heartbeat with only the fields
    older bridges send (no overnight, idle or title fields) is still taken and its sessions
    listed, and its poll answered. The hub reads every newer field with a default."""
    stop = start_hub(fleet.store)
    try:
        url = f"http://127.0.0.1:{stop.port}"
        _, tok = person(fleet.store, "dave")
        status, body = http.request("POST", url + "/bridge/heartbeat", tok, {
            "machine": "pc-d", "sessions": [{"key": "d1", "tool": "codex", "project": "proj-a"}],
            "tools": ["codex"], "bridge": True, "folders": {},  # no "overnight" (newer), no idle_s/title
            "facts": {"host": "pc-d", "os_user": "dave", "version": OLD}})
        assert status == 200 and body["version"] and "auto_update" in body
        assert fleet.store.agent("d1") is not None
        assert http.request("GET", url + "/bridge/poll?machine=pc-d&wait=0", tok)[0] == 200
    finally:
        stop()


def test_a_bridge_that_updated_and_then_went_silent_holds_the_rest(fleet):
    rollout.state(fleet.store, NEW)
    later(fleet, 60, *ALL)
    updated(fleet, "hub", "box-1")
    later(fleet, SOAK_S, "hub", "laptop")  # box-1 reported the new version once, then nothing
    told = rollout.tick(fleet.store, NEW, fleet.clock[0])
    assert len(told) == 1 and "u@box-1 stopped reporting after its update" in told[0]
    assert not may(fleet, "laptop")


def test_a_first_machine_that_was_offline_before_the_update_is_not_a_hold(fleet):
    rollout.state(fleet.store, NEW)
    with fleet.store.lock:  # box-1 was already gone when the update started
        fleet.store.db.execute("UPDATE devices SET last_used=? WHERE id=?",
                               (fleet.clock[0] - 3600, fleet.ids["box-1"]))
    later(fleet, SOAK_S + 1, "hub", "laptop")
    updated(fleet, "hub")
    assert rollout.tick(fleet.store, NEW, fleet.clock[0]) == []
    assert rollout.status(fleet.store, NEW)["state"] == "passed"
