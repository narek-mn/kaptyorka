"""Working together: sessions that really work together (at least 4 real messages each way over
15 minutes or more, within a day) and a session with the workers it started form one group in
the feed, until a day after their last real message or until one goes offline; the group's
conversation has its own route. A viewer sees only the sessions they may already see: a partner they may not is
"another session" (a count), with nothing of it and none of its messages."""
from types import SimpleNamespace

import pytest

from conftest import start_hub
from helpers import person
from kaptyorka import http
from kaptyorka.hub_feed import feed
import time

from kaptyorka.hub_groups import ACTIVE_S, LINK_S, MIN_SPAN_S
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
    """Real work between two sessions: 4 messages each way of over 20 characters, over 20
    minutes, the last `ago` seconds ago."""
    for i in range(4):
        t = ago + 1200 - i * 400
        said(store, a, b, f"part {i}: here is what I found in the parser", t + 100)
        said(store, b, a, f"part {i}: thanks, I changed the tests to match", t)


def keep_online(team, *keys):
    """The sessions report again (after the fake clock moved on)."""
    with team.store.lock:
        team.store.db.execute(f"UPDATE agents SET last_seen=? WHERE key IN ({','.join('?' * len(keys))})",
                              (team.store.clock(), *keys))


def later(team, seconds, *online):
    team.clock[0] += seconds
    keep_online(team, *online)


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
    clock = [time.time()]
    s.clock = lambda: clock[0]  # a fake clock: tests move it on with later()
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
    yield SimpleNamespace(store=s, ids=ids, tok=tok, team_id=tid, clock=clock)
    s.close()


def groups(t, who):
    return feed(t.store, t.store.authenticate(t.tok[who]))["groups"]


def ids(*keys):
    return [session_id(k) for k in keys]


def member_ids(g):
    return [m["id"] for m in g["members"]]


REAL = "here is the next part of the change, please check it"


def test_real_work_both_ways_makes_a_group(team):
    s = team.store
    both(s, "b1", "b2")
    [g] = groups(team, "bob")
    assert member_ids(g) == ids("b1", "b2") and g["project"] == "proj-a" and g["others"] == 0
    assert g["id"] == "g" + session_id("b1")[1:] and g["active_s"] == 0 and g["last_id"] > 0
    assert g["quiet"] is False
    assert all("key" not in m for m in g["members"])  # the hub's keys never reach the page


def test_three_each_way_is_no_link(team):
    s = team.store
    for i in range(3):
        said(team.store, "b1", "b2", REAL, 1500 - i * 500)
        said(team.store, "b2", "b1", REAL, 1450 - i * 500)
    assert groups(team, "bob") == []
    said(s, "b1", "b2", REAL)
    assert groups(team, "bob") == []  # 4 one way, 3 the other
    said(s, "b2", "b1", REAL)
    assert len(groups(team, "bob")) == 1


def test_four_each_way_within_ten_minutes_is_no_link(team):
    for i in range(4):
        said(team.store, "b1", "b2", REAL, 600 - i * 150)
        said(team.store, "b2", "b1", REAL, 590 - i * 150)
    assert groups(team, "bob") == []  # a quick exchange, not work together


def test_short_messages_do_not_count(team):
    """ok, hi, an emoji or a file with no words: never counted, however many."""
    for i in range(6):
        for text in ("ok", "hi  ", "\U0001F44D", "", "  thanks a lot!  \n"):
            said(team.store, "b1", "b2", text, 2000 - i * 300)
            said(team.store, "b2", "b1", text, 1990 - i * 300)
    assert groups(team, "bob") == []


def test_one_question_and_answer_to_five_sessions_links_nobody(team):
    s = team.store
    for i in range(5):
        s.seen(f"w{i}", "pc-b", "codex", "proj-a", principal=s.authenticate(team.tok["bob"]))
        said(s, "b1", f"w{i}", "which branch has the fix for the import error?", 1800)
        said(s, f"w{i}", "b1", "the branch fix-imports, merged into main yesterday", 1000)
    assert groups(team, "bob") == []


def test_a_quiet_stretch_keeps_the_group(team):
    both(team.store, "b1", "b2")
    later(team, 5 * 3600, "b1", "b2")
    [g] = groups(team, "bob")
    assert g["quiet"] is True and g["active_s"] == 5 * 3600
    later(team, -5 * 3600 + ACTIVE_S - 60, "b1", "b2")
    assert groups(team, "bob")[0]["quiet"] is False  # under 30 minutes: still active


def test_a_day_after_the_last_real_message_it_ends(team):
    both(team.store, "b1", "b2")
    later(team, LINK_S - 60, "b1", "b2")
    assert len(groups(team, "bob")) == 1
    later(team, 120, "b1", "b2")
    assert groups(team, "bob") == []


def test_it_ends_when_one_session_goes_offline(team):
    both(team.store, "b1", "b2")
    later(team, 120, "b1")  # b2 stopped reporting
    assert groups(team, "bob") == []
    keep_online(team, "b2")  # back under the same key: the rule still holds
    assert len(groups(team, "bob")) == 1


def test_the_span_is_counted_from_first_to_last(team):
    assert MIN_SPAN_S == 900
    for i in range(4):
        said(team.store, "b1", "b2", REAL, 1000 - i * 30)
        said(team.store, "b2", "b1", REAL, 990 - i * 30)
    assert groups(team, "bob") == []  # 4 each way, 2 minutes apart
    said(team.store, "b1", "b2", REAL, 0)  # the same exchange goes on 16 minutes later
    assert len(groups(team, "bob")) == 1


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


def test_own_session_with_a_door_partner(team):
    """bob's own session and carol's behind a door: shown with her session named, the title
    "mixed", never a project he has no role in."""
    s = team.store
    s.open_door_group(team.ids["bob"], team.ids["carol"], "alice")
    both(s, "b1", "c1")
    [g] = groups(team, "bob")
    assert sorted(member_ids(g)) == sorted(ids("b1", "c1")) and g["project"] == "mixed"
    assert g["others"] == 0


def door_people(team):
    """bob has open doors with carol and with dave; carol's c1 and dave's d1."""
    s = team.store
    dave, dtok = person(s, "dave")
    s.seen("d1", "pc-d", "codex", "proj-d", principal=s.authenticate(dtok))
    s.open_door_group(team.ids["bob"], team.ids["carol"], "alice")
    s.open_door_group(team.ids["bob"], dave, "alice")


def second_team_session(team):
    s = team.store
    s.seen("t-a2", "box-1", "codex", "proj-a",
           principal=s.authenticate(s.device_add(team.team_id, "box-1b")))


def test_a_door_never_shows_two_other_people_talking(team):
    s = team.store
    door_people(team)
    both(s, "c1", "d1")
    assert groups(team, "bob") == []  # no group, no "another session"
    assert len(groups(team, "alice")) == 1  # the admin sees it


def test_two_team_machine_sessions_in_my_project(team):
    second_team_session(team)
    both(team.store, "t-a", "t-a2")
    [g] = groups(team, "bob")
    assert sorted(member_ids(g)) == sorted(ids("t-a", "t-a2"))
    assert groups(team, "carol") == []  # no role in proj-a


def test_mixed_group_keeps_only_readable_links(team):
    """b1 - c1 (door), c1 - d1 (two others), t-a - b2: bob sees b1 with c1, and b2 with t-a;
    d1 is in no group of his, not even as another session."""
    s = team.store
    door_people(team)
    both(s, "b1", "c1")
    both(s, "c1", "d1")
    both(s, "t-a", "b2")
    got = sorted(sorted(member_ids(g)) for g in groups(team, "bob"))
    assert got == sorted([sorted(ids("b1", "c1")), sorted(ids("b2", "t-a"))])
    assert all(g["others"] == 0 for g in groups(team, "bob"))
    assert session_id("d1") not in str(groups(team, "bob"))
    [g] = [g for g in groups(team, "alice") if len(g["members"]) == 3]
    assert sorted(member_ids(g)) == sorted(ids("b1", "c1", "d1"))


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
    got = [(m["sender"], m["to"]) for m in body["messages"]]
    assert len(got) == 8 and set(got) == {(s.agent("b1").name, s.agent("b2").name), (s.agent("b2").name, s.agent("b1").name)}
    assert "not in the group" not in str(body)
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
    assert len(body["messages"]) == 8
    assert "c1" not in str(body) and s.agent("c1").name not in str(body)
    [ga] = groups(hub, "alice")
    _, body = conv(hub, "alice", ga["id"])
    assert len(body["messages"]) == 16  # the admin: all of it
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


def test_conversation_never_shows_two_other_people_talking(hub):
    """The door case on the route: carol and dave talk; bob's group of b1 and c1 returns only
    what b1 and c1 said, and no other group id opens carol and dave's talk for him."""
    s = hub.store
    door_people(hub)
    both(s, "c1", "d1")
    both(s, "b1", "c1")
    [g] = groups(hub, "bob")
    _, body = conv(hub, "bob", g["id"])
    c1 = "pc-c/codex/(private)"  # her project stays hidden from him
    got = [(m["sender"], m["to"]) for m in body["messages"]]
    assert len(got) == 8 and set(got) == {(s.agent("b1").name, c1), (c1, s.agent("b1").name)}
    [ga] = groups(hub, "alice")  # the admin's one group of the three
    assert len(conv(hub, "alice", ga["id"])[1]["messages"]) == 16
    for gid in {ga["id"], "g" + session_id("c1")[1:], "g" + session_id("d1")[1:]} - {g["id"]}:
        assert conv(hub, "bob", gid)[0] == 404


def test_conversation_of_team_machine_sessions(hub):
    second_team_session(hub)
    both(hub.store, "t-a", "t-a2")
    [g] = groups(hub, "bob")
    assert len(conv(hub, "bob", g["id"])[1]["messages"]) == 8
