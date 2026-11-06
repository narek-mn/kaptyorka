import pytest

from helpers import person
from kaptyorka.store import ONLINE_S, Store
from kaptyorka.workers import WorkerStore


@pytest.fixture
def clock():
    return [1_000_000.0]


@pytest.fixture
def store(tmp_path, clock):
    s = Store(tmp_path / "hub.db", clock=lambda: clock[0])
    yield s
    s.close()


@pytest.fixture
def ws(store):
    return WorkerStore(store)


def device(store, person_id, host, os_user="u"):
    """A redeemed device (with a signing secret) on `host`, bridge online now."""
    code = store.invite_create(person_id)
    token, _ = store.invite_redeem(code, f"{host}-{person_id}", host=host, os_user=os_user)
    p = store.authenticate(token)
    store.heartbeat(host, [], principal=p, facts={"host": host, "os_user": os_user})
    return p.device_id, token


@pytest.fixture
def alice(store):
    return person(store, "alice")[0]


@pytest.fixture
def bob(store):
    return person(store, "bob")[0]


@pytest.fixture
def dev(store, alice):
    return device(store, alice, "box-1")[0]


def principal(store, person_id):
    code = store.invite_create(person_id)
    token, _ = store.invite_redeem(code, f"web-{person_id}")
    return store.authenticate(token)


def test_facts_roundtrip_drops_unknown_keys(ws, dev):
    ws.facts_seen(dev, {"tmux": True, "load": 0.5, "cpus": 4, "junk": 1}, {"proj-a": "/home/alice/proj-a"})
    assert ws.facts(dev) == {"tmux": True, "load": 0.5, "cpus": 4}
    assert ws.folders(dev) == {"proj-a": "/home/alice/proj-a"}


def test_facts_cut_strings_and_drop_bad_types(ws, dev):
    ws.facts_seen(dev, {"version": "v" * 500, "tmux": "yes", "cpus": True,
                        "tools": {"claude-code": "x" * 300}, "hooks_ok": {"claude-code": True},
                        "git": {"/w": {"branch": "main", "dirty": True, "subject": "s" * 300,
                                       "extra": 1}}},
                  {"proj-a": "/" + "p" * 300})
    f = ws.facts(dev)
    assert f["version"] == "v" * 200
    assert "tmux" not in f and "cpus" not in f
    assert f["tools"] == {"claude-code": "x" * 200}
    assert f["hooks_ok"] == {"claude-code": True}
    assert f["git"] == {"/w": {"branch": "main", "dirty": True, "subject": "s" * 200}}
    assert len(ws.folders(dev)["proj-a"]) == 200


def test_facts_empty_when_none(ws, dev):
    assert ws.facts(dev) == {} and ws.folders(dev) == {}


def test_candidates_own_device_shared_device_and_reasons(store, ws, alice, bob, clock):
    own = device(store, alice, "box-1")[0]
    shared = device(store, bob, "box-2")[0]
    store.device_set_shared(shared, True)
    store.grant_machine("box-2", alice, by="test")
    device(store, bob, "box-4")  # bob's private device: alice never sees it
    token = store.device_add(bob, "box-3")  # no signing secret
    p = store.authenticate(token)
    store.heartbeat("box-3", [], principal=p, facts={"host": "box-3", "os_user": "u"})
    store.device_set_shared(p.device_id, True)
    store.grant_machine("box-3", alice, by="test")
    ws.facts_seen(own, {"tmux": True, "load": 0.1, "cpus": 2}, {"proj-a": "/home/alice/proj-a"})
    me = principal(store, alice)

    usable, reasons = ws.candidates(me, "proj-a", None)
    assert [c["host"] for c in usable] == ["box-1", "box-2"]
    first = usable[0]
    assert first["device_id"] == own and first["person_id"] == alice and first["os_user"] == "u"
    assert first["tmux"] is True and first["load"] == 0.1 and first["folder"] == "/home/alice/proj-a"
    assert usable[1]["folder"] is None
    assert reasons == ["box-3 (bob): still on the shared token; invite it or assign it so it gets"
                       " its own token"]

    usable, _ = ws.candidates(me, "proj-a", "box-2")
    assert [c["host"] for c in usable] == ["box-2"]

    clock[0] += ONLINE_S + 1
    usable, reasons = ws.candidates(me, "proj-a", "box-2")
    assert usable == [] and reasons == ["box-2 (bob): bridge offline"]


def test_choose_prefers_free_folder_then_low_load(store, ws, alice, clock):
    d1 = device(store, alice, "box-1")[0]
    d2 = device(store, alice, "box-2")[0]
    d3 = device(store, alice, "box-3")[0]
    ws.facts_seen(d1, {"tmux": True, "load": 0.1, "cpus": 4}, {"proj-a": "/w/proj-a"})
    ws.facts_seen(d2, {"tmux": True, "load": 3.0, "cpus": 4}, {"proj-a": "/w/proj-a"})
    ws.facts_seen(d3, {"tmux": True, "load": 0.0, "cpus": 4}, {})  # no folder
    store.heartbeat("box-1", [{"key": "claude:1", "tool": "claude-code", "project": "proj-a",
                               "cwd": "/w/proj-a"}], facts={"host": "box-1", "os_user": "u"})
    usable, _ = ws.candidates(principal(store, alice), "proj-a", None)
    now = clock[0]
    got = ws.choose(usable, "proj-a", "claude-code", now)
    assert got["host"] == "box-2" and got["occupied"] is False
    # once box-1's session is gone, its lower load wins
    got = ws.choose(usable, "proj-a", "claude-code", now + ONLINE_S + 1)
    assert got["host"] == "box-1" and got["occupied"] is False
    # every folder busy: the lowest load, marked occupied
    store.heartbeat("box-2", [{"key": "claude:2", "tool": "claude-code", "project": "proj-a",
                               "cwd": "/w/proj-a"}], facts={"host": "box-2", "os_user": "u"})
    got = ws.choose(usable, "proj-a", "claude-code", now)
    assert got["host"] == "box-1" and got["occupied"] is True


def test_choose_needs_tmux_for_non_claude(ws):
    c = {"device_id": 1, "host": "box-1", "os_user": "u", "person_id": 1, "tmux": False,
         "folder": "/x", "load": 0, "cpus": 1, "tools": {}}
    assert ws.choose([c], "p", "codex", 0) is None
    assert ws.choose([c], "p", "claude-code", 0)["host"] == "box-1"
    assert ws.choose([{**c, "folder": None}], "p", "claude-code", 0) is None


def test_effective_auto_update_default_by_role(store, ws):
    admin = person(store, "alice", "admin")[0]
    member = person(store, "bob")[0]
    admin_dev = device(store, admin, "box-1")[0]
    member_dev = device(store, member, "box-2")[0]
    ws.facts_seen(member_dev, {"tmux": True}, {"proj-a": "/w/proj-a"})
    rows = ws.machine_rows(principal(store, admin))
    devs = {d["id"]: d for m in rows for d in m["devices"]}
    assert devs[admin_dev]["auto_update"] is False
    assert devs[member_dev]["auto_update"] is True
    assert devs[member_dev]["online"] is True
    assert devs[member_dev]["facts"] == {"tmux": True}
    assert devs[member_dev]["folders"] == {"proj-a": "/w/proj-a"}
    ws.set_auto_update(member_dev, False)
    ws.set_auto_update(admin_dev, True)
    devs = {d["id"]: d for m in ws.machine_rows(principal(store, admin)) for d in m["devices"]}
    assert devs[member_dev]["auto_update"] is False and devs[admin_dev]["auto_update"] is True
    ws.set_auto_update(member_dev, None)
    devs = {d["id"]: d for m in ws.machine_rows(principal(store, admin)) for d in m["devices"]}
    assert devs[member_dev]["auto_update"] is True


def test_machine_rows_offline_bridge(store, ws, clock):
    admin = person(store, "alice", "admin")[0]
    d = device(store, admin, "box-1")[0]
    me = principal(store, admin)
    clock[0] += ONLINE_S + 1
    devs = {x["id"]: x for m in ws.machine_rows(me) for x in m["devices"]}
    assert devs[d]["online"] is False and devs[d]["facts"] == {}


def test_worker_tables_exist(store):
    names = {r[0] for r in store.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"workers", "launches", "device_facts", "device_settings"} <= names


def test_agents_cwd_is_stored(store):
    store.heartbeat("box-1", [{"key": "claude:1", "tool": "claude-code", "project": "proj-a", "cwd": "/home/alice/proj-a"}])
    assert store.db.execute("SELECT cwd FROM agents WHERE key='claude:1'").fetchone()[0] == "/home/alice/proj-a"


def test_facts_drop_non_finite_load(ws, dev):
    ws.facts_seen(dev, {"load": float("nan"), "cpus": 2}, {})
    assert ws.facts(dev) == {"cpus": 2}
    ws.facts_seen(dev, {"load": float("inf")}, {})
    assert ws.facts(dev) == {}
