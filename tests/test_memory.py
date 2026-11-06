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
    assert [r["dirty"] for r in ms.git_recent("proj-a")] == [True, False]


def test_worktree_counts_as_the_project(ms):
    assert ms.project_of("/home/alice/proj-a--2", F) == "proj-a"
    assert ms.project_of("/home/alice/proj-a", F) == "proj-a"
    assert ms.project_of("/x/y/webshop--3", {}) == "webshop"
    assert ms.project_of("/x/y/webshop", {}) == "webshop"


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
    assert memory.prune(store, clock[0]) == 1
    assert [r["subject"] for r in ms.git_recent("proj-a", days=60)] == ["new"]


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
    finally:
        stop()
        store.close()
