"""Private by default: a member sees and reaches only their own sessions, the manager's and
those behind a door the admin opened."""
from types import SimpleNamespace

import pytest
from helpers import call, call_error, old_knock, person, run

from kaptyorka import http
from kaptyorka.store import KaptyorkaError


def as_person(hub, token):
    return SimpleNamespace(url=hub.url, token=token)


def ident(key, machine, project="proj-a"):
    return {"key": key, "machine": machine, "tool": "codex", "project": project}


@pytest.fixture
def team(hub):
    """alice and bob (members), the admin (token T) and the manager, each with one online
    session in folder proj-a on box-1."""
    s = hub.store
    ids, tokens = {}, {}
    for name, role in (("alice", "member"), ("bob", "member"), ("admin", "admin"),
                       ("manager", "manager")):
        ids[name], tokens[name] = person(s, name, role)
    labels = {"alice": "front", "bob": "bob-desk", "admin": "boss-desk", "manager": "hq"}
    keys = {}
    for name in ("alice", "bob", "admin", "manager"):
        keys[name] = f"k:{name}"
        s.heartbeat("box-1", [{"key": keys[name], "tool": "codex", "project": "proj-a",
                               "label": labels[name]}], principal=s.authenticate(tokens[name]),
                    facts={"host": "box-1", "os_user": name})
    names = {n: s.agent(k).name for n, k in keys.items()}
    return SimpleNamespace(hub=hub, store=s, ids=ids, tokens=tokens, keys=keys, names=names,
                           labels=labels)


def client(team, who):
    return as_person(team.hub, team.tokens[who])


def me(team, who):
    return ident(team.keys[who], "box-1")


def feed(team, who):
    status, body = http.request("GET", team.hub.url + "/api/feed", team.tokens[who])
    assert status == 200, body
    return body


def brief_text(team, who):
    h = {f"X-Kaptyorka-{k.capitalize()}": v for k, v in me(team, who).items()}
    status, body = http.request("GET", team.hub.url + "/api/brief?event=start",
                                team.tokens[who], headers=h)
    assert status == 200, body
    return body


def seen_by(team, who):
    """Person names of the sessions `who` sees in the feed and in who()."""
    in_feed = {a["person"] for a in feed(team, who)["agents"]}
    in_who = {r["person"] for r in run(call(client(team, who), me(team, who), "who"))}
    return in_feed, in_who


def test_member_sees_only_own_and_manager(team):
    assert seen_by(team, "alice") == ({"alice", "manager"}, {"alice", "manager"})
    text = brief_text(team, "alice")
    others = text.split("online:")[1].split(".")[0]
    assert team.names["manager"] in others or "hq" in others
    for other in ("bob", "admin"):
        assert team.names[other] not in text and team.labels[other] not in text
    assert "2 others" not in text and "3 others" not in text
    err = call_error(client(team, "alice"), me(team, "alice"), "send", to="nobody", text="x")
    assert err.endswith("no session named nobody that you can reach; to reach someone else,"
                   " message the manager")
    for other in ("bob", "admin"):
        assert team.names[other] not in err and team.labels[other] not in err


def test_member_sees_door_partner_until_closed(team):
    s = team.store
    did = s.open_door_group(team.ids["alice"], team.ids["bob"], "admin")
    assert seen_by(team, "alice") == ({"alice", "bob", "manager"},) * 2
    assert seen_by(team, "bob") == ({"alice", "bob", "manager"},) * 2
    s.close_door(did, "admin")
    assert seen_by(team, "alice") == ({"alice", "manager"},) * 2


def test_admin_and_manager_see_everyone(team):
    everyone = {"alice", "bob", "admin", "manager"}
    assert seen_by(team, "admin") == (everyone, everyone)
    assert seen_by(team, "manager") == (everyone, everyone)


def test_member_projects_machines_and_branches_count_only_listed(team):
    s = team.store
    s.project_register("proj-a")
    for name in ("alice", "bob"):
        s.grant_project("proj-a", team.ids[name], "read", "admin")
    status, rows = http.request("GET", team.hub.url + "/api/projects", team.tokens["alice"])
    assert set(rows[0]["working_now"]) == {team.names["alice"], team.names["manager"]}
    status, rows = http.request("GET", team.hub.url + "/api/projects", team.tokens["admin"])
    assert len(rows[0]["working_now"]) == 4
    s.grant_machine("box-1", team.ids["alice"], "admin")
    alice = s.authenticate(team.tokens["alice"])
    assert [m["online"] for m in s.machines(alice)] == [2]
    assert [m["online"] for m in s.machines(s.authenticate(team.tokens["admin"]))] == [4]


def test_member_warnings_are_their_own(team):
    s = team.store
    for who in ("alice", "bob"):
        assert http.request("POST", team.hub.url + "/bridge/heartbeat", team.tokens[who], {
            "machine": "box-1", "sessions": [],
            "facts": {"host": f"box-{who}", "os_user": who, "version": "0.0.1"}})[0] == 200
    w = feed(team, "alice")["warnings"]
    assert w and all(x["person"] == "alice" for x in w)
    assert "warnings" not in feed(team, "admin")
    assert http.request("GET", team.hub.url + "/api/warnings", team.tokens["alice"])[0] == 403


# Task 2: only the admin opens doors; members ask the manager

ASK = "ask the manager: send it a message saying who you need and why"


def test_member_knock_refused_points_to_manager(team):
    s = team.store
    for who in ("alice", "manager"):
        with pytest.raises(KaptyorkaError, match=ASK):
            s.knock(team.keys[who], "bob", "review?")
    assert call_error(client(team, "alice"), me(team, "alice"), "knock", to="bob",
                      about="review?").endswith(ASK)
    assert s.db.execute("SELECT COUNT(*) FROM knocks").fetchone()[0] == 0
    assert s.knock(team.keys["admin"], "bob", "a word?") > 0  # the admin still may


def test_knocked_member_cannot_answer(team):
    s = team.store
    kid = old_knock(s, team.keys["bob"], team.ids["alice"], "help?")
    with pytest.raises(KaptyorkaError, match="only the admin opens doors"):
        s.answer(kid, team.keys["alice"], True)
    assert call_error(client(team, "alice"), me(team, "alice"), "answer", knock_id=kid,
                      answer="yes").endswith("only the admin opens doors")
    status, body = http.request("POST", f"{team.hub.url}/api/knocks/{kid}/answer",
                                team.tokens["alice"], {"answer": "yes"})
    assert (status, body) == (400, "only the admin opens doors")
    assert s.doors() == []


def test_admin_answer_opens_door(team):
    s = team.store
    kid = old_knock(s, team.keys["bob"], team.ids["alice"], "help?")
    status, body = http.request("POST", f"{team.hub.url}/api/knocks/{kid}/answer",
                                team.tokens["admin"], {"answer": "yes"})
    assert (status, body) == (200, {"door": "group"})
    assert s.send(team.keys["alice"], team.names["bob"], "hi") == 1
    assert s.send(team.keys["bob"], team.names["alice"], "hi back") == 1


def test_member_send_all_reaches_own_and_door_partners_only(team):
    s = team.store
    s.heartbeat("box-1", [{"key": "k:alice2", "tool": "codex", "project": "proj-b"}],
                principal=s.authenticate(team.tokens["alice"]))
    s.heartbeat("box-1", [{"key": team.keys["alice"], "tool": "codex", "project": "proj-a",
                           "label": "front"},
                          {"key": "k:alice2", "tool": "codex", "project": "proj-b"}],
                principal=s.authenticate(team.tokens["alice"]))
    assert s.send(team.keys["alice"], "all", "hi") == 1  # her other session only
    s.open_door_group(team.ids["alice"], team.ids["bob"], "admin")
    assert s.send(team.keys["alice"], "all", "hi") == 2


def test_member_reaches_manager_without_door(team):
    a = client(team, "alice")
    assert run(call(a, me(team, "alice"), "send", to=team.names["manager"], text="who?")) \
        == "sent to 1 session"
    assert run(call(a, me(team, "alice"), "send", to="hq", text="by label")) \
        == "sent to 1 session"
    assert [m.text for m in team.store.inbox(team.keys["manager"])] == ["who?", "by label"]


def test_member_sends_a_file_to_the_manager(team):
    s = team.store
    fid = s.file_add("a.txt", b"hello", team.ids["alice"])
    a = client(team, "alice")
    assert run(call(a, me(team, "alice"), "send", to="hq", text="see this",
                    file=f"id:{fid}")) == "sent to 1 session"
    [m] = s.inbox(team.keys["manager"])
    assert (m.text, m.file["id"]) == ("see this", fid)
    other = s.file_add("b.txt", b"x", team.ids["bob"])
    assert call_error(a, me(team, "alice"), "send", to="hq", text="theirs",
                      file=f"id:{other}").endswith("that file is not yours")
    assert call_error(a, me(team, "alice"), "send", to="hq", text="again",
                      file=f"id:{fid}").endswith("that file is not yours")


def _alice_reads(team) -> str:
    """Everything alice can read about sessions: GET routes, read tools, brief, errors."""
    s, url, tok = team.store, team.hub.url, team.tokens["alice"]
    a, ident_a = client(team, "alice"), me(team, "alice")
    out = []
    for path in ("/api/feed", "/api/machines", "/api/projects", "/api/projects/proj-a/activity",
                 "/api/knocks", "/api/me/access", "/api/workers", "/api/requests"):
        status, body = http.request("GET", url + path, tok)
        assert status == 200, (path, body)
        if path == "/api/feed":  # the directory (0.9.4) names everyone, without content:
            for row in body.pop("others"):  # see test_connects
                assert set(row) == {"id", "name", "label", "person", "machine", "tool",
                                    "project", "online", "reachable", "request"}
        out.append(repr(body))
    for tool in ("who", "projects", "workers", "inbox"):
        out.append(repr(run(call(a, ident_a, tool))))
    h = {f"X-Kaptyorka-{k.capitalize()}": v for k, v in ident_a.items()}
    run(call(client(team, "manager"), me(team, "manager"), "send", to=team.names["alice"],
             text="x"))  # an unread message: a fresh brief, not the cached one
    out.append(http.request("GET", url + "/api/brief?event=start", tok, headers=h)[1])
    return "\n".join(out)


def _alice_errors_name_no_one(team, hidden: bool) -> None:
    """A send or door_close to the admin's session (always) or bob's (unless a door is open)
    gets the one refusal text, which names only what alice typed."""
    a, ident_a, tok = client(team, "alice"), me(team, "alice"), team.tokens["alice"]
    names = ["nobody", team.names["admin"], team.labels["admin"]]
    if hidden:
        names += [team.names["bob"], team.labels["bob"]]
    for to in names:
        want = ("no session named " + to + " that you can reach; to reach someone else,"
                " message the manager")
        assert call_error(a, ident_a, "send", to=to, text="x").endswith(want)
        assert http.request("POST", team.hub.url + "/api/send", tok,
                            {"to": to, "text": "x"}) == (400, want)
        assert call_error(a, ident_a, "door_close", to=to).endswith(want)


def _hidden(team, who) -> list:
    """Words of `who`'s session that show in a text: its name, label and folder."""
    return [team.names[who], team.labels[who], f"/w/{who}"]


def test_sweep_member_reads_name_no_one_else(team):
    s = team.store
    s.project_register("proj-a")
    for name in ("alice", "bob"):
        s.grant_project("proj-a", team.ids[name], "read", "admin")
    s.grant_machine("box-1", team.ids["alice"], "admin")
    for who in ("alice", "bob", "admin", "manager"):  # each session in its own folder, on git
        cwd = f"/w/{who}"
        status, _ = http.request("POST", team.hub.url + "/bridge/heartbeat", team.tokens[who], {
            "machine": "box-1", "folders": {"proj-a": cwd},
            "sessions": [{"key": team.keys[who], "tool": "codex", "project": "proj-a",
                          "label": team.labels[who], "cwd": cwd}],
            "facts": {"host": "box-1", "os_user": who,
                      "git": {cwd: {"branch": f"br-{who}", "subject": "wip"}}}})
        assert status == 200
    old_knock(s, team.keys["bob"], team.ids["alice"], "a word?")  # from before 0.9.0

    def check(visible):
        text = _alice_reads(team)
        for who in ("bob", "admin"):
            for word in _hidden(team, who)[:2]:  # name and label: only behind a door
                assert (word in text) == (who in visible), (who, word)
            # the folder never; git lines of a shared project name branches and people (kept)
            assert f"/w/{who}" not in text, who
        assert team.names["manager"] in text
        _alice_errors_name_no_one(team, hidden="bob" not in visible)

    check(visible=())
    did = s.open_door_group(team.ids["alice"], team.ids["bob"], "admin")
    check(visible=("bob",))
    s.close_door(did, "admin")
    check(visible=())


def test_shared_label_and_offline_name(team):
    s = team.store
    s.heartbeat("box-1", [{"key": team.keys["bob"], "tool": "codex", "project": "proj-a",
                           "label": "front"}], principal=s.authenticate(team.tokens["bob"]))
    s.heartbeat("box-1", [{"key": "k:alice2", "tool": "codex", "project": "proj-b"}],
                principal=s.authenticate(team.tokens["alice"]))
    assert s.send("k:alice2", "front", "mine") == 1  # alice's "front", not bob's
    assert [m.text for m in s.inbox(team.keys["alice"])] == ["mine"]
    assert s.inbox(team.keys["bob"]) == []
    bob_name = team.names["bob"]
    with s.lock:
        s.db.execute("UPDATE agents SET last_seen=last_seen-100 WHERE key=?", (team.keys["bob"],))
    with pytest.raises(KaptyorkaError) as offline:
        s.send("k:alice2", bob_name, "x")
    with pytest.raises(KaptyorkaError) as unknown:
        s.send("k:alice2", "nobody", "x")
    assert str(offline.value) == str(unknown.value).replace("nobody", bob_name)


def test_feed_marks_messages_from_and_to_the_manager(team):
    """The page keeps a member's manager chat while the manager sleeps and its name is masked:
    each message says whether it is from or to the manager's sessions."""
    s = team.store
    with s.lock:
        for frm, frm_name, to, to_name in (("web:alice", "alice", "k:manager", team.names["manager"]),
                                           ("k:manager", team.names["manager"], "web:alice", "alice"),
                                           ("k:alice", team.names["alice"], "web:alice", "alice")):
            s.db.execute("INSERT INTO messages (sender_key, sender_name, to_key, to_name, machine, tool,"
                         " project, text, created) VALUES (?,?,?,?,'box-1','codex','proj-a','x',1)",
                         (frm, frm_name, to, to_name))
    for who in ("alice", "admin"):
        got = [(m["from_manager"], m["to_manager"]) for m in feed(team, who)["messages"]]
        assert got == [(False, True), (True, False), (False, False)], who


def test_member_page_sweep_shows_only_their_own(team):
    """A member's page and tools: their own sessions and chats, the manager, and nothing of
    another person's except the content-less Others list used to ask the admin."""
    s = team.store
    s.project_register("proj-b", by="admin")
    s.grant_project("proj-b", team.ids["bob"], "write", "admin")
    s.heartbeat("box-2", [{"key": "k:bob2", "tool": "codex", "project": "proj-b",
                           "label": "bob-secret", "activity": "bob-activity"}],
                principal=s.authenticate(team.tokens["bob"]), facts={"host": "box-2", "os_user": "bob"})
    s.set_status("k:bob2", "bob-status")
    s.send("k:bob2", team.names["bob"], "bob-private-text")
    s.send(team.keys["admin"], s.agent("k:bob2").name, "admin-to-bob-text")
    s.notes_write("k:bob2", "bob-plan", "bob-note-text", "proj-b")
    body = feed(team, "alice")
    assert {a["person"] for a in body["agents"]} == {"alice", "manager"}
    assert all(m["sender"] not in (team.names["bob"], "bob") for m in body["messages"])
    dump = repr(body)
    for secret in ("bob-private-text", "admin-to-bob-text", "bob-note-text", "bob-plan",
                   "bob-status", "bob-activity"):
        assert secret not in dump, secret
    # Others: bob's sessions without content, to ask for a connection; never reachable
    assert body["others"] and all(o["reachable"] is False for o in body["others"])
    assert all("status" not in o and "activity" not in o for o in body["others"])
    rows = run(call(client(team, "alice"), me(team, "alice"), "who"))
    assert {r["person"] for r in rows} == {"alice", "manager"}
    with pytest.raises(RuntimeError):
        run(call(client(team, "alice"), me(team, "alice"), "send", to=team.names["bob"], text="x"))
    with pytest.raises(RuntimeError):
        run(call(client(team, "alice"), me(team, "alice"), "notes_read", topic="bob-plan",
                 project="proj-b"))
    status, _ = http.request("POST", team.hub.url + "/api/send", team.tokens["alice"],
                             {"to": s.agent("k:bob2").name, "text": "x"})
    assert status != 200
    assert s.inbox("k:bob2") and all(m.text != "x" for m in s.inbox("k:bob2"))
