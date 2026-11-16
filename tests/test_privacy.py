"""Private by default: a member sees and reaches only their own sessions, the manager's and
those behind a door the admin opened."""
from types import SimpleNamespace

import pytest
from helpers import call, call_error, person, run

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
    labels = {"alice": "front", "bob": "back", "admin": "boss-desk", "manager": "hq"}
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


def test_member_offline_or_unknown_name_same_refusal(team):
    s = team.store
    with pytest.raises(KaptyorkaError) as unknown:
        with s.lock:
            s._targets(team.keys["alice"], "nobody", s.clock(), team.ids["alice"])
    with pytest.raises(KaptyorkaError) as bobs:
        with s.lock:
            s._targets(team.keys["alice"], team.names["bob"], s.clock(), team.ids["alice"])
    assert str(bobs.value) == str(unknown.value).replace("nobody", team.names["bob"])
