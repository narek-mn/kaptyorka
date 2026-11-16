"""The session directory and connection requests (0.9.4): a member sees every other online
session without content, asks to be connected to one, the manager hears of it and only the
admin approves, which opens a session door between exactly the two sides."""
import json
from types import SimpleNamespace

import pytest
from helpers import call, call_error, person, run

from kaptyorka import http
from kaptyorka.store import session_id


def as_person(hub, token):
    return SimpleNamespace(url=hub.url, token=token)


def ident(key, machine="box-1", project="proj-a"):
    return {"key": key, "machine": machine, "tool": "codex", "project": project}


@pytest.fixture
def team(hub):
    """alice and bob (members), the admin and the manager. alice has a1 on box-1 in proj-a,
    bob has b1 on box-2 in proj-b (registered, bob only) with a status and an activity, the
    admin x1 on box-3, the manager m1."""
    s = hub.store
    ids, tokens = {}, {}
    for name, role in (("alice", "member"), ("bob", "member"), ("admin", "admin"),
                       ("manager", "manager")):
        ids[name], tokens[name] = person(s, name, role)
    s.project_register("proj-b", by="admin")
    s.grant_project("proj-b", ids["bob"], "write", "admin")
    t = SimpleNamespace(hub=hub, store=s, ids=ids, tokens=tokens,
                        idents={"alice": ident("a1"), "bob": ident("b1", "box-2", "proj-b"),
                                "admin": ident("x1", "box-3", "proj-x"),
                                "manager": ident("m1", "box-3", "manager")})
    for who in ("alice", "bob", "admin", "manager"):
        run(call(client(t, who), t.idents[who], "who"))
    s.set_status("b1", "secret plan")
    with s.lock:
        s.db.execute("UPDATE agents SET activity='editing payroll.py', label='api' WHERE key='b1'")
    return t


def client(t, who):
    return as_person(t.hub, t.tokens[who])


def tool(t, who, name, **args):
    return run(call(client(t, who), t.idents[who], name, **args))


def tool_err(t, who, name, **args):
    return call_error(client(t, who), t.idents[who], name, **args)


def feed(t, who):
    status, body = http.request("GET", t.hub.url + "/api/feed", t.tokens[who])
    assert status == 200, body
    return body


def post(t, who, path, body):
    return http.request("POST", t.hub.url + path, t.tokens[who], body)


def others(t, who):
    return [r for r in tool(t, who, "who", others=True) if r.get("reachable") is False]


def b1(t):
    return session_id("b1")


# privacy first


def test_directory_has_no_content_keys_or_hidden_project(team):
    t = team
    rows = others(t, "alice")
    assert {r["person"] for r in rows} == {"bob", "admin"}  # not her own, not the manager
    bob = next(r for r in rows if r["person"] == "bob")
    assert bob == {"id": b1(t), "name": "box-2/codex/(private)", "label": "api", "person": "bob",
                   "machine": "box-2", "tool": "codex", "project": "(private)", "online": True,
                   "reachable": False, "request": None}
    for text in (json.dumps(rows), json.dumps(feed(t, "alice")["others"])):
        for secret in ("b1", "x1", "proj-b", "proj-x", "secret plan", "payroll"):
            assert secret not in text.replace(b1(t), "").replace(session_id("x1"), "")
    assert feed(t, "alice")["others"] == rows
    # the project shows under the existing rule: alice gets a role in proj-b
    t.store.grant_project("proj-b", t.ids["alice"], "read", "admin")
    bob = next(r for r in others(t, "alice") if r["person"] == "bob")
    assert (bob["name"], bob["project"]) == ("box-2/codex/proj-b", "proj-b")
    assert "status" not in bob and "activity" not in bob


def test_others_stay_unreachable_before_approval(team):
    t = team
    assert [r["person"] for r in tool(t, "alice", "who")] == ["alice", "manager"]
    for to in (b1(t), "box-2/codex/(private)", "api", "box-2/codex/proj-b"):
        assert "that you can reach" in tool_err(t, "alice", "send", to=to, text="hi")
    tool(t, "alice", "request_access", kind="session", target=b1(t), why="review")
    assert "that you can reach" in tool_err(t, "alice", "send", to=b1(t), text="hi")
    assert post(t, "alice", "/api/send", {"to": b1(t), "text": "hi"})[0] == 400
    assert [a["person"] for a in feed(t, "alice")["agents"]] == ["alice", "manager"]
    assert t.store.inbox("b1") == []


def test_directory_empty_for_admin_and_manager(team):
    assert others(team, "admin") == [] and feed(team, "admin")["others"] == []
    assert others(team, "manager") == []


def test_asking_never_tests_a_hidden_real_name(team):
    err = tool_err(team, "alice", "request_access", kind="session", target="box-2/codex/proj-b")
    assert err.endswith("no online session named box-2/codex/proj-b in the directory")


# the agent flow


def test_agent_request_approve_door_and_both_talk(team):
    t = team
    out = tool(t, "alice", "request_access", kind="session", target=b1(t), why="review my api")
    cid = int(out.split("#")[1].split(";")[0])
    assert out.endswith("the manager is told and the admin decides")
    [mgr] = t.store.inbox("m1")
    assert f"Connection request #{cid}: alice (box-1/codex/(private))" in mgr.text
    assert "bob's session box-2/codex/(private) (api). Why: review my api." in mgr.text
    assert "Only the admin approves" in mgr.text
    assert next(r for r in others(t, "alice") if r["person"] == "bob")["request"] == \
        {"id": cid, "state": "requested"}
    [req] = [r for r in feed(t, "admin")["requests"] if r["kind"] == "session"]
    assert (req["id"], req["person"], req["target_person"], req["why"]) == \
        (cid, "alice", "bob", "review my api")
    assert req["target"] == "box-2/codex/proj-b"  # the admin has a role in every project
    assert post(t, "admin", f"/api/connects/{cid}", {"approve": True})[0] == 204
    [door] = t.store.doors()
    assert (door["scope"], door["a_session"], door["b_session"], door["opened_by"]) == \
        ("session", "box-1/codex/proj-a", "box-2/codex/proj-b", "admin")
    [a_note] = t.store.inbox("a1")
    assert f'send(to="{b1(t)}")' in a_note.text and "proj-b" not in a_note.text
    [b_note] = t.store.inbox("b1")
    assert "alice's session box-1/codex/(private)" in b_note.text
    assert f'send(to="{session_id("a1")}")' in b_note.text
    assert tool(t, "alice", "send", to=b1(t), text="hi bob") == "sent to 1 session"
    [m] = tool(t, "bob", "inbox")
    assert (m["from"], m["reply_to"], m["text"]) == ("box-1/codex/(private)", session_id("a1"), "hi bob")
    assert tool(t, "bob", "send", to=m["reply_to"], text="hi alice") == "sent to 1 session"
    [m] = tool(t, "alice", "inbox")
    assert m["from"] == "box-2/codex/(private)" and "proj-b" not in json.dumps(m)
    assert b1(t) in [r["id"] for r in tool(t, "alice", "who")]
    assert b1(t) not in [r["id"] for r in others(t, "alice")]
    log = [r["sentence"] for r in feed(t, "admin")["log"]]
    assert f"admin approved alice's connection request {cid}" in log
    assert "admin opened a door between box-1/codex/proj-a and box-2/codex/proj-b" in log
    assert f"alice asked to connect box-1/codex/proj-a to box-2/codex/proj-b (request {cid})" in log
    # it closes the usual ways
    tool(t, "alice", "door_close", to=b1(t))
    assert t.store.doors() == []
    assert "that you can reach" in tool_err(t, "alice", "send", to=b1(t), text="again")


def test_deny_sends_the_reason(team):
    t = team
    cid = int(tool(t, "alice", "request_access", kind="session", target="api").split("#")[1].split(";")[0])
    assert post(t, "admin", f"/api/connects/{cid}", {"approve": False, "reason": "not now"})[0] == 204
    assert t.store.doors() == []
    [note] = t.store.inbox("a1")
    assert note.text == f"Your connection request #{cid} to box-2/codex/(private) (api) was denied: not now"
    page = [m for m in feed(t, "alice")["messages"] if m["to"] == "alice"]
    assert page[-1]["text"] == note.text
    assert next(r for r in others(t, "alice") if r["person"] == "bob")["request"] == \
        {"id": cid, "state": "denied", "reason": "not now"}
    [dec] = [r for r in feed(t, "admin")["decisions"] if r.get("kind") == "session"]
    assert (dec["decision"], dec["decided_by"], dec["reason"]) == ("denied", "admin", "not now")
    # a denied request may be asked again
    assert tool(t, "alice", "request_access", kind="session", target=b1(t)).startswith("requested #")


def test_refusals(team):
    t = team
    ask = lambda to: tool_err(t, "alice", "request_access", kind="session", target=to)  # noqa: E731
    assert ask(session_id("a1")).endswith("that is your own session")
    assert ask(session_id("m1")).endswith("the manager is always reachable; send to it")
    tool(t, "alice", "request_access", kind="session", target=b1(t))
    assert "already requested (#" in ask(b1(t))
    assert tool_err(t, "admin", "request_access", kind="session", target=b1(t)).endswith(
        "you can already message every session")
    # already behind an open door with this session
    t.store.open_door_group(t.ids["alice"], t.ids["admin"], "admin")
    assert ask(session_id("x1")).endswith("already connected: send to it")


def test_already_connected_refused(team):
    t = team
    cid = int(tool(t, "alice", "request_access", kind="session", target=b1(t)).split("#")[1].split(";")[0])
    post(t, "admin", f"/api/connects/{cid}", {"approve": True})
    # bob's session is now listed for alice, so it left the directory; a request by handle
    # through the page from that same session is refused
    status, body = post(t, "alice", "/api/connects", {"to": b1(t), "from": "box-1/codex/proj-a"})
    assert status == 400 and ("already connected" in body or "in the directory" in body)


def test_manager_notified_but_cannot_approve(team):
    t = team
    cid = int(tool(t, "alice", "request_access", kind="session", target=b1(t)).split("#")[1].split(";")[0])
    assert post(t, "manager", f"/api/connects/{cid}", {"approve": True})[0] == 403
    assert post(t, "alice", f"/api/connects/{cid}", {"approve": True})[0] == 403
    assert t.store.doors() == []
    from kaptyorka.connects import ADMIN_APPROVES, ConnectStore
    cs = ConnectStore(t.store, lambda k, x: True, lambda: t.ids["manager"])
    with pytest.raises(Exception, match=ADMIN_APPROVES):
        cs.decide(cid, t.store.authenticate(t.tokens["manager"]), True)


def test_offline_at_approval_opens_no_door(team):
    t = team
    cid = int(tool(t, "alice", "request_access", kind="session", target=b1(t)).split("#")[1].split(";")[0])
    with t.store.lock:
        t.store.db.execute("UPDATE agents SET last_seen=last_seen-100 WHERE key='b1'")
    status, body = post(t, "admin", f"/api/connects/{cid}", {"approve": True})
    assert status == 409 and "no door was opened" in body
    assert t.store.doors() == []
    assert "no door was opened" in t.store.inbox("a1")[0].text
    assert [r for r in feed(t, "admin")["requests"] if r["kind"] == "session"] == []


# the page flow


def test_page_request_approve_and_page_talks(team):
    t = team
    status, body = post(t, "alice", "/api/connects", {"to": b1(t), "from": "page", "why": "a question"})
    assert status == 200
    cid = body["id"]
    assert post(t, "alice", "/api/connects", {"to": b1(t), "from": "page"})[0] == 400  # duplicate
    assert post(t, "alice", "/api/connects", {"to": b1(t), "from": "box-9/codex/x"})[0] == 400
    assert next(r for r in feed(t, "alice")["others"] if r["person"] == "bob")["request"] == \
        {"id": cid, "state": "requested"}
    assert post(t, "admin", f"/api/connects/{cid}", {"approve": True})[0] == 204
    [door] = t.store.doors()
    assert (door["a_session"], door["b_session"]) == ("", "box-2/codex/proj-b")
    row = next(a for a in feed(t, "alice")["agents"] if a["person"] == "bob")
    assert (row["name"], row["id"], row["project"]) == ("box-2/codex/(private)", b1(t), "(private)")
    assert post(t, "alice", "/api/send", {"to": b1(t), "text": "hi from the page"})[0] == 200
    [m] = tool(t, "bob", "inbox")[-1:]
    assert m["text"] == "hi from the page" and m["from"] == "alice"
    assert tool(t, "bob", "send", to="alice", text="back") == "sent to 1 session"
    back = [m for m in feed(t, "alice")["messages"] if m["text"] == "back"]
    assert back[0]["sender"] == "box-2/codex/(private)" and back[0]["sender_id"] == b1(t)
    assert any("Your page and box-2/codex/(private) (api) can talk" in m["text"]
               for m in feed(t, "alice")["messages"] if m["to"] == "alice")



# naming a session whose project is hidden


def test_hidden_name_and_handle_address_a_session(team):
    """Two of bob's sessions on one machine, in two projects alice may not see, both show as
    box-2/codex/(private): the hidden name alone is ambiguous and the error lists handles
    only; each handle names one. A message from one says which handle to reply to."""
    t = team
    run(call(client(t, "bob"), ident("b2", "box-2", "proj-c"), "who"))
    t.store.open_door_group(t.ids["alice"], t.ids["bob"], "admin")
    err = tool_err(t, "alice", "send", to="box-2/codex/(private)", text="hi")
    assert err.endswith(f"several sessions are named box-2/codex/(private); use one of:"
                        f" {b1(t)} (api), {session_id('b2')}")
    assert "proj-" not in err
    assert tool(t, "alice", "send", to=session_id("b2"), text="hi") == "sent to 1 session"
    assert [m.text for m in t.store.inbox("b2")] == ["hi"] and t.store.inbox("b1") == []
    # a message from a hidden project: masked in inbox, the push text and the brief
    t.store.send("b2", "box-1/codex/proj-a", "pushed")
    [item] = [o for o in t.store.offer("box-1") if o["kind"] == "message"]
    assert item["text"] == f"Kaptyorka message from box-2/codex/(private) (reply to {session_id('b2')}):\n\npushed"
    t.store.ack(item["id"], True)
    t.store.send("b2", "box-1/codex/proj-a", "read me")
    [m] = tool(t, "alice", "inbox")
    assert (m["from"], m["reply_to"]) == ("box-2/codex/(private)", session_id("b2"))
