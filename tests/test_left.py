"""Sessions left behind (left.py): truthful idle time, who started a session, the parent's Stop
check, "left by", the daily line and the narrow auto-close.
docs/superpowers/specs/2026-11-24-left-sessions.md"""
import json
import os
import time
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from helpers import call, run, write_session_file
from test_hub_manager import clock, hub  # noqa: F401 (fixtures)
from test_hub_workers import bridge, heartbeat, ident, member
from kaptyorka import hook, left
from kaptyorka import bridge as bridge_mod
from kaptyorka.adapters import claude as claude_mod
from kaptyorka.adapters.base import Session
from kaptyorka.adapters.claude import ClaudeAdapter
from kaptyorka.hub_feed import feed
from kaptyorka.workers import HUB_KEY

DAY = 86400
PARENT = "aaaaaaaa-1111-2222-3333-444444444444"
CHILD = "bbbbbbbb-1111-2222-3333-444444444444"


def stamp(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).isoformat().replace("+00:00", "Z")


def transcript(home, sid, cwd, lines):
    d = home / ".claude" / "projects" / cwd.replace("/", "-")
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{sid}.jsonl"
    p.write_text("".join(json.dumps(r) + "\n" for r in lines))
    return p


def derived(child, parent, via="launch"):
    return {"type": "derived-from", "v": 1, "sessionId": child,
            "sessions": [{"type": "claude_code_session", "id": parent, "via": via, "started": child}]}


# ---------- (a) truthful idle time ----------

def test_last_real_skips_what_claude_code_writes_while_nobody_works(tmp_path):
    now = time.time()
    real = now - 3 * DAY
    lines = [
        {"type": "user", "message": {"content": "fix the bug"}, "timestamp": stamp(real - 60)},
        {"type": "assistant", "message": {"content": []}, "timestamp": stamp(real)},
        {"type": "system", "subtype": "away_summary", "timestamp": stamp(now - DAY)},
        {"type": "system", "subtype": "compact_boundary", "timestamp": stamp(now - 3600)},
        {"type": "user", "isCompactSummary": True, "message": {"content": "summary"},
         "timestamp": stamp(now - 3600)},
        {"type": "user", "isMeta": True, "message": {"content": "Compacted while idle"},
         "timestamp": stamp(now - 3599)},
        {"type": "user", "origin": {"kind": "task-notification"}, "message": {"content": "done"},
         "timestamp": stamp(now - 60)},
        {"type": "user", "isSidechain": True, "message": {"content": "x"}, "timestamp": stamp(now)},
        {"type": "last-prompt", "lastPrompt": "fix the bug"},
        {"type": "custom-title", "customTitle": "t"},
    ]
    p = tmp_path / "t.jsonl"
    p.write_text("".join(json.dumps(r) + "\n" for r in lines))
    assert claude_mod.last_real(p, p.stat().st_size) == pytest.approx(real, abs=1)
    p.write_text(json.dumps(lines[2]) + "\n")
    assert claude_mod.last_real(p, p.stat().st_size) is None


def test_idle_s_from_the_last_real_line_and_subagents_count(tmp_path):
    home, cwd, now = tmp_path, "/w/proj-a", time.time()
    write_session_file(home, os.getpid(), cwd=cwd, session_id=PARENT)
    p = transcript(home, PARENT, cwd, [
        {"type": "assistant", "message": {"content": []}, "timestamp": stamp(now - 2 * DAY)},
        {"type": "system", "subtype": "away_summary", "timestamp": stamp(now)}])
    os.utime(p, (now, now))  # the file is fresh; the session is not
    s, = ClaudeAdapter(home=home, log=lambda _: None).sessions()
    assert s.idle_s == pytest.approx(2 * DAY, abs=60)
    sub = p.parent / PARENT / "subagents"
    sub.mkdir(parents=True)
    (sub / "agent-1.jsonl").write_text("{}\n")
    os.utime(sub / "agent-1.jsonl", (now - 600, now - 600))
    s, = ClaudeAdapter(home=home, log=lambda _: None).sessions()
    assert s.idle_s == pytest.approx(600, abs=60)


# ---------- (b) who started it ----------

def test_parent_oneshot_parent_gone_and_launched(tmp_path):
    home, cwd = tmp_path, "/w/proj-a"
    write_session_file(home, os.getpid(), cwd=cwd, session_id=CHILD, extra={"kind": "bg"})
    transcript(home, CHILD, cwd, [derived(CHILD, PARENT), {"type": "user", "entrypoint": "cli",
                                                           "message": {"content": "go"}}])
    a = ClaudeAdapter(home=home, log=lambda _: None)
    s, = a.sessions()
    assert (s.parent, s.oneshot, s.helper, s.parent_gone) == (f"claude:{PARENT}", False, True, True)
    assert a.launched == {f"claude:{CHILD}"}
    # its parent runs: not gone
    write_session_file(home, os.getppid(), cwd=cwd, session_id=PARENT)
    gone = {x.key: x.parent_gone for x in a.sessions()}
    assert gone[f"claude:{CHILD}"] is False


def test_a_resume_names_itself_and_a_run_with_nothing_is_todays_session(tmp_path, monkeypatch):
    home, cwd = tmp_path, "/w/proj-a"
    monkeypatch.setattr(claude_mod, "tool_ancestor", lambda pid: "")
    monkeypatch.setattr(claude_mod, "notes_parent", lambda sid: "")
    write_session_file(home, os.getpid(), cwd=cwd, session_id=CHILD)
    transcript(home, CHILD, cwd, [derived(CHILD, CHILD, via="resume"),
                                  {"type": "user", "entrypoint": "sdk-cli", "message": {"content": "x"}}])
    s, = ClaudeAdapter(home=home, log=lambda _: None).sessions()
    assert (s.parent, s.oneshot, s.helper, s.parent_gone) == ("", True, False, False)


def test_notes_dir_names_the_parent(tmp_path):
    notes = tmp_path / f"claude-{os.getuid()}" / "-w-proj-a" / PARENT / "started"
    notes.mkdir(parents=True)
    (notes / f"{CHILD}.x1y2").write_text("")
    assert claude_mod.notes_parent(CHILD, tmp=tmp_path) == PARENT
    assert claude_mod.notes_parent("cccccccc", tmp=tmp_path) == ""


def child_row(key, parent="claude:p1", idle_s=None, background=True, gone=False, oneshot=False):
    return {"key": key, "tool": "claude-code", "project": "proj-a", "cwd": "/w/proj-a",
            "background": background, "idle_s": idle_s, "parent": parent, "oneshot": oneshot,
            "helper": True, "parent_gone": gone}


def plain(key, idle_s=None):
    return {"key": key, "tool": "claude-code", "project": "proj-a", "cwd": "/w/proj-a",
            "idle_s": idle_s}


def test_fresh_children_are_not_listed_until_two_minutes_or_a_message(hub, clock):
    hub.store.project_register("proj-a", by="test")
    pid, client = member(hub, "alice")
    b = bridge(hub, pid, sessions=[plain("claude:p1"), child_row("claude:c1"),
                                   child_row("claude:o1", parent="", oneshot=True)])
    principal = hub.store.authenticate(b.token)
    keys = {a["id"] for a in feed(hub.store, principal)["agents"]}
    assert hub.store.fresh_helpers() == {"claude:c1", "claude:o1"} and len(keys) == 1
    me = ident("claude:p1", machine="box-1", tool="claude-code")
    assert [r["you"] for r in run(call(client, me, "who"))] == [True]
    run(call(client, me, "send", to=hub.store.agent("claude:c1").name, text="hi"))
    assert hub.store.fresh_helpers() == {"claude:o1"}
    clock[0] += left.FRESH_S + 1
    heartbeat(hub, b.token, "box-1", [plain("claude:p1"), child_row("claude:c1"),
                                      child_row("claude:o1", parent="", oneshot=True)])
    assert hub.store.fresh_helpers() == set()
    rows = {a["name"]: a for a in feed(hub.store, principal)["agents"]}
    assert len(rows) == 3
    kid = rows[hub.store.agent("claude:c1").name]
    assert kid["parent"] == rows[hub.store.agent("claude:p1").name]["id"] and "left_by" not in kid


def test_an_older_bridge_leaves_lineage_alone(hub):
    hub.store.project_register("proj-a", by="test")
    pid, _ = member(hub, "alice")
    b = bridge(hub, pid, sessions=[child_row("claude:c1")])
    heartbeat(hub, b.token, "box-1", [plain("claude:c1")])  # no "parent": unchanged
    assert hub.store.agent("claude:c1").parent == "claude:p1"


# ---------- (c) the parent's Stop check ----------

@pytest.fixture
def stop_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("KAPTYORKA_HOME", str(tmp_path / "kh"))
    monkeypatch.delenv("CLAUDE_CODE_CHILD_NOTES_DIR", raising=False)
    write_session_file(tmp_path, os.getpid(), session_id=CHILD, name="helper-1", extra={"kind": "bg"})
    notes = tmp_path / "notes" / PARENT / "started"
    notes.mkdir(parents=True)
    return tmp_path, notes


def stop(**kw):
    return hook._children_check({"session_id": PARENT, "cwd": "/w/proj-a", **kw})


def test_stop_with_a_running_child_blocks_once_per_set(stop_home, monkeypatch):
    home, notes = stop_home
    monkeypatch.setenv("CLAUDE_CODE_CHILD_NOTES_DIR", str(notes))
    assert stop() == ""  # it started none
    (notes / f"{CHILD}.ab12").write_text("")
    out = json.loads(stop())
    assert out["decision"] == "block"
    assert out["reason"] == ("You started helper-1 (`claude stop bbbbbbbb`), still running: stop"
                             " them or say in one line why they must keep running.")
    assert stop() == ""  # the same set: never again
    assert stop(stop_hook_active=True) == ""


def test_stop_check_falls_back_to_derived_from(stop_home):
    home, _ = stop_home
    transcript(home, CHILD, "/w/proj-a", [derived(CHILD, PARENT)])
    assert json.loads(stop())["decision"] == "block"


def test_stop_check_skips_workers_and_the_manager(stop_home, monkeypatch):
    home, notes = stop_home
    monkeypatch.setenv("CLAUDE_CODE_CHILD_NOTES_DIR", str(notes))
    (notes / f"{CHILD}.ab12").write_text("")
    monkeypatch.setattr(hook.offhub, "load_team", lambda: (None, {f"claude:{CHILD}"}))
    assert stop() == ""  # its child is a hub worker: the hub closes those
    monkeypatch.setattr(hook.offhub, "load_team", lambda: (None, {f"claude:{PARENT}"}))
    assert stop() == ""  # it is a worker itself
    monkeypatch.setattr(hook.offhub, "load_team", lambda: (None, set()))
    monkeypatch.setattr(hook.paths, "in_manager_folder", lambda c: True)
    assert stop() == ""


# ---------- (a)(d) the daily line and "left by" ----------

def hub_lines(hub):
    with hub.store.lock:
        return [(r["to_key"], r["text"]) for r in hub.store.db.execute(
            "SELECT to_key, text FROM messages WHERE sender_key=? ORDER BY id", (HUB_KEY,))]


def test_daily_idle_line_once_a_day_to_the_owner(hub, clock):
    hub.store.project_register("proj-a", by="test")
    pid, _ = member(hub, "alice")
    beat = [plain("claude:p1", 10), plain("claude:i1", 3 * DAY + 5)]
    b = bridge(hub, pid, sessions=beat)
    hub.tick()
    name = hub.store.agent("claude:i1").name
    assert hub_lines(hub) == [("claude:p1", f"1 session of yours on box-1 has done nothing for"
                               f" 3 days: {name}; close them or leave them.")]
    hub.tick()
    assert len(hub_lines(hub)) == 1
    clock[0] += DAY + 1
    heartbeat(hub, b.token, "box-1", beat)
    hub.tick()
    assert len(hub_lines(hub)) == 2


def test_the_page_hears_it_when_no_other_session_runs(hub):
    hub.store.project_register("proj-a", by="test")
    pid, _ = member(hub, "alice")
    bridge(hub, pid, sessions=[plain("claude:i1", 2 * DAY)])
    hub.tick()
    assert [k for k, _ in hub_lines(hub)] == ["web:alice"]


def test_left_by_shows_and_the_owner_hears_once(hub, clock):
    hub.store.project_register("proj-a", by="test")
    pid, _ = member(hub, "alice")
    b = bridge(hub, pid, sessions=[plain("claude:p1"), plain("claude:o1"), child_row("claude:c1")])
    parent = hub.store.agent("claude:p1").name
    clock[0] += left.FRESH_S + 1
    heartbeat(hub, b.token, "box-1", [plain("claude:o1"), child_row("claude:c1", gone=True)],
              extra={"tools": ["claude-code"]})  # complete: its parent ended
    hub.tick()
    child = hub.store.agent("claude:c1").name
    assert hub_lines(hub) == [("claude:o1", f"Session `{child}` on box-1 was started by `{parent}`,"
                               " which has ended; it still runs. To stop it: `claude stop c1`"
                               " on box-1.")]
    hub.tick()
    assert len(hub_lines(hub)) == 1
    row = {a["name"]: a for a in feed(hub.store, hub.store.authenticate(b.token))["agents"]}[child]
    assert row["left_by"] == parent


# ---------- (e) the narrow auto-close ----------

def close_asked(hub, b, sessions, extra=None):
    status, body = heartbeat(hub, b.token, "box-1", sessions, extra=extra)
    assert status == 200
    return body.get("close_left", [])


def test_the_hub_asks_to_close_only_the_narrow_case(hub, clock):
    hub.store.project_register("proj-a", by="test")
    pid, client = member(hub, "alice")
    b = bridge(hub, pid, sessions=[])
    old = 2 * DAY
    assert close_asked(hub, b, [child_row("claude:c1", idle_s=old, gone=True)]) == ["claude:c1"]
    assert close_asked(hub, b, [child_row("claude:c1", idle_s=DAY - 60, gone=True)]) == []
    assert close_asked(hub, b, [child_row("claude:c1", idle_s=old)]) == []  # its parent runs
    assert close_asked(hub, b, [child_row("claude:c1", idle_s=old, gone=True,
                                          background=False)]) == []  # interactive
    assert close_asked(hub, b, [child_row("claude:c1", idle_s=old, gone=True, parent="")]) == []
    hub.store.set_close_left(False, "test")
    assert close_asked(hub, b, [child_row("claude:c1", idle_s=old, gone=True)]) == []
    hub.store.set_close_left(True, "test")
    heartbeat(hub, b.token, "box-1", [plain("claude:p2"), child_row("claude:c1", idle_s=old, gone=True)])
    run(call(client, ident("claude:p2", machine="box-1", tool="claude-code"), "send",
             to=hub.store.agent("claude:c1").name, text="hi"))
    assert close_asked(hub, b, [child_row("claude:c1", idle_s=old, gone=True)]) == []  # a message


def test_a_closed_session_is_logged_and_in_the_daily_line(hub):
    hub.store.project_register("proj-a", by="test")
    pid, _ = member(hub, "alice")
    b = bridge(hub, pid, sessions=[plain("claude:p2"), child_row("claude:c1", idle_s=2 * DAY, gone=True)])
    name = hub.store.agent("claude:c1").name
    heartbeat(hub, b.token, "box-1", [plain("claude:p2")], extra={"closed": ["claude:c1"]})
    with hub.store.lock:
        logged = [r["detail"] for r in hub.store.db.execute(
            "SELECT detail FROM log WHERE kind='left_closed'")]
    assert logged == [f"{name} on box-1"]
    hub.tick()
    assert ("claude:p2", f"Closed on box-1 (started by a session that ended, idle over a day, no"
            f" hub message): {name}.") in hub_lines(hub)


def test_close_left_setting_on_the_settings_route(hub):
    from helpers import person
    from kaptyorka import http

    _, token = person(hub.store, "root", "admin")
    assert http.request("GET", hub.url + "/api/settings", token)[1]["close_left"] is True
    status, body = http.request("POST", hub.url + "/api/settings", token, {"close_left": False})
    assert status == 200 and body["close_left"] is False and hub.store.close_left() is False
    assert hub.store.retention_days() == 180


class Runner:
    def __init__(self, code=0):
        self.calls, self.code = [], code

    def __call__(self, cmd, **kw):
        self.calls.append(cmd)
        return SimpleNamespace(returncode=self.code, stdout="", stderr="")


def test_the_bridge_checks_again_and_stops_only_with_claude_stop(tmp_path, monkeypatch):
    monkeypatch.setenv("KAPTYORKA_HOME", str(tmp_path / "kh"))
    (tmp_path / "kh").mkdir()
    key = f"claude:{CHILD}"
    s = Session(key, "claude-code", "proj-a", "idle", "helper-1", "/w/proj-a", True,
                idle_s=2 * DAY, parent=f"claude:{PARENT}", helper=True, parent_gone=True)
    claude = SimpleNamespace(prefix="claude", tool="claude-code", launched={key},
                             sessions=lambda: [s])
    runner, lines = Runner(), []
    b = bridge_mod.Bridge({"hub": "http://127.0.0.1:9", "token": "t", "machine": "box-1"},
                          [claude], lines.append, run=runner)
    b.hub = {"close_left": [key]}
    for changed in ({"idle_s": 3600}, {"background": False}, {"parent_gone": False}):
        b._close_left([Session(**{**s.__dict__, **changed})])
    claude.launched = set()
    b._close_left([s])  # not started via launch here
    assert runner.calls == []
    claude.launched = {key}
    b._close_left([s])
    assert runner.calls == [["claude", "stop", "bbbbbbbb"]] and b._closed == [key]
    assert any(line.startswith("closed left session helper-1") for line in lines)
    b._close_left([s])
    assert len(runner.calls) == 1  # once
