"""What the first ticks after an update must not do (found by a dry run on a copy of a real
hub's data): ask people to update machines the staged rollout updates by itself, remind the
admin of door requests older than the door_request tool."""
from helpers import call, run
from test_hub_manager import clock, device, hub, inbox, manager_online, old_beat  # noqa: F401
from test_hub_workers import bridge, heartbeat, member

import kaptyorka
from kaptyorka import rollout
from kaptyorka.workers import HUB_KEY


def old_texts(mgr, me):
    return [t for t in inbox(mgr, me) if "old kaptyorka" in t]


def test_no_update_request_while_the_rollout_holds_the_machine_back(hub, clock):
    mgr, me = manager_online(hub)
    bob_id, _ = member(hub, "bob", role=None)
    d = device(hub, bob_id, "bob")
    old_beat(hub, d)  # behind the hub, its auto-update has not run yet
    st = rollout.state(hub.store, kaptyorka.__version__, clock[0])
    st.update(state="soak", first=[], decided_at=None)
    rollout._put(hub.store, st)
    clock[0] += 1801
    old_beat(hub, d)
    run(call(mgr, me, "inbox"))
    hub.tick()
    assert old_texts(mgr, me) == []  # the rollout updates it by itself
    st.update(state="passed", decided_at=clock[0])
    rollout._put(hub.store, st)
    clock[0] += 1801  # half an hour after the rollout let it go, still behind: now ask
    old_beat(hub, d)
    run(call(mgr, me, "inbox"))
    hub.tick()
    assert len(old_texts(mgr, me)) == 1


def test_a_failed_update_is_told_even_while_the_rollout_holds(hub, clock):
    mgr, me = manager_online(hub)
    bob_id, _ = member(hub, "bob", role=None)
    d = device(hub, bob_id, "bob")
    old_beat(hub, d, update_failed="auto-update failed")
    st = rollout.state(hub.store, kaptyorka.__version__, clock[0])
    st.update(state="soak", first=[], decided_at=None)
    rollout._put(hub.store, st)
    clock[0] += 1801
    old_beat(hub, d, update_failed="auto-update failed")
    run(call(mgr, me, "inbox"))
    hub.tick()
    assert len(old_texts(mgr, me)) == 1


def test_a_door_request_from_before_0_9_11_never_reminds(hub, clock):
    s = hub.store
    s.project_register("proj-a", by="test")
    a_id, _ = member(hub, "alice")
    b_id, _ = member(hub, "bob")
    s.seen("k:a", "box-1", "codex", "proj-a", principal=s._person_principal(a_id))
    s.seen("k:b", "box-2", "codex", "proj-a", principal=s._person_principal(b_id))
    with s.lock:
        s.db.execute("INSERT INTO connects (person_id, from_key, to_key, to_person, why, created)"
                     " VALUES (?,?,?,?,?,?)", (a_id, "k:a", "k:b", b_id, "old", clock[0] - 90 * 86400))
    hub.tick()
    with s.lock:
        texts = [r[0] for r in s.db.execute("SELECT text FROM messages")]
    assert not [t for t in texts if t.startswith("Still waiting")]
