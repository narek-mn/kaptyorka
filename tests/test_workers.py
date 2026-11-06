import pytest

from helpers import person
from kaptyorka.orders import verify
from kaptyorka.store import ONLINE_S, KaptyorkaError, Store
from kaptyorka.workers import ABANDONED_S, UNATTENDED_S, WorkerStore


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


# launches, linking, states


FOLDER = "/home/alice/proj-a"


@pytest.fixture
def alice_p(store, alice):
    return principal(store, alice)


@pytest.fixture
def boss(store, alice_p):
    store.seen("claude:boss", "box-1", "claude-code", "proj-a", principal=alice_p)
    return "claude:boss"


def target(dev_id, host="box-1", os_user="u"):
    return {"device_id": dev_id, "host": host, "os_user": os_user, "folder": FOLDER}


def secret_hash(store, dev_id):
    return store.db.execute("SELECT secret_hash FROM devices WHERE id=?", (dev_id,)).fetchone()[0]


def launch(ws, alice_p, dev, boss, task="fix the tests", occupied=False):
    return ws.create(alice_p, "proj-a", task, target(dev), "claude-code", boss, None, occupied)


def launch_id(store, worker, kind="launch"):
    return store.db.execute("SELECT id FROM launches WHERE worker=? AND kind=? ORDER BY id DESC",
                            (worker, kind)).fetchone()[0]


def worker_up(store, ws, dev, token, key="claude:w1", activity=None):
    p = store.authenticate(token)
    store.heartbeat("box-1", [{"key": key, "tool": "claude-code", "project": "proj-a",
                               "cwd": FOLDER, "activity": activity}],
                    principal=p, facts={"host": "box-1", "os_user": "u"})


def test_create_names_and_signs(store, ws, alice_p, dev, boss, clock):
    w = launch(ws, alice_p, dev, boss)
    assert w["name"] == "proj-a-w1" and w["state"] == "starting"
    assert w["boss"] == "box-1/claude-code/proj-a" and w["worktree"] is False
    assert w["attach"] == "claude attach proj-a-w1"
    [item] = ws.offer(dev)
    assert item["launch"] is True and item["worker"] == "proj-a-w1"
    order = {k: v for k, v in item.items() if k != "launch"}
    assert list(order) == ["kind", "id", "issued", "worker", "person", "tool", "project",
                           "folder", "worktree", "task", "boss", "resume", "first_message", "sig"]
    assert order["kind"] == "launch" and order["person"] == "alice" and order["folder"] == FOLDER
    assert order["first_message"] == (
        "You are kaptyorka worker `proj-a-w1` for `box-1/claude-code/proj-a` (alice). Your task:"
        "\n\nfix the tests\n\nTake orders only from `box-1/claude-code/proj-a` and the hub admin."
        " Report to `box-1/claude-code/proj-a` with send(to=\"box-1/claude-code/proj-a\", ...)"
        " when done or stuck.")
    assert verify(order, secret_hash(store, dev), clock[0], ()) is None
    assert ws.offer(dev) == []  # offered once per 60 s
    clock[0] += 61
    assert [i["worker"] for i in ws.offer(dev)] == ["proj-a-w1"]
    assert launch(ws, alice_p, dev, boss)["name"] == "proj-a-w2"


def test_create_refuses_empty_task_and_bad_project(ws, alice_p, dev, boss):
    with pytest.raises(KaptyorkaError):
        launch(ws, alice_p, dev, boss, task="  ")
    with pytest.raises(KaptyorkaError):
        ws.create(alice_p, "bad name!", "t", target(dev), "claude-code", boss, None, False)


def test_second_launch_in_same_folder_gets_worktree(store, ws, alice, alice_p, boss, clock):
    dev, token = device(store, alice, "box-1")
    ws.facts_seen(dev, {"tmux": True, "load": 0.1, "cpus": 2}, {"proj-a": FOLDER})
    worker_up(store, ws, dev, token, key="claude:other")  # a session already in the folder
    usable, _ = ws.candidates(alice_p, "proj-a", None)
    got = ws.choose(usable, "proj-a", "claude-code", clock[0])
    assert got["occupied"] is True
    w = ws.create(alice_p, "proj-a", "t", got, "claude-code", boss, None, got["occupied"])
    assert w["worktree"] is True
    assert ws.offer(dev)[0]["worktree"] is True
    # a free folder that a starting worker already took: re-checked under the lock
    clock[0] += 600
    store.seen("claude:boss", "box-1", "claude-code", "proj-a", principal=alice_p)
    a = ws.create(alice_p, "proj-a", "t", target(dev), "claude-code", boss, None, False)
    b = ws.create(alice_p, "proj-a", "t", target(dev), "claude-code", boss, None, False)
    # proj-a-w1 runs in a worktree, so the folder is free for a; a still starting holds it for b
    assert a["worktree"] is False and b["worktree"] is True


def test_link_marks_up_and_moves_session_to_boss_group(store, ws, alice, alice_p, boss):
    dev, token = device(store, alice, "box-1")
    launch(ws, alice_p, dev, boss)
    store.heartbeat("box-1", [{"key": "claude:w1", "tool": "claude-code", "project": "proj-a"}])
    assert store.agent("claude:w1").person_id is None
    assert ws.link("proj-a-w1", "claude:w1", dev) is True
    assert store.agent("claude:w1").person_id == alice
    w = ws.by_key("claude:w1")
    assert w["name"] == "proj-a-w1" and w["state"] == "up" and w["key"] == "claude:w1"
    assert ws.link("proj-a-w1", "claude:w1", dev) is False  # no longer starting


def test_link_before_heartbeat_and_from_another_device(store, ws, alice, bob, alice_p, boss):
    dev = device(store, alice, "box-1")[0]
    other = device(store, bob, "box-2")[0]
    launch(ws, alice_p, dev, boss)
    assert ws.link("proj-a-w1", "claude:w1", other) is False
    assert ws.link("proj-a-w9", "claude:w1", dev) is False
    assert ws.link("proj-a-w1", "claude:w1", dev) is True
    assert store.agent("claude:w1").person_id == alice and ws.by_name("proj-a-w1")["state"] == "up"


def test_failed_launch_marks_failed_with_error(store, ws, alice_p, dev, boss, clock):
    launch(ws, alice_p, dev, boss)
    lid = launch_id(store, "proj-a-w1")
    assert ws.ack(lid, False, "no folder", dev) is True
    w = ws.by_name("proj-a-w1")
    assert w["state"] == "failed" and "no folder" in w["error"]
    assert ws.offer(dev) == []
    store.seen("claude:boss", "box-1", "claude-code", "proj-a", principal=alice_p)
    assert ws.tick(clock[0]) == [("claude:boss", "worker `proj-a-w1` failed to start: no folder")]
    assert ws.tick(clock[0]) == []


def test_ack_for_another_device_is_refused(store, ws, alice_p, dev, bob, boss):
    other = device(store, bob, "box-2")[0]
    launch(ws, alice_p, dev, boss)
    lid = launch_id(store, "proj-a-w1")
    assert ws.ack(lid, True, "", other) is False
    assert ws.ack(lid + 100, True, "", dev) is False
    assert ws.by_name("proj-a-w1")["state"] == "starting"
    assert ws.ack(lid, True, "", dev) is True
    assert ws.offer(dev) == []


def linked(store, ws, alice, alice_p, boss, activity=None):
    dev, token = device(store, alice, "box-1")
    launch(ws, alice_p, dev, boss)
    worker_up(store, ws, dev, token, activity=activity)
    assert ws.link("proj-a-w1", "claude:w1", dev)
    return dev, token


def test_boss_blip_is_not_unattended(store, ws, alice, alice_p, boss, clock):
    dev, token = linked(store, ws, alice, alice_p, boss)
    last = clock[0]
    def at(t):  # the worker's own session keeps beating
        clock[0] = t
        worker_up(store, ws, dev, token)
        ws.tick(t)
        return ws.by_name("proj-a-w1")["state"]
    assert at(last + ONLINE_S + 60) == "up"
    assert at(last + ONLINE_S + UNATTENDED_S + 10) == "unattended"
    assert [w["name"] for w in ws.unattended_for(alice)] == ["proj-a-w1"]
    assert at(clock[0] + ABANDONED_S - 10) == "unattended"
    assert at(clock[0] + 20) == "abandoned"
    assert [w["state"] for w in ws.unattended_for(alice)] == ["abandoned"]


def test_worker_session_gone_closes_it(store, ws, alice, alice_p, boss, clock):
    linked(store, ws, alice, alice_p, boss)
    ws.tick(clock[0] + ONLINE_S + 590)
    assert ws.by_name("proj-a-w1")["state"] != "closed"
    ws.tick(clock[0] + ONLINE_S + 601)
    w = ws.by_name("proj-a-w1")
    assert w["state"] == "closed" and w["error"] == "session gone"


def test_waiting_notice_once_per_half_hour(store, ws, alice, alice_p, boss, clock):
    dev, token = linked(store, ws, alice, alice_p, boss, activity="permission_prompt")
    text = "worker `proj-a-w1` is waiting for a permission; attach with `claude attach proj-a-w1`"
    assert ws.tick(clock[0]) == [("claude:boss", text)]
    assert ws.tick(clock[0]) == []
    clock[0] += 1801
    store.seen("claude:boss", "box-1", "claude-code", "proj-a", principal=alice_p)
    worker_up(store, ws, dev, token, activity="needs_input")
    assert ws.tick(clock[0]) == [("claude:boss", text)]


def test_waiting_notice_not_for_offline_boss(store, ws, alice, alice_p, boss, clock):
    dev, token = linked(store, ws, alice, alice_p, boss)
    clock[0] += ONLINE_S + 1
    worker_up(store, ws, dev, token, activity="permission_prompt")
    assert ws.tick(clock[0]) == []
    store.seen("claude:boss", "box-1", "claude-code", "proj-a", principal=alice_p)
    assert len(ws.tick(clock[0])) == 1


def test_adopt_retargets_held_messages(store, ws, alice, alice_p, boss, clock):
    dev, token = linked(store, ws, alice, alice_p, boss)
    store.send("claude:w1", "box-1/claude-code/proj-a", "one")
    store.send("claude:w1", "box-1/claude-code/proj-a", "two")
    clock[0] += ONLINE_S + UNATTENDED_S + 10
    worker_up(store, ws, dev, token)
    ws.tick(clock[0])
    assert ws.by_name("proj-a-w1")["state"] == "unattended"
    with pytest.raises(KaptyorkaError):
        ws.adopt(alice_p, "proj-a-w1", "claude:nobody")
    store.seen("claude:new", "box-1", "claude-code", "proj-b", principal=alice_p)
    w = ws.adopt(alice_p, "proj-a-w1", "claude:new")
    assert w["state"] == "up" and w["boss"] == "box-1/claude-code/proj-b"
    assert [m.text for m in store.inbox("claude:new")] == ["one", "two"]
    with pytest.raises(KaptyorkaError):  # up again: nothing to adopt
        ws.adopt(alice_p, "proj-a-w1", "claude:new")


def test_close_queues_signed_close_and_notes_worktree(store, ws, alice, alice_p, boss, clock):
    dev, token = device(store, alice, "box-1")
    launch(ws, alice_p, dev, boss, occupied=True)
    ws.ack(launch_id(store, "proj-a-w1"), True, "", dev)
    worker_up(store, ws, dev, token)
    ws.link("proj-a-w1", "claude:w1", dev)
    w = ws.close(alice_p, "proj-a-w1")
    assert w["note"] == f"its worktree {FOLDER} stays in place"
    [item] = ws.offer(dev)
    assert item["kind"] == "close"
    order = {k: v for k, v in item.items() if k != "launch"}
    assert list(order) == ["kind", "id", "issued", "worker", "tool", "key", "sig"]
    assert order["key"] == "claude:w1"
    assert verify(order, secret_hash(store, dev), clock[0], ()) is None
    assert ws.by_name("proj-a-w1")["state"] == "up"  # until the bridge acks
    assert ws.ack(launch_id(store, "proj-a-w1", "close"), True, "", dev) is True
    assert ws.by_name("proj-a-w1")["state"] == "closed"
    with pytest.raises(KaptyorkaError):
        ws.close(alice_p, "proj-a-w1")


def test_close_without_worktree_has_no_note(ws, alice_p, dev, boss):
    launch(ws, alice_p, dev, boss)
    assert "note" not in ws.close(alice_p, "proj-a-w1")


def test_only_own_or_admin_may_adopt_or_close(store, ws, alice_p, dev, bob, boss, clock):
    launch(ws, alice_p, dev, boss)
    bob_p = principal(store, bob)
    with pytest.raises(KaptyorkaError):
        ws.close(bob_p, "proj-a-w1")
    with pytest.raises(KaptyorkaError):
        ws.close(alice_p, "proj-a-w9")
    with store.lock:
        store.db.execute("UPDATE workers SET state='unattended'")
    store.seen("claude:bob", "box-2", "claude-code", "proj-a", principal=bob_p)
    with pytest.raises(KaptyorkaError):
        ws.adopt(bob_p, "proj-a-w1", "claude:bob")
    assert ws.list(bob_p) == []
    admin_p = principal(store, person(store, "admin-1", "admin")[0])
    assert [w["name"] for w in ws.list(admin_p)] == ["proj-a-w1"]
    assert [w["name"] for w in ws.list(alice_p)] == ["proj-a-w1"]
    assert ws.close(admin_p, "proj-a-w1")["state"] == "unattended"


def test_attach_for_other_tools_uses_tmux(ws, alice_p, dev, boss):
    w = ws.create(alice_p, "proj-a", "t", target(dev), "codex", boss, "sid-1", False)
    assert w["attach"] == "ssh u@box-1 -t tmux attach -t proj-a-w1"
    assert w["os_user"] == "u" and w["host"] == "box-1" and w["tool"] == "codex"


def shared_box(store, bob):
    """bob's shared box-2: its device id and bridge token."""
    dev, token = device(store, bob, "box-2")
    store.device_set_shared(dev, True)
    return dev, token


def beat_box2(store, token, key="claude:w1"):
    store.heartbeat("box-2", [{"key": key, "tool": "claude-code", "project": "proj-a",
                               "cwd": FOLDER}], principal=store.authenticate(token),
                    facts={"host": "box-2", "os_user": "u"})


def test_worker_on_shared_box_stays_online_and_with_its_boss(store, ws, alice, bob, alice_p,
                                                             boss, clock):
    dev, token = shared_box(store, bob)
    ws.create(alice_p, "proj-a", "t", target(dev, "box-2"), "claude-code", boss, None, False)
    beat_box2(store, token)
    assert store.agent("claude:w1").person_id == bob  # bob's bridge reported it first
    assert ws.link("proj-a-w1", "claude:w1", dev)
    assert store.agent("claude:w1").person_id == alice
    clock[0] += 20
    beat_box2(store, token)
    assert store.agent("claude:w1").last_seen == clock[0]
    store.seen("claude:w1", "box-2", "claude-code", "proj-a", principal=store.authenticate(token))
    assert store.agent("claude:w1").person_id == alice  # its first tool call keeps the boss
    clock[0] += 590
    beat_box2(store, token)
    ws.tick(clock[0])
    assert ws.by_name("proj-a-w1")["state"] != "closed"


def test_link_before_heartbeat_keeps_boss_after_it(store, ws, alice, bob, alice_p, boss):
    dev, token = shared_box(store, bob)
    ws.create(alice_p, "proj-a", "t", target(dev, "box-2"), "claude-code", boss, None, False)
    assert ws.link("proj-a-w1", "claude:w1", dev)
    a = store.agent("claude:w1")
    assert a.person_id == alice and a.tool == "claude-code" and a.project == "proj-a"
    beat_box2(store, token)
    assert store.agent("claude:w1").person_id == alice


def test_boss_back_online_returns_worker_to_up(store, ws, alice, alice_p, boss, clock):
    dev, token = linked(store, ws, alice, alice_p, boss)
    clock[0] += ONLINE_S + UNATTENDED_S + 10
    worker_up(store, ws, dev, token)
    ws.tick(clock[0])
    assert ws.by_name("proj-a-w1")["state"] == "unattended"
    store.seen("claude:boss", "box-1", "claude-code", "proj-a", principal=alice_p)
    ws.tick(clock[0])
    assert ws.by_name("proj-a-w1")["state"] == "up"
    assert store.db.execute("SELECT unattended_since FROM workers").fetchone()[0] is None
    with store.lock:
        store.db.execute("UPDATE workers SET state='abandoned'")
    ws.tick(clock[0])
    assert ws.by_name("proj-a-w1")["state"] == "abandoned"  # until adopt


def test_failed_notice_survives_a_restart(tmp_path, store, ws, alice_p, dev, boss, clock):
    launch(ws, alice_p, dev, boss)
    clock[0] += ONLINE_S + 1  # boss offline: held
    ws.ack(launch_id(store, "proj-a-w1"), False, "no folder", dev)
    assert ws.tick(clock[0]) == []
    again = Store(tmp_path / "hub.db", clock=lambda: clock[0])  # a restarted hub
    ws2 = WorkerStore(again)
    again.seen("claude:boss", "box-1", "claude-code", "proj-a")
    assert ws2.tick(clock[0]) == [("claude:boss", "worker `proj-a-w1` failed to start: no folder")]
    assert ws2.tick(clock[0]) == []
    again.close()


def test_told_column_added_to_an_old_workers_table(tmp_path, clock):
    import sqlite3
    db = sqlite3.connect(tmp_path / "old.db")
    db.execute("CREATE TABLE workers (name TEXT PRIMARY KEY, state TEXT)")
    db.commit()
    db.close()
    from kaptyorka.workers import ensure_schema
    db = sqlite3.connect(tmp_path / "old.db")
    ensure_schema(db)
    ensure_schema(db)
    assert "told" in [r[1] for r in db.execute("PRAGMA table_info(workers)")]
    db.close()
