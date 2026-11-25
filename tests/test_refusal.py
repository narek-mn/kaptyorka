"""The manager's reply stopped by the model's safety filter (refusal.py): the transcript
marker, the bridge's report, a fresh manager, the question given once more, the stop after
too many, and what the admin sees. No real agent tool runs."""
import json
from types import SimpleNamespace

import pytest

from helpers import call, person, run
from test_bridge import FakeAdapter
from test_bridge import no_real_commands  # noqa: F401
from test_hub_manager import HOST, admin_bridge, beat, clock, hub, page_texts, state  # noqa: F401
from test_hub_workers import ident
from kaptyorka import bridge as bridge_mod
from kaptyorka import compat, http, limits, manager, paths, refusal
from kaptyorka.adapters.base import Session
from kaptyorka.adapters.claude import ClaudeAdapter, last_refusal

def at(ts):
    from datetime import datetime
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()


SID = "11111111-2222-3333-4444-555555555555"


def lines(*recs):
    return "".join(json.dumps(r) + "\n" for r in recs)


def blocked_turn(ts):
    """The three lines Claude Code writes for a blocked turn (their shape only)."""
    return [{"type": "user", "isSidechain": False, "timestamp": ts,
             "message": {"role": "user", "content": "Your response above was stopped by a safety"
                         " classifier — this is not a tool or API error."}},
            {"type": "system", "subtype": "model_refusal_no_fallback", "isSidechain": False,
             "level": "warning", "content": "", "timestamp": ts},
            {"type": "assistant", "isSidechain": False, "timestamp": ts,
             "message": {"role": "assistant", "model": "<synthetic>",
                         "content": [{"type": "text", "text": "API Error: the model's safeguards"
                                      " flagged this message"}]}}]


def transcript(home, cwd, text):
    d = home / ".claude" / "projects" / paths_dash(cwd)
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{SID}.jsonl"
    p.write_text(text)
    return p


def paths_dash(cwd):
    import re
    return re.sub(r"[^A-Za-z0-9]", "-", cwd)


# ---- the marker ----

def test_the_reader_finds_the_newest_marker_and_skips_subagents(tmp_path):
    p = tmp_path / "t.jsonl"
    p.write_text(lines({"type": "user", "timestamp": "2026-11-25T08:00:00.000Z"}))
    assert last_refusal(p, p.stat().st_size) is None
    p.write_text(lines(*blocked_turn("2026-11-25T08:08:13.871Z"),
                       *blocked_turn("2026-11-25T08:10:24.555Z"),
                       {"type": "system", "subtype": "model_refusal_no_fallback",
                        "isSidechain": True, "timestamp": "2026-11-25T09:00:00.000Z"}))
    assert last_refusal(p, p.stat().st_size) == pytest.approx(at("2026-11-25T08:10:24.555Z"))


def test_the_adapter_reads_it_again_only_when_the_transcript_changed(home):
    cwd = "/w/manager"
    p = transcript(home, cwd, lines({"type": "user", "timestamp": "2026-11-25T08:00:00.000Z"}))
    a = ClaudeAdapter(home=home)
    assert a.refused(f"claude:{SID}", cwd) is None
    p.write_text(p.read_text() + lines(*blocked_turn("2026-11-25T08:08:13.871Z")))
    assert a.refused(f"claude:{SID}", cwd) == pytest.approx(at("2026-11-25T08:08:13.871Z"))


def test_the_probe_checks_the_recorded_line_and_lines_on_disk(home, monkeypatch):
    ctx = compat.Ctx(home, None)
    assert compat.p_claude_refusal(ctx) == (compat.OK, "")
    bad = home / "bad.jsonl"
    bad.write_text(lines({"type": "system", "subtype": "model_refusal_no_fallback"}))  # no time
    monkeypatch.setattr(compat, "_transcripts", lambda ctx: [bad])
    assert compat.p_claude_refusal(ctx)[0] == compat.BROKEN


def test_the_bridge_reports_it_for_the_manager_session_only(home, monkeypatch):
    folder = str(paths.home() / manager.DIR)
    fake = FakeAdapter("claude-code", "claude", [Session(f"claude:{SID}", "claude-code", "manager",
                                                         cwd=folder),
                                                 Session("claude:other", "claude-code", "proj-a",
                                                         cwd="/w/proj-a")])
    fake.refused = lambda key, cwd: 1234.5
    sent = []

    def spy(method, url, token, body=None, *a, **kw):  # no hub: only what the bridge sends
        sent.append(body)
        return 200, {}
    monkeypatch.setattr(bridge_mod.http, "request", spy)
    bridge_mod.Bridge({"hub": "http://127.0.0.1:9", "token": "T", "machine": "box-1"}, [fake],
                      bridge_mod.log).heartbeat()
    beat = [b for b in sent if isinstance(b, dict) and "sessions" in b][0]
    assert beat["manager_refused"] == {f"claude:{SID}": 1234.5}


# ---- the hub ----

def up(hub, b, clock, key="claude:m1"):
    """The launched manager comes up as `key`; returns its client and identity."""
    token = json.loads(hub.config.read_text())["token"]  # the manager's own account
    client = SimpleNamespace(url=hub.url, token=token)
    me = ident(key, project="manager")
    run(call(client, me, "inbox"))
    assert hub.ws.link("manager", key, b.device_id)
    return client, me


def report(hub, b, key, at):
    body = {"machine": HOST, "sessions": [], "tools": [], "manager_refused": {key: at},
            "facts": {"host": HOST, "os_user": "u", "version": "x"}}
    assert http.request("POST", hub.url + "/bridge/heartbeat", b.token, body)[0] == 200


def close_acked(hub, b):
    """The bridge stops the session it was told to close."""
    status, items = http.request("GET", f"{hub.url}/bridge/poll?machine={HOST}", b.token)
    assert status == 200, (status, items)
    closes = [i for i in items if i.get("launch") and i.get("kind") == "close"] or \
        [i for i in items if i.get("launch") and "close" in json.dumps(i)]
    assert closes, items
    for i in closes:
        assert http.request("POST", hub.url + "/bridge/ack", b.token,
                            {"id": i["id"], "ok": True, "launch": True})[0] == 204


def ask(hub, text, key="claude:m1"):
    """alice asks the manager; the message is delivered (pushed)."""
    pid, token = person(hub.store, "alice")
    hub.store.project_register("proj-a", by="test")
    hub.store.grant_project("proj-a", pid, "write", "test")
    alice = SimpleNamespace(url=hub.url, token=token)
    A = {"key": "k:alice", "machine": "box-1", "tool": "codex", "project": "proj-a"}
    run(call(alice, A, "send", to=hub.store.agent(key).name, text=text))
    with hub.store.lock:
        hub.store.db.execute("UPDATE messages SET push_state='pushed', read_at=created WHERE sender_key='k:alice'")
    return alice, A


def to(hub, key):
    with hub.store.lock:
        return [(r["sender_key"], r["text"]) for r in hub.store.db.execute(
            "SELECT sender_key, text FROM messages WHERE to_key=? ORDER BY id", (key,))]


def fresh(hub, b, clock, key):
    """Ticks until the blocked session is closed and a fresh one is up as `key`."""
    hub.tick()  # closes the blocked session
    close_acked(hub, b)
    clock[0] += manager.RELAUNCH_S + 1
    beat(hub, b)
    hub.tick()  # launches a fresh one
    assert state(hub) == "starting", hub.mm.launch_reason(clock[0])
    up(hub, b, clock, key)
    hub.tick()  # the question goes to it


def test_a_block_starts_a_fresh_manager_that_gets_the_question_once(hub, clock):
    b = admin_bridge(hub)
    hub.mm.set_enabled(True, "test")
    hub.tick()
    up(hub, b, clock)
    alice, A = ask(hub, "please start a worker on proj-a")
    clock[0] += 5
    report(hub, b, "claude:m1", clock[0] - 1)
    fresh(hub, b, clock, "claude:m2")
    assert ("k:alice", "please start a worker on proj-a") in to(hub, "claude:m2")
    assert hub.mm.crashes() == []
    hm = limits.hm(clock[0] - manager.RELAUNCH_S - 7)
    assert f"The manager's reply was blocked by the model's safety filter at {hm}. The hub" \
        " started a fresh manager." in page_texts(hub)
    _, admin = person(hub.store, "boss", "admin")
    st = http.request("GET", hub.url + "/api/settings", admin)[1]
    assert (st["manager_state"], st["manager_reason"]) == \
        ("up", f"reply blocked by the model's safety filter at {hm}, restarted")
    # the page's feed carries the same reason, for everyone who writes to the manager
    _, member = person(hub.store, "bob")
    f = http.request("GET", hub.url + "/api/feed", member)[1]
    assert (f["manager"], f["manager_why"]) == ("up", st["manager_reason"])
    # the fresh manager is blocked on it again: no second try, alice hears one line
    with hub.store.lock:
        hub.store.db.execute("UPDATE messages SET push_state='pushed', read_at=created WHERE to_key='claude:m2'")
    clock[0] += 5
    report(hub, b, "claude:m2", clock[0] - 1)
    hub.tick()
    told = [t for k, t in to(hub, "k:alice") if k.startswith("hub:")]
    assert told == [f'{refusal.SENDER_TEXT} Your message: "please start a worker on proj-a"']
    close_acked(hub, b)
    clock[0] += manager.RELAUNCH_S + 1
    beat(hub, b)
    hub.tick()
    up(hub, b, clock, "claude:m3")
    hub.tick()
    assert not any(k == "k:alice" for k, _ in to(hub, "claude:m3"))


def test_a_hub_notice_that_led_to_a_block_is_never_given_again(hub, clock):
    b = admin_bridge(hub)
    hub.mm.set_enabled(True, "test")
    hub.tick()
    up(hub, b, clock)
    hub.tell("claude:m1", "knowledge: two notes changed")
    with hub.store.lock:
        hub.store.db.execute("UPDATE messages SET push_state='pushed', read_at=created WHERE to_key='claude:m1'")
    clock[0] += 5
    report(hub, b, "claude:m1", clock[0] - 1)
    report(hub, b, "claude:m1", clock[0] - 1)  # the same marker again: one restart
    fresh(hub, b, clock, "claude:m2")
    assert not any("knowledge:" in t for _, t in to(hub, "claude:m2"))
    assert len([t for t in page_texts(hub) if "safety filter" in t]) == 1


def test_only_the_managers_own_bridge_and_session_count(hub, clock):
    b = admin_bridge(hub)
    hub.mm.set_enabled(True, "test")
    hub.tick()
    up(hub, b, clock)
    report(hub, b, "k:someone-else", clock[0])
    assert hub.mm._get(refusal.PENDING) in (None, "")
    report(hub, b, "claude:m1", clock[0])
    assert json.loads(hub.mm._get(refusal.PENDING))["key"] == "claude:m1"


def test_blocks_that_keep_coming_stop_the_restarts_for_the_hour(hub, clock):
    b = admin_bridge(hub)
    hub.mm.set_enabled(True, "test")
    hub.tick()
    up(hub, b, clock)
    start = clock[0]
    for n in range(2, 5):  # three fresh starts
        clock[0] += 5
        report(hub, b, f"claude:m{n - 1}", clock[0] - 1)
        fresh(hub, b, clock, f"claude:m{n}")
    clock[0] += 5
    report(hub, b, "claude:m4", clock[0] - 1)
    hub.tick()  # the fourth: closed, and no fresh one this hour
    close_acked(hub, b)
    clock[0] += manager.RELAUNCH_S + 1
    beat(hub, b)
    hub.tick()
    assert state(hub) == "closed"
    until = limits.hm(start + 5 + refusal.WINDOW_S)  # an hour after the first restart
    _, admin = person(hub.store, "boss", "admin")
    st = http.request("GET", hub.url + "/api/settings", admin)[1]
    assert st["manager_state"] == "blocked"
    assert st["manager_reason"].startswith("reply blocked by the model's safety filter 4 times"
                                           " within an hour")
    assert st["manager_reason"].endswith(f"restarts stopped until {until}")
    f = http.request("GET", hub.url + "/api/feed", admin)[1]
    assert (f["manager"], f["manager_why"]) == ("blocked", st["manager_reason"])
    assert any("stopped it and starts a fresh one" in t for t in page_texts(hub))
    clock[0] = start + refusal.WINDOW_S + 2 * manager.RELAUNCH_S
    beat(hub, b)
    hub.tick()
    assert state(hub) == "starting"  # it comes back by itself after the hour


def test_a_hand_run_claude_in_the_manager_folder_never_restarts_the_manager(hub, clock):
    b = admin_bridge(hub)
    hub.mm.set_enabled(True, "test")
    hub.tick()
    client, _ = up(hub, b, clock)
    run(call(client, ident("claude:hand", project="manager"), "inbox"))  # not its worker's key
    report(hub, b, "claude:hand", clock[0])
    hub.tick()
    assert state(hub) == "up" and hub.mm._get(refusal.PENDING) == ""
    assert not any("safety filter" in t for t in page_texts(hub))
    status, items = http.request("GET", f"{hub.url}/bridge/poll?machine={HOST}", b.token)
    assert not [i for i in items if i.get("kind") == "close"]


def answering_case(hub, clock, setup):
    """alice's message, then `setup` changes it; after a block, what reaches claude:m2."""
    b = admin_bridge(hub)
    hub.mm.set_enabled(True, "test")
    hub.tick()
    client, me = up(hub, b, clock)
    ask(hub, "please start a worker on proj-a")
    setup(hub, client, me)
    clock[0] += 5
    report(hub, b, "claude:m1", clock[0] - 1)
    fresh(hub, b, clock, "claude:m2")
    return [t for k, t in to(hub, "claude:m2") if k == "k:alice"]


def test_a_message_the_manager_already_answered_is_not_given_again(hub, clock):
    def answered(hub, client, me):
        clock[0] += 1
        run(call(client, me, "send", to=hub.store.agent("k:alice").name, text="started"))
    assert answering_case(hub, clock, answered) == []


def test_a_message_not_read_yet_is_not_copied(hub, clock):
    def unread(hub, client, me):
        with hub.store.lock:
            hub.store.db.execute("UPDATE messages SET read_at=NULL, push_state='offered',"
                                 " offered_at=created"
                                 " WHERE sender_key='k:alice'")
    assert answering_case(hub, clock, unread) == []


def test_a_knock_is_never_copied(hub, clock):
    def knock(hub, client, me):
        with hub.store.lock:
            hub.store.db.execute("UPDATE messages SET kind='knock' WHERE sender_key='k:alice'")
    assert answering_case(hub, clock, knock) == []
