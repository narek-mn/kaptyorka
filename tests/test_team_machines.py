"""Team machines: one shared OS account everyone logs into, owned by the team. Its sessions are
seen and messaged by everyone with a role in their project, and by nobody else."""
import pytest

from helpers import person
from kaptyorka import admin_ops, usage
from kaptyorka.hub_feed import feed
from kaptyorka.store import KaptyorkaError, Principal, Store
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
    assert sorted(d["name"] for d in m["devices"]) == ["jet-1", "jet-1-bob"]
    assert usage.short(None) == "" and usage.short({"users": {}}) == ""


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
