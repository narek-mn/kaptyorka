"""Team machines: one shared OS account everyone logs into, owned by the team. Its sessions are
seen and messaged by everyone with a role in their project, and by nobody else."""
import pytest

from helpers import person
from kaptyorka import admin_ops, usage
from kaptyorka.hub_feed import feed
from kaptyorka.store import KaptyorkaError, Principal, Store, session_id
from kaptyorka.workers import WorkerStore


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "hub.db")
    yield s
    s.close()


def who(store, name):
    p = store.person_by_name(name)
    return Principal(p["id"], name, p["role"], 0, "laptop", False, False)


@pytest.fixture
def team(store):
    """alice (admin), bob (write on proj-a), carol (no role), and the team machine jet-1 joined
    from its shared account, with one session in proj-a and one in proj-b."""
    person(store, "alice", "admin")
    bob, _ = person(store, "bob")
    person(store, "carol")
    store.project_register("proj-a", by="alice")
    store.project_register("proj-b", by="alice")
    store.grant_project("proj-a", bob, "write", "alice")
    pid, code = store.team_machine_add("jet-1", "alice")
    token, _ = store.invite_redeem(code, "jet-1", host="jet-1", os_user="nvidia")
    p = store.authenticate(token)
    store.seen("t-a", "jet-1", "claude", "proj-a", principal=p)
    store.seen("t-b", "jet-1", "claude", "proj-b", principal=p)
    return pid, p


def test_added_as_a_team_row_with_an_invite(store, team):
    pid, p = team
    jet = next(x for x in store.people() if x["name"] == "jet-1")
    assert jet["team"] and jet["role"] == "member" and p.person_id == pid
    assert admin_ops.log_sentence(next(r for r in store.log(20) if r["kind"] == "team_machine")) \
        == "alice added the team machine jet-1"
    with pytest.raises(KaptyorkaError, match="the name jet-1 is taken"):
        store.team_machine_add("jet-1", "alice")
    with pytest.raises(KaptyorkaError, match="jet-1 is a team machine; it has no role to change"):
        admin_ops.set_role(store, "jet-1", "admin", who(store, "alice"))


def test_sessions_seen_by_project_role_only(store, team):
    names = lambda name: sorted(a["name"] for a in feed(store, who(store, name))["agents"]
                                if a["person"] == "jet-1 (team)")
    assert len(names("bob")) == 1 and "proj-a" in names("bob")[0]  # bob: proj-a only
    assert names("carol") == []  # no role: sees none of it
    assert len(names("alice")) == 2  # the admin sees all
    assert store.listed(who(store, "bob").person_id, "t-a", team[0])
    assert not store.listed(who(store, "bob").person_id, "t-b", team[0])


def test_messaged_both_ways_by_project_role(store, team):
    bob, carol = who(store, "bob"), who(store, "carol")
    store.seen("b1", "pc-b", "codex", "proj-a", principal=store.authenticate(
        store.device_add(bob.person_id, "pc-b")))
    assert store.page_may_message("bob", bob.person_id, "t-a", team[0])
    assert not store.page_may_message("bob", bob.person_id, "t-b", team[0])
    assert not store.page_may_message("carol", carol.person_id, "t-a", team[0])
    assert store.may_message("b1", "t-a") and store.may_message("t-a", "b1")
    assert not store.may_message("t-b", "b1")
    assert store.send("t-a", store.agent("b1").name, "hello") == 1
    with pytest.raises(KaptyorkaError):
        store.send("t-b", store.agent("b1").name, "hello")


def test_never_owns_the_host_and_personal_accounts_stay_personal(store, team):
    with store.lock:
        store.db.execute("INSERT OR IGNORE INTO machines (host, owner_person_id, first_seen)"
                         " VALUES ('jet-1', NULL, 0)")
    store.heartbeat("jet-1", [], principal=team[1], facts={"host": "jet-1", "os_user": "nvidia"})
    assert store.db.execute("SELECT owner_person_id FROM machines WHERE host='jet-1'"
                            ).fetchone()[0] is None
    # bob's own OS account on the same box: a personal machine, his sessions his own
    bob = who(store, "bob")
    p = store.authenticate(store.device_add(bob.person_id, "jet-1-bob"))
    store.heartbeat("jet-1", [], principal=p, facts={"host": "jet-1", "os_user": "bob"})
    assert store.db.execute("SELECT owner_person_id FROM machines WHERE host='jet-1'"
                            ).fetchone()[0] == bob.person_id
    store.seen("bob-own", "jet-1", "codex", "proj-b", principal=p)
    assert not store.listed(who(store, "carol").person_id, "bob-own", bob.person_id)


def test_use_counts_as_the_team_account(store, team):
    use = {"users": {"nvidia": {"cpu": 2.0, "mem": 100}, "bob": {"cpu": 1.0, "mem": 10}}}
    out = usage.people_use(use, {"bob": ["bob"]}, {"nvidia"})
    assert set(out) == {"account nvidia (team)", "bob"}
    ws = WorkerStore(store)
    import json
    with store.lock:
        store.db.execute("INSERT INTO device_facts (device_id, facts, updated) VALUES (?,?,?)",
                         (team[1].device_id, json.dumps({"use": {"age": 0, "users": {
                             "nvidia": {"cpu": 1.0, "mem": 5}}}}), store.clock()))
    _, by = ws.host_use("jet-1", store.clock())
    assert list(by) == ["account nvidia (team)"]


def test_team_machine_cannot_sign_in_or_add_machines(store, team):
    from kaptyorka import signin
    with pytest.raises(KaptyorkaError, match="a team machine cannot sign in with GitLab"):
        signin.set_user(store, None, "jet-1", "jet-gl", who(store, "alice"))
    with pytest.raises(KaptyorkaError, match="cannot add a machine"):
        admin_ops.invite_self(store, team[1])


def test_overview_lists_hosts_people_team_and_use(store, team):
    import json
    ws = WorkerStore(store)
    bob = who(store, "bob")
    p = store.authenticate(store.device_add(bob.person_id, "jet-1-bob"))
    store.heartbeat("jet-1", [], principal=p, facts={"host": "jet-1", "os_user": "bob"})
    store.heartbeat("jet-1", [], principal=team[1], facts={"host": "jet-1", "os_user": "nvidia"})
    with store.lock:
        store.db.execute("INSERT INTO device_facts (device_id, facts, updated) VALUES (?,?,?)",
                         (team[1].device_id, json.dumps({"use": {
                             "age": 0, "cpus": 8, "mem_mb": 1000,
                             "gpus": [{"index": 0, "util": 90.0, "mem": 0, "total": 0}],
                             "users": {"nvidia": {"cpu": 2.0, "mem": 400}}}}), store.clock()))
    [m] = [m for m in ws.overview() if m["host"] == "jet-1"]
    assert m["people"] == ["bob"] and m["team"] == ["jet-1"] and m["online"]
    assert m["use"] == "GPU 90% · CPU 25% · memory 40%"
    assert sorted(d["name"] for d in m["devices"]) == ["bob@jet-1", "nvidia@jet-1"]  # account@computer
    assert usage.short(None) == "" and usage.short({"users": {}}) == ""
    assert m["kind"] == "GPU · 8 CPUs · 1 GB RAM"  # the size hint, from the last report


def test_overview_kind_is_the_computers_size_even_offline(store, team):
    """The Machines header's size hint: GPUs and their memory (a Jetson's one GPU reports no
    memory), CPUs and RAM, from the newest report however old; "" when none came."""
    import json
    big = {"cpus": 32, "mem_mb": 131072, "gpus": [{"index": i, "util": 1.0, "mem": 0, "total": 24576} for i in (0, 1)]}
    jet = {"cpus": 8, "mem_mb": 16384, "gpus": [{"index": 0, "util": 1.0, "mem": None, "total": None}]}
    assert usage.kind(big) == "2 GPUs 24 GB · 32 CPUs · 128 GB RAM"
    assert usage.kind(jet) == "Jetson · 8 CPUs · 16 GB RAM"
    assert usage.kind({"cpus": 4, "mem_mb": 8192}) == "4 CPUs · 8 GB RAM"
    assert usage.kind(None) == "" and usage.kind({}) == ""
    ws = WorkerStore(store)
    store.heartbeat("jet-1", [], principal=team[1], facts={"host": "jet-1", "os_user": "nvidia"})
    [m] = [m for m in ws.overview() if m["host"] == "jet-1"]
    assert m["kind"] == ""
    with store.lock:
        store.db.execute("INSERT INTO device_facts (device_id, facts, updated) VALUES (?,?,?)",
                         (team[1].device_id, json.dumps({"use": {**jet, "age": 0}}), store.clock() - 86400))
    [m] = [m for m in ws.overview() if m["host"] == "jet-1"]
    assert m["kind"] == "Jetson · 8 CPUs · 16 GB RAM" and m["use"] == ""  # a day old: no use line, the size stays


def test_move_a_machine_to_another_person(store, team):
    bob, carol = who(store, "bob"), who(store, "carol")
    p = store.authenticate(store.device_add(bob.person_id, "laptop-1"))
    store.heartbeat("lap", [], principal=p, facts={"host": "lap", "os_user": "bob"})
    assert store.db.execute("SELECT owner_person_id FROM machines WHERE host='lap'").fetchone()[0] \
        == bob.person_id
    store.seen("s-lap", "lap", "codex", "proj-a", principal=p)
    done = store.device_move(p.device_id, "carol", "alice")
    assert done == {"device": "laptop-1", "from": "bob", "to": "carol"}
    row = store.db.execute("SELECT person_id FROM devices WHERE id=?", (p.device_id,)).fetchone()
    assert row[0] == carol.person_id
    assert store.agent("s-lap").person_id == carol.person_id  # its session moved with it
    assert store.db.execute("SELECT owner_person_id FROM machines WHERE host='lap'").fetchone()[0] \
        == carol.person_id
    assert admin_ops.log_sentence(store.log(1)[0]) == "alice moved the machine laptop-1 from bob to carol"
    with pytest.raises(KaptyorkaError, match="it is already carol's"):
        store.device_move(p.device_id, "carol", "alice")
    with pytest.raises(KaptyorkaError, match="no person named dave"):
        store.device_move(p.device_id, "dave", "alice")
    with pytest.raises(KaptyorkaError, match="no such machine"):
        store.device_move(9999, "bob", "alice")


def test_move_keeps_the_old_owners_history(store, team, monkeypatch):
    """Only sessions running now follow a moved machine. An older session, and the messages it
    sent and got, stay with the old owner; the new owner never sees them."""
    from kaptyorka.store import ONLINE_S
    bob, carol = who(store, "bob"), who(store, "carol")
    p = store.authenticate(store.device_add(bob.person_id, "laptop-1"))
    store.heartbeat("lap", [], principal=p, facts={"host": "lap", "os_user": "bob"})
    store.seen("s-old", "lap", "codex", "proj-a", principal=p)
    store.seen("b1", "pc-b", "codex", "proj-b", principal=store.authenticate(
        store.device_add(bob.person_id, "pc-b")))
    store.send("s-old", store.agent("b1").name, "bob's private note")
    later = store.clock() + ONLINE_S + 60
    monkeypatch.setattr(store, "clock", lambda: later)
    store.seen("s-now", "lap", "codex", "proj-a", principal=p)
    store.device_move(p.device_id, "carol", "alice")
    assert store.agent("s-now").person_id == carol.person_id  # running: moved
    assert store.agent("s-old").person_id == bob.person_id  # older: stays bob's
    texts = lambda w: [m["text"] for m in store.messages_for(w)]
    assert "bob's private note" in texts(bob)
    assert "bob's private note" not in texts(carol)


def test_routes(tmp_path):
    from conftest import start_hub
    from kaptyorka import http
    from kaptyorka.manager import ManagerStore
    s = Store(tmp_path / "h.db")
    ManagerStore(s).set_enabled(False, "test")
    stop = start_hub(s)
    url = f"http://127.0.0.1:{stop.port}"
    try:
        _, admin = person(s, "alice", "admin")
        bob, member = person(s, "bob")
        status, body = http.request("POST", url + "/api/team-machines", admin, {"name": "jet-1"})
        assert status == 200 and body["command"].startswith("kaptyorka join " + url + "/i/")
        assert http.request("POST", url + "/api/team-machines", admin, {"name": "jet-1"})[0] == 409
        for method, path, b in [("POST", "/api/team-machines", {"name": "x"}),
                                ("GET", "/api/team/machines", None),
                                ("POST", "/api/devices/1/move", {"person": "bob"})]:
            assert http.request(method, url + path, member, b) == (403, "admin only")
        dev = s.authenticate(member).device_id
        s.heartbeat("pc", [], principal=s.authenticate(member), facts={"host": "pc", "os_user": "b"})
        assert http.request("GET", url + "/api/team/machines", admin)[1][0]["host"] == "pc"
        status, body = http.request("POST", f"{url}/api/devices/{dev}/move", admin, {"person": "alice"})
        assert (status, body) == (200, {"device": "bob-laptop", "from": "bob", "to": "alice"})
    finally:
        stop()
        s.close()


def test_account_lists_the_team_machines_you_can_use(store, team):
    """The Machines page: a member sees every team machine, with only the projects they have a
    role in (none for carol); the machine is not itself."""
    def tm(name):
        return [(m["name"], m["os_user"], m["projects"])
                for m in admin_ops.account(store, who(store, name))["team_machines"]]
    assert tm("bob") == [("nvidia@jet-1", "nvidia", ["proj-a"])]  # not proj-b: no role there
    assert admin_ops.account(store, who(store, "bob"))["team_machines"][0]["host"] == "jet-1"  # its card on Machines
    assert tm("carol") == [("nvidia@jet-1", "nvidia", [])]  # the machine shows, no project name leaks
    assert tm("alice") == [("nvidia@jet-1", "nvidia", ["proj-a", "proj-b"])]
    assert admin_ops.account(store, team[1])["team_machines"] == []  # not itself
    store.grant_project("proj-b", who(store, "carol").person_id, "read", "alice")
    assert tm("carol") == [("nvidia@jet-1", "nvidia", ["proj-b"])]


def test_a_person_becomes_a_team_machine(store, tmp_path, monkeypatch):
    """The person the hub made at install for its own OS account is no human: it becomes a team
    machine, its machine keeps working, and the hub key in admin-token keeps its admin rights."""
    from kaptyorka import paths

    monkeypatch.setattr(paths, "home", lambda: tmp_path)
    person(store, "alice", "admin")
    svc = store.person_create("svc")
    with store.lock:
        store.db.execute("UPDATE people SET role='admin' WHERE id=?", (svc,))
    key, _ = store.invite_redeem(store.invite_create(svc), "svc@box-1", host="box-1",
                                 os_user="svc")
    (tmp_path / "admin-token").write_text(key + "\n")
    assert store.authenticate(key).hub_key
    done = admin_ops.make_team_machine(store, "svc", who(store, "alice"))
    assert done["name"] == "svc" and done["was"] == "admin"
    row = next(x for x in store.people() if x["name"] == "svc")
    assert row["team"] and row["role"] == "member" and admin_ops.admins(store) == ["alice"]
    assert admin_ops.log_sentence(store.log(1)[0]) == "alice made svc a team machine"
    # it leaves People (the page lists people without team rows) and shows as Team in Machines
    assert [p["name"] for p in store.people() if not p["team"]] == ["alice"]
    box = next(m for m in WorkerStore(store).overview() if m["host"] == "box-1")
    assert box["team"] == ["svc"] and box["people"] == [] and box["devices"][0]["team"]
    # the hub key still does admin work, even with nobody an admin
    hub_key = store.authenticate(key)
    assert hub_key.is_admin() and store.is_team(hub_key.person_id)
    with store.lock:
        store.db.execute("UPDATE people SET role='member' WHERE id<>?", (svc,))
    admin_ops.set_role(store, "alice", "admin", store.authenticate(key))
    assert admin_ops.admins(store) == ["alice"]
    with pytest.raises(KaptyorkaError, match="svc is already a team machine"):
        admin_ops.make_team_machine(store, "svc", who(store, "alice"))


def test_only_a_person_without_gitlab_or_the_shared_token_becomes_a_team_machine(tmp_path):
    store = Store(tmp_path / "hub.db", legacy_token="old-shared")
    first = next(p for p in store.people() if p["role"] == "admin")  # the shared token's person
    alice = who(store, first["name"])
    bob, _ = person(store, "bob")
    with store.lock:
        store.db.execute("UPDATE people SET gitlab_user='bob-gl' WHERE id=?", (bob,))
    with pytest.raises(KaptyorkaError, match="bob signs in with GitLab, so is a person"):
        admin_ops.make_team_machine(store, "bob", alice)
    carol, _ = person(store, "carol")
    dev = store.authenticate(store.device_add(carol, "gitlab-web")).device_id
    with store.lock:
        store.db.execute("INSERT INTO gitlab_sessions (device_id, person_id, gitlab_id, refresh,"
                         " checked) VALUES (?, ?, 3, 'r', 0)", (dev, carol))
    with pytest.raises(KaptyorkaError, match="carol has browsers signed in with GitLab"):
        admin_ops.make_team_machine(store, "carol", alice)
    person(store, "dan", "admin")
    with pytest.raises(KaptyorkaError, match="the old shared token still speaks for"
                                             f" {first['name']}; turn it off first:"
                                             " kaptyorka shared-token off"):
        admin_ops.make_team_machine(store, first["name"], who(store, "dan"))
    store.set_legacy_enabled(False, "dan")
    admin_ops.make_team_machine(store, first["name"], who(store, "dan"))
    # the last admin person stays one; team machines never count
    with pytest.raises(KaptyorkaError, match="you are the last admin"):
        admin_ops.make_team_machine(store, "dan", who(store, "dan"))
    with pytest.raises(KaptyorkaError, match="you are the last admin"):
        admin_ops.set_role(store, "dan", "member", who(store, "dan"))
    store.close()


def test_one_name_for_a_machine_everywhere(store, team):
    """A machine is account@computer everywhere (the feed's sessions, Mine, Everyone), its label
    when renamed, its joined name when the computer is unknown. The hub's own account, joined
    with another name, reads account@computer with no data change."""
    from kaptyorka.store import machine_name
    assert machine_name("", "alice", "box-1", "hub-dev") == "alice@box-1"
    assert machine_name("work box", "alice", "box-1", "hub-dev") == "work box"
    assert machine_name("", "", "box-1", "hub-dev") == "hub-dev" == machine_name("", "alice", "", "hub-dev")
    alice = store.person_by_name("alice")["id"]
    token = store.device_add(alice, "hub-dev")
    p = store.authenticate(token)
    store.heartbeat("hub-dev", [], principal=p, facts={"host": "box-1", "os_user": "alice"})
    store.seen("a-1", "hub-dev", "claude", "proj-a", principal=p)
    rows = {a["name"]: a for a in feed(store, who(store, "alice"))["agents"]}
    assert rows["hub-dev/claude/proj-a"]["where"] == "alice@box-1" and not rows["hub-dev/claude/proj-a"]["team"]
    assert {a["where"] for a in rows.values() if a["team"]} == {"nvidia@jet-1"}
    mine = admin_ops.account(store, who(store, "alice"))["machines"]
    assert [m["name"] for m in mine if m["machine"] == "hub-dev"] == ["alice@box-1"]
    ws = WorkerStore(store)
    every = [d["name"] for h in ws.overview() for d in h["devices"]]
    assert "alice@box-1" in every and "nvidia@jet-1" in every
    with store.lock:
        store.db.execute("UPDATE devices SET label='lab box' WHERE id=?", (p.device_id,))
    assert feed(store, who(store, "alice"))["agents"][-1]["where"] == "lab box"


def test_machines_show_their_agent_tools(store, team):
    """The tools a machine's bridge reported, with their display names and logins: on your own
    machines and the team machines you can use (Mine), and on every account (Everyone). Nothing
    when the bridge never reported; [] when it has no tool. No one else's machines leak."""
    import json
    from kaptyorka.workers import tool_tags
    assert tool_tags(None) is None and tool_tags({"os_user": "x"}) is None
    assert tool_tags({"tools": {}}) == []
    assert tool_tags({"tools": {"codex": "0.9", "claude-code": "2.1"}, "logins": {"codex": False}}) == [
        {"tool": "claude-code", "name": "Claude Code", "logged_in": None},
        {"tool": "codex", "name": "Codex", "logged_in": False}]
    ws = WorkerStore(store)
    bob = store.person_by_name("bob")["id"]
    bt = store.authenticate(store.device_add(bob, "bob-pc"))
    store.heartbeat("bob-pc", [], principal=bt, facts={"host": "box-2", "os_user": "bob"})
    ws.facts_seen(bt.device_id, {"tools": {"claude-code": "2.1"}, "logins": {"claude-code": True}}, {})
    ws.facts_seen(team[1].device_id, {"tools": {"claude-code": "2.1"}, "logins": {"claude-code": False}}, {})
    acc = admin_ops.account(store, who(store, "bob"))
    assert [m["tools"] for m in acc["machines"] if m["machine"] == "bob-pc"] == [
        [{"tool": "claude-code", "name": "Claude Code", "logged_in": True}]]
    assert [m["machine"] for m in acc["machines"] if m["tools"] is None] == ["bob-laptop"]  # never reported
    assert acc["team_machines"][0]["tools"] == [{"tool": "claude-code", "name": "Claude Code", "logged_in": False}]
    assert "alice" not in json.dumps(acc) and "carol" not in json.dumps(acc)
    every = {d["name"]: d["tools"] for h in ws.overview() for d in h["devices"]}
    assert every["bob@box-2"][0]["logged_in"] is True and every["nvidia@jet-1"][0]["logged_in"] is False


def test_a_team_machine_hidden_from_one_person(store, team):
    """Every team machine is there for everyone by default. Hidden from bob, it is gone for
    him: not on his Machines, its sessions not in his feed or who, no messages either way, no
    workers there, no request for it. Shown again, it is back. Admins see it always, and the
    switch refuses them. Removing bob or the team machine drops the row."""
    from kaptyorka.workers import WorkerStore as WS
    jet, p = team
    bob = who(store, "bob")
    store.seen("b-1", "box-2", "claude", "proj-a", principal=Principal(bob.person_id, "bob", "member", 0, "laptop", False, False))

    def sees():
        return (len(admin_ops.account(store, bob)["team_machines"]),
                sorted(a["name"] for a in feed(store, bob)["agents"] if a["team"]),
                store.listed(bob.person_id, "t-a", jet),
                store.may_message("b-1", "t-a"), store.may_message("t-a", "b-1"),
                store.listed(jet, "b-1", bob.person_id))
    assert sees() == (1, ["jet-1/claude/proj-a"], True, True, True, True)  # the default
    store.team_machine_set_visible("jet-1", "bob", False, "alice")
    assert sees() == (0, [], False, False, False, False)
    with pytest.raises(KaptyorkaError, match="no machine named jet-1"):
        store.request_access(bob.person_id, "machine", "jet-1", "")
    assert store.team_hidden() == {"jet-1": ["bob"]}
    assert admin_ops.log_sentence(store.log(1)[0]) == "alice hid jet-1 from bob"
    # admins always see it; the switch refuses them
    alice = who(store, "alice")
    assert len(admin_ops.account(store, alice)["team_machines"]) == 1 and store.listed(alice.person_id, "t-a", jet)
    with pytest.raises(KaptyorkaError, match="sees every team machine"):
        store.team_machine_set_visible("jet-1", "alice", False, "alice")
    with pytest.raises(KaptyorkaError, match="no team machine named bob"):
        store.team_machine_set_visible("bob", "carol", False, "alice")
    store.team_machine_set_visible("jet-1", "bob", True, "alice")
    assert sees() == (1, ["jet-1/claude/proj-a"], True, True, True, True)
    assert admin_ops.log_sentence(store.log(1)[0]) == "alice showed jet-1 to bob"
    # an open group door does not get around the hide
    store.open_door_group(bob.person_id, jet, "alice")
    store.team_machine_set_visible("jet-1", "bob", False, "alice")
    assert not store.may_message("b-1", "t-a") and not store.may_message("t-a", "b-1")
    assert not store.page_may_message("bob", bob.person_id, "t-a", jet)
    assert not store.listed(bob.person_id, "t-a", jet) and not store.listed(jet, "b-1", bob.person_id)
    store.team_machine_set_visible("jet-1", "bob", True, "alice")
    assert store.may_message("b-1", "t-a") and store.may_message("t-a", "b-1")
    assert store.page_may_message("bob", bob.person_id, "t-a", jet)
    store.team_machine_set_visible("jet-1", "bob", False, "alice")
    # the manager never routes bob's work there
    store.team_machine_set_visible("jet-1", "bob", False, "alice")
    with store.lock:
        store.db.execute("UPDATE devices SET shared=1 WHERE id=?", (p.device_id,))
        store.db.execute("INSERT INTO machine_access (host, person_id) VALUES ('jet-1', ?)", (bob.person_id,))
    assert WS(store).candidates_for(bob.person_id, "proj-a", "jet-1") == ([], [])  # not even named
    store.team_machine_set_visible("jet-1", "bob", True, "alice")
    assert WS(store).candidates_for(bob.person_id, "proj-a", "jet-1")[0] != []  # shown: it would be used
    store.team_machine_set_visible("jet-1", "bob", False, "alice")
    # removing bob cleans his row up; so does removing the team machine
    admin_ops.remove_person(store, WS(store), "bob", alice)
    assert store.team_hidden() == {}
    with store.lock:
        assert store.db.execute("SELECT COUNT(*) FROM team_machine_hidden").fetchone()[0] == 0
    store.team_machine_set_visible("jet-1", "carol", False, "alice")
    admin_ops.remove_person(store, WS(store), "jet-1", alice)
    with store.lock:
        assert store.db.execute("SELECT COUNT(*) FROM team_machine_hidden").fetchone()[0] == 0


def test_hide_route_and_cli(tmp_path, monkeypatch, capsys):
    """POST /api/people/<person>/team-machines/<machine> {visible} (admin only), and
    `kaptyorka machine hide|show <machine> <person>`."""
    from conftest import start_hub
    from kaptyorka import cli, http, paths
    s = Store(tmp_path / "hub.db")
    stop = start_hub(s)
    url = f"http://127.0.0.1:{stop.port}"
    try:
        _, admin = person(s, "alice", "admin")
        _, member = person(s, "bob")
        s.team_machine_add("jet-1", "alice")
        path = url + "/api/people/bob/team-machines/jet-1"
        assert http.request("POST", path, member, {"visible": False}) == (403, "admin only")
        assert http.request("POST", path, admin, {"visible": "no"})[0] == 400
        assert http.request("POST", url + "/api/people/bob/team-machines/nope", admin, {"visible": False}) == \
            (404, "no team machine named nope")
        assert http.request("POST", path, admin, {"visible": False})[0] == 204
        assert s.team_hidden() == {"jet-1": ["bob"]}
        assert http.request("GET", url + "/api/feed", admin)[1]["people"][-1]["hidden_from"] == ["bob"]
        paths.save_config({"hub": url, "token": admin, "machine": "pc"})
        monkeypatch.setattr(cli.sys, "argv", ["kaptyorka", "machine", "show", "jet-1", "bob"])
        with pytest.raises(SystemExit) as e:
            cli.entry()
        assert e.value.code == 0 and capsys.readouterr().out == "jet-1 is shown to bob\n"
        assert s.team_hidden() == {}
        monkeypatch.setattr(cli.sys, "argv", ["kaptyorka", "machine", "hide", "jet-1", "bob"])
        with pytest.raises(SystemExit) as e:
            cli.entry()
        assert e.value.code == 0 and capsys.readouterr().out == "jet-1 is hidden from bob\n"
        assert s.team_hidden() == {"jet-1": ["bob"]}
    finally:
        stop()
        s.close()


def _member_rows(store, name):
    """machine_rows for a member: {host: [(person, os_user)]}."""
    out = {}
    for m in WorkerStore(store).machine_rows(who(store, name)):
        out[m["host"]] = sorted((d["person"], d["os_user"]) for d in m["devices"])
    return out


def _join(store, name, host, os_user):
    p = store.authenticate(store.device_add(store.person_by_name(name)["id"], f"{os_user}-{host}"))
    store.heartbeat(host, [], principal=p, facts={"host": host, "os_user": os_user})
    return p


def test_machines_list_shows_a_member_only_their_own_and_allowed_team_accounts(store, team):
    """A shared computer lists only the member's own account; every team machine shows unless
    it is hidden from them; admins see all."""
    store.heartbeat("jet-1", [], principal=team[1], facts={"host": "jet-1", "os_user": "nvidia"})
    _join(store, "alice", "box-1", "alice-u")
    _join(store, "bob", "box-1", "bob-u")
    _join(store, "carol", "box-1", "carol-u")
    _join(store, "carol", "box-3", "carol-u")
    rows = _member_rows(store, "bob")
    assert rows["box-1"] == [("bob", "bob-u")]  # no alice, no carol
    assert "box-3" not in rows  # nothing of bob's there
    assert rows["jet-1"] == [("jet-1", "nvidia")]  # allowed team machine (role in proj-a)
    text = repr(WorkerStore(store).machine_rows(who(store, "bob")))
    assert "alice-u" not in text and "carol" not in text
    assert _member_rows(store, "carol")["jet-1"] == [("jet-1", "nvidia")]  # no project role needed
    store.team_machine_set_visible("jet-1", "bob", False, "alice")
    assert "jet-1" not in _member_rows(store, "bob")  # hidden from bob
    assert _member_rows(store, "bob")["box-1"] == [("bob", "bob-u")]
    full = _member_rows(store, "alice")  # the admin sees everything
    assert full["box-1"] == [("alice", "alice-u"), ("bob", "bob-u"), ("carol", "carol-u")]
    assert "jet-1" in full and "box-3" in full


def test_fleet_list_of_a_member_has_only_their_rows(store, team):
    from kaptyorka import fleet
    _join(store, "bob", "box-1", "bob-u")
    _join(store, "carol", "box-1", "carol-u")
    rows = WorkerStore(store).machine_rows(who(store, "bob"))
    for d in (d for m in rows for d in m["devices"]):
        d.setdefault("online", False)
    text = "\n".join(fleet.list(rows))
    assert "bob-u" in text and "carol" not in text


def test_a_new_team_machine_with_no_sessions_shows_to_members(store):
    person(store, "alice", "admin")
    bob, _ = person(store, "bob")
    store.project_register("proj-a", by="alice")
    store.grant_project("proj-a", bob, "write", "alice")
    _, code = store.team_machine_add("jet-1", "alice")
    t, _ = store.invite_redeem(code, "jet-1", host="jet-1", os_user="nvidia")
    store.heartbeat("jet-1", [], principal=store.authenticate(t), facts={"host": "jet-1", "os_user": "nvidia"})
    [m] = admin_ops.account(store, who(store, "bob"))["team_machines"]
    assert m["host"] == "jet-1" and m["projects"] == []
    store.request_access(bob, "machine", "jet-1")  # visible: no "no machine named"
    assert [r["host"] for r in WorkerStore(store).machine_rows(who(store, "bob"))] == ["jet-1"]
    store.team_machine_set_visible("jet-1", "bob", False, "alice")
    assert admin_ops.account(store, who(store, "bob"))["team_machines"] == []
    with pytest.raises(KaptyorkaError, match="no machine named jet-1"):  # hidden: not there
        store.request_access(bob, "machine", "jet-1")
    assert WorkerStore(store).machine_rows(who(store, "bob")) == []


def test_a_session_on_a_visible_team_machine_still_needs_its_project_role(store):
    person(store, "alice", "admin")
    bob, _ = person(store, "bob")
    carol, _ = person(store, "carol")
    store.project_register("gros", by="alice")
    store.grant_project("gros", carol, "write", "alice")
    _, code = store.team_machine_add("jet-1", "alice")
    token, _ = store.invite_redeem(code, "jet-1", host="jet-1", os_user="nvidia")
    store.seen("t-g", "jet-1", "claude", "gros", principal=store.authenticate(token))
    sessions = lambda n: [a for a in feed(store, who(store, n))["agents"] if a["person"] == "jet-1 (team)"]
    assert sessions("bob") == [] and len(sessions("carol")) == 1
    [m] = admin_ops.account(store, who(store, "bob"))["team_machines"]  # the machine shows to both
    assert m["projects"] == [] and "gros" not in repr(WorkerStore(store).machine_rows(who(store, "bob")))
    assert admin_ops.account(store, who(store, "carol"))["team_machines"][0]["projects"] == ["gros"]


def test_title_shows_only_to_those_who_may_see_the_session(store, team):
    """A session's title reaches the feed with the session and no wider: a member without a
    role in its project sees nothing of it."""
    p = team[1]
    store.heartbeat("jet-1", [{"key": "t-a", "tool": "claude", "project": "proj-a", "title": "Secret plan"}], principal=p)
    store.heartbeat("jet-1", [{"key": "t-b", "tool": "claude", "project": "proj-b", "title": "Other plan"}], principal=p)
    titles = lambda n: sorted(a["title"] for a in feed(store, who(store, n))["agents"] if a["person"] == "jet-1 (team)")
    assert titles("bob") == ["Secret plan"]  # proj-a only
    assert titles("carol") == []
    assert titles("alice") == ["Other plan", "Secret plan"]


def test_a_team_session_answers_one_that_wrote_to_it(store, team, monkeypatch):
    """bob's session in proj-c writes to the team session in proj-a (bob has a role there).
    The team session may answer it for a day, though it cannot list or reach it otherwise;
    a hidden team machine blocks it both ways."""
    from kaptyorka.store import REPLY_S
    jet, p = team
    bob = who(store, "bob")
    store.project_register("proj-c", by="alice")
    store.grant_project("proj-c", bob.person_id, "write", "alice")
    bp = store.authenticate(store.device_add(bob.person_id, "pc-b"))
    store.seen("b-c", "pc-b", "codex", "proj-c", principal=bp)
    name = store.agent("b-c").name
    assert not store.listed(jet, "b-c", bob.person_id)
    with pytest.raises(KaptyorkaError, match="that you can reach"):
        store.send("t-a", name, "unasked")
    store.send("b-c", store.agent("t-a").name, "please run the tests")
    assert store.may_message("t-a", "b-c")  # by project role already; the name was the gap
    assert store.send("t-a", name, "done") == 1
    assert store.send("t-a", session_id("b-c"), "by id too") == 1
    assert [m.text for m in store.inbox("b-c")] == ["done", "by id too"]
    # no listing change: the team session's who still leaves bob's proj-c session out
    assert not store.listed(jet, "b-c", bob.person_id)
    assert "b-c" not in [t["key"] for t in store_targets_all(store, "t-a", jet)]
    with pytest.raises(KaptyorkaError, match="that you can reach"):
        store.send("t-b", name, "only the session it wrote to answers")
    # hidden wins
    store.team_machine_set_visible("jet-1", "bob", False, "alice")
    assert not store.may_message("t-a", "b-c")
    with pytest.raises(KaptyorkaError, match="that you can reach"):
        store.send("t-a", name, "hidden")
    store.team_machine_set_visible("jet-1", "bob", True, "alice")
    # after the window
    later = store.clock() + REPLY_S + 1
    monkeypatch.setattr(store, "clock", lambda: later)
    store.seen("t-a", "jet-1", "claude", "proj-a", principal=p)
    store.seen("b-c", "pc-b", "codex", "proj-c", principal=bp)
    with pytest.raises(KaptyorkaError, match="that you can reach"):
        store.send("t-a", name, "too late")


def store_targets_all(store, key, viewer):
    with store.lock:
        return store._targets(key, "all", store.clock(), viewer)
