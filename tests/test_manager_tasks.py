"""The manager's task list (saved, carried into a new run, reminded), bundled routine
notices, people before routine digests, and the manager's load line. A fake clock."""
import kaptyorka
from helpers import call, run
from test_hub_manager import (HOST, admin_bridge, ack, clock, device, hub, inbox,  # noqa: F401
                              insert_worker, manager_online, orders, plain)
from test_hub_manager import ident as mident
from test_hub_manager import old_beat
from test_hub_workers import member
from kaptyorka import manager
from kaptyorka.hub import MANAGER_FIRST, old_versions_text

TASKS = "- alice: waits for the GPU answer from bob\n- relay to bob: the log path"


def knowledge(texts):
    return [t for t in texts if t.startswith("knowledge")]


# the task list

def test_task_list_takes_any_text_and_reads_back(hub):
    mgr, me = manager_online(hub)
    assert run(call(mgr, me, "state_write", project="manager", text=TASKS)) \
        == "state of manager updated"
    assert run(call(mgr, me, "state", project="manager")) == TASKS
    assert hub.mm.tasks()[0] == TASKS


def test_the_rule_is_at_the_top_and_compaction_keeps_promises():
    md = manager.claude_md("")
    top = md.split("## What the hub sends you")[0]
    assert 'state_write("manager", text)' in top and "save your task list now" in top
    assert md.count("five headings") == 0
    assert "# Compact instructions" in md and "open promise" in md


def test_launch_first_message_carries_the_saved_task_list(hub, clock):
    b = admin_bridge(hub)
    hub.mm.set_enabled(True, "test")
    hub.mm.set_enabled(True, "test")
    with hub.store.lock:
        hub.store._note("state", TASKS, "manager", "manager")
    hub.tick()
    [o] = orders(hub, b)
    assert o["first_message"] == (MANAGER_FIRST + "\n\nYour task list as the last run saved it:"
                                  "\n\n" + TASKS)
    assert manager.first_message("x", None) == "x"
    long = manager.first_message("x", "y" * (manager.TASKS_FIRST_MAX + 10))
    assert long.endswith('[cut; read the rest with state("manager")]')


def ask(hub, bob, B, text):
    run(call(bob, B, "send", to=hub.store.agent("k:mgr").name, text=text))


def test_task_list_reminder_rides_on_the_next_notice_after_15_messages(hub, clock):
    mgr, me = manager_online(hub)
    run(call(mgr, me, "state_write", project="manager", text=TASKS))
    clock[0] += 1
    _, _, bob = plain(hub, "bob")
    B = mident("k:bob", project="proj-b", machine="pc-2")
    for i in range(14):
        ask(hub, bob, B, f"q{i}")
    tell = hub.tell
    tell("k:mgr", "first notice")
    assert [t for t in inbox(mgr, me) if t.startswith("first")] == ["first notice"]
    ask(hub, bob, B, "q14")
    tell("k:mgr", "second notice")
    tell("k:mgr", "third notice")
    got = [t for t in inbox(mgr, me) if "notice" in t]
    assert got == ["second notice\n\n" + manager.TASKS_LINE, "third notice"]  # once, then counts again
    tell("k:bob", "not the manager")
    assert all(manager.TASKS_LINE not in m["text"] for m in run(call(bob, B, "inbox")))


def test_task_list_reminder_after_two_hours(hub, clock):
    mgr, me = manager_online(hub)
    run(call(mgr, me, "state_write", project="manager", text=TASKS))
    tell = hub.tell
    clock[0] += 7199
    tell("k:mgr", "a")
    clock[0] += 2
    tell("k:mgr", "b")
    assert [t for t in inbox(mgr, me) if t in ("a", "b") or t.startswith("b\n")] \
        == ["a", "b\n\n" + manager.TASKS_LINE]


# bundled knowledge notices

def note_setup(hub):
    hub.store.project_register("proj-a", by="test")
    alice, _, _ = plain(hub, "alice")
    hub.store.grant_project("proj-a", alice, "write", "test")
    hub.store.seen("k:alice", "box-1", "claude-code", "proj-a",
                   principal=hub.store._person_principal(alice))
    return alice


def test_knowledge_waits_five_minutes_bundles_and_dedupes(hub, clock):
    mgr, me = manager_online(hub)
    hub.tick()
    alice = note_setup(hub)
    clock[0] += 1
    hub.store.notes_write("k:alice", "state", "v1", "proj-a")
    hub.tick()
    assert knowledge(inbox(mgr, me)) == []
    clock[0] += 200
    hub.store.notes_write("k:alice", "state", "v2", "proj-a")  # the same note again
    hub.store.notes_write("k:alice", "api", "x", "proj-a")
    insert_worker(hub, "w-ok", "closed", alice, clock[0])
    insert_worker(hub, "w-bad", "failed", alice, clock[0], "no tmux")  # needs nothing: left out
    with hub.store.lock:
        hub.store.db.execute("UPDATE workers SET closed_at=?", (clock[0],))
    run(call(mgr, me, "inbox"))
    hub.tick()
    assert knowledge(inbox(mgr, me)) == []  # the first change is not 5 minutes old yet
    clock[0] += 100
    run(call(mgr, me, "inbox"))
    hub.tick()
    assert knowledge(inbox(mgr, me)) == [
        "knowledge: proj-a/state written by alice; proj-a/api written by alice; worker `w-ok`"
        " in proj-a finished. If it changes what the team knows, update the team notes (read"
        " first, merge)."]


def test_knowledge_skips_own_and_unchanged_writes(hub, clock):
    mgr, me = manager_online(hub)
    hub.tick()
    note_setup(hub)
    hub.store.notes_write("k:alice", "state", "same", "proj-a")
    clock[0] += 601
    run(call(mgr, me, "inbox"))
    hub.tick()
    inbox(mgr, me)
    clock[0] += 1
    hub.store.notes_write("k:alice", "state", "same", "proj-a")  # nothing changed
    run(call(mgr, me, "notes_write", topic="api", text="mine", project="proj-a"))
    run(call(mgr, me, "state_write", project="manager", text=TASKS))
    clock[0] += 601
    run(call(mgr, me, "inbox"))
    hub.tick()
    assert knowledge(inbox(mgr, me)) == []


def test_old_versions_come_as_one_message(hub, clock):
    mgr, me = manager_online(hub)
    ds = []
    for name in ("bob", "carol", "dave"):
        pid, _ = member(hub, name, role=None)
        ds.append(device(hub, pid, name))
    clock[0] += 1801
    for d in ds:
        old_beat(hub, d, update_failed="auto-update failed")
    run(call(mgr, me, "inbox"))
    hub.tick()
    v = kaptyorka.__version__
    got = [t for t in inbox(mgr, me) if "old kaptyorka" in t]
    assert got == [
        f"3 machines run an old kaptyorka, the hub runs {v}: bob@{HOST} runs 0.0.1"
        f" (auto-update failed); carol@{HOST} runs 0.0.1 (auto-update failed); dave@{HOST} runs"
        " 0.0.1 (auto-update failed). Send bob one short message (their online session, else"
        f" their page web:bob) naming bob@{HOST}, the reason and `kaptyorka update` to run there;"
        f" send carol one short message (their online session, else their page web:carol) naming"
        f" carol@{HOST}, the reason and `kaptyorka update` to run there; send dave one short"
        f" message (their online session, else their page web:dave) naming dave@{HOST}, the"
        " reason and `kaptyorka update` to run there. Once; never repeat it."]
    clock[0] += 60
    run(call(mgr, me, "inbox"))
    hub.tick()
    assert [t for t in inbox(mgr, me) if "old kaptyorka" in t] == []


def test_old_versions_text_for_team_machines():
    rows = [{"os_user": u, "host": "box-1", "version": "0.0.1", "reason": "r", "team": True,
             "person": u} for u in ("a", "b")]
    assert old_versions_text(rows, ["root"]).endswith(
        "Tell each admin (root) in one line to run `kaptyorka update` on the team machines"
        " (a@box-1, b@box-1). Once; never repeat it.")


# people first

def test_routine_digest_waits_for_a_person_then_goes_at_the_cap(hub, clock):
    mgr, me = manager_online(hub)
    hub.tick()
    note_setup(hub)
    _, _, bob = plain(hub, "bob")
    B = mident("k:bob", project="proj-b", machine="pc-2")
    clock[0] += 1
    hub.store.notes_write("k:alice", "state", "v1", "proj-a")
    clock[0] += 300
    ask(hub, bob, B, "are you there?")
    run(call(mgr, me, "inbox"))
    hub.tick()
    assert knowledge(inbox(mgr, me)) == []  # bob waits for an answer: the digest is held
    # an urgent notice is never held
    hub.tell("k:mgr", "worker `w` of bob is abandoned; tell the admin")
    assert any("abandoned" in t for t in inbox(mgr, me))
    clock[0] += 100
    ask(hub, bob, B, "hello?")  # a new message keeps it waiting, up to the cap
    run(call(mgr, me, "inbox"))
    hub.tick()
    assert knowledge(inbox(mgr, me)) == []
    clock[0] += 201  # held 5 minutes: it goes anyway
    ask(hub, bob, B, "still there?")
    run(call(mgr, me, "inbox"))
    hub.tick()
    assert len(knowledge(inbox(mgr, me))) == 1


def test_routine_digest_goes_once_the_person_is_answered(hub, clock):
    mgr, me = manager_online(hub)
    hub.tick()
    note_setup(hub)
    _, _, bob = plain(hub, "bob")
    B = mident("k:bob", project="proj-b", machine="pc-2")
    clock[0] += 1
    hub.store.notes_write("k:alice", "state", "v1", "proj-a")
    clock[0] += 300
    ask(hub, bob, B, "are you there?")
    run(call(mgr, me, "inbox"))
    hub.tick()
    assert knowledge(inbox(mgr, me)) == []
    run(call(mgr, me, "send", to="pc-2/codex/proj-b", text="yes"))
    hub.tick()
    assert len(knowledge(inbox(mgr, me))) == 1


# the load line

def test_load_line(hub, clock):
    mgr, me = manager_online(hub)
    _, _, bob = plain(hub, "bob")
    B = mident("k:bob", project="proj-b", machine="pc-2")
    now = clock[0]
    assert manager.load_line(hub.mm.load(now), now) == (
        "0 messages in the last hour, no replies, 0 waiting; task list saved never")
    ask(hub, bob, B, "can you start proj-b on box-1?")
    clock[0] += 40
    run(call(mgr, me, "send", to="pc-2/codex/proj-b", text="done"))
    clock[0] += 10
    ask(hub, bob, B, "thanks!")  # no question: it never waits
    ask(hub, bob, B, "and is box-2 free tomorrow?")
    ask(hub, bob, B, "it is for the long training run")  # the same sender waits once
    run(call(mgr, me, "state_write", project="manager", text=TASKS))
    clock[0] += 200
    now = clock[0]
    assert manager.load_line(hub.mm.load(now), now) == (
        "4 messages in the last hour, median reply 40 s, 1 waiting; task list saved 3 min ago")
    hub.tell("k:bob", "the manager started `proj-b-w1` on box-2")  # the hub answered for it
    assert hub.mm.load(clock[0])["waiting"] == 0


def test_load_line_reaches_the_admin_only(hub):
    from helpers import person
    from kaptyorka import http
    manager_online(hub)
    _, admin = person(hub.store, "root", "admin")
    _, alice = person(hub.store, "alice")
    want = "0 messages in the last hour, no replies, 0 waiting; task list saved never"
    assert http.request("GET", hub.url + "/api/settings", admin)[1]["manager_load"] == want
    assert http.request("GET", hub.url + "/api/feed", admin)[1]["manager_load"] == want
    assert "manager_load" not in http.request("GET", hub.url + "/api/feed", alice)[1]
