"""The done check: a session that did work in a project must update its state note."""
import os
from types import SimpleNamespace
from urllib.parse import urlencode

import pytest

from conftest import start_hub
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
    """Sessions as the bridge reports them: each named after its folder (a worktree too)."""
    store.heartbeat("box-1", [{"key": f"claude:{i}", "tool": "claude-code",
                               "project": os.path.basename(c), "cwd": c}
                              for i, c in enumerate(cwds, 1)],
                    principal=p, facts={"host": "box-1", "os_user": "u"})


def check(ms, store, key, p, now):
    """done_check with the project resolved from the session's folder, as the hub does."""
    a = store.agent(key)
    return ms.done_check(key, p, now, project=ms.session_project(a.cwd, a.project, F))


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
    assert check(ms, store, "claude:2", p, clock[0]) == {"decision": "ok"}
    assert check(ms, store, "claude:1", p, clock[0])["decision"] == "block"


def test_worktree_session_is_checked_in_its_base_project(ms, store, clock, p):
    beat(store, p, B)  # named "proj-a--2", which is no registered project
    commit(ms, store, clock, p, B)
    assert ms.done_check("claude:1", p, clock[0], commit=False) == {"decision": "ok"}
    assert check(ms, store, "claude:1", p, clock[0]) == {"decision": "block", "reason": REASON}


def test_commit_state_write_then_dirty_edit_is_not_blocked(ms, store, clock, p):
    beat(store, p, A)
    git(ms, p)  # the baseline
    clock[0] += 10
    git(ms, p, subject="fix")  # a live commit
    clock[0] += 10
    ms.state_written("claude:1", "proj-a", p.person_id, clock[0])
    clock[0] += 10
    git(ms, p, subject="fix", dirty=True)  # one more edit after the note: no new commit
    assert ms.done_check("claude:1", p, clock[0]) == {"decision": "ok"}


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
    ms.status_signal("claude:2", "all DONE", "proj-a")
    ms.status_signal("claude:2", "done: no state change", "proj-a")
    assert check(ms, store, "claude:2", p, clock[0]) == {"decision": "ok"}
    ms.status_signal("claude:2", "working on the api", "proj-a")
    assert check(ms, store, "claude:2", p, clock[0]) == {"decision": "ok"}
    ms.status_signal("claude:2", "done again", "proj-a")
    assert check(ms, store, "claude:2", p, clock[0])["decision"] == "block"


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


@pytest.fixture
def dhub(tmp_path, clock):
    """A hub with alice's own device (not the legacy token), proj-a registered, write role."""
    store = Store(tmp_path / "hub2.db", clock=lambda: clock[0], legacy_token="T")
    stop = start_hub(store)
    pid = person(store, "alice")[0]
    store.project_register("proj-a", by="admin")
    store.grant_project("proj-a", pid, "write", "admin")
    token, _ = store.invite_redeem(store.invite_create(pid), "box-1", host="box-1", os_user="u")
    yield SimpleNamespace(url=f"http://127.0.0.1:{stop.port}", token=token, store=store,
                          ms=MemoryStore(store))
    stop()
    store.close()


def _beat(h, cwd, git):
    body = {"machine": "box-1", "tools": [], "folders": F,
            "sessions": [{"key": "claude:1", "tool": "claude-code",
                          "project": os.path.basename(cwd), "cwd": cwd}],
            "facts": {"host": "box-1", "os_user": "u", "git": git}}
    assert http.request("POST", h.url + "/bridge/heartbeat", h.token, body)[0] == 200


def _stop_check(h, cwd, **params):
    headers = {"X-Kaptyorka-Key": "claude:1", "X-Kaptyorka-Machine": "box-1",
               "X-Kaptyorka-Tool": "claude-code", "X-Kaptyorka-Project": os.path.basename(cwd)}
    q = "?" + urlencode(params) if params else ""
    status, body = http.request("GET", h.url + "/api/done-check" + q, h.token, headers=headers)
    assert status == 200
    return body


def _main(subject="init", dirty=False):
    return {"branch": "main", "dirty": dirty, "subject": subject}


def test_route_checks_a_worktree_session_in_its_base_project(dhub, clock):
    _beat(dhub, B, {B: _main()})
    clock[0] += 10
    _beat(dhub, B, {B: _main("fix")})
    body = _stop_check(dhub, B)
    assert body["decision"] == "block" and body["reason"] == REASON


def test_route_stop_git_params_catch_a_commit_the_bridge_never_reported(dhub, clock):
    _beat(dhub, A, {A: _main()})
    clock[0] += 10
    assert _stop_check(dhub, A) == {"decision": "ok", "mode": "block"}
    body = _stop_check(dhub, A, branch="main", subject="fix", dirty="0")
    assert body["decision"] == "block" and body["reason"] == REASON
    _stop_check(dhub, A, branch="main", subject="fix", dirty="0")  # the same values again
    with dhub.store.lock:
        rows = dhub.store.db.execute("SELECT cwd, host, os_user, subject, dirty, project"
                                     " FROM git_facts ORDER BY id").fetchall()
    assert [tuple(r) for r in rows] == [(A, "box-1", "u", "init", 0, "proj-a"),
                                        (A, "box-1", "u", "fix", 0, "proj-a")]


def test_route_stop_git_params_are_capped(dhub, clock):
    _beat(dhub, A, {A: _main()})
    clock[0] += 10
    _stop_check(dhub, A, branch="b" * 500, subject="s" * 500, dirty="1")
    with dhub.store.lock:
        r = dhub.store.db.execute("SELECT * FROM git_facts ORDER BY id DESC LIMIT 1").fetchone()
    assert len(r["branch"]) == len(r["subject"]) == 200 and r["dirty"] == 1
