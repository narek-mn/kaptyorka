import pytest

from helpers import person
from kaptyorka.orders import verify
from kaptyorka.store import ONLINE_S, KaptyorkaError, Store
from kaptyorka.workers import ABANDONED_S, UNATTENDED_S, WorkerStore, clean_facts, safe_name


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
    got = ws.choose(usable, "proj-a", "claude-code", now, alice)
    assert got["host"] == "box-2" and got["occupied"] is False
    # once box-1's session is gone, its lower load wins
    got = ws.choose(usable, "proj-a", "claude-code", now + ONLINE_S + 1, alice)
    assert got["host"] == "box-1" and got["occupied"] is False
    # every folder busy: the lowest load, marked occupied
    store.heartbeat("box-2", [{"key": "claude:2", "tool": "claude-code", "project": "proj-a",
                               "cwd": "/w/proj-a"}], facts={"host": "box-2", "os_user": "u"})
    got = ws.choose(usable, "proj-a", "claude-code", now, alice)
    assert got["host"] == "box-1" and got["occupied"] is True


def test_choose_prefers_own_account_and_skips_shared_beside_it(ws):
    def c(host, person, load):
        return {"device_id": 1, "host": host, "os_user": f"u{person}", "person_id": person,
                "tmux": True, "folder": "/x", "load": load, "cpus": 1, "tools": {}}
    # bob's shared box-1 is idle, alice's own box-2 is loaded: her own account wins
    got = ws.choose([c("box-1", 2, 0.0), c("box-2", 1, 3.0)], "p", "claude-code", 0, 1)
    assert got["host"] == "box-2"
    # alice has her own account on box-1 too, without the folder: bob's there is never used
    own = {**c("box-1", 1, 0.0), "folder": None}
    assert ws.choose([c("box-1", 2, 0.0), own], "p", "claude-code", 0, 1) is None
    assert ws.choose([c("box-1", 2, 0.0), c("box-3", 3, 1.0)], "p", "claude-code", 0,
                     1)["host"] == "box-1"  # no own account anywhere: the lower load


def test_choose_needs_tmux_for_non_claude(ws):
    c = {"device_id": 1, "host": "box-1", "os_user": "u", "person_id": 1, "tmux": False,
         "folder": "/x", "load": 0, "cpus": 1, "tools": {}}
    assert ws.choose([c], "p", "codex", 0, 1) is None
    assert ws.choose([c], "p", "claude-code", 0, 1)["host"] == "box-1"
    assert ws.choose([{**c, "folder": None}], "p", "claude-code", 0, 1) is None


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
    assert w["attach"] == "ssh -t -- u@box-1 claude attach proj-a-w1" and w["person"] == "alice"
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


def test_worker_names_have_no_dots(store, ws, alice_p, dev, boss):
    store.seen("claude:boss", "box-1", "claude-code", "proj-a", principal=alice_p)
    a = ws.create(alice_p, "proj.a", "t", target(dev), "claude-code", boss, None, False)
    assert a["name"] == "proj-a-w1" and a["project"] == "proj.a"
    b = ws.create(alice_p, "proj-a", "t", target(dev), "claude-code", boss, None, False)
    assert b["name"] == "proj-a-w2"  # proj-a and proj.a share the name space
    assert ws.offer(dev)[0]["worker"] == "proj-a-w1"


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
    got = ws.choose(usable, "proj-a", "claude-code", clock[0], alice)
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
    assert ws.link("proj-a-w1", "claude:w1", dev) is True  # a retried link: same key
    assert ws.link("proj-a-w1", "claude:other", dev) is False  # up with another key
    assert ws.state("proj-a-w1", dev) == "up"


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


def bridge_beat(store, token, t, clock):
    """The bridge reports at time t, without the worker's session."""
    clock[0] = t
    store.authenticate(token)


def test_worker_session_gone_closes_it_and_back_reopens(store, ws, alice, alice_p, boss,
                                                        clock):
    dev, token = linked(store, ws, alice, alice_p, boss)
    start = clock[0]
    bridge_beat(store, token, start + ONLINE_S + 590, clock)
    ws.tick(clock[0])
    assert ws.by_name("proj-a-w1")["state"] != "closed"
    bridge_beat(store, token, start + ONLINE_S + 601, clock)
    ws.tick(clock[0])
    w = ws.by_name("proj-a-w1")
    assert w["state"] == "closed" and w["error"] == "session gone"
    worker_up(store, ws, dev, token)  # the session is reported again
    assert ws.tick(clock[0]) == [("claude:boss", "worker `proj-a-w1` is back")]
    w = ws.by_name("proj-a-w1")
    assert w["state"] == "up" and w["error"] == ""
    assert ws.tick(clock[0]) == []


def test_bridge_offline_is_not_session_gone(store, ws, alice, alice_p, boss, clock):
    linked(store, ws, alice, alice_p, boss)
    ws.tick(clock[0] + 3600)  # the bridge is silent: nobody can say the session is gone
    assert ws.by_name("proj-a-w1")["state"] in ("up", "unattended")


def test_hub_down_is_not_session_gone(store, ws, alice, alice_p, boss, clock):
    dev, token = linked(store, ws, alice, alice_p, boss)
    clock[0] += 900  # the hub was down 15 minutes and starts again
    again = WorkerStore(store)
    store.authenticate(token)  # the bridge's first beat, before it lists sessions
    again.tick(clock[0])
    assert again.by_name("proj-a-w1")["state"] != "closed"
    worker_up(store, again, dev, token)
    bridge_beat(store, token, clock[0] + 119, clock)
    again.tick(clock[0])
    assert again.by_name("proj-a-w1")["state"] != "closed"


def test_unattended_without_since_does_not_stop_the_tick(store, ws, alice, alice_p, boss,
                                                         clock):
    dev, token = linked(store, ws, alice, alice_p, boss)
    store.db.execute("UPDATE workers SET state='unattended', unattended_since=NULL")
    later = clock[0] + ONLINE_S + 10  # the boss is offline: the worker stays unattended
    assert any("is unattended" in x["text"] for x in ws.warnings(later, "0.0.1"))
    ws.tick(later)
    clock[0] = later + ABANDONED_S + 1
    worker_up(store, ws, dev, token)  # its bridge and session still report
    ws.tick(clock[0])
    assert ws.by_name("proj-a-w1")["state"] == "abandoned"


def test_waiting_notice_once_per_half_hour(store, ws, alice, alice_p, boss, clock):
    dev, token = linked(store, ws, alice, alice_p, boss, activity="permission_prompt")
    text = "worker `proj-a-w1` is waiting for a permission; attach with `ssh -t -- u@box-1 claude attach proj-a-w1`"
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
    assert w["attach"] == "ssh -t -- u@box-1 tmux attach -t =proj-a-w1"
    assert w["os_user"] == "u" and w["host"] == "box-1" and w["tool"] == "codex"


def test_attach_line_quotes_bridge_reported_values(store, ws, alice_p, dev, boss):
    with store.lock:  # a bridge may report anything as its user
        store.db.execute("UPDATE devices SET os_user='u; rm -rf ~' WHERE id=?", (dev,))
    w = launch(ws, alice_p, dev, boss)
    assert w["attach"] == "ssh -t -- 'u; rm -rf ~@box-1' claude attach proj-a-w1"


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


def test_shared_bridge_may_not_take_another_persons_key(store, ws, alice, bob):
    own, own_token = device(store, alice, "box-1")
    store.heartbeat("box-1", [{"key": "cursor:box-1:/home/alice/p", "tool": "cursor",
                               "project": "p"}], principal=store.authenticate(own_token),
                    facts={"host": "box-1", "os_user": "u"})
    dev, token = shared_box(store, bob)
    bob_bridge = store.authenticate(token)
    store.heartbeat("box-2", [{"key": "cursor:box-1:/home/alice/p", "tool": "cursor",
                               "project": "p"}], principal=bob_bridge,
                    facts={"host": "box-2", "os_user": "u"})
    row = store.db.execute("SELECT device_id, person_id, host FROM agents WHERE key=?",
                           ("cursor:box-1:/home/alice/p",)).fetchone()
    assert tuple(row) == (own, alice, "box-1")
    with pytest.raises(KaptyorkaError):
        store.seen("cursor:box-1:/home/alice/p", "box-2", "cursor", "p", principal=bob_bridge)


def test_link_refuses_a_key_it_may_not_take(store, ws, alice, bob, alice_p, boss):
    own, own_token = device(store, alice, "box-1")
    store.heartbeat("box-1", [{"key": "claude:alice2", "tool": "claude-code",
                               "project": "proj-a"}], principal=store.authenticate(own_token),
                    facts={"host": "box-1", "os_user": "u"})
    bob_p = principal(store, bob)
    store.seen("claude:bob", "box-3", "claude-code", "proj-a", principal=bob_p)
    bob_dev = device(store, bob, "box-3")[0]
    ws.create(bob_p, "proj-a", "t", target(bob_dev, "box-3"), "claude-code", "claude:bob", None,
              False)
    assert ws.link("proj-a-w1", "claude:alice2", bob_dev) is False  # alice's session
    assert store.agent("claude:alice2").person_id == alice
    # a key another live worker already has
    ws.create(alice_p, "proj-a", "t", target(own), "claude-code", boss, None, False)
    ws.create(alice_p, "proj-a", "t", target(own), "claude-code", boss, None, False)
    assert ws.link("proj-a-w2", "claude:w9", own) is True
    assert ws.link("proj-a-w3", "claude:w9", own) is False
    assert ws.by_name("proj-a-w3")["state"] == "starting"


def test_web_boss_adopts_and_is_never_unattended(store, ws, alice, alice_p, boss, clock):
    dev, token = linked(store, ws, alice, alice_p, boss, activity="permission_prompt")
    with store.lock:
        store.db.execute("UPDATE workers SET state='unattended'")
    with pytest.raises(KaptyorkaError):
        ws.adopt(alice_p, "proj-a-w1", "web:bob")
    assert ws.adopt(alice_p, "proj-a-w1", "web:alice")["state"] == "up"
    clock[0] += ONLINE_S + UNATTENDED_S + 10
    worker_up(store, ws, dev, token, activity="permission_prompt")
    notices = ws.tick(clock[0])
    assert ws.by_name("proj-a-w1")["state"] == "up"
    assert [n[0] for n in notices] == ["web:alice"]


def test_close_before_start_cancels_the_launch(store, ws, alice_p, dev, boss):
    launch(ws, alice_p, dev, boss)
    w = ws.close(alice_p, "proj-a-w1")
    assert w["state"] == "closed"
    assert ws.offer(dev) == []
    row = store.db.execute("SELECT done_at, ok, error FROM launches WHERE kind='launch'").fetchone()
    assert row["done_at"] is not None and row["ok"] == 0 and row["error"] == "closed before start"
    assert store.db.execute("SELECT COUNT(*) FROM launches WHERE kind='close'").fetchone()[0] == 0


def test_hosting_bridge_delivers_to_a_worker_on_its_shared_box(store, ws, alice, bob, alice_p,
                                                               boss):
    dev, token = shared_box(store, bob)
    bob_bridge = store.authenticate(token)
    ws.create(alice_p, "proj-a", "t", target(dev, "box-2"), "claude-code", boss, None, False)
    beat_box2(store, token)
    assert ws.link("proj-a-w1", "claude:w1", dev)
    store.send("claude:boss", store.agent("claude:w1").name, "hi")
    items = store.offer("box-2", "0.3", principal=bob_bridge)
    assert [(i["key"], i["text"].endswith("hi")) for i in items] == [("claude:w1", True)]
    # alice's own session elsewhere is still not offered to bob's bridge
    own, own_token = device(store, alice, "box-1")
    store.heartbeat("box-1", [{"key": "claude:pc", "tool": "claude-code", "project": "proj-a"}],
                    principal=store.authenticate(own_token), facts={"host": "box-1", "os_user": "u"})
    store.send("claude:boss", store.agent("claude:pc").name, "private")
    assert store.offer("box-1", "0.3", principal=bob_bridge) == []
    # bob's bridge reporting the box empty marks the worker's session gone
    store.heartbeat("box-2", [], tools=["claude-code"], principal=bob_bridge,
                    facts={"host": "box-2", "os_user": "u"})
    assert store.agent("claude:w1").online is False


def _worker_row(store, name, device_id, host, state, created, **extra):
    cols = {"name": name, "project": "proj-a", "task": "t", "boss_key": "web:1", "boss_person": 1,
            "device_id": device_id, "host": host, "tool": "claude-code", "state": state,
            "created": created, **extra}
    store.db.execute(f"INSERT INTO workers ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                     list(cols.values()))


def test_warnings_texts_and_order(store, ws, alice, bob, clock):
    import time
    a = device(store, alice, "box-1", "alice")[0]
    b = device(store, bob, "box-2", "bob")[0]
    c = device(store, bob, "box-3", "bob")[0]
    store.device_set_shared(b, True)
    ws.facts_seen(a, {"version": "0.2.9", "tmux": True, "hooks_ok": {"claude-code": False}}, {})
    ws.facts_seen(b, {"version": "0.3.0", "tmux": False, "hooks_ok": {"claude-code": True}}, {})
    ws.facts_seen(c, {"version": "0.3.0", "tmux": True}, {})
    store.db.execute("UPDATE devices SET last_used=? WHERE id=?", (clock[0] - 125 * 60, c))
    since = clock[0] - 300
    _worker_row(store, "w1", a, "box-1", "unattended", clock[0], unattended_since=since)
    _worker_row(store, "w2", a, "box-1", "abandoned", clock[0])
    _worker_row(store, "w3", b, "box-2", "failed", clock[0] - 3600, error="no tmux")
    _worker_row(store, "w4", b, "box-2", "failed", clock[0] - 90000, error="old")
    got = ws.warnings(clock[0], "0.3.0")
    hhmm = time.strftime("%H:%M", time.localtime(since))
    assert [w["text"] for w in got] == [
        "worker w3 on box-2 failed to start: no tmux",
        "bridge on box-3 (bob) has been offline for 125 minutes",
        "box-1 (alice) runs kaptyorka 0.2.9; the hub runs 0.3.0",
        "claude-code on box-1 (alice) is set up but its kaptyorka hooks are missing",
        f"worker w1 on box-1 is unattended since {hhmm}",
        "worker w2 on box-1 was abandoned",
        "box-2 (bob) has no tmux: only Claude Code workers can run there",
    ]
    assert got[0]["person"] == "alice" and got[1]["person"] == "bob"
    assert got[1]["host"] == "box-3" and got[1]["os_user"] == "bob"


def test_warnings_none_when_healthy_and_skip_revoked_legacy(store, ws, alice, clock):
    a = device(store, alice, "box-1")[0]
    ws.facts_seen(a, {"version": "0.3.0", "tmux": True, "hooks_ok": {"claude-code": True}}, {})
    assert ws.warnings(clock[0], "0.3.0") == []
    ws.facts_seen(a, {"version": "0.1", "tmux": False, "hooks_ok": {"claude-code": False}}, {})
    store.db.execute("UPDATE devices SET revoked_at=? WHERE id=?", (clock[0], a))
    assert ws.warnings(clock[0], "0.3.0") == []
    store.db.execute("UPDATE devices SET revoked_at=NULL, legacy=1 WHERE id=?", (a,))
    assert ws.warnings(clock[0], "0.3.0") == []
    ws.facts_seen(a, {"tmux": True}, {})  # no version: not warned
    store.db.execute("UPDATE devices SET legacy=0 WHERE id=?", (a,))
    assert ws.warnings(clock[0], "0.3.0") == []


def test_warnings_old_version_from_device_row_without_facts(store, ws, alice, clock):
    a = device(store, alice, "box-1", "alice")[0]
    store.db.execute("UPDATE devices SET version='0.2.1' WHERE id=?", (a,))
    assert [w["text"] for w in ws.warnings(clock[0], "0.3.0")] == [
        "box-1 (alice) runs kaptyorka 0.2.1; the hub runs 0.3.0"]


def test_bridge_gone_an_hour_closes_its_workers(store, ws, alice, alice_p, boss, clock):
    dev, token = linked(store, ws, alice, alice_p, boss)
    ws.tick(clock[0] + 3500)
    assert ws.by_name("proj-a-w1")["state"] != "closed"
    notices = ws.tick(clock[0] + 3601)
    w = ws.by_name("proj-a-w1")
    assert w["state"] == "closed" and w["error"] == "bridge offline for over an hour"
    assert notices[0] == ("claude:boss",
                          "worker `proj-a-w1` is closed: bridge offline for over an hour")
    assert store.db.execute("SELECT COUNT(*) FROM launches WHERE worker='proj-a-w1'"
                            " AND done_at IS NULL").fetchone()[0] == 0
    assert not any("proj-a-w1" in t for _, t in ws.tick(clock[0] + 3700))  # told once


def test_bridge_gone_waits_for_the_hub_to_settle(store, ws, alice, alice_p, boss, clock):
    linked(store, ws, alice, alice_p, boss)
    clock[0] += 7200  # the hub itself was down two hours
    again = WorkerStore(store)
    again.tick(clock[0] + 60)
    assert again.by_name("proj-a-w1")["state"] != "closed"


def test_revoked_device_closes_its_workers(store, ws, alice, alice_p, boss, clock):
    dev, _ = linked(store, ws, alice, alice_p, boss)
    store.device_revoke(dev)
    ws.tick(clock[0])
    w = ws.by_name("proj-a-w1")
    assert w["state"] == "closed" and w["error"] == "device revoked"


def test_close_with_bridge_offline_is_done_at_once(store, ws, alice, alice_p, boss, clock):
    dev, _ = linked(store, ws, alice, alice_p, boss)
    clock[0] += ONLINE_S + 1
    w = ws.close(alice_p, "proj-a-w1")
    assert w["state"] == "closed"
    assert w["note"] == "the bridge is offline; the session may still run on box-1"
    assert ws.offer(dev) == []  # no close order nobody would ack


def test_facts_drop_names_that_read_as_options():
    assert clean_facts({"os_user": "-oProxyCommand=x"})["os_user"] == ""
    assert clean_facts({"os_user": "u.name_1"})["os_user"] == "u.name_1"
    assert safe_name("-box") == "" and safe_name("box-1") == "box-1" and safe_name(3) == ""


def test_clean_facts_keeps_harness_known_tools_only():
    ok = {"rules": True, "hooks": False, "skills": None}
    got = clean_facts({"harness": {"codex": ok, "vim": ok, "cursor": {"rules": 1, "hooks": True},
                                   "opencode": {"rules": True, "hooks": True, "skills": True}}})
    assert got["harness"] == {"codex": ok, "opencode": {"rules": True, "hooks": True, "skills": True}}
    assert "harness" not in clean_facts({"harness": "x", "load": 1})



def test_clean_facts_logins():
    got = clean_facts({"logins": {"claude-code": False, "codex": None, "opencode": "yes",
                                  "cursor": True, "vim": True}})
    assert got["logins"] == {"claude-code": False, "codex": None, "cursor": True}
    assert "logins" not in clean_facts({"logins": "x"})


def test_warning_for_a_tool_not_logged_in(store, ws, alice, clock):
    a = device(store, alice, "box-1", "alice")[0]
    ws.facts_seen(a, {"version": "0.3.0", "tmux": True,
                      "logins": {"claude-code": False, "codex": None, "opencode": True}}, {})
    got = ws.warnings(clock[0], "0.3.0")
    assert [w["text"] for w in got] == [
        "box-1 (alice): Claude Code is not logged in. Run `claude auth login` there."]
    assert got[0]["host"] == "box-1" and got[0]["person"] == "alice"


# the manager worker


def manager_setup(store):
    """An admin with a bridge on box-1 and the manager person; (admin id, manager id, row)."""
    admin = person(store, "root", "admin")[0]
    dev_id = device(store, admin, "box-1")[0]
    mid = store.person_create("manager", "manager", "test")
    with store.lock:
        row = dict(store.db.execute("SELECT id, host, os_user FROM devices WHERE id=?",
                                    (dev_id,)).fetchone())
    return admin, mid, row


def test_create_manager_order_has_env_and_folder(store, ws, clock):
    admin, mid, row = manager_setup(store)
    w = ws.create_manager(admin, mid, row, "/h/.kaptyorka/manager", "hello manager")
    assert w["name"] == "manager" and w["project"] == "manager" and w["tool"] == "claude-code"
    assert w["state"] == "starting" and w["folder"] == "/h/.kaptyorka/manager"
    assert w["worktree"] is False and w["person"] == "manager"
    with store.lock:
        r = store.db.execute("SELECT boss_key, boss_person, manager FROM workers"
                             " WHERE name='manager'").fetchone()
    assert (r["boss_key"], r["boss_person"], r["manager"]) == ("web:root", mid, 1)
    [item] = ws.offer(row["id"])
    order = {k: v for k, v in item.items() if k != "launch"}
    assert order["folder"] == "/h/.kaptyorka/manager" and order["manager"] is True
    assert "env" not in order
    assert order["person"] == "root" and order["first_message"] == "hello manager"
    assert verify(order, secret_hash(store, row["id"]), clock[0], ()) is None
    with pytest.raises(KaptyorkaError, match="already running"):
        ws.create_manager(admin, mid, row, "/x", "again")
    # an ended manager is replaced, its old order no longer offered
    with store.lock:
        store.db.execute("UPDATE workers SET state='closed' WHERE name='manager'")
    clock[0] += 61
    assert ws.create_manager(admin, mid, row, "/x", "again")["folder"] == "/x"
    assert [i["first_message"] for i in ws.offer(row["id"])] == ["again"]


def test_manager_never_unattended(store, ws, clock):
    admin, mid, row = manager_setup(store)
    ws.create_manager(admin, mid, row, "/m", "hi")
    assert ws.link("manager", "claude:m", row["id"])
    assert store.agent("claude:m").person_id == mid
    with store.lock:  # the page boss is an admin who left long ago: still not unattended
        store.db.execute("UPDATE workers SET boss_key='k:gone' WHERE name='manager'")
    for _ in range(4):
        clock[0] += ABANDONED_S // 2
        store.seen("claude:m", "manager@box-1", "claude-code", "manager")
        with store.lock:
            store.db.execute("UPDATE devices SET last_used=? WHERE id=?", (clock[0], row["id"]))
        ws.tick(clock[0])
        assert ws.by_name("manager")["state"] == "up"
    with store.lock:  # even a row left unattended by an older hub goes back to up
        store.db.execute("UPDATE workers SET state='unattended', unattended_since=0")
    ws.tick(clock[0])
    assert ws.by_name("manager")["state"] == "up"
    with store.lock:
        store.db.execute("UPDATE workers SET state='unattended', unattended_since=0")
    assert not [x for x in ws.warnings(clock[0], "0.0.1") if "manager" in x["text"]]


def test_manager_column_added_to_an_old_workers_table(tmp_path):
    import sqlite3
    from kaptyorka.workers import ensure_schema
    db = sqlite3.connect(tmp_path / "old.db")
    db.execute("CREATE TABLE workers (name TEXT PRIMARY KEY, state TEXT)")
    ensure_schema(db)
    ensure_schema(db)
    assert "manager" in [r[1] for r in db.execute("PRAGMA table_info(workers)")]
    db.close()


def manager_token(store, mid):
    token = store.device_add(mid, "manager", "test")
    with store.lock:
        store.db.execute("UPDATE devices SET host='box-1' WHERE person_id=?", (mid,))
    return store.authenticate(token)


def test_manager_links_after_its_own_call_first_launch_and_relaunch(store, ws, clock):
    admin, mid, row = manager_setup(store)
    mp = manager_token(store, mid)
    ws.create_manager(admin, mid, row, "/m", "hi")
    store.seen("claude:m1", "manager@box-1", "claude-code", "manager", principal=mp)
    assert ws.link("manager", "claude:m1", row["id"])  # its hook came before the bridge
    a = store.agent("claude:m1")
    assert ws.by_name("manager")["state"] == "up"
    assert (a.person_id, a.device_id) == (mid, mp.device_id)  # keeps its own device
    with store.lock:  # session gone; the hub starts a new one with a new key
        store.db.execute("UPDATE workers SET state='closed', error='session gone'")
    ws.create_manager(admin, mid, row, "/m", "hi")
    store.seen("claude:m2", "manager@box-1", "claude-code", "manager", principal=mp)
    assert ws.link("manager", "claude:m2", row["id"])
    assert ws.by_name("manager")["key"] == "claude:m2"
    # another person's session is still refused, manager or not
    with store.lock:
        store.db.execute("UPDATE workers SET state='closed'")
    ws.create_manager(admin, mid, row, "/m", "hi")
    bob = person(store, "bob")[0]
    bp = principal(store, bob)
    store.seen("claude:b", "box-1", "claude-code", "proj-a", principal=bp)
    assert not ws.link("manager", "claude:b", row["id"])


def test_clean_facts_git_age():
    got = clean_facts({"git": {"/p": {"branch": "main", "subject": "x", "age": 12.5}}})
    assert got["git"]["/p"]["age"] == 12.5
    for bad in (-1, "soon", True, float("nan")):
        assert "age" not in clean_facts({"git": {"/p": {"branch": "m", "age": bad}}})["git"]["/p"]
    assert clean_facts({"git": {"/p": {"age": 10 ** 9}}})["git"]["/p"]["age"] == 86400
    assert clean_facts({"git": {"/p": {"read_at": 5.0}}})["git"]["/p"]["read_at"] == 5.0
