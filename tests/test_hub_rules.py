from types import SimpleNamespace

from helpers import call, call_error, person, run
from kaptyorka import http
from kaptyorka.brief import RULES_POINTER
from kaptyorka.rules import HEADING, TRAILER

ADMIN = "T"
S1 = {"key": "claude:s1", "machine": "box-1", "tool": "claude-code", "project": "proj-a"}
S2 = {"key": "claude:s2", "machine": "box-2", "tool": "claude-code", "project": "proj-a"}


def as_token(hub, token):
    return SimpleNamespace(url=hub.url, token=token)


def hdr(who):
    return {f"X-Kaptyorka-{k.capitalize()}": v for k, v in who.items()}


def set_rules(hub, text):
    return http.request("POST", hub.url + "/api/rules", ADMIN, {"text": text})


def test_rules_tool_and_brief_heading(hub):
    _, tok = person(hub.store, "alice")
    a = as_token(hub, tok)
    assert run(call(a, S1, "rules")) == "No team rules yet."
    assert set_rules(hub, "be kind")[1] == {"version": 1}
    assert run(call(a, S1, "rules")) == "Team rules v1:\nbe kind"
    _, brief = http.request("GET", hub.url + "/api/brief?event=start", tok, headers=hdr(S1))
    assert HEADING in brief and TRAILER in brief and "be kind" in brief


def test_long_rules_cut_to_pointer_in_brief(hub):
    _, tok = person(hub.store, "alice")
    text = "x" * 7000
    set_rules(hub, text)
    _, brief = http.request("GET", hub.url + "/api/brief?event=start", tok, headers=hdr(S1))
    assert len(brief) < 6000 and RULES_POINTER in brief
    assert run(call(as_token(hub, tok), S1, "rules")).endswith(text)


def test_post_rules_member_forbidden(hub):
    _, tok = person(hub.store, "alice")
    status, body = http.request("POST", hub.url + "/api/rules", tok, {"text": "x"})
    assert status == 403 and "admin only" in body
    assert http.request("GET", hub.url + "/api/rules", tok)[0] == 200


def test_put_skills_and_bridge_fetch(hub):
    _, tok = person(hub.store, "alice")
    status, summary = http.request("PUT", hub.url + "/api/skills", ADMIN,
                                   {"skills": {"one": "a", "two": "b"}})
    assert status == 200 and summary["added"] == ["one", "two"]
    assert http.request("PUT", hub.url + "/api/skills", tok, {"skills": {}})[0] == 403
    status, body = http.request("GET", hub.url + "/bridge/skills", tok)
    assert status == 200 and [s["name"] for s in body["skills"]] == ["one", "two"]
    beat = {"machine": "box-1", "sessions": []}
    for t in (tok, ADMIN):
        status, hb = http.request("POST", hub.url + "/bridge/heartbeat", t, beat)
        assert status == 200 and hb["skills_version"] == body["version"] != ""
    assert http.request("GET", hub.url + "/bridge/skills", ADMIN)[0] == 200
    assert http.request("GET", hub.url + "/api/rules", tok)[1]["skills"][0]["size"] == 1


def test_propose_and_accept_makes_new_version(hub):
    _, tok = person(hub.store, "alice")
    set_rules(hub, "one")
    out = run(call(as_token(hub, tok), S1, "propose_rule", text="two", evidence="said so"))
    assert out == "Proposal 1 filed; the admin decides on the Rules tab."
    rows = http.request("GET", hub.url + "/api/proposals?status=open", ADMIN)[1]
    assert [r["id"] for r in rows] == [1]
    assert http.request("POST", hub.url + "/api/proposals/1", tok, {"accept": True})[0] == 403
    assert http.request("POST", hub.url + "/api/proposals/1", ADMIN, {"accept": True})[0] == 200
    cur = http.request("GET", hub.url + "/api/rules", tok)[1]
    assert cur["version"] == 2 and cur["text"] == "one\n\ntwo"
    msgs = run(call(as_token(hub, tok), S1, "inbox"))
    assert "accepted" in msgs[0]["text"] and "v2" in msgs[0]["text"]
    assert http.request("POST", hub.url + "/api/proposals/1", ADMIN, {"accept": True})[0] == 409


def test_reject_notifies_proposer_only(hub):
    _, ta = person(hub.store, "alice")
    _, tb = person(hub.store, "bob")
    run(call(as_token(hub, ta), S2, "who"))
    run(call(as_token(hub, tb), S1, "propose_rule", text="no tabs", evidence="e"))
    assert http.request("GET", hub.url + "/api/proposals", ta)[1] == []
    assert len(http.request("GET", hub.url + "/api/proposals", tb)[1]) == 1
    http.request("POST", hub.url + "/api/proposals/1", ADMIN, {"accept": False, "reason": "too vague"})
    assert "too vague" in run(call(as_token(hub, tb), S1, "inbox"))[0]["text"]
    assert run(call(as_token(hub, ta), S2, "inbox")) == []


def test_conflict_tool_logs_with_project(hub):
    _, tok = person(hub.store, "alice")
    out = run(call(as_token(hub, tok), S1, "conflict", rule_a="a", rule_b="b"))
    assert out == "Conflict 1 logged for the admin."
    rows = http.request("GET", hub.url + "/api/conflicts", ADMIN)[1]
    assert rows[0]["project"] == "proj-a" and rows[0]["person"] == "alice"
    assert http.request("GET", hub.url + "/api/conflicts", tok)[0] == 403


def test_legacy_token_cannot_propose(hub):
    assert "device token" in call_error(hub, S1, "propose_rule", text="x", evidence="y")
