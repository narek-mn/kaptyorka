from types import SimpleNamespace

from helpers import call, call_error, person, run
from kaptyorka import http
from kaptyorka.brief import RULES_POINTER
from kaptyorka.rules import HEADING, TRAILER
from kaptyorka.store import ONLINE_S

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
    assert run(call(a, S1, "rules")) == "Team rules v1:\n" + HEADING + "\n\nbe kind\n\n" + TRAILER
    _, brief = http.request("GET", hub.url + "/api/brief?event=start", tok, headers=hdr(S1))
    assert HEADING in brief and TRAILER in brief and "be kind" in brief


def test_long_rules_cut_to_pointer_in_brief(hub):
    _, tok = person(hub.store, "alice")
    text = "x" * 7000
    set_rules(hub, text)
    _, brief = http.request("GET", hub.url + "/api/brief?event=start", tok, headers=hdr(S1))
    assert len(brief) < 6000 and RULES_POINTER in brief and text not in brief
    assert HEADING + "\n\n" + RULES_POINTER + "\n\n" + TRAILER in brief
    out = run(call(as_token(hub, tok), S1, "rules"))
    assert HEADING in out and text in out and out.endswith(TRAILER)


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
    assert "session_key" not in rows[0] and "person_id" not in rows[0]
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
    hub.store.clock = lambda t=hub.store.clock(): t + ONLINE_S + 1  # the proposer's session went away
    assert "claude:s1" not in {x.key for x in hub.store.online()}
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


def test_reject_reason_is_capped_at_1000(hub):
    _, tok = person(hub.store, "alice")
    run(call(as_token(hub, tok), S1, "propose_rule", text="x", evidence="e"))
    http.request("POST", hub.url + "/api/proposals/1", ADMIN, {"accept": False, "reason": "r" * 1500})
    text = run(call(as_token(hub, tok), S1, "inbox"))[0]["text"]
    assert "r" * 1000 in text and "r" * 1001 not in text


def test_decide_accept_must_be_bool(hub):
    _, tok = person(hub.store, "alice")
    run(call(as_token(hub, tok), S1, "propose_rule", text="x", evidence="e"))
    status, body = http.request("POST", hub.url + "/api/proposals/1", ADMIN, {"accept": "yes"})
    assert status == 400 and "accept must be true or false" in body
    assert http.request("GET", hub.url + "/api/proposals?status=open", ADMIN)[1][0]["id"] == 1


def test_member_sees_own_proposal_behind_100_newer_ones(hub):
    from kaptyorka.rules import RulesStore

    _, ta = person(hub.store, "alice")
    bid, _ = person(hub.store, "bob")
    run(call(as_token(hub, ta), S1, "propose_rule", text="mine", evidence="e"))
    rs = RulesStore(hub.store)
    with hub.store.lock:
        hub.store.db.executemany(
            "INSERT INTO proposals (person_id, session_key, text, evidence, status, time)"
            " VALUES (?, 'k', ?, 'e', 'rejected', 0)", [(bid, f"bob {i}") for i in range(100)])
    assert len(rs.proposals()) == 100
    assert [p["text"] for p in http.request("GET", hub.url + "/api/proposals", ta)[1]] == ["mine"]


def test_page_proposal_refuses_shared_token(hub):
    status, body = http.request("POST", hub.url + "/api/proposals", ADMIN, {"text": "x", "evidence": "y"})
    assert status == 400 and body.startswith("use your own device token")


def test_page_proposal_rejects_secrets(hub):
    _, tok = person(hub.store, "bob")
    status, body = http.request("POST", hub.url + "/api/proposals", tok,
                                {"text": "key sk-ant-" + "a" * 40, "evidence": ""})
    assert status == 400 and body.startswith("looks like a secret")


def test_page_proposal_decision_reaches_person(hub):
    _, tok = person(hub.store, "bob")
    _, ta = person(hub.store, "alice", "admin")
    status, body = http.request("POST", hub.url + "/api/proposals", tok,
                                {"text": "Always run tests", "evidence": "asked twice"})
    assert status == 200
    assert http.request("POST", f"{hub.url}/api/proposals/{body['id']}", ta, {"accept": True})[0] == 200
    msgs = http.request("GET", hub.url + "/api/feed", tok)[1]["messages"]
    assert any("accepted" in m["text"] for m in msgs)
