"""Connection requests. A member no longer sees other people's sessions or asks for one: they
message the manager, and the admin opens a door. A request made before that (0.9.4 to 0.9.10)
is still decided by the admin alone, which opens a session door between exactly the two sides."""
import json
from types import SimpleNamespace

import pytest
from helpers import call, call_error, person, run

from kaptyorka import http
from kaptyorka.hub import ASK_MANAGER
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


def pending(t, from_key="a1", to_key="b1", why=""):
    """A connection request a member made before 0.9.11, still waiting for the admin."""
    with t.store.lock:
        to_person = t.store.db.execute("SELECT person_id FROM agents WHERE key=?",
                                       (to_key,)).fetchone()[0]
        return t.store.db.execute(
            "INSERT INTO connects (person_id, from_key, to_key, to_person, why, created)"
            " VALUES (?,?,?,?,?,?)", (t.ids["alice"], from_key, to_key, to_person, why,
                                      t.store.clock())).lastrowid


def b1(t):
    return session_id("b1")


# privacy first


def test_a_member_sees_no_other_person_and_asks_the_manager(team):
    t = team
    assert [r["person"] for r in tool(t, "alice", "who")] == ["alice", "manager"]
    body = feed(t, "alice")
    assert "others" not in body and [a["person"] for a in body["agents"]] == ["alice", "manager"]
    dump = json.dumps(body)
    for secret in ("bob", "box-2", "api", "secret plan", "payroll"):
        assert secret not in dump, secret
    for to in (b1(t), "box-2/codex/(private)", "api", "box-2/codex/proj-b", "bob"):
        assert "that you can reach" in tool_err(t, "alice", "send", to=to, text="hi")
        assert tool_err(t, "alice", "request_access", kind="session", target=to).endswith(ASK_MANAGER)
    status, said = post(t, "alice", "/api/connects", {"to": b1(t), "from": "page"})
    assert (status, said) == (400, ASK_MANAGER)
    assert post(t, "alice", "/api/send", {"to": b1(t), "text": "hi"})[0] == 400
    assert t.store.inbox("b1") == []
    # the way in: the manager, always reachable (its session is labelled manager)
    with t.store.lock:
        t.store.db.execute("UPDATE agents SET label='manager' WHERE key='m1'")
    assert tool(t, "alice", "send", to="manager", text="may I talk to bob's api session?") \
        == "sent to 1 session"
    # the admin sees everyone
    assert {a["person"] for a in feed(t, "admin")["agents"]} == {"alice", "bob", "admin", "manager"}


def test_who_ignores_an_old_others_flag(team):
    assert [r["person"] for r in tool(team, "alice", "who", others=True)] == ["alice", "manager"]


# a request from before 0.9.11


def test_old_request_approve_door_and_both_talk(team):
    t = team
    cid = pending(t, why="review my api")
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
    assert tool(t, "alice", "send", to=b1(t), text="hi bob") == "sent to 1 session"
    [m] = tool(t, "bob", "inbox")
    assert (m["from"], m["reply_to"], m["text"]) == ("box-1/codex/(private)", session_id("a1"), "hi bob")
    assert tool(t, "bob", "send", to=m["reply_to"], text="hi alice") == "sent to 1 session"
    [m] = tool(t, "alice", "inbox")
    assert m["from"] == "box-2/codex/(private)" and "proj-b" not in json.dumps(m)
    assert b1(t) in [r["id"] for r in tool(t, "alice", "who")]  # granted: it shows now
    tool(t, "alice", "door_close", to=b1(t))
    assert t.store.doors() == []
    assert "that you can reach" in tool_err(t, "alice", "send", to=b1(t), text="again")


def test_deny_sends_the_reason(team):
    t = team
    cid = pending(t)
    assert post(t, "admin", f"/api/connects/{cid}", {"approve": False, "reason": "not now"})[0] == 204
    assert t.store.doors() == []
    [note] = t.store.inbox("a1")
    assert note.text == f"Your connection request #{cid} to box-2/codex/(private) (api) was denied: not now"
    page = [m for m in feed(t, "alice")["messages"] if m["to"] == "alice"]
    assert page[-1]["text"] == note.text
    [dec] = [r for r in feed(t, "admin")["decisions"] if r.get("kind") == "session"]
    assert (dec["decision"], dec["decided_by"], dec["reason"]) == ("denied", "admin", "not now")


def test_only_the_admin_approves(team):
    t = team
    cid = pending(t)
    assert post(t, "manager", f"/api/connects/{cid}", {"approve": True})[0] == 403
    assert post(t, "alice", f"/api/connects/{cid}", {"approve": True})[0] == 403
    assert t.store.doors() == []
    from kaptyorka.connects import ADMIN_APPROVES, ConnectStore
    cs = ConnectStore(t.store, lambda k, x: True, lambda: t.ids["manager"])
    with pytest.raises(Exception, match=ADMIN_APPROVES):
        cs.decide(cid, t.store.authenticate(t.tokens["manager"]), True)


def test_offline_at_approval_opens_no_door(team):
    t = team
    cid = pending(t)
    with t.store.lock:
        t.store.db.execute("UPDATE agents SET last_seen=last_seen-700 WHERE key='b1'")
    status, body = post(t, "admin", f"/api/connects/{cid}", {"approve": True})
    assert status == 409 and "no door was opened" in body
    assert t.store.doors() == []
    assert "no door was opened" in t.store.inbox("a1")[0].text
    assert [r for r in feed(t, "admin")["requests"] if r["kind"] == "session"] == []


# the page flow


def test_old_page_request_approve_and_page_talks(team):
    t = team
    cid = pending(t, from_key="web:alice", why="a question")
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
    assert item["text"] == (f"Kaptyorka message from box-2/codex/(private) (reply to {session_id('b2')}):\n\npushed"
                            f"\nAnswer with send(to=\"{session_id('b2')}\"); text written only in your"
                            " terminal is not seen by them.")
    t.store.ack(item["id"], True)
    t.store.send("b2", "box-1/codex/proj-a", "read me")
    [m] = tool(t, "alice", "inbox")
    assert (m["from"], m["reply_to"]) == ("box-2/codex/(private)", session_id("b2"))


# hub notices name a session the way the receiver sees it


def test_door_closed_notice_hides_the_closers_project(team):
    t = team
    cid = pending(t)
    post(t, "admin", f"/api/connects/{cid}", {"approve": True})
    t.store.inbox("b1")
    tool(t, "alice", "door_close", to=b1(t))
    [m] = tool(t, "bob", "inbox")
    assert (m["from"], m["text"]) == ("box-1/codex/(private)", "Door closed by alice (box-1/codex/(private)).")


def test_knock_notice_hides_the_admins_project(team):
    t = team
    t.store.knock("x1", "bob", "a word?")
    [m] = t.store.inbox("b1")
    assert m.text.startswith("Kaptyorka knock from admin (box-3/codex/(private)): a word?.")
    assert "proj-x" not in m.text


def test_knock_yes_notice_hides_the_admins_project(team):
    t = team
    from helpers import old_knock
    kid = old_knock(t.store, "a1", t.ids["admin"], "a word?")
    t.store.answer(kid, "x1", True)
    [m] = t.store.inbox("a1")
    assert m.text == (f"Door open: box-3/codex/(private) (admin) said yes to your knock #{kid}."
                      f' You can send(to="{session_id("x1")}", ...) until either side closes it.')
    assert tool(t, "alice", "send", to=session_id("x1"), text="thanks") == "sent to 1 session"
