"""The done check: a session that did work in a project must update its state note."""
import pytest

from helpers import call, person, run
from kaptyorka import http
from kaptyorka.memory import MemoryStore
from kaptyorka.store import Store

A = "/home/alice/proj-a"
B = "/home/alice/proj-a--2"
F = {"proj-a": A}
REASON = ("You did work in proj-a (commit) but its state note was not updated. Call "
          "state_write(\"proj-a\", text) with the five headings (What it is, Where it stands, "
          "Decisions, Open problems, How to run and test), updating what changed, then stop. "
          "If nothing changed in the project, call status(\"done: no state change\") and stop.")


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


def device(store, pid, host="box-1"):
    token, _ = store.invite_redeem(store.invite_create(pid), host, host=host, os_user="u")
    return store.authenticate(token)


@pytest.fixture
def p(store, alice):
    store.project_register("proj-a", by="admin")
    store.grant_project("proj-a", alice, "write", "admin")
    return device(store, alice)


def beat(store, p, *cwds):
    store.heartbeat("box-1", [{"key": f"claude:{i}", "tool": "claude-code", "project": "proj-a",
                               "cwd": c} for i, c in enumerate(cwds, 1)],
                    principal=p, facts={"host": "box-1", "os_user": "u"})


def git(ms, p, cwd=A, subject="init", dirty=False):
    ms.git_seen(p, p.device_id, {cwd: {"branch": "main", "dirty": dirty, "subject": subject}}, F)


def commit(ms, store, clock, p, cwd=A):
    """A session in cwd starts, then a commit is seen there."""
    git(ms, p, cwd)
    clock[0] += 10
    git(ms, p, cwd, subject="fix")
    clock[0] += 10


def test_read_only_session_is_never_blocked(ms, store, clock, alice):
    store.project_register("proj-a", by="admin")
    store.grant_project("proj-a", alice, "read", "admin")
    p = device(store, alice)
    beat(store, p, A)
    commit(ms, store, clock, p)
    assert ms.done_check("claude:1", p, clock[0]) == {"decision": "ok"}


def test_commit_signal_blocks_once_then_never_again(ms, store, clock, p):
    beat(store, p, A)
    commit(ms, store, clock, p)
    assert ms.done_check("claude:1", p, clock[0]) == {"decision": "block", "reason": REASON}
    with store.lock:
        assert store.db.execute("SELECT blocked_at FROM done_checks").fetchone()[0] == clock[0]
    assert ms.done_check("claude:1", p, clock[0]) == {"decision": "ok"}
    clock[0] += 10
    git(ms, p, subject="more")
    assert ms.done_check("claude:1", p, clock[0]) == {"decision": "ok"}


def test_first_sighting_commit_is_seen_against_the_start(ms, store, clock, p):
    git(ms, p)  # seen before the session started: the session's baseline
    clock[0] += 10
    beat(store, p, A)
    assert ms.done_check("claude:1", p, clock[0]) == {"decision": "ok"}
    clock[0] += 10
    with store.lock:  # offline when the commit lands: git_seen signals nobody
        store.db.execute("UPDATE agents SET last_seen=0")
    git(ms, p, subject="fix")
    assert ms.done_check("claude:1", p, clock[0])["decision"] == "block"


def test_state_write_after_signal_clears_the_block(ms, store, clock, p):
    beat(store, p, A)
    commit(ms, store, clock, p)
    ms.state_written("claude:1", "proj-a", p.person_id, clock[0])
    assert ms.done_check("claude:1", p, clock[0]) == {"decision": "ok"}
    # a write before the signal does not count
    clock[0] += 10
    git(ms, p, subject="next")
    assert ms.done_check("claude:1", p, clock[0])["decision"] == "block"


def test_state_written_from_page_by_same_person_satisfies(ms, store, clock, p):
    beat(store, p, A)
    commit(ms, store, clock, p)
    ms.state_written("web:alice", "proj-a", p.person_id, clock[0])
    assert ms.done_check("claude:1", p, clock[0]) == {"decision": "ok"}


def test_write_in_another_project_does_not_satisfy(ms, store, clock, p):
    beat(store, p, A)
    commit(ms, store, clock, p)
    ms.state_written("claude:1", "proj-b", p.person_id, clock[0])
    assert ms.done_check("claude:1", p, clock[0])["decision"] == "block"


def test_signal_is_per_session(ms, store, clock, p):
    beat(store, p, A, B)
    git(ms, p, B)
    commit(ms, store, clock, p, A)
    assert ms.done_check("claude:2", p, clock[0]) == {"decision": "ok"}
    assert ms.done_check("claude:1", p, clock[0])["decision"] == "block"


def test_no_write_role_never_blocks(ms, store, clock, alice):
    p = device(store, alice)  # proj-a registered, alice has no role
    store.project_register("proj-a", by="admin")
    beat(store, p, A)
    commit(ms, store, clock, p)
    assert ms.done_check("claude:1", p, clock[0]) == {"decision": "ok"}


def test_unregistered_project_or_no_person_never_blocks(ms, store, clock, alice):
    p = device(store, alice)
    beat(store, p, A)
    commit(ms, store, clock, p)
    assert ms.done_check("claude:1", p, clock[0]) == {"decision": "ok"}
    store.project_register("proj-a", by="admin")
    store.grant_project("proj-a", alice, "write", "admin")
    with store.lock:
        store.db.execute("UPDATE agents SET person_id=NULL")
    assert ms.done_check("claude:1", p, clock[0]) == {"decision": "ok"}


def test_unknown_session_is_ok(ms, p, clock):
    assert ms.done_check("claude:9", p, clock[0]) == {"decision": "ok"}


def test_status_done_is_a_signal_and_no_state_change_clears_it(ms, store, clock, p):
    beat(store, p, A)
    clock[0] += 10
    ms.status_signal("claude:1", "Done with the fix")
    out = ms.done_check("claude:1", p, clock[0])
    assert out["decision"] == "block" and "(done)" in out["reason"]
    beat(store, p, A, B)
    clock[0] += 10
    ms.status_signal("claude:2", "all DONE")
    ms.status_signal("claude:2", "done: no state change")
    assert ms.done_check("claude:2", p, clock[0]) == {"decision": "ok"}
    ms.status_signal("claude:2", "working on the api")
    assert ms.done_check("claude:2", p, clock[0]) == {"decision": "ok"}


def test_status_tool_feeds_the_signal(hub):
    hub.store.project_register("proj-a", by="admin")
    pc = {"key": "claude:1", "machine": "pc", "tool": "claude-code", "project": "proj-a"}
    run(call(hub, pc, "status", text="done"))
    with hub.store.lock:
        assert hub.store.db.execute("SELECT signal FROM done_checks").fetchone()[0] == "done"


def _check(hub, ident):
    h = {f"X-Kaptyorka-{k.capitalize()}": v for k, v in ident.items()}
    return http.request("GET", hub.url + "/api/done-check", hub.token, headers=h)


def test_route_mode_by_tool(hub):
    hub.store.project_register("proj-a", by="admin")
    for i, (tool, mode) in enumerate([("claude-code", "block"), ("codex", "block"),
                                      ("cursor", "remind"), ("opencode", "remind")]):
        ident = {"key": f"s:{i}", "machine": "pc", "tool": tool, "project": "proj-a"}
        status, body = _check(hub, ident)
        assert status == 200 and body == {"decision": "ok", "mode": mode}
        run(call(hub, ident, "status", text="done"))
        status, body = _check(hub, ident)
        assert status == 200 and body["decision"] == "block" and body["mode"] == mode
        assert body["reason"].startswith("You did work in proj-a (done)")
    assert _check(hub, {})[0] == 400


def test_done_matches_a_whole_word(ms, store, clock, p):
    beat(store, p, A)
    clock[0] += 10
    ms.status_signal("claude:1", "abandoned the refactor, undone changes")
    assert ms.done_check("claude:1", p, clock[0]) == {"decision": "ok"}


def test_state_write_then_done_is_never_blocked(ms, store, clock, p):
    beat(store, p, A)
    clock[0] += 10
    ms.state_written("claude:1", "proj-a", p.person_id, clock[0])
    clock[0] += 10
    ms.status_signal("claude:1", "done: shipped")
    assert ms.done_check("claude:1", p, clock[0]) == {"decision": "ok"}


def test_commit_write_then_done_is_not_blocked(ms, store, clock, p):
    beat(store, p, A)
    commit(ms, store, clock, p)
    ms.state_written("web:alice", "proj-a", p.person_id, clock[0])
    clock[0] += 10
    ms.status_signal("claude:1", "done")
    assert ms.done_check("claude:1", p, clock[0]) == {"decision": "ok"}


def test_commit_then_done_without_write_blocks_once(ms, store, clock, p):
    beat(store, p, A)
    commit(ms, store, clock, p)
    ms.status_signal("claude:1", "done")
    assert ms.done_check("claude:1", p, clock[0]) == {"decision": "block", "reason": REASON}
    assert ms.done_check("claude:1", p, clock[0]) == {"decision": "ok"}


def test_shared_cwd_both_signalled_one_write_satisfies_both(ms, store, clock, p):
    beat(store, p, A, A)  # two sessions of alice in one folder
    commit(ms, store, clock, p)
    with store.lock:
        assert store.db.execute("SELECT COUNT(*) FROM done_checks WHERE signal='commit'"
                                ).fetchone()[0] == 2
    ms.state_written("claude:1", "proj-a", p.person_id, clock[0])
    assert ms.done_check("claude:1", p, clock[0]) == {"decision": "ok"}
    assert ms.done_check("claude:2", p, clock[0]) == {"decision": "ok"}
