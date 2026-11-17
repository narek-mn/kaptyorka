"""What the hub decides by itself so the admin's Team page stays small (0.9.7): the first person
to join from a machine name owns it, a later person's use waits for the admin's Yes, the old
shared token switches itself off after 7 unused days, and removing a person signs them out."""
import pytest

from helpers import person
from kaptyorka import admin_ops
from kaptyorka.auth import LEGACY_OFF
from kaptyorka.cli import _problem
from kaptyorka.store import LEGACY_IDLE_S, Store
from kaptyorka.workers import WorkerStore


@pytest.fixture
def clock():
    return [1_000_000.0]


@pytest.fixture
def store(tmp_path, clock):
    s = Store(tmp_path / "hub.db", clock=lambda: clock[0])
    yield s
    s.close()


def join(store, person_id, host, name=None):
    """A redeemed device on `host`, its bridge's first heartbeat sent; (principal, token)."""
    code = store.invite_create(person_id)
    token, _ = store.invite_redeem(code, name or f"{host}-{person_id}", host=host, os_user="u")
    p = store.authenticate(token)
    store.heartbeat(host, [], principal=p, facts={"host": host, "os_user": "u"})
    return p, token


def owner(store, host):
    return next(m["owner"] for m in store.machines(store.authenticate(store._admin_token))
                if m["host"] == host)


@pytest.fixture
def team(store):
    ids = {n: person(store, n, r)[0] for n, r in (("admin", "admin"), ("alice", "member"),
                                                  ("bob", "member"))}
    _, store._admin_token = join(store, ids["admin"], "hub-box")
    return ids


def test_first_person_to_join_owns_the_machine(store, team):
    join(store, team["alice"], "box-1")
    assert owner(store, "box-1") == "alice"
    assert store.may_use("box-1", team["alice"])
    assert not store.requests()  # no admin step
    assert any(r["kind"] == "machine_owner_auto" for r in store.log())
    row = next(r for r in store.log() if r["kind"] == "machine_owner_auto")
    assert admin_ops.log_sentence(row) == "alice owns box-1: the first to join from it"


def test_a_second_person_on_an_owned_name_gets_no_use_until_yes(store, team):
    alice_p, _ = join(store, team["alice"], "box-1")
    store.device_set_shared(alice_p.device_id, True)
    join(store, team["bob"], "box-1")  # bob reports alice's machine name
    assert owner(store, "box-1") == "alice"
    assert not store.may_use("box-1", team["bob"])
    # he cannot start a worker on alice's (shared) account there; his own device stays his
    ws = WorkerStore(store)
    usable, _ = ws.candidates_for(team["bob"], "proj-a", "box-1")
    assert alice_p.device_id not in [c["device_id"] for c in usable]
    # his ask waits for the admin, once
    reqs = store.requests()
    assert [(r["person"], r["kind"], r["target"]) for r in reqs] == [("bob", "machine", "box-1")]
    join(store, team["bob"], "box-1", name="second")
    assert len(store.requests()) == 1
    store.decide_request(reqs[0]["id"], team["admin"], True)  # Yes: the machine grant
    assert store.may_use("box-1", team["bob"])
    usable, _ = ws.candidates_for(team["bob"], "proj-a", "box-1")
    assert alice_p.device_id in [c["device_id"] for c in usable]


def test_a_no_is_not_asked_again(store, team):
    join(store, team["alice"], "box-1")
    join(store, team["bob"], "box-1")
    store.decide_request(store.requests()[0]["id"], team["admin"], False)
    join(store, team["bob"], "box-1", name="again")
    assert store.requests() == [] and not store.may_use("box-1", team["bob"])


def test_admin_and_manager_account_never_ask(store, team):
    join(store, team["alice"], "box-1")
    join(store, team["admin"], "box-1", name="admin-2")
    _, mgr_token = person(store, "manager", "manager")
    mgr = store.authenticate(mgr_token)
    store.heartbeat("hub-box", [], principal=mgr, facts={"host": "hub-box", "os_user": "u"})
    assert store.requests() == []
    assert owner(store, "hub-box") == "admin"


def test_no_owner_when_two_people_were_there_before(store, team):
    # devices from before 0.9.7: two people already report the name, neither is first for sure
    for pid, name in ((team["alice"], "a"), (team["bob"], "b")):
        store.invite_redeem(store.invite_create(pid), name, host="box-9", os_user="u")
    with store.lock:
        store.db.execute("INSERT INTO machines (host, owner_person_id, first_seen) VALUES ('box-9', NULL, 1)")
    join(store, team["alice"], "box-9", name="c")
    assert owner(store, "box-9") is None and store.requests() == []


def legacy(tmp_path, clock):
    s = Store(tmp_path / "old.db", clock=lambda: clock[0], legacy_token="old-shared")
    person(s, "admin", "admin")
    return s


def test_shared_token_switches_itself_off_after_7_unused_days(tmp_path, clock):
    s = legacy(tmp_path, clock)
    assert s.authenticate("old-shared", machine="box-1") is not None  # used now
    clock[0] += LEGACY_IDLE_S - 60
    assert not s.legacy_auto_off(clock[0]) and s.legacy_enabled()  # used within 7 days: stays
    clock[0] += 120
    assert s.legacy_auto_off(clock[0]) and not s.legacy_enabled()
    assert not s.legacy_auto_off(clock[0])  # once
    assert s.authenticate("old-shared", machine="box-1") is None
    assert s.legacy_switched_off("old-shared") and not s.legacy_switched_off("other")
    row = next(r for r in s.log() if r["kind"] == "legacy_auto_off")
    assert admin_ops.log_sentence(row) == \
        "The shared token switched itself off: no machine used it for 7 days"
    s.close()


def test_shared_token_never_used_counts_from_when_it_was_made(tmp_path, clock):
    s = legacy(tmp_path, clock)
    clock[0] += LEGACY_IDLE_S + 1
    assert s.legacy_auto_off(clock[0])
    s.close()


def test_switched_off_machine_is_told_what_to_do(tmp_path, clock):
    from conftest import start_hub
    from kaptyorka import http
    s = legacy(tmp_path, clock)
    s.set_legacy_enabled(False, "admin")
    stop = start_hub(s)
    try:
        status, body = http.request("GET", f"http://127.0.0.1:{stop.port}/api/me", "old-shared")
        assert status == 401 and body == LEGACY_OFF
        assert "kaptyorka shared-token on" in body and "kaptyorka join <link>" in body
        assert _problem((status, body)) == LEGACY_OFF
        status, body = http.request("GET", f"http://127.0.0.1:{stop.port}/api/me", "nope")
        assert status == 401 and body == "unauthorized"
    finally:
        stop()
        s.close()


def test_removing_a_person_signs_out_every_device(store, team):
    tokens = [join(store, team["bob"], "box-2", name=n)[1] for n in ("laptop", "desk", "phone")]
    assert all(store.authenticate(t) for t in tokens)
    admin = store.authenticate(store._admin_token)
    done = admin_ops.remove_person(store, WorkerStore(store), "bob", admin)
    assert done["devices"] == 4  # with the one person() made
    assert all(store.authenticate(t) is None for t in tokens)


def test_the_hub_tick_switches_the_shared_token_off(tmp_path, clock):
    from conftest import start_hub
    s = legacy(tmp_path, clock)
    stop = start_hub(s)
    try:
        clock[0] += LEGACY_IDLE_S + 1
        stop.app.state.tick()
        assert not s.legacy_enabled()
    finally:
        stop()
        s.close()
