"""A session others depend on stopped: its hook tells the hub why (usage limit and reset time,
API error, end), the hub says so, and the manager resumes it by name, now or at the reset.
A fake clock."""
import time
from types import SimpleNamespace

import pytest
from helpers import call, call_error, person, run
from test_hub_manager import clock, hub, manager_online  # noqa: F401 (fixtures)
from test_hub_workers import heartbeat, poll

from kaptyorka import hook, http, session_end
from kaptyorka.identity import Identity

JET_SESSION = {"key": "claude:sid-jet", "tool": "claude-code", "project": "proj-a",
               "cwd": "/home/u/proj-a"}


def at(hour, minute, now):
    t = time.localtime(now)
    x = time.mktime((t.tm_year, t.tm_mon, t.tm_mday, hour, minute, 0, 0, 0, -1))
    return x if x > now else x + 86400


@pytest.mark.parametrize("text,hm", [
    ("API Error: 5-hour limit reached ∙ resets 7:30pm", (19, 30)),
    ("Claude usage limit reached. Your limit resets at 19:30 (Europe/Berlin).", (19, 30)),
    ("limit reached, resets 7pm", (19, 0)),
    ("resets 12am", (0, 0)),
])
def test_reset_time_is_read_from_the_error_text(text, hm):
    now = time.mktime((2026, 11, 24, 12, 0, 0, 0, 0, -1))
    assert session_end.reset_at(text, now) == at(*hm, now)


def test_no_reset_time_when_the_text_names_none():
    assert session_end.reset_at("API Error: Rate limit reached") is None
    assert session_end.reset_at("resets 31:99") is None


def test_events():
    now = time.mktime((2026, 11, 24, 12, 0, 0, 0, 0, -1))
    ev = session_end.event("failure", {"error": "rate_limit", "error_details": "429",
                                       "last_assistant_message": "limit reached, resets 7pm"}, now)
    assert ev == {"kind": "failure", "error": "rate_limit", "text": "limit reached, resets 7pm 429",
                  "resets_at": at(19, 0, now)}
    assert session_end.event("failure", {"error": "server_error"})["resets_at"] is None
    assert session_end.event("end", {"reason": "logout"}) == {"kind": "end", "reason": "logout"}
    assert session_end.event("end", {"reason": "clear"}) is None  # it goes on


def test_the_hook_reports_a_team_session_only(monkeypatch):
    sent = []
    monkeypatch.setattr(hook.http, "request", lambda *a, **k: sent.append((a, k)))
    cfg = {"hub": "http://h", "token": "T", "machine": "box-1"}
    data = {"error": "rate_limit", "last_assistant_message": "resets 7pm", "cwd": "/w/proj-a"}
    monkeypatch.setattr(hook, "_night_team", lambda sid, cwd, cfg: (False, "claude:s1", "proj-a"))
    hook._report_end("failure", "s1", data, cfg)
    assert sent == []  # a personal session sends nothing
    monkeypatch.setattr(hook, "_night_team", lambda sid, cwd, cfg: (True, "claude:s1", "proj-a"))
    hook._report_end("failure", "s1", data, cfg)
    [(args, kw)] = sent
    assert args[1] == "http://h/api/session-event" and args[3]["kind"] == "failure"
    assert kw["timeout"] == 1 and kw["headers"]["X-Kaptyorka-Key"] == "claude%3As1"


# the hub side

@pytest.fixture
def lab(hub, clock):
    """A team machine jet-1 with one Claude Code session in proj-a; alice (write in proj-a)
    with a session that depends on it; the manager online."""
    s = hub.store
    s.project_register("proj-a", by="test")
    jet, _ = s.team_machine_add("jet-1", "test")
    s.grant_project("proj-a", jet, "write", "test")
    code = s.invite_create(jet)
    jtoken, _ = s.invite_redeem(code, "jet-1", host="jet-1", os_user="u")
    assert heartbeat(hub, jtoken, "jet-1", sessions=[JET_SESSION])[0] == 200
    alice, atoken = person(s, "alice")
    s.grant_project("proj-a", alice, "write", "test")
    A = {"key": "k:alice", "machine": "box-1", "tool": "codex", "project": "proj-a"}
    ac = SimpleNamespace(url=hub.url, token=atoken)
    run(call(ac, A, "who"))
    mgr, me = manager_online(hub)
    return SimpleNamespace(hub=hub, s=s, clock=clock, jtoken=jtoken, ac=ac, A=A, mgr=mgr,
                           me=me, name=s.agent("claude:sid-jet").name)


def event(lab, body, token=None):
    ident = Identity("claude:sid-jet", "jet-1", "claude-code", "proj-a", "/home/u/proj-a")
    return http.request("POST", lab.hub.url + "/api/session-event", token or lab.jtoken, body,
                        headers=ident.headers())


def gone(lab, after=40):
    """The session's bridge stops listing it."""
    lab.clock[0] += after
    assert heartbeat(lab.hub, lab.jtoken, "jet-1", sessions=[])[0] == 200
    run(call(lab.ac, lab.A, "who"))
    run(call(lab.mgr, lab.me, "who"))


def mtexts(lab):
    return [m["text"] for m in run(call(lab.mgr, lab.me, "inbox"))]


def test_usage_limit_is_told_and_shown(lab):
    resets = lab.clock[0] + 3 * 3600
    assert event(lab, {"kind": "failure", "error": "rate_limit", "resets_at": resets})[0] == 200
    hm = time.strftime("%H:%M", time.localtime(resets))
    assert f"{lab.name} hit its usage limit, resets {hm}; resume it with" \
        f" resume_session(\"{lab.name}\") when someone needs it" in mtexts(lab)
    assert event(lab, {"kind": "failure", "error": "rate_limit", "resets_at": resets})[0] == 200
    assert not [t for t in mtexts(lab) if "usage limit" in t]  # once
    gone(lab)
    out = run(call(lab.ac, lab.A, "send", to=lab.name, text="are you there?"))
    assert out == (f"held: {lab.name} hit its usage limit, resets {hm}; it gets the message"
                   " when it next reports")


def test_only_the_sessions_own_person_reports_it(lab):
    _, other = person(lab.s, "bob")
    status, body = event(lab, {"kind": "end", "reason": "other"}, token=other)
    assert status == 400 and "no such session" in body


def test_the_manager_sees_team_machine_projects_and_state(lab):
    [row] = [r for r in run(call(lab.mgr, lab.me, "who")) if r["name"] == lab.name]
    assert row["project"] == "proj-a" and "stuck" in row and "status" in row


def test_manager_resumes_the_same_session_for_the_asker(lab):
    event(lab, {"kind": "end", "reason": "other"})
    gone(lab)
    run(call(lab.ac, lab.A, "send", to=lab.s.agent("k:mgr").name, text="jet's session died"))
    out = run(call(lab.mgr, lab.me, "resume_session", name=lab.name))
    assert out.startswith(f"{lab.name} ended (other); resuming {lab.name} on jet-1 as `proj-a-w1`")
    b = SimpleNamespace(token=lab.jtoken, host="jet-1")
    [order] = [i for i in poll(lab.hub, b) if i.get("launch")]
    assert (order["resume"], order["folder"], order["boss"]) == (
        "sid-jet", "/home/u/proj-a", lab.s.agent("k:alice").name)


def test_resume_waits_for_the_reset(lab):
    resets = lab.clock[0] + 3600
    event(lab, {"kind": "failure", "error": "rate_limit", "resets_at": resets})
    gone(lab)
    run(call(lab.ac, lab.A, "send", to=lab.s.agent("k:mgr").name, text="resume jet at reset"))
    out = run(call(lab.mgr, lab.me, "resume_session", name=lab.name, when="reset"))
    assert out.endswith("it is resumed then, as a worker of the asker")
    b = SimpleNamespace(token=lab.jtoken, host="jet-1")
    lab.hub.tick()
    assert [i for i in poll(lab.hub, b) if i.get("launch")] == []
    lab.clock[0] = resets + 1
    assert heartbeat(lab.hub, lab.jtoken, "jet-1", sessions=[])[0] == 200
    run(call(lab.mgr, lab.me, "who"))
    lab.hub.tick()
    [order] = [i for i in poll(lab.hub, b) if i.get("launch")]
    assert order["resume"] == "sid-jet"
    assert any(t.startswith(f"resuming {lab.name}") for t in
               [m["text"] for m in run(call(lab.ac, lab.A, "inbox"))])


def test_resume_is_refused_for_someone_elses_own_machine(lab):
    """bob's session on bob's own laptop: the manager may not resume it for alice."""
    bob, btoken = person(lab.s, "bob")
    lab.s.grant_project("proj-a", bob, "write", "test")
    code = lab.s.invite_create(bob)
    tok, _ = lab.s.invite_redeem(code, "lap", host="lap", os_user="u")
    heartbeat(lab.hub, tok, "lap", sessions=[{**JET_SESSION, "key": "claude:sid-bob"}])
    name = lab.s.agent("claude:sid-bob").name
    lab.clock[0] += 40
    heartbeat(lab.hub, tok, "lap", sessions=[])
    heartbeat(lab.hub, lab.jtoken, "jet-1", sessions=[])
    run(call(lab.ac, lab.A, "send", to=lab.s.agent("k:mgr").name, text="resume bob's"))
    run(call(lab.mgr, lab.me, "who"))
    assert "no ended session named" in call_error(lab.mgr, lab.me, "resume_session", name=name)
    assert "only the manager" in call_error(lab.ac, lab.A, "resume_session", name=lab.name)
