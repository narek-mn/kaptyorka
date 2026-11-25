"""What the digest and light notices hold (coordination only), and the manager's note rights."""
import time

import pytest

from helpers import person
from kaptyorka import knowledge
from kaptyorka.store import KaptyorkaError, Store, session_id

T0 = 1_000_000.0


@pytest.fixture
def clock():
    return [T0]


@pytest.fixture
def store(tmp_path, clock):
    s = Store(tmp_path / "hub.db", clock=lambda: clock[0])
    yield s
    s.close()


def session(store, name, key, project, role="member"):
    pid, token = person(store, name, role)
    store.seen(key, "box-1", "claude-code", project, principal=store.authenticate(token))
    return pid, store.authenticate(token)


def worker(store, name, project, boss, state, closed_at, task="fix the login", error=""):
    with store.lock:
        store.db.execute(
            "INSERT INTO workers (name, project, task, boss_key, boss_person, device_id, host,"
            " tool, state, error, created, closed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (name, project, task, "k:boss", boss, 0, "box-1", "claude-code", state, error,
             closed_at - 60, closed_at))


# the manager's note rights

def test_manager_writes_team_and_every_team_projects_notes(store):
    store.project_register("proj-a", by="test")
    store.project_register("proj-b", by="test")
    _, mgr = session(store, "manager", "k:mgr", "manager", role="manager")
    store.notes_write("k:mgr", "team", "what the lab is")
    for p in ("proj-a", "proj-b"):
        store.notes_write("k:mgr", "state", "x", p)
        assert store.notes_read("state", mgr, p).author == "manager"
    assert [p["name"] for p in store.projects_for(mgr)] == ["proj-a", "proj-b"]
    assert store.notes_read("team", mgr).text == "what the lab is"


def test_manager_keeps_the_secret_unregistered_and_removed_gates(store):
    store.project_register("proj-a", by="test")
    _, mgr = session(store, "manager", "k:mgr", "manager", role="manager")
    with pytest.raises(KaptyorkaError, match="looks like a secret"):
        store.notes_write("k:mgr", "team", "token ghp_" + "a" * 36)
    with pytest.raises(KaptyorkaError, match="not registered"):
        store.notes_write("k:mgr", "state", "x", "home-stuff")
    with store.lock:
        store.db.execute("UPDATE projects SET archived_at=? WHERE name='proj-a'", (T0,))
    with pytest.raises(KaptyorkaError, match="was removed"):
        store.notes_write("k:mgr", "state", "x", "proj-a")
    assert store.projects_for(mgr) == []


def test_a_members_note_rights_are_unchanged(store):
    store.project_register("proj-a", by="test")
    store.project_register("proj-b", by="test")
    pid, alice = session(store, "alice", "k:a", "proj-a")
    store.grant_project("proj-a", pid, "read", by="test")
    with pytest.raises(KaptyorkaError, match="no access"):
        store.notes_write("k:a", "state", "x", "proj-a")  # read only
    with pytest.raises(KaptyorkaError, match="no access"):
        store.notes_read("state", alice, "proj-b")
    assert [p["name"] for p in store.projects_for(alice)] == ["proj-a"]
    store.notes_write("k:a", "team", "ok")  # the team board, as before


# the digest

def test_digest_lists_changes_since_the_last_one(store, clock):
    store.project_register("proj-a", description="the robot arm", by="test")
    pid, _ = session(store, "alice", "k:a", "proj-a")
    store.grant_project("proj-a", pid, "write", by="test")
    store.notes_write("k:a", "state", "one\ntwo\n", "proj-a")
    store.notes_write("k:a", "old", "kept", "proj-a")
    since = clock[0]
    clock[0] += 100
    store.notes_write("k:a", "state", "one\nthree\nfour\n", "proj-a")
    store.notes_write("k:a", "glossary", "IK: inverse kinematics")
    store.project_register("proj-b", by="test")
    person(store, "bob")
    store.team_machine_add("jetson-1", "test")
    store.seen("k:a", "box-1", "claude-code", "proj-a")
    store.set_status("k:a", "tuning the gripper")
    worker(store, "w-1", "proj-a", pid, "closed", clock[0])
    worker(store, "w-2", "proj-a", pid, "failed", clock[0], error="no tmux")
    worker(store, "w-old", "proj-a", pid, "closed", since - 10)
    text = knowledge.digest(store, since, clock[0])
    assert text.startswith("team digest since ")
    assert "state" not in text.split("\n", 1)[1] and "glossary" not in text  # never notes
    assert "inverse kinematics" not in text and "robot arm" not in text  # nor what a project is
    assert "- bob: new member" in text and "- jetson-1: new" in text
    assert "- proj-b: new" in text and "proj-a: new" not in text
    assert f"- proj-a\n  - alice {session_id('k:a')} " in text and "tuning the gripper" in text
    assert "`w-1` of alice in proj-a on box-1 finished; task: fix the login" in text
    assert "`w-2` of alice in proj-a on box-1 failed (no tmux)" in text
    assert "w-old" not in text
    assert knowledge.digest(store, clock[0], clock[0]) is not None  # alice is still working
    store.db.execute("UPDATE agents SET last_seen=0")
    assert knowledge.digest(store, clock[0], clock[0]) is None


def test_digest_shows_only_what_the_manager_may_see(store, clock):
    store.project_register("proj-a", by="test")
    pid, _ = session(store, "alice", "k:a", "proj-a")
    store.grant_project("proj-a", pid, "write", by="test")
    session(store, "bob", "k:b", "home-stuff")  # not a team project
    store.set_status("k:b", "private things")
    store.set_status("k:a", "deploy with ghp_" + "b" * 36)
    worker(store, "w-home", "home-stuff", pid, "closed", clock[0] + 1)
    _, mgr = session(store, "manager", "k:mgr", "manager", role="manager")
    since = clock[0]
    clock[0] += 5
    with store.lock:  # a removed project's note: the admin's alone
        store.db.execute("INSERT INTO projects (name, created, archived_at) VALUES"
                         " ('proj-old', 0, 1)")
        store.db.execute("INSERT INTO notes (topic, text, author, time, project) VALUES"
                         " ('state', 'x', 'alice', ?, 'proj-old')", (clock[0],))
    text = knowledge.digest(store, since, clock[0])
    assert "home-stuff" not in text and "private things" not in text and "bob" not in text
    assert "ghp_" not in text and knowledge.HIDDEN in text
    assert "proj-old" not in text and "w-home" not in text


def test_digest_is_bounded(store, clock):
    store.project_register("proj-a", by="test")
    pid, _ = session(store, "alice", "k:a", "proj-a")
    store.grant_project("proj-a", pid, "write", by="test")
    since = clock[0]
    clock[0] += 1
    for i in range(40):
        store.notes_write("k:a", f"topic-{i}", "x" * 50, "proj-a")
        worker(store, f"w-{i}", "proj-a", pid, "closed", clock[0], task="t" * 500)
    for i in range(40):
        person(store, f"p{i:02d}")
    text = knowledge.digest(store, since, clock[0])
    assert len(text) <= knowledge.MAX_DIGEST
    assert "- and 25 more" in text
    assert all(len(line) <= knowledge.MAX_LINE for line in text.splitlines()[1:])


def test_digest_never_asks_about_project_notes_and_skips_a_notes_only_day(store, clock):
    store.project_register("proj-a", by="test")
    pid, _ = session(store, "alice", "k:a", "proj-a")
    store.grant_project("proj-a", pid, "write", by="test")
    clock[0] += 20 * 86400
    store.notes_write("k:a", "state", "x", "proj-a")
    store.notes_write("k:a", "projects", "- proj-a: x")
    store.db.execute("UPDATE agents SET last_seen=0")
    assert knowledge.digest(store, clock[0] - 10, clock[0]) is None  # notes alone: nothing
    store.seen("k:a", "box-1", "claude-code", "proj-a")
    text = knowledge.digest(store, clock[0] - 10, clock[0])
    assert "\n- proj-a\n" in text and "note" not in text.split("\n\n", 1)[1]


def test_light_notice_for_finished_workers_only(store, clock):
    store.project_register("proj-a", by="test")
    pid, _ = session(store, "alice", "k:a", "proj-a")
    store.grant_project("proj-a", pid, "write", by="test")
    since = clock[0]
    assert knowledge.light(store, since) is None
    clock[0] += 1
    store.notes_write("k:a", "team", "a team note")
    store.notes_write("k:a", "state", "x", "proj-a")
    assert knowledge.light(store, since) is None  # notes never
    worker(store, "w-1", "proj-a", pid, "closed", clock[0])
    worker(store, "w-2", "proj-a", pid, "failed", clock[0], error="no tmux")
    assert knowledge.light(store, since) == (
        "workers finished: `w-1` in proj-a. Update your task list if a task waited on one.")
    assert knowledge.light(store, clock[0]) is None


def test_digest_comes_once_a_day_after_the_quiet_hour():
    day = time.mktime((2026, 11, 19, 12, 0, 0, 0, 0, -1))
    quiet = time.mktime((2026, 11, 19, knowledge.DIGEST_HOUR, 0, 0, 0, 0, -1))
    assert knowledge.quiet_mark(day) == quiet
    assert knowledge.quiet_mark(quiet - 1) == time.mktime(
        (2026, 11, 18, knowledge.DIGEST_HOUR, 0, 0, 0, 0, -1))
    assert knowledge.digest_due(quiet - 1, day)
    assert not knowledge.digest_due(quiet, day)
    assert not knowledge.digest_due(day - 3600, day)
    assert knowledge.digest_due(quiet - 3 * 86400, day)  # asleep for days: one digest
