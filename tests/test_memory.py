"""Memory: git facts history from heartbeats."""
from dataclasses import replace

import pytest

import kaptyorka
from conftest import start_hub
from helpers import person
from kaptyorka import http, memory
from kaptyorka.memory import MemoryStore
from kaptyorka.store import ONLINE_S, Store

A = "/home/alice/proj-a"
F = {"proj-a": A}


def g(branch="main", dirty=False, subject="init"):
    return {A: {"branch": branch, "dirty": dirty, "subject": subject}}


@pytest.fixture
def clock():
    return [1_000_000.0]


@pytest.fixture
def store(tmp_path, clock):
    s = Store(tmp_path / "hub.db", clock=lambda: clock[0])
    yield s
    s.close()


@pytest.fixture
def ms(store):
    return MemoryStore(store)


@pytest.fixture
def alice(store):
    return person(store, "alice")[0]


@pytest.fixture
def p(store, alice):
    code = store.invite_create(alice)
    token, _ = store.invite_redeem(code, "x", host="box-1", os_user="u")
    return store.authenticate(token)


@pytest.fixture
def dev(p):
    return p.device_id


def test_git_seen_stores_a_row_only_on_change(ms, p, dev):
    assert ms.git_seen(p, dev, g(), F) == ["proj-a"]
    assert ms.git_seen(p, dev, g(), F) == []
    assert len(ms.git_recent("proj-a")) == 1
    ms.git_seen(p, dev, g(dirty=True), F)
    with ms.lock:  # the dirty flip is kept, for the done check
        assert ms.db.execute("SELECT COUNT(*) FROM git_facts").fetchone()[0] == 2
    assert [r["dirty"] for r in ms.git_recent("proj-a")] == [False]  # but it is not activity


def test_git_recent_shows_branch_or_subject_changes_only_newest_20(ms, store, clock, p, dev):
    ms.git_seen(p, dev, g(), F)
    clock[0] += 1
    ms.git_seen(p, dev, g(dirty=True), F)
    clock[0] += 1
    ms.git_seen(p, dev, g(branch="feat", dirty=True), F)
    clock[0] += 1
    ms.git_seen(p, dev, g(branch="feat", subject="fix"), F)
    assert [(r["branch"], r["subject"]) for r in ms.git_recent("proj-a")] == [
        ("feat", "fix"), ("feat", "init"), ("main", "init")]
    for i in range(30):
        clock[0] += 1
        ms.git_seen(p, dev, g(subject=f"c{i}"), F)
    rows = ms.git_recent("proj-a")
    assert len(rows) == 20 and rows[0]["subject"] == "c29"


def test_worktree_counts_as_the_project(ms):
    assert ms.project_of("/home/alice/proj-a--2", F) == "proj-a"
    assert ms.project_of("/home/alice/proj-a", F) == "proj-a"
    assert ms.project_of("/x/y/webshop--3", {}) == "webshop"
    assert ms.project_of("/x/y/webshop", {}) == "webshop"
    # a learned worktree entry never wins over its base project, whatever the order
    learned = {"proj-a--2": "/home/alice/proj-a--2", "proj-a": A}
    assert ms.project_of("/home/alice/proj-a--2", learned) == "proj-a"


def test_git_recent_window_and_person_name(ms, store, clock, p, dev):
    ms.git_seen(p, dev, g(subject="old"), F)
    clock[0] += 8 * 86400
    ms.git_seen(p, dev, g(subject="new"), F)
    rows = ms.git_recent("proj-a")
    assert [r["subject"] for r in rows] == ["new"]
    assert rows[0]["person"] == "alice" and rows[0]["host"] == "box-1"
    with store.lock:
        store.db.execute("UPDATE git_facts SET person_id=NULL")
    assert ms.git_recent("proj-a")[0]["person"] == "u"
    assert len(ms.git_recent("proj-a", days=10)) == 2


def test_prune_deletes_older_than_30_days(ms, store, clock, p, dev):
    ms.git_seen(p, dev, g(subject="old"), F)
    clock[0] += 31 * 86400
    ms.git_seen(p, dev, g(subject="new"), F)
    assert memory.prune(store, clock[0])["git_facts"] == 1
    assert [r["subject"] for r in ms.git_recent("proj-a", days=60)] == ["new"]


def test_prune_done_checks_of_long_offline_sessions_and_old_state_writes(ms, store, clock, p):
    for key in ("claude:1", "claude:2"):
        store.heartbeat("box-1", [{"key": key, "tool": "claude-code", "project": "proj-a",
                                   "cwd": A}], principal=p, facts={"host": "box-1", "os_user": "u"})
        ms.status_signal(key, "done")
    ms.state_written("claude:1", "proj-a", None, clock[0])
    clock[0] += 31 * 86400
    store.heartbeat("box-1", [{"key": "claude:2", "tool": "claude-code", "project": "proj-a",
                               "cwd": A}], principal=p, facts={"host": "box-1", "os_user": "u"})
    ms.state_written("claude:2", "proj-a", None, clock[0])
    assert memory.prune(store, clock[0]) == {"git_facts": 0, "done_checks": 1, "state_writes": 1}
    with store.lock:
        assert [r[0] for r in store.db.execute("SELECT key FROM done_checks")] == ["claude:2"]
        assert [r[0] for r in store.db.execute("SELECT key FROM state_writes")] == ["claude:2"]


def test_signals_for_online_sessions_only(ms, store, clock, p, dev):
    def beat(*sessions):
        store.heartbeat("box-1", list(sessions), principal=p, facts={"host": "box-1", "os_user": "u"})
    here = {"key": "claude:1", "tool": "claude-code", "project": "proj-a", "cwd": A}
    other = {"key": "claude:2", "tool": "claude-code", "project": "proj-a", "cwd": "/elsewhere"}
    beat(here, other)
    ms.git_seen(p, dev, g(dirty=True), F)

    def sig():
        with store.lock:
            return {r["key"]: r["signal"] for r in store.db.execute("SELECT key, signal FROM done_checks")}
    assert sig() == {}  # the first sighting has nothing to compare with
    clock[0] += 10
    ms.git_seen(p, dev, g(dirty=False, subject="fix"), F)
    assert sig() == {"claude:1": "clean"}  # the later of commit and clean
    with store.lock:
        row = store.db.execute("SELECT * FROM done_checks WHERE key='claude:1'").fetchone()
    assert row["project"] == "proj-a" and row["signal_at"] == clock[0]
    # an offline session gets no signal
    clock[0] += ONLINE_S + 1
    ms.git_seen(p, dev, g(subject="again"), F)
    assert sig() == {"claude:1": "clean"}
    with store.lock:
        assert store.db.execute("SELECT signal_at FROM done_checks").fetchone()[0] == clock[0] - ONLINE_S - 1


def test_branches_with_sessions(ms, store, clock, p, dev):
    store.heartbeat("box-1", [{"key": "claude:1", "tool": "claude-code", "project": "proj-a",
                               "cwd": A}], principal=p, facts={"host": "box-1", "os_user": "u"})
    ms.git_seen(p, dev, g(branch="feat"), F)
    name = store.agent("claude:1").name
    assert ms.branches_with_sessions("proj-a", clock[0]) == {"feat": [name]}
    assert ms.branches_with_sessions("proj-a", clock[0] + ONLINE_S + 5) == {}


def test_shared_device_has_no_person(ms, store, p, dev):
    ms.git_seen(replace(p, shared=True), dev, g(), F)
    with store.lock:
        assert store.db.execute("SELECT person_id FROM git_facts").fetchone()[0] is None


def test_heartbeat_feeds_git_facts(tmp_path, clock):
    store = Store(tmp_path / "hub.db", clock=lambda: clock[0], legacy_token="T")
    stop = start_hub(store)
    try:
        pid = person(store, "alice")[0]
        code = store.invite_create(pid)
        token, _ = store.invite_redeem(code, "box-1", host="box-1", os_user="u")
        body = {"machine": "box-1", "sessions": [], "tools": [],
                "facts": {"host": "box-1", "os_user": "u", "version": kaptyorka.__version__,
                          "git": g()},
                "folders": F}
        status, _ = http.request("POST", f"http://127.0.0.1:{stop.port}/bridge/heartbeat", token, body)
        assert status == 200
        rows = MemoryStore(store).git_recent("proj-a")
        assert [(r["branch"], r["person"]) for r in rows] == [("main", "alice")]
        long = {"branch": "b" * 500, "dirty": False, "subject": "s" * 500}
        body["facts"]["git"] = {A + "/" + "x" * 500: long}
        assert http.request("POST", f"http://127.0.0.1:{stop.port}/bridge/heartbeat", token,
                            body)[0] == 200
        with store.lock:  # cleaned like the device facts: every string capped
            row = store.db.execute("SELECT * FROM git_facts ORDER BY id DESC LIMIT 1").fetchone()
        assert len(row["cwd"]) == len(row["branch"]) == len(row["subject"]) == 200
    finally:
        stop()
        store.close()


def test_null_git_fields_do_not_fail(ms, p, dev):
    ms.git_seen(p, dev, {A: {"branch": None, "dirty": None, "subject": None}}, F)
    assert ms.git_recent("proj-a")[0]["subject"] == ""


def test_bad_git_payload_does_not_fail_the_heartbeat(tmp_path, clock, monkeypatch):
    store = Store(tmp_path / "hub.db", clock=lambda: clock[0], legacy_token="T")
    stop = start_hub(store)
    try:
        pid = person(store, "alice")[0]
        token, _ = store.invite_redeem(store.invite_create(pid), "box-1", host="box-1", os_user="u")
        body = {"machine": "box-1", "sessions": [], "tools": [],
                "facts": {"host": "box-1", "os_user": "u", "git": {A: {"subject": None}}},
                "folders": F}
        url = f"http://127.0.0.1:{stop.port}/bridge/heartbeat"
        assert http.request("POST", url, token, body)[0] == 200
        monkeypatch.setattr(MemoryStore, "git_seen", lambda *a: 1 / 0)
        assert http.request("POST", url, token, body)[0] == 200
    finally:
        stop()
        store.close()


GOOD = "# What it is\nx\n## Where it stands\nx\n### decisions\nx\n# Open problems\nx\n## How to run and test\nx\n"


def test_check_state_accepts_all_headings_in_order():
    assert memory.check_state(GOOD) is None


def test_check_state_rejects_missing_or_misordered():
    msg = ("the state note needs these headings in order: What it is, Where it stands, "
           "Decisions, Open problems, How to run and test")
    assert memory.check_state("just text") == msg
    assert memory.check_state(GOOD.replace("# Open problems\nx\n", "")) == msg
    swapped = "# Where it stands\n# What it is\n# Decisions\n# Open problems\n# How to run and test\n"
    assert memory.check_state(swapped) == msg
    assert memory.check_state("#### What it is\n" + GOOD.split("\n", 1)[1]) == msg


def test_state_written_records_row(ms, store, clock):
    ms.state_written("claude:1", "proj-a", 3, clock[0])
    row = store.db.execute("SELECT project, key, person_id, time FROM state_writes").fetchone()
    assert tuple(row) == ("proj-a", "claude:1", 3, clock[0])
