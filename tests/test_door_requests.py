"""Door requests: the manager or a member's session files one, the admin decides it (two
sessions, or two people for 7 days), two sessions of one shared project get theirs at once,
and a session door lasts through a short drop. A fake clock."""
from types import SimpleNamespace

import pytest
from helpers import call, call_error, person, run
from test_hub_manager import clock, hub  # noqa: F401 (fixtures)

from kaptyorka import http
from kaptyorka.store import UNREACHABLE

SESSIONS = {"alice": ("box-1", "proj-a", "front"), "bob": ("box-2", "proj-a", "rig"),
            "admin": ("box-3", "proj-x", "desk"), "manager": ("box-3", "manager", "")}


@pytest.fixture
def team(hub, clock):
    s = hub.store
    tokens, ids = {}, {}
    for name, role in (("alice", "member"), ("bob", "member"), ("admin", "admin"),
                       ("manager", "manager")):
        ids[name], tokens[name] = person(s, name, role)
    s.project_register("proj-a", by="admin")

    def beat(*who):
        for name in who:
            machine, project, label = SESSIONS[name]
            s.heartbeat(machine, [{"key": f"k:{name}", "tool": "codex", "project": project,
                                   "label": label}], principal=s.authenticate(tokens[name]),
                        facts={"host": machine, "os_user": name})

    beat(*SESSIONS)
    t = SimpleNamespace(hub=hub, s=s, clock=clock, tokens=tokens, ids=ids, beat=beat)
    t.name = lambda who: s.agent(f"k:{who}").name
    return t


def ident(who):
    machine, project, _ = SESSIONS[who]
    return {"key": f"k:{who}", "machine": machine, "tool": "codex", "project": project}


def tool(t, who, name, **args):
    return run(call(SimpleNamespace(url=t.hub.url, token=t.tokens[who]), ident(who), name, **args))


def err(t, who, name, **args):
    return call_error(SimpleNamespace(url=t.hub.url, token=t.tokens[who]), ident(who), name,
                      **args)


def texts(t, who):
    return [m["text"] for m in tool(t, who, "inbox")]


def page(t, who):
    with t.s.lock:
        return [r["text"] for r in t.s.db.execute("SELECT text FROM messages WHERE to_key=?",
                                                  (f"web:{who}",))]


def file_by_manager(t):
    """The manager files a door nobody it can verify asked for: it waits for the admin."""
    return tool(t, "manager", "door_request", a=t.name("alice"), b=t.name("bob"),
                why="alice needs bob's test logs")


def asks(t, who, text="please open a door to bob's rig"):
    tool(t, who, "send", to=t.name("manager"), text=text)


# the manager opens a teammate's own request at once

def test_manager_opens_a_door_for_the_asker(team):
    t = team
    asks(t, "alice")
    out = tool(t, "manager", "door_request", a=t.name("alice"), b=t.name("bob"),
               why="alice needs bob's test logs")
    assert out == (f"door open between {t.name('alice')} and {t.name('bob')}, for"
                   f" {t.name('alice')}; both sessions were told, the admin hears it later")
    assert (f"Connected: a door is open between this session and {t.name('alice')}"
            f" ({t.name('alice')} asked the manager: alice needs bob's test logs). Write with"
            f" send(to=\"{t.name('alice')}\"); it stays open while both sessions are around."
            f" Close it with door_close(\"{t.name('alice')}\").") in texts(t, "bob")
    assert any("(you asked the manager)" in x for x in texts(t, "alice"))
    assert tool(t, "alice", "send", to=t.name("bob"), text="hi") == "sent to 1 session"
    assert not [x for x in texts(t, "admin") if x.startswith("Door request")]
    with t.s.lock:
        [detail] = [r[0] for r in t.s.db.execute("SELECT detail FROM log WHERE kind='door_open'")]
    assert detail == (f"{t.name('alice')} <-> {t.name('bob')} (by manager for"
                      f" {t.name('alice')}: alice needs bob's test logs)")
    t.hub.tick()
    t.clock[0] += 301
    t.beat(*SESSIONS)
    t.hub.tick()
    assert [x for x in page(t, "admin") if x.startswith("Doors opened")] == [
        f"Doors opened without you: {detail}. Turn these off with kaptyorka auto-doors off or"
        " kaptyorka manager-doors off."]


def test_manager_opens_for_the_two_people_for_a_week(team):
    t = team
    asks(t, "bob")
    out = tool(t, "manager", "door_request", a=t.name("alice"), b=t.name("bob"), why="a week",
               asked_by=t.name("bob"), size="people")
    assert "for the two people, 7 days" in out
    with t.s.lock:
        assert t.s.db.execute("SELECT scope, expires_at FROM doors").fetchone()[:] == (
            "group", t.clock[0] + 7 * 86400)


def test_an_asker_who_owns_neither_side_goes_to_the_admin(team):
    t = team
    asks(t, "admin")  # the admin's session is neither alice's nor bob's
    out = tool(t, "manager", "door_request", a=t.name("alice"), b=t.name("bob"), why="x",
               asked_by=t.name("admin"))
    assert out.startswith("door request #1 filed")
    assert err(t, "alice", "send", to=t.name("bob"), text="hi").endswith(
        UNREACHABLE.format(to=t.name("bob")))


def test_a_spoofed_asker_goes_to_the_admin(team):
    t = team
    asks(t, "alice", "hello")  # someone wrote, but bob never did
    out = tool(t, "manager", "door_request", a=t.name("alice"), b=t.name("bob"), why="x",
               asked_by=t.name("bob"))
    assert out.startswith("door request #1 filed")
    t.clock[0] += 601  # and alice's message is too old to count now
    t.beat(*SESSIONS)
    assert tool(t, "manager", "door_request", a=t.name("alice"), b=t.name("bob"), why="x",
                asked_by=t.name("alice")).endswith("already waits for the admin")


def test_a_hidden_team_machine_goes_to_the_admin(team):
    from kaptyorka.store import Principal
    t = team
    jet, _ = t.s.team_machine_add("jet-1", "admin")
    t.s.seen("k:jet", "jet-1", "codex", "proj-x",
             principal=Principal(jet, "jet-1", "member", 0, "jet-1", True, False))
    t.s.team_machine_set_visible("jet-1", "alice", False, "admin")
    asks(t, "alice")
    out = tool(t, "manager", "door_request", a=t.name("alice"), b=t.s.agent("k:jet").name,
               why="x")
    assert out.startswith("door request #1 filed")
    # a member naming it gets the answer for a name it cannot reach
    assert err(t, "alice", "door_request", a="", b=t.s.agent("k:jet").name, why="x").endswith(
        UNREACHABLE.format(to=t.s.agent("k:jet").name))


def test_manager_doors_off_goes_to_the_admin(team):
    t = team
    assert http.request("POST", t.hub.url + "/api/settings", t.tokens["admin"],
                        {"manager_doors": False})[1]["manager_doors"] is False
    asks(t, "alice")
    assert tool(t, "manager", "door_request", a=t.name("alice"), b=t.name("bob"),
                why="x").startswith("door request #1 filed")


def test_a_removed_person_gets_no_door(team):
    t = team
    with t.s.lock:
        t.s.db.execute("UPDATE people SET removed_at=1 WHERE name='bob'")
    asks(t, "alice")
    assert "no longer on the team" in err(t, "manager", "door_request", a=t.name("alice"),
                                          b=t.name("bob"), why="x")


# filed for the admin

def test_manager_files_and_the_admin_gets_one_line(team):
    t = team
    assert file_by_manager(t).startswith("door request #1 filed; it waits for the admin")
    [line] = [x for x in texts(t, "admin") if x.startswith("Door request")]
    assert line == (f"Door request #1: {t.name('alice')} (alice, box-1, proj-a) and"
                    f" {t.name('bob')} (bob, box-2, proj-a): alice needs bob's test logs."
                    " Answer with door_decide(1, \"sessions\" |"
                    " \"people\" | \"deny\"): the two sessions, the two people for 7 days, or"
                    " no. Also on the page.")
    status, feed = http.request("GET", t.hub.url + "/api/feed", t.tokens["admin"])
    assert [d["id"] for d in feed["door_requests"]] == [1]
    assert "door_requests" not in http.request("GET", t.hub.url + "/api/feed",
                                               t.tokens["alice"])[1]
    # the same pair again: no second request
    assert tool(t, "manager", "door_request", a=t.name("bob"), b=t.name("alice"),
                why="again") == "door request #1 for these two already waits for the admin"
    assert "only the admin" in err(t, "alice", "door_decide", request_id=1, how="sessions")
    assert "only the admin" in err(t, "manager", "door_decide", request_id=1, how="sessions")


def test_a_member_files_for_its_own_session_only(team):
    t = team
    out = tool(t, "alice", "door_request", a="", b=t.name("bob"), why="compare runs")
    assert out.startswith("door request #1 filed")
    assert any(x.endswith(f"Asked by {t.name('alice')}. Answer with door_decide(1, \"sessions\" |"
                          " \"people\" | \"deny\"): the two sessions, the two people for 7"
                          " days, or no. Also on the page.") for x in texts(t, "admin"))
    # never for someone else's session, nor to a name it cannot know
    assert err(t, "alice", "door_request", a=t.name("bob"), b=t.name("admin"),
               why="x").endswith(UNREACHABLE.format(to=t.name("bob")))
    assert err(t, "alice", "door_request", a="", b="rig", why="x").endswith(
        UNREACHABLE.format(to="rig"))  # another person's label: never matched for a member
    assert err(t, "alice", "door_request", a="", b="box-9/codex/x", why="x").endswith(
        UNREACHABLE.format(to="box-9/codex/x"))
    assert "say why" in err(t, "alice", "door_request", a="", b=t.name("admin"), why="")


# deciding

def test_allow_the_two_sessions(team):
    t = team
    file_by_manager(t)
    assert tool(t, "admin", "door_decide", request_id=1, how="sessions") \
        == "door request #1: allowed (sessions)"
    assert any(x.startswith("Connected: the admin approved your request #1") for x in texts(t, "alice"))
    assert any(x.startswith("Connected: the admin opened a door") for x in texts(t, "bob"))
    assert any(x.startswith("door request #1 (") and x.endswith("allowed for the two sessions")
               for x in texts(t, "manager"))
    assert tool(t, "alice", "send", to=t.name("bob"), text="hi") == "sent to 1 session"
    assert "already decided" in err(t, "admin", "door_decide", request_id=1, how="deny")


def test_allow_the_two_people_for_seven_days(team):
    t = team
    file_by_manager(t)
    assert tool(t, "admin", "door_decide", request_id=1, how="people") \
        == "door request #1: allowed (people)"
    assert "Connected: the admin let alice and bob message each other's sessions for 7 days" \
        " (request #1)." in texts(t, "bob")
    assert tool(t, "alice", "send", to=t.name("bob"), text="hi") == "sent to 1 session"
    t.clock[0] += 7 * 86400 + 1
    t.beat("alice", "bob")
    assert err(t, "alice", "send", to=t.name("bob"), text="hi").endswith(
        UNREACHABLE.format(to=t.name("bob")))


def test_existing_group_doors_stay_unlimited(team):
    t = team
    did = t.s.open_door_group(t.ids["alice"], t.ids["bob"], "admin")
    t.clock[0] += 30 * 86400
    t.beat("alice", "bob")
    t.s.expire_doors(t.clock[0])
    with t.s.lock:
        assert t.s.db.execute("SELECT closed_at FROM doors WHERE id=?", (did,)).fetchone()[0] is None


def test_deny(team):
    t = team
    file_by_manager(t)
    assert tool(t, "admin", "door_decide", request_id=1, how="deny", reason="not now") \
        == "door request #1: denied"
    assert any("was denied: not now" in x for x in texts(t, "alice"))
    assert any(x.endswith("denied: not now") for x in texts(t, "manager"))
    assert err(t, "alice", "send", to=t.name("bob"), text="hi").endswith(
        UNREACHABLE.format(to=t.name("bob")))


def test_the_page_allows_with_either_size(team):
    t = team
    file_by_manager(t)
    assert http.request("POST", t.hub.url + "/api/connects/1", t.tokens["admin"],
                        {"approve": True, "how": "people"})[0] == 204
    with t.s.lock:
        row = t.s.db.execute("SELECT scope, expires_at FROM doors").fetchone()
    assert row[0] == "group" and row[1] == t.clock[0] + 7 * 86400


def test_reminder_once_after_an_hour(team):
    t = team
    file_by_manager(t)
    texts(t, "admin")
    t.clock[0] += 3599
    t.beat(*SESSIONS)
    t.hub.tick()
    assert [x for x in texts(t, "admin") if x.startswith("Still waiting")] == []
    t.clock[0] += 2
    t.beat(*SESSIONS)
    t.hub.tick()
    assert len([x for x in texts(t, "admin") if x.startswith("Still waiting for you: Door"
                                                               " request #1")]) == 1
    t.clock[0] += 7200
    t.beat(*SESSIONS)
    t.hub.tick()
    assert [x for x in texts(t, "admin") if x.startswith("Still waiting")] == []


# same project: at once

def roles(t, *who):
    for name in who:
        t.s.grant_project("proj-a", t.ids[name], "write", "admin")


def test_same_project_opens_at_once_and_tells_the_admin_after(team):
    t = team
    roles(t, "alice", "bob")
    out = tool(t, "manager", "door_request", a=t.name("alice"), b=t.name("bob"), why="pairing")
    assert out == (f"door open between {t.name('alice')} and {t.name('bob')}: same project"
                   " proj-a; both sessions were told")
    assert any(x.startswith("Connected: a door is open between this session and"
                            f" {t.name('bob')} (same project proj-a)") for x in texts(t, "alice"))
    assert tool(t, "alice", "send", to=t.name("bob"), text="hi") == "sent to 1 session"
    assert not [x for x in texts(t, "admin") if x.startswith("Door request")]
    with t.s.lock:
        details = [r[0] for r in t.s.db.execute("SELECT detail FROM log WHERE kind='door_open'")]
    assert details == [f"{t.name('alice')} <-> {t.name('bob')} (auto: same project proj-a)"]
    t.hub.tick()  # the first tick starts counting
    assert not [x for x in page(t, "admin") if x.startswith("Doors opened")]
    t.clock[0] += 301
    t.beat(*SESSIONS)
    t.hub.tick()
    assert [x for x in page(t, "admin") if x.startswith("Doors opened")] == [
        f"Doors opened without you: {details[0]}. Turn these off with kaptyorka auto-doors off or"
        " kaptyorka manager-doors off."]


def test_same_project_needs_both_roles_and_the_project(team):
    t = team
    roles(t, "alice")  # bob has no role in proj-a
    assert tool(t, "alice", "door_request", a="", b=t.name("bob"), why="x").startswith(
        "door request #1 filed")


def test_a_coordinator_or_other_project_never_opens_by_itself(team):
    t = team
    roles(t, "alice", "bob")
    t.s.heartbeat("box-2", [{"key": "k:bob2", "tool": "codex", "project": "(team)"}],
                  principal=t.s.authenticate(t.tokens["bob"]), facts={"host": "box-2", "os_user": "bob"})
    out = tool(t, "alice", "door_request", a="", b=t.s.agent("k:bob2").name, why="x")
    assert out.startswith("door request #1 filed")


def test_auto_doors_off_sends_it_to_the_admin(team):
    t = team
    roles(t, "alice", "bob")
    assert http.request("POST", t.hub.url + "/api/settings", t.tokens["admin"],
                        {"auto_doors": False})[1]["auto_doors"] is False
    assert tool(t, "alice", "door_request", a="", b=t.name("bob"), why="x").startswith(
        "door request #1 filed")
    with t.s.lock:
        assert t.s.db.execute("SELECT detail FROM log WHERE kind='settings'").fetchone()[0] \
            == "auto_doors=off"


# a session door through a short drop

def test_a_session_door_survives_a_short_drop_and_closes_after_ten_minutes(team):
    t = team
    file_by_manager(t)
    tool(t, "admin", "door_decide", request_id=1, how="sessions")
    t.clock[0] += 300  # bob drops for 5 minutes
    t.beat("alice", "admin", "manager")
    t.s.expire_doors(t.clock[0])
    assert tool(t, "alice", "send", to=t.name("bob"), text="still there?").startswith("held:")
    t.beat("bob")
    assert tool(t, "alice", "send", to=t.name("bob"), text="hi") == "sent to 1 session"
    t.clock[0] += 601  # now gone over 10 minutes
    t.beat("alice", "admin", "manager")
    t.s.expire_doors(t.clock[0])
    t.beat("bob")
    assert err(t, "alice", "send", to=t.name("bob"), text="hi").endswith(
        UNREACHABLE.format(to=t.name("bob")))
