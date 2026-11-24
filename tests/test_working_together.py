"""Working together: sessions that wrote to each other both ways in the last 30 minutes, and a
session with the workers it started, form one group in the feed; the group's conversation has
its own route. A viewer sees only the sessions they may already see: a partner they may not is
"another session" (a count), with nothing of it and none of its messages."""
from types import SimpleNamespace

import pytest

from conftest import start_hub
from helpers import person
from kaptyorka import http
from kaptyorka.hub_feed import feed
from kaptyorka.hub_groups import WINDOW_S
from kaptyorka.manager import ManagerStore
from kaptyorka.store import Store, session_id


def said(store, a, b, text="hi", ago=0.0):
    """One message from session `a` to session `b` (straight in the table, as the hub keeps it)."""
    with store.lock:
        r = store.db.execute("SELECT * FROM agents WHERE key=?", (b,)).fetchone()
        s = store.db.execute("SELECT * FROM agents WHERE key=?", (a,)).fetchone()
        return store.db.execute(
            "INSERT INTO messages (sender_key, sender_name, to_key, to_name, machine, tool, project,"
            " text, created) VALUES (?,?,?,?,?,?,?,?,?)",
            (a, s["name"], b, r["name"], r["machine"], r["tool"], r["project"], text,
             store.clock() - ago)).lastrowid


def both(store, a, b, ago=0.0):
    said(store, a, b, "ping", ago)
    said(store, b, a, "pong", ago)


def worker(store, key, boss_key, boss_person, name="w-1", ago=0.0):
    with store.lock:
        store.db.execute(
            "INSERT INTO workers (name, project, task, boss_key, boss_person, device_id, host, tool,"
            " folder, state, created, key) VALUES (?, 'proj-a', 'a task', ?, ?, 0, 'box-1',"
            " 'codex', '/w', 'up', ?, ?)", (name, boss_key, boss_person, store.clock() - ago, key))


@pytest.fixture
def team(tmp_path):
    """alice (admin), bob (write on proj-a), carol (no role); bob has b1 and b2 in proj-a,
    carol c1 in proj-c, alice a1; the team machine box-1 has t-a (proj-a) and t-b (proj-b)."""
    s = Store(tmp_path / "hub.db")
    ManagerStore(s).set_enabled(False, "test")
    ids, tok = {}, {}
    for name, role in (("alice", "admin"), ("bob", "member"), ("carol", "member")):
        ids[name], tok[name] = person(s, name, role)
    for p in ("proj-a", "proj-b", "proj-c"):
        s.project_register(p, by="alice")
    s.grant_project("proj-a", ids["bob"], "write", "alice")
    s.grant_project("proj-c", ids["carol"], "write", "alice")
    s.seen("b1", "pc-b", "codex", "proj-a", principal=s.authenticate(tok["bob"]))
    s.seen("b2", "pc-b", "codex", "proj-a", principal=s.authenticate(tok["bob"]))
    s.seen("c1", "pc-c", "codex", "proj-c", principal=s.authenticate(tok["carol"]))
    s.seen("a1", "pc-a", "codex", "proj-a", principal=s.authenticate(tok["alice"]))
    tid, code = s.team_machine_add("box-1", "alice")
    ttoken, _ = s.invite_redeem(code, "box-1", host="box-1", os_user="u")
    s.seen("t-a", "box-1", "claude", "proj-a", principal=s.authenticate(ttoken))
    s.seen("t-b", "box-1", "claude", "proj-b", principal=s.authenticate(ttoken))
    yield SimpleNamespace(store=s, ids=ids, tok=tok, team_id=tid)
    s.close()


def groups(t, who):
    return feed(t.store, t.store.authenticate(t.tok[who]))["groups"]


def ids(*keys):
    return [session_id(k) for k in keys]


def member_ids(g):
    return [m["id"] for m in g["members"]]


def test_both_ways_in_the_window_makes_a_group(team):
    s = team.store
    said(s, "b1", "b2")
    assert groups(team, "bob") == []  # one way only: not yet together
    said(s, "b2", "b1")
    [g] = groups(team, "bob")
    assert member_ids(g) == ids("b1", "b2") and g["project"] == "proj-a" and g["others"] == 0
    assert g["id"] == "g" + session_id("b1")[1:] and g["active_s"] == 0 and g["last_id"] > 0
    assert all("key" not in m for m in g["members"])  # the hub's keys never reach the page


def test_quiet_for_30_minutes_dissolves(team):
    both(team.store, "b1", "b2", ago=WINDOW_S + 5)
    assert groups(team, "bob") == []


def test_linked_sessions_join_one_group(team):
    s = team.store
    both(s, "b1", "b2")
    both(s, "b2", "t-a")
    [g] = groups(team, "bob")
    assert sorted(member_ids(g)) == sorted(ids("b1", "b2", "t-a")) and g["project"] == "proj-a"


def test_a_worker_stays_with_its_boss(team):
    s = team.store
    worker(s, "b2", "b1", team.ids["bob"])
    [g] = groups(team, "bob")  # no message needed
    assert g["members"] == [{"id": session_id("b1")}, {"id": session_id("b2"), "boss": session_id("b1")}]
    with s.lock:
        s.db.execute("UPDATE workers SET state='closed'")
    assert groups(team, "bob") == []


def test_the_manager_is_never_in_a_group(team):
    s = team.store
    mtoken = ManagerStore(s).ensure_account("box-1", "alice")
    s.seen("m1", "box-1", "claude", "manager", principal=s.authenticate(mtoken))
    both(s, "m1", "b1")
    both(s, "m1", "c1")
    assert groups(team, "alice") == []  # else it would join everyone's work into one


def test_member_sees_a_hidden_partner_as_another_session(team):
    s = team.store
    both(s, "b1", "c1")  # bob may not see carol's session, nor carol bob's
    [g] = groups(team, "bob")
    assert member_ids(g) == ids("b1") and g["others"] == 1 and g["project"] == "proj-a"
    [g] = groups(team, "carol")
    assert member_ids(g) == ids("c1") and g["others"] == 1
    [g] = groups(team, "alice")  # the admin sees both
    assert sorted(member_ids(g)) == sorted(ids("b1", "c1")) and g["others"] == 0
    assert g["project"] == "mixed"
    text = str(groups(team, "bob"))
    assert session_id("c1") not in text and "proj-c" not in text and "pc-c" not in text


def test_no_link_through_a_hidden_session(team):
    """b1 and t-b (hidden from bob: proj-b) and t-b and b2: bob is not told b1 and b2 share a
    partner; each shows alone with another session."""
    s = team.store
    both(s, "b1", "t-b")
    both(s, "t-b", "b2")
    got = groups(team, "bob")
    assert sorted(member_ids(g) for g in got) == sorted([ids("b1"), ids("b2")])
    assert all(g["others"] == 1 for g in got)
    [g] = groups(team, "alice")
    assert len(g["members"]) == 3


def test_hidden_team_machine(team):
    s = team.store
    both(s, "b1", "t-a")
    [g] = groups(team, "bob")
    assert sorted(member_ids(g)) == sorted(ids("b1", "t-a"))
    s.team_machine_set_visible("box-1", "bob", False, "alice")
    [g] = groups(team, "bob")
    assert member_ids(g) == ids("b1") and g["others"] == 1
    assert session_id("t-a") not in str(g)


def test_a_project_the_viewer_cannot_read_is_not_named(team):
    """t-b and t-b's partner both in proj-b for the admin; a group bob sees by a door has the
    title "mixed", never a project he has no role in."""
    s = team.store
    s.seen("c2", "pc-c", "codex", "proj-c", principal=s.authenticate(team.tok["carol"]))
    s.open_door_group(team.ids["bob"], team.ids["carol"], "alice")
    both(s, "c1", "c2")
    [g] = groups(team, "bob")
    assert sorted(member_ids(g)) == sorted(ids("c1", "c2")) and g["project"] == "mixed"
    [g] = groups(team, "carol")
    assert g["project"] == "proj-c"


@pytest.fixture
def hub(team):
    stop = start_hub(team.store)
    yield SimpleNamespace(url=f"http://127.0.0.1:{stop.port}", **vars(team))
    stop()


def conv(hub, who, gid):
    return http.request("GET", f"{hub.url}/api/groups/{gid}/messages", hub.tok[who])


def test_conversation_route(hub):
    s = hub.store
    both(s, "b1", "b2")
    said(s, "b1", "a1", "not in the group")
    [g] = groups(hub, "bob")
    status, body = conv(hub, "bob", g["id"])
    assert status == 200
    assert [(m["sender"], m["to"], m["text"]) for m in body["messages"]] == [
        (s.agent("b1").name, s.agent("b2").name, "ping"), (s.agent("b2").name, s.agent("b1").name, "pong")]
    assert conv(hub, "carol", g["id"])[0] == 404  # not her group
    assert conv(hub, "alice", g["id"])[0] == 200  # the admin's
    assert conv(hub, "bob", "gnope")[0] == 404
    assert http.request("GET", f"{hub.url}/api/groups/{g['id']}/messages", "")[0] == 401


def test_conversation_leaves_out_a_hidden_partner(hub):
    s = hub.store
    both(s, "b1", "b2")
    both(s, "b2", "c1")  # carol's: hidden from bob
    [g] = groups(hub, "bob")
    assert g["others"] == 1
    _, body = conv(hub, "bob", g["id"])
    assert {m["text"] for m in body["messages"]} == {"ping", "pong"} and len(body["messages"]) == 2
    assert "c1" not in str(body) and s.agent("c1").name not in str(body)
    [ga] = groups(hub, "alice")
    _, body = conv(hub, "alice", ga["id"])
    assert len(body["messages"]) == 4  # the admin: all of it
    # carol's own view of the same work: her session alone, and none of bob's words
    [gc] = groups(hub, "carol")
    _, body = conv(hub, "carol", gc["id"])
    assert body["messages"] == []


def test_page_draws_groups_and_holds_still(hub):
    """The page side (driven for real in test_page_together_slow.py): the block under the
    manager, the group's chat from the hub's route, the header's partners, the list matched by
    key instead of redrawn, and a grace of 2 polls for a session or group that went missing."""
    _, html = http.request("GET", hub.url + "/", "")
    assert 'id="chat-with"' in html and '"Working with"' in html and '"Working together"' in html
    assert 'get("/api/groups/" + encodeURIComponent(id) + "/messages")' in html
    assert "var GRACE = 2," in html and "feed = steady(JSON.parse(text));" in html
    assert "morph(live, box);" in html and "box.textContent = \"\";\n    box.removeAttribute" not in html
    assert '@keyframes wt-pulse' in html and ".working.kept { animation: none; }" in html
