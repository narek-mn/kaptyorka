"""The manager and the workers it approves: it may close them, it sees them in `workers`, its
request notices list the live workers from the database, and it may turn its own "wait" into
a start or a no. A fake clock."""
from helpers import call, call_error, run
from test_hub_manager import (HOST, busy_setup, clock, hub, inbox, insert_worker,  # noqa: F401
                              manager_online)
from test_hub_manager import ask as ask_work
from test_hub_workers import BOSS, poll

from kaptyorka import http, manager


def started(hub):
    """bob asked for proj-a on busy box-1 and the manager started proj-a-w1 for him."""
    mgr, me = manager_online(hub)
    b, bob_id, bob = busy_setup(hub)
    ask_work(hub, bob)
    run(call(mgr, me, "work_decide", request_id=1, machine="box-1", reason="alice agreed"))
    [item] = poll(hub, b)  # the launch: taken and linked, so the worker is up
    http.request("POST", hub.url + "/bridge/ack", b.token,
                 {"id": item["id"], "ok": True, "launch": True})
    assert http.request("POST", hub.url + "/bridge/worker", b.token,
                        {"worker": "proj-a-w1", "key": "claude:w1"})[0] == 204
    return mgr, me, b, bob_id, bob


def log_details(hub, kind):
    with hub.store.lock:
        return [r["detail"] for r in hub.store.db.execute("SELECT * FROM log WHERE kind=?",
                                                          (kind,))]


# 1. closing

def test_manager_closes_a_worker_it_approved(hub):
    mgr, me, b, _, bob = started(hub)
    assert "reason" in call_error(mgr, me, "close_worker", name="proj-a-w1")
    out = run(call(mgr, me, "close_worker", name="proj-a-w1", reason="bob asked: test is over"))
    assert out.startswith("closing `proj-a-w1`")
    assert [(i["kind"], i["worker"]) for i in poll(hub, b)] == [("close", "proj-a-w1")]
    assert log_details(hub, "worker_close") == ["proj-a-w1: bob asked: test is over"]
    assert "the manager closed worker `proj-a-w1`: bob asked: test is over" in inbox(bob, BOSS)


def test_manager_cannot_close_a_worker_it_did_not_approve(hub, clock):
    mgr, me, _, bob_id, bob = started(hub)
    insert_worker(hub, "proj-a-w9", "up", bob_id, clock[0])  # bob started it himself
    assert "no worker proj-a-w9 of yours" in call_error(mgr, me, "close_worker",
                                                        name="proj-a-w9", reason="bob asked")
    # nor a later worker that reused an approved name for someone else
    with hub.store.lock:
        hub.store.db.execute("UPDATE workers SET boss_person=? WHERE name='proj-a-w1'",
                             (bob_id + 100,))
    assert "no worker proj-a-w1 of yours" in call_error(mgr, me, "close_worker",
                                                        name="proj-a-w1", reason="x")


def test_workers_lists_the_ones_it_approved(hub, clock):
    mgr, me, _, bob_id, _ = started(hub)
    insert_worker(hub, "proj-a-w9", "up", bob_id, clock[0])
    names = [w["name"] for w in run(call(mgr, me, "workers"))]
    assert "proj-a-w1" in names and "proj-a-w9" not in names


def test_rules_say_exactly_what_it_may_close():
    md = manager.claude_md("")
    assert "works only on a worker the hub started on a request you decided" in md
    assert "never promise and never try" in md


# 2. the live workers come from the database

def test_request_notice_lists_the_live_workers_now(hub, clock):
    mgr, me, b, bob_id, bob = started(hub)
    inbox(mgr, me)
    with hub.store.lock:
        hub.store.db.execute("UPDATE workers SET state='closed', closed_at=? WHERE"
                             " name='proj-a-w1'", (clock[0],))
    insert_worker(hub, "proj-a-w7", "up", bob_id, clock[0])
    ask_work(hub, bob)
    [text] = [t for t in inbox(mgr, me) if "Request 2" in t]
    assert text.startswith(f"Live workers on {HOST} now (the hub's list): `proj-a-w7` of bob"
                           " in proj-a (up).")  # first: the decision rests on it
    assert "proj-a-w1" not in text


def test_live_line_when_none():
    t = manager.text_busy("bob", "proj-a", "box-1", "alice", "/w", 3, live=[])
    assert t.startswith("No live workers on box-1 now (the hub's list; a worker you remember is"
                        " gone). bob wants proj-a on box-1")
    t = manager.text_in_use("bob", "proj-a", "box-1", ["alice"], [], 3, live=[])
    assert t.startswith("No live workers on box-1 now")


def test_a_decision_leaning_on_a_closed_worker_is_refused_and_the_live_list_comes_back(hub, clock):
    from helpers import call_error
    mgr, me = manager_online(hub)
    b, bob_id, bob = busy_setup(hub)
    insert_worker(hub, "proj-a-w6", "closed", bob_id, clock[0])  # it closed itself
    ask_work(hub, bob)
    err = call_error(mgr, me, "work_decide", request_id=1, machine="wait",
                     reason="box-1 still has proj-a-w6 running")
    assert "not decided" in err and "`proj-a-w6` (closed)" in err
    assert "No live workers on box-1 now" in err
    assert hub.mm.request(1)["decided_at"] is None  # nothing decided, nothing told
    out = run(call(mgr, me, "work_decide", request_id=1, machine="wait",
                   reason="proj-a-w6 is done; alice still works in that folder"))
    assert "told them to wait. No live workers on box-1 now" in out


# 3. wait, then yes or no

def test_wait_then_start(hub):
    mgr, me = manager_online(hub)
    b, _, bob = busy_setup(hub)
    ask_work(hub, bob)
    run(call(mgr, me, "work_decide", request_id=1, machine="wait", reason="alice is mid-run"))
    out = run(call(mgr, me, "work_decide", request_id=1, machine="box-1", reason="alice is done"))
    assert "started `proj-a-w1`" in out
    texts = inbox(bob, BOSS)
    assert "the manager says wait on proj-a: alice is mid-run" in texts
    assert "the manager started `proj-a-w1` on box-1 for proj-a: alice is done" in texts
    assert "already decided: start box-1" in call_error(mgr, me, "work_decide", request_id=1,
                                                        machine="wait", reason="x")


def test_wait_then_no_is_final(hub):
    mgr, me = manager_online(hub)
    _, _, bob = busy_setup(hub)
    ask_work(hub, bob)
    run(call(mgr, me, "work_decide", request_id=1, machine="wait", reason="busy"))
    assert run(call(mgr, me, "work_decide", request_id=1, machine="no",
                    reason="the run takes all week")) == "request 1: told them no"
    assert "the manager says no to proj-a for now: the run takes all week" in inbox(bob, BOSS)
    assert "already decided: no" in call_error(mgr, me, "work_decide", request_id=1,
                                               machine="box-1", reason="x")


def test_wait_cannot_be_changed_after_an_hour(hub, clock):
    mgr, me = manager_online(hub)
    _, _, bob = busy_setup(hub)
    ask_work(hub, bob)
    run(call(mgr, me, "work_decide", request_id=1, machine="wait", reason="busy"))
    clock[0] += 3601
    hub.tick()  # expire_requests leaves a decided request alone
    assert hub.mm.request(1)["decision"] == "wait"
    run(call(mgr, me, "inbox"))
    assert "waited over an hour" in call_error(mgr, me, "work_decide", request_id=1,
                                               machine="box-1", reason="x")
