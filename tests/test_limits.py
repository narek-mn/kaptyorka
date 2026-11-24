"""Usage limits per account (limits.py): an account at its limit is marked from a session's
report, the bridge's reading or the manager's launch error; Machines and `machines` show it,
work requests go elsewhere, and the manager waits for the reset without a crash. A fake clock."""
import json
import time
from types import SimpleNamespace

import pytest

from helpers import call, person, run
from test_hub_manager import (HOST, admin_bridge, beat, clock, hub, manager_online,  # noqa: F401
                              page_texts, state)
from kaptyorka import hubstate, identity, limits, paths
from kaptyorka.identity import Identity
from kaptyorka import http
from kaptyorka.store import Store

H = 3600


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "hub.db")
    now = [1_000_000.0]
    s.clock = lambda: now[0]
    s.now = now
    yield s
    s.close()


def test_mark_words_and_expiry(store):
    now = store.now[0]
    assert limits.mark(store, "box-1", "u", "claude-code", now + 2 * H, "session", now)
    assert not limits.mark(store, "box-1", "u", "claude-code", now + 2 * H + 30, "session", now)
    a = limits.get(store, "box-1", "u", "claude-code", now)
    assert a["known"] and a["since"] == now
    assert limits.words(a) == f"at its Claude Code usage limit until {limits.hm(now + 2 * H)}"
    assert limits.get(store, "box-1", "u", "codex", now) is None  # another tool's login
    assert limits.get(store, "box-1", "v", "claude-code", now) is None  # another OS account
    assert limits.active(store, now + 2 * H + 1) == []  # past the reset: gone
    limits.mark(store, "box-1", "u", "codex", None, "session", now)
    a = limits.get(store, "box-1", "u", "codex", now)
    assert not a["known"] and limits.words(a) == "at its Codex usage limit (reset time unknown)"


def test_work_goes_elsewhere_with_a_plain_reason(store):
    now = store.now[0]
    limits.mark(store, "box-1", "u", "claude-code", now + H, "session", now)
    usable = [{"host": "box-1", "os_user": "u"}, {"host": "box-2", "os_user": "u"}]
    reasons = []
    assert limits.skip_limited(store, usable, reasons, "claude-code", now) == [usable[1]]
    assert reasons == [f"box-1 (u): at its Claude Code usage limit until {limits.hm(now + H)};"
                       f" pick another machine or wait until {limits.hm(now + H)}"]
    assert limits.skip_limited(store, usable, [], "codex", now) == usable


def test_readings_mark_and_clear(store):
    now = store.now[0]
    limits.from_facts(store, "box-1", "u", {"claude-code": {"until": now + H}, "x": {}}, now)
    assert limits.get(store, "box-1", "u", "claude-code", now)["until"] == now + H
    limits.from_facts(store, "box-1", "u", {"claude-code": {"ok": True}}, now)
    assert limits.get(store, "box-1", "u", "claude-code", now) is None
    limits.from_facts(store, "box-1", "u", {"codex": {"limited": True}}, now)
    assert not limits.get(store, "box-1", "u", "codex", now)["known"]


def test_claude_reading_from_the_status_line(tmp_path, monkeypatch):
    monkeypatch.setenv("KAPTYORKA_HOME", str(tmp_path))
    now = time.time()
    rl = {"five_hour": {"used_percentage": 100, "resets_at": now + 600},
          "seven_day": {"used_percentage": 41.2, "resets_at": now + 86400}}
    limits.save_statusline({"rate_limits": rl}, now)
    assert limits.claude_reading(now) == {"until": now + 600}
    limits.save_statusline({"rate_limits": {"five_hour": {"used_percentage": 20, "resets_at": now + 600}}},
                           now + 120)
    assert limits.claude_reading(now + 120) == {"ok": True}
    assert limits.claude_reading(now + 120 + limits.FRESH_S + 1) is None  # stale says nothing
    limits.save_statusline({"model": {}}, now)  # no rate_limits (an API key login): nothing
    assert limits.claude_reading(now + 120) == {"ok": True}


def test_the_status_line_keeps_rate_limits(tmp_path, monkeypatch):
    monkeypatch.setenv("KAPTYORKA_HOME", str(tmp_path))
    data = {"session_id": "s1", "cwd": "/w", "rate_limits": {"five_hour": {"used_percentage": 100,
                                                                         "resets_at": time.time() + 60}}}
    hubstate.statusline("", json.dumps(data))
    assert json.loads((tmp_path / limits.FILE).read_text())["claude-code"]["rate_limits"] == \
        data["rate_limits"]


def test_codex_reading():
    now = 1000.0
    snap = {"ordinaryUsageAllowed": True, "rateLimits": {
        "primary": {"usedPercent": 100, "resetsAt": 4600, "windowDurationMins": 300},
        "secondary": {"usedPercent": 30, "resetsAt": 90000}}}
    assert limits.codex_reading(snap, now) == {"until": 4600.0}
    snap["rateLimits"]["primary"]["usedPercent"] = 50
    assert limits.codex_reading(snap, now) == {"ok": True}
    assert limits.codex_reading({"ordinaryUsageAllowed": False, "rateLimits": {}}, now) == \
        {"limited": True}
    assert limits.codex_reading(None, now) is None


def test_an_unknown_reset_clears_once_the_account_works_again(store):
    now = store.now[0]
    pid, token = person(store, "alice")
    store.heartbeat("box-1", [{"key": "claude:a", "tool": "claude-code", "project": "proj-a",
                               "write_s": 0}],
                    principal=store.authenticate(token), facts={"host": "box-1", "os_user": "u"})
    limits.mark(store, "box-1", "u", "claude-code", None, "session", now)
    limits.tick(store, now)
    assert limits.get(store, "box-1", "u", "claude-code", now) is not None
    store.now[0] += limits.WORKED_S + 10
    store.heartbeat("box-1", [{"key": "claude:a", "tool": "claude-code", "project": "proj-a",
                               "write_s": 0}],
                    principal=store.authenticate(token), facts={"host": "box-1", "os_user": "u"})
    limits.tick(store, store.now[0])
    assert limits.get(store, "box-1", "u", "claude-code", store.now[0]) is None


# ---- on the hub ----

def team_session(hub):
    """A team machine jet-1 (OS user u) with one Claude Code session in proj-a."""
    s = hub.store
    s.project_register("proj-a", by="test")
    jet, _ = s.team_machine_add("jet-1", "test")
    s.grant_project("proj-a", jet, "write", "test")
    token, _ = s.invite_redeem(s.invite_create(jet), "jet-1", host="jet-1", os_user="u")
    body = {"machine": "jet-1", "tools": [], "facts": {"host": "jet-1", "os_user": "u"},
            "sessions": [{"key": "claude:j1", "tool": "claude-code", "project": "proj-a",
                          "cwd": "/home/u/proj-a"}]}
    assert http.request("POST", hub.url + "/bridge/heartbeat", token, body)[0] == 200
    return token


def test_a_sessions_limit_marks_its_account_for_machines_and_the_manager(hub, clock):
    token = team_session(hub)
    resets = clock[0] + 3 * H
    ident = Identity("claude:j1", "jet-1", "claude-code", "proj-a", "/home/u/proj-a")
    assert http.request("POST", hub.url + "/api/session-event", token,
                        {"kind": "failure", "error": "rate_limit", "resets_at": resets},
                        headers=ident.headers())[0] == 200
    line = f"u: at its Claude Code usage limit until {limits.hm(resets)}"
    _, admin = person(hub.store, "root", "admin")
    status, rows = http.request("GET", hub.url + "/api/team/machines", admin)
    assert status == 200 and [r["limits"] for r in rows if r["host"] == "jet-1"] == [[line]]
    mgr, me = manager_online(hub)
    got = {m["host"]: m for m in run(call(mgr, me, "machines"))}
    assert got["jet-1"]["limits"] == [line]


def test_the_bridge_reading_clears_it(hub, clock):
    token = team_session(hub)
    body = {"machine": "jet-1", "tools": [], "sessions": [],
            "facts": {"host": "jet-1", "os_user": "u", "limits": {"claude-code": {"until": clock[0] + H}}}}
    http.request("POST", hub.url + "/bridge/heartbeat", token, body)
    assert limits.get(hub.store, "jet-1", "u", "claude-code", clock[0]) is not None
    body["facts"]["limits"] = {"claude-code": {"ok": True}}
    http.request("POST", hub.url + "/bridge/heartbeat", token, body)
    assert limits.get(hub.store, "jet-1", "u", "claude-code", clock[0]) is None


# ---- the manager ----

def test_the_manager_waits_for_its_reset_and_it_is_no_crash(hub, clock, monkeypatch):
    b = admin_bridge(hub)
    hub.mm.set_enabled(True, "test")
    resets = clock[0] + 2 * H
    limits.mark(hub.store, HOST, "u", "claude-code", resets, "session", clock[0])
    hub.tick()
    assert state(hub) is None  # not launched
    hm = limits.hm(resets)
    _, admin = person(hub.store, "boss", "admin")
    st = http.request("GET", hub.url + "/api/settings", admin)[1]
    assert (st["manager_state"], st["manager_reason"]) == ("waiting", f"waiting for its usage limit, resets {hm}")
    told = [t for t in page_texts(hub) if "usage limit" in t]
    assert told == [f"The manager's account on {HOST} is at its Claude Code usage limit until {hm};"
                    " the manager waits and comes back by itself then. Messages to it wait for it."]
    hub.tick()
    assert len([t for t in page_texts(hub) if "usage limit" in t]) == 1  # once
    clock[0] = resets + 1
    beat(hub, b)
    hub.tick()
    assert state(hub) == "starting" and hub.mm.crashes() == []


def test_a_launch_that_fails_on_the_limit_marks_the_account(hub, clock):
    admin_bridge(hub)
    hub.mm.set_enabled(True, "test")
    hub.tick()
    assert state(hub) == "starting"
    with hub.store.lock:
        hub.store.db.execute("UPDATE workers SET state='failed', closed_at=?, error=? WHERE name='manager'",
                             (clock[0], "claude: Claude usage limit reached. Your limit resets 7pm"))
    clock[0] += 10
    hub.tick()
    lim = limits.get(hub.store, HOST, "u", "claude-code", clock[0])
    assert lim is not None and lim["source"] == "manager launch"
    assert hub.mm.crashes() == []  # the failed run is not a crash


def test_people_who_write_to_it_meanwhile_hear_one_line(hub, clock):
    admin_bridge(hub)
    mgr, me = manager_online(hub)
    resets = clock[0] + H
    limits.mark(hub.store, HOST, "u", "claude-code", resets, "session", clock[0])
    hub.tick()  # the hub sees it wait
    hub.store.project_register("proj-a", by="test")
    pid, token = person(hub.store, "alice")
    hub.store.grant_project("proj-a", pid, "write", "test")
    alice = SimpleNamespace(url=hub.url, token=token)
    A = {"key": "k:alice", "machine": "box-1", "tool": "codex", "project": "proj-a"}
    out = run(call(alice, A, "send", to=hub.store.agent("k:mgr").name, text="please start a worker"))
    assert out.endswith(f"; the manager is at its usage limit until {limits.hm(resets)}; your message waits")
    # at the reset, the manager still running hears it once
    clock[0] = resets + 1
    run(call(mgr, me, "inbox"))
    hub.tick()
    assert limits.BACK in [m["text"] for m in run(call(mgr, me, "inbox"))]


def test_the_managers_own_hook_reports_its_limit(monkeypatch):
    """The manager's StopFailure goes to the hub too (it is no team session, but its account's
    limit decides when it comes back)."""
    from kaptyorka import hook

    sent = []
    monkeypatch.setattr(hook.http, "request", lambda *a, **k: sent.append((a, k)))
    monkeypatch.setattr(hook.paths, "in_manager_folder", lambda c: True)
    cfg = {"hub": "http://h", "token": "M", "machine": "box-1"}
    hook._report_end("failure", "m1", {"error": "rate_limit", "cwd": "/k/manager",
                                       "last_assistant_message": "limit reached, resets 7pm"}, cfg)
    [(args, kw)] = sent
    assert args[3]["error"] == "rate_limit" and kw["headers"]["X-Kaptyorka-Key"] == "claude%3Am1"


def test_an_unknown_reset_lets_the_manager_try_after_an_hour(hub, clock):
    """No reset time and no manager session to clear the mark: it waits an hour, not five,
    then a launch tries; one that fails on the limit's text waits the next hour."""
    b = admin_bridge(hub)
    hub.mm.set_enabled(True, "test")
    limits.mark(hub.store, HOST, "u", "claude-code", None, "session", clock[0])
    hub.tick()
    assert state(hub) is None
    clock[0] += H - 60
    beat(hub, b)
    hub.tick()
    assert state(hub) is None  # still within the hour
    clock[0] += 61
    beat(hub, b)
    hub.tick()
    assert state(hub) == "starting"  # the try
    with hub.store.lock:
        hub.store.db.execute("UPDATE workers SET state='failed', closed_at=?, error=? WHERE"
                             " name='manager'", (clock[0], "claude: usage limit reached"))
    clock[0] += 10
    beat(hub, b)
    hub.tick()
    lim = limits.get(hub.store, HOST, "u", "claude-code", clock[0])
    assert lim is not None and lim["since"] == clock[0] and state(hub) == "failed"
    assert len([t for t in page_texts(hub) if t.startswith("The manager's account")]) == 1  # once
    clock[0] += H - 60
    beat(hub, b)
    hub.tick()
    assert state(hub) == "failed"  # the next hour
    clock[0] += 61
    beat(hub, b)
    hub.tick()
    assert state(hub) == "starting" and hub.mm.crashes() == []


def test_a_known_reset_still_waits_for_it(hub, clock):
    b = admin_bridge(hub)
    hub.mm.set_enabled(True, "test")
    limits.mark(hub.store, HOST, "u", "claude-code", clock[0] + 3 * H, "session", clock[0])
    clock[0] += 2 * H
    beat(hub, b)
    hub.tick()
    assert state(hub) is None
