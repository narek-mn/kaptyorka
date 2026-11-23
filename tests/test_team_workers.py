"""Workers on team machines: a person machine turned into a team machine hosts workers for
anyone with the write role in the project; a session on a team machine may ask for one; the
worker closes itself when its task is done or its boss session really ended. A bridge is
simulated with raw HTTP calls, as in test_hub_workers."""
from types import SimpleNamespace

import pytest

import kaptyorka
from conftest import start_hub
from helpers import call, call_error, person, run
from kaptyorka import http
from kaptyorka.hub import ON_SHARED
from kaptyorka.store import Store
from kaptyorka.workers import WorkerStore

ADMIN = "T"  # the fixture's legacy token belongs to the admin


@pytest.fixture
def clock():
    return [1_000_000.0]


@pytest.fixture
def hub(tmp_path, clock):
    store = Store(tmp_path / "hub.db", clock=lambda: clock[0], legacy_token=ADMIN)
    stop = start_hub(store)
    yield SimpleNamespace(url=f"http://127.0.0.1:{stop.port}", token=ADMIN, store=store,
                          app=stop.app, ws=WorkerStore(store))
    stop()
    store.close()


def as_token(hub, token):
    return SimpleNamespace(url=hub.url, token=token)


def ident(key, project="proj-a", machine="laptop", tool="claude-code"):
    return {"key": key, "machine": machine, "tool": tool, "project": project}


def heartbeat(hub, token, host, sessions=(), folders=None):
    body = {"machine": host, "sessions": list(sessions), "tools": [],
            "facts": {"host": host, "os_user": "u", "version": kaptyorka.__version__,
                      "tmux": True, "load": 0.1, "cpus": 4},
            "folders": {"proj-a": "/w/proj-a"} if folders is None else folders}
    return http.request("POST", hub.url + "/bridge/heartbeat", token, body)


def bridge(hub, person_id, host, **kw):
    code = hub.store.invite_create(person_id)
    token, secret = hub.store.invite_redeem(code, f"u@{host}", host=host, os_user="u")
    status, answer = heartbeat(hub, token, host, **kw)
    assert status == 200
    return SimpleNamespace(token=token, secret=secret, host=host, answer=answer,
                           device_id=hub.store.authenticate(token).device_id)


def poll(hub, b):
    status, items = http.request("GET", f"{hub.url}/bridge/poll?machine={b.host}", b.token)
    assert status == 200
    return items


def take(hub, b, key):
    """The bridge on b takes the one launch waiting there and links its session `key`."""
    item = poll(hub, b)[0]
    assert item["kind"] == "launch"
    assert http.request("POST", hub.url + "/bridge/ack", b.token,
                        {"id": item["id"], "ok": True, "launch": True})[0] == 204
    assert http.request("POST", hub.url + "/bridge/worker", b.token,
                        {"worker": item["worker"], "key": key})[0] == 204
    return item


@pytest.fixture
def admin(hub):
    """The admin's own token (admin work refuses the shared token once they have one)."""
    pid, token = person(hub.store, "root", "admin")
    return as_token(hub, token)


@pytest.fixture
def lab(hub, admin):
    """proj-a; alice (write) with her laptop's bridge; jetson-1 joined as if it were a person,
    with its own bridge: what the hub had before anyone turned it into a team machine."""
    hub.store.project_register("proj-a", by="test")
    alice_id, alice_token = person(hub.store, "alice")
    hub.store.grant_project("proj-a", alice_id, "write", "test")
    laptop = bridge(hub, alice_id, "laptop")
    jet_id = hub.store.person_create("jetson-1")
    jet = bridge(hub, jet_id, "jetson-1")
    return SimpleNamespace(alice_id=alice_id, alice=as_token(hub, alice_token), laptop=laptop,
                           jet_id=jet_id, jet=jet)


ASKER = ident("k:alice", machine="laptop")


def test_a_person_machine_hosts_no_worker_until_it_is_a_team_machine(hub, admin, lab):
    """The admin steps, proven: before, jetson-1 is someone's private machine; one command
    (`kaptyorka machine team --from jetson-1`, the route behind it) is all it takes."""
    before = run(call(lab.alice, ASKER, "work", project="proj-a", task="x", machine="jetson-1"))
    assert "jetson-1" in before and not before.startswith("starting"), before
    status, done = http.request("POST", hub.url + "/api/people/jetson-1/team", admin.token)
    assert status == 200 and done["name"] == "jetson-1"
    assert heartbeat(hub, lab.jet.token, "jetson-1")[1]["shared"] is True
    assert run(call(lab.alice, ASKER, "work", project="proj-a", task="x",
                    machine="jetson-1")) == "starting `proj-a-w1` on jetson-1 (u)"


def team(hub, admin):
    status, _ = http.request("POST", hub.url + "/api/people/jetson-1/team", admin.token)
    assert status == 200


def test_the_worker_runs_as_alices_on_the_team_machine(hub, admin, lab):
    team(hub, admin)
    run(call(lab.alice, ASKER, "work", project="proj-a", task="x", machine="jetson-1"))
    item = take(hub, lab.jet, "claude:w1")
    assert item["person"] == "alice"  # the bridge takes it: the hub says the account is shared
    w = hub.ws.by_name("proj-a-w1")
    assert w["state"] == "up" and w["host"] == "jetson-1" and w["person"] == "alice"
    assert hub.ws.boss_key("proj-a-w1") == "k:alice"
    # its bridge keeps reporting it: alice's session on a shared account is not dropped
    sessions = [{"key": "claude:w1", "tool": "claude-code", "project": "proj-a",
                 "cwd": "/w/proj-a"}]
    assert heartbeat(hub, lab.jet.token, "jetson-1", sessions)[0] == 200
    assert hub.store.agent("claude:w1").person_id == lab.alice_id


def test_undo_turns_it_back_and_is_refused_while_others_workers_run(hub, admin, lab):
    team(hub, admin)
    run(call(lab.alice, ASKER, "work", project="proj-a", task="x", machine="jetson-1"))
    take(hub, lab.jet, "claude:w1")
    url = hub.url + "/api/people/jetson-1/team"
    assert http.request("DELETE", url, admin.token) == \
        (400, "other people's workers run on jetson-1 (proj-a-w1); close them first")
    run(call(lab.alice, ASKER, "close_worker", name="proj-a-w1"))
    item = poll(hub, lab.jet)[0]
    http.request("POST", hub.url + "/bridge/ack", lab.jet.token,
                 {"id": item["id"], "ok": True, "launch": True})
    assert http.request("DELETE", url, admin.token) == (200, {"name": "jetson-1"})
    assert heartbeat(hub, lab.jet.token, "jetson-1")[1]["shared"] is False
    row = next(p for p in hub.store.people() if p["name"] == "jetson-1")
    assert not row["team"] and row["role"] == "member"
    answer = run(call(lab.alice, ASKER, "work", project="proj-a", task="x", machine="jetson-1"))
    assert "jetson-1 is someone's own machine, not a team machine" in answer
    assert http.request("DELETE", url, admin.token) == (400, "jetson-1 is not a team machine")


def test_who_may_start_on_a_team_machine(hub, admin, lab):
    """The write role in the project, on a real team machine only; a hidden one stays off;
    admins anywhere. A device merely marked shared still needs a grant."""
    team(hub, admin)
    bob_id, bob_token = person(hub.store, "bob")
    hub.store.grant_project("proj-a", bob_id, "read", "test")
    bob = as_token(hub, bob_token)
    assert "that takes the write role" in call_error(
        bob, ident("k:bob"), "work", project="proj-a", task="x", machine="jetson-1")
    carol_id, carol_token = person(hub.store, "carol")
    hub.store.grant_project("proj-a", carol_id, "write", "test")
    hub.store.team_machine_set_visible("jetson-1", "carol", False, "test")
    carol = as_token(hub, carol_token)
    assert run(call(carol, ident("k:carol"), "work", project="proj-a", task="x",
                    machine="jetson-1")) == (
        "no machine can take proj-a: no machine named jetson-1 that you may use; check the name"
        " on the page's Machines list, or ask an admin")
    # dave's account on box-1 is marked shared but is no team machine: a grant is needed
    dave_id = hub.store.person_create("dave")
    box = bridge(hub, dave_id, "box-1")
    hub.store.device_set_shared(box.device_id, True)
    answer = run(call(lab.alice, ASKER, "work", project="proj-a", task="x", machine="box-1"))
    assert answer == ("no machine can take proj-a: box-1 (u): a shared account you have no access"
                      " to; an admin can give it: `kaptyorka grant <you> machine:box-1`")
    hub.store.grant_machine("box-1", lab.alice_id, by="test")
    assert run(call(lab.alice, ASKER, "work", project="proj-a", task="x",
                    machine="box-1")).startswith("starting `proj-a-w1` on box-1")
    # an admin with no role in the project
    assert run(call(admin, ident("k:root"), "work", project="proj-a", task="x",
                    machine="jetson-1")).startswith("starting `proj-a-w2` on jetson-1")


def test_a_session_on_the_team_machine_asks_and_is_the_boss(hub, admin, lab):
    team(hub, admin)
    jet = as_token(hub, lab.jet.token)
    here = ident("claude:j1", machine="u@jetson-1")
    assert call_error(jet, here, "work", project="proj-a", task="x") == (
        "Error executing tool work: jetson-1 is a team machine without the write role in proj-a;"
        " an admin runs `kaptyorka grant jetson-1 proj-a:write`")
    hub.store.grant_project("proj-a", lab.jet_id, "write", "test")
    assert run(call(jet, here, "work", project="proj-a", task="x")) == \
        "starting `proj-a-w1` on jetson-1 (u)"
    take(hub, lab.jet, "claude:w1")
    with hub.store.lock:
        row = hub.store.db.execute("SELECT boss_key, boss_person FROM workers WHERE"
                                   " name='proj-a-w1'").fetchone()
    assert (row["boss_key"], row["boss_person"]) == ("claude:j1", lab.jet_id)
    # that worker is a session of the team machine too, but still may not start more
    worker = ident("claude:w1", machine="u@jetson-1")
    for tool, args in (("work", {"project": "proj-a", "task": "y"}),
                       ("adopt", {"name": "proj-a-w1"}),
                       ("close_worker", {"name": "proj-a-w1"})):
        assert ON_SHARED in call_error(jet, worker, tool, **args)
    assert http.request("POST", hub.url + "/api/workers/proj-a-w1/close", lab.jet.token,
                        headers={"X-Kaptyorka-Key": "claude:w1"}) == (403, ON_SHARED)
    # nor may alice's worker on it
    run(call(lab.alice, ASKER, "work", project="proj-a", task="x", machine="jetson-1"))
    take(hub, lab.jet, "claude:w2")
    assert ON_SHARED in call_error(jet, ident("claude:w2", machine="u@jetson-1"), "work",
                                   project="proj-a", task="y")
    # the session that asked closes its worker
    assert run(call(jet, here, "close_worker", name="proj-a-w1")).startswith("closing")


def test_first_use_lines(hub, admin, lab, clock):
    """Each thing that stops "open a session on jetson-1 in proj-a" gets one line with the fix."""
    def ask():
        return run(call(lab.alice, ASKER, "work", project="proj-a", task="x", machine="jetson-1"))

    assert ask() == ("no machine can take proj-a: jetson-1 is someone's own machine, not a team"
                     " machine; if the whole team uses it, an admin runs `kaptyorka machine team"
                     " --from <its person>`")
    team(hub, admin)
    heartbeat(hub, lab.jet.token, "jetson-1", folders={})
    assert ask() == ("no machine can take proj-a: jetson-1 (u): no folder for proj-a; its bridge"
                     " looks for ~/proj-a once the team machine has a role there: `kaptyorka grant"
                     " jetson-1 proj-a:write`, or open a session in the proj-a folder there once")
    hub.store.grant_project("proj-a", lab.jet_id, "write", "test")
    assert ask().endswith("jetson-1 (u): no folder for proj-a; clone it to ~/proj-a there, or"
                          " open a session in its folder there once")
    heartbeat(hub, lab.jet.token, "jetson-1")
    clock[0] += 3600  # its bridge has been silent for an hour
    assert ask() == ("no machine can take proj-a: jetson-1 (jetson-1): bridge offline; on that"
                     " machine run `kaptyorka doctor --fix`")
    assert run(call(lab.alice, ASKER, "work", project="proj-a", task="x",
                    machine="jetson-9")).endswith("no machine named jetson-9 that you may use;"
                                                  " check the name on the page's Machines list,"
                                                  " or ask an admin")
