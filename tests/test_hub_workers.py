"""The hub's worker side: heartbeat answers, launches in the bridge stream, the worker tools
and routes. A bridge is simulated with raw HTTP calls."""
from types import SimpleNamespace

import pytest

import kaptyorka
from conftest import start_hub
from helpers import _client, call, call_error, person, run
from kaptyorka import brief as brief_mod
from kaptyorka import facts, http
from kaptyorka.adapters.base import Session
from kaptyorka.orders import signing_key, verify
from kaptyorka.store import ONLINE_S, Store
from kaptyorka.workers import UNATTENDED_S, WorkerStore

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


def ident(key, project="proj-a", machine="laptop", tool="codex"):
    return {"key": key, "machine": machine, "tool": tool, "project": project}


def heartbeat(hub, token, host, sessions=(), folders=None, tmux=True, extra=None):
    body = {"machine": host, "sessions": list(sessions), "tools": [],
            "facts": {"host": host, "os_user": "u", "version": kaptyorka.__version__,
                      "tmux": tmux, "load": 0.1, "cpus": 4},
            "folders": {"proj-a": "/w/proj-a"} if folders is None else folders, **(extra or {})}
    return http.request("POST", hub.url + "/bridge/heartbeat", token, body)


def bridge(hub, person_id, host="box-1", **kw):
    """A redeemed device on `host` whose bridge has just heartbeated."""
    code = hub.store.invite_create(person_id)
    token, secret = hub.store.invite_redeem(code, host, host=host, os_user="u")
    status, _ = heartbeat(hub, token, host, **kw)
    assert status == 200
    return SimpleNamespace(token=token, secret=secret, host=host,
                           device_id=hub.store.authenticate(token).device_id)


def poll(hub, b):
    status, items = http.request("GET", f"{hub.url}/bridge/poll?machine={b.host}", b.token)
    assert status == 200
    return items


def member(hub, name, role="write", project="proj-a"):
    pid, token = person(hub.store, name)
    if role:
        hub.store.grant_project(project, pid, role, "test")
    return pid, as_token(hub, token)


@pytest.fixture
def registered(hub):
    hub.store.project_register("proj-a", by="test")


@pytest.fixture
def alice(hub, registered):
    return member(hub, "alice")


def inbox_texts(client, who):
    return [m["text"] for m in run(call(client, who, "inbox"))]


BOSS = ident("k:boss")


def start_worker(hub, alice, needs_brief=False, **kw):
    """alice's boss session asks for a worker; the bridge on box-1 takes and links it."""
    pid, client = alice
    b = bridge(hub, pid)
    answer = run(call(client, BOSS, "work", project="proj-a", task="fix the tests", **kw))
    assert answer == "starting `proj-a-w1` on box-1 (u)"
    item = poll(hub, b)[0]
    http.request("POST", hub.url + "/bridge/ack", b.token,
                 {"id": item["id"], "ok": True, "launch": True})
    assert http.request("POST", hub.url + "/bridge/worker", b.token,
                        {"worker": "proj-a-w1", "key": "claude:w1",
                         "needs_brief": needs_brief})[0] == 204
    return b


def test_heartbeat_answers_json_with_projects_and_version(hub, alice):
    hub.store.project_register("proj-b", by="test")  # alice has no role there
    pid, _ = alice
    code = hub.store.invite_create(pid)
    token, _ = hub.store.invite_redeem(code, "box-1", host="box-1", os_user="u")
    status, body = heartbeat(hub, token, "box-1",
                             sessions=[{"key": "claude:1", "tool": "claude-code",
                                        "project": "proj-a", "cwd": "/w/proj-a"}])
    assert status == 200
    assert body == {"version": kaptyorka.__version__, "auto_update": True,
                    "projects": ["proj-a"], "you": "alice", "shared": False, "skills_version": ""}
    dev = hub.store.authenticate(token).device_id
    assert hub.ws.facts(dev)["tmux"] is True and hub.ws.folders(dev) == {"proj-a": "/w/proj-a"}
    with hub.store.lock:
        assert hub.store.db.execute("SELECT cwd FROM agents WHERE key='claude:1'"
                                    ).fetchone()["cwd"] == "/w/proj-a"
    # a 0.2 bridge: no facts, no folders, the shared token
    status, body = http.request("POST", hub.url + "/bridge/heartbeat", ADMIN,
                                {"machine": "old", "sessions": []})
    assert status == 200 and body["auto_update"] is False and body["shared"] is False
    assert body["projects"] == []  # the shared token is nobody's: no project list


def test_poll_carries_signed_launch_and_ack_links_worker(hub, alice):
    pid, client = alice
    b = bridge(hub, pid)
    answer = run(call(client, BOSS, "work", project="proj-a", task="fix the tests"))
    assert answer == "starting `proj-a-w1` on box-1 (u)"
    items = poll(hub, b)
    assert len(items) == 1 and items[0]["launch"] is True and items[0]["kind"] == "launch"
    order = {k: v for k, v in items[0].items() if k != "launch"}
    assert verify(order, signing_key(b.secret), hub.store.clock(), []) is None
    assert order["worker"] == "proj-a-w1" and order["task"] == "fix the tests"
    assert order["folder"] == "/w/proj-a" and order["boss"] == "laptop/codex/proj-a"
    assert http.request("POST", hub.url + "/bridge/ack", b.token,
                        {"id": order["id"], "ok": True, "launch": True})[0] == 204
    assert http.request("POST", hub.url + "/bridge/ack", b.token,
                        {"id": 999, "ok": True, "launch": True})[0] == 404
    assert http.request("POST", hub.url + "/bridge/worker", b.token,
                        {"worker": "proj-a-w1", "key": "claude:w1"})[0] == 204
    assert hub.ws.by_name("proj-a-w1")["state"] == "up"
    assert inbox_texts(client, BOSS) == [
        "worker `proj-a-w1` is up on box-1; attach with `ssh -t -- u@box-1 claude attach proj-a-w1`"]
    # a retried link (the answer was lost) is a success and tells the boss nothing new
    assert http.request("POST", hub.url + "/bridge/worker", b.token,
                        {"worker": "proj-a-w1", "key": "claude:w1"})[0] == 204
    assert inbox_texts(client, BOSS) == []
    assert http.request("POST", hub.url + "/bridge/worker", b.token,
                        {"worker": "proj-a-w1", "key": "claude:other"})[0] == 404

    # a launch the bridge could not start: the boss hears it on the next tick
    run(call(client, BOSS, "work", project="proj-a", task="second"))
    other = bridge(hub, pid, host="box-2")  # not this device's worker
    assert http.request("POST", hub.url + "/bridge/worker", other.token,
                        {"worker": "proj-a-w2", "error": "no claude"})[0] == 404
    assert http.request("POST", hub.url + "/bridge/worker", b.token,
                        {"worker": "proj-a-w2", "error": "no claude"})[0] == 204
    assert hub.ws.by_name("proj-a-w2")["state"] == "failed"
    hub.app.state.tick()
    assert inbox_texts(client, BOSS) == ["worker `proj-a-w2` failed to start: no claude"]
    # a closed or failed worker answers 410: the bridge stops that session
    assert http.request("POST", hub.url + "/bridge/worker", b.token,
                        {"worker": "proj-a-w2", "key": "claude:w2"})[0] == 410
    assert http.request("POST", hub.url + "/bridge/worker", b.token,
                        {"worker": "proj-a-w2", "error": "again"})[0] == 410
    assert http.request("POST", hub.url + "/bridge/worker", b.token,
                        {"worker": "proj-a-w9", "key": "claude:w9"})[0] == 404


def test_work_refuses_without_write_role(hub, registered):
    pid, client = member(hub, "alice", role="read")
    bridge(hub, pid)
    err = call_error(client, BOSS, "work", project="proj-a", task="x")
    assert "write role" in err
    assert hub.ws.list(hub.store.authenticate(client.token)) == []


def test_work_allows_own_unregistered_folder(hub, tmp_path, monkeypatch):
    monkeypatch.setenv("KAPTYORKA_HOME", str(tmp_path / "kh"))
    pid, client = member(hub, "alice", role=None)
    own = tmp_path / "w" / "proj-b"
    own.mkdir(parents=True)
    # what a real bridge sends: alice's hub list is empty, her session works in proj-b
    _, body = heartbeat(hub, client.token, "laptop", folders={})
    assert body["projects"] == []
    sent = facts.folders(body["projects"], [Session("claude:a", "claude-code", "proj-b",
                                                    cwd=str(own))], tmp_path)
    assert sent == {"proj-b": str(own)}
    bridge(hub, pid, folders=sent)
    own = ident("k:a", project="proj-b")
    assert run(call(client, own, "work", project="proj-b", task="x")) == \
        "starting `proj-b-w1` on box-1 (u)"
    _, bob = member(hub, "bob", role=None)  # same folder name, not bob's folder
    err = call_error(bob, ident("k:b", project="proj-c"), "work", project="proj-b", task="x")
    assert "write role" in err


def test_legacy_device_cannot_host_workers(hub, alice):
    pid, client = alice
    token = hub.store.device_add(pid, "box-1")  # a token without a signing secret
    assert heartbeat(hub, token, "box-1")[0] == 200
    answer = run(call(client, BOSS, "work", project="proj-a", task="x"))
    assert answer.startswith("no machine can take proj-a: box-1 (alice): still on the shared"
                             " token")


def admin_id(hub):
    return next(p["id"] for p in hub.store.people() if p["role"] == "admin")


def test_admin_cannot_use_members_private_device(hub, alice):
    pid, _ = alice
    bridge(hub, pid)
    admin_token = hub.store.device_add(admin_id(hub), "admin-own")
    admin = as_token(hub, admin_token)
    answer = run(call(admin, ident("k:x", machine="hubbox"), "work", project="proj-a",
                      task="x", machine="box-1"))
    assert answer == "no machine can take proj-a: no machine named box-1 that you may use"
    answer = run(call(admin, ident("k:x", machine="hubbox"), "work", project="proj-a", task="x"))
    assert answer.startswith("no machine can take proj-a: ")
    assert hub.ws.list(hub.store.authenticate(admin_token)) == []


def test_reasons_for_folder_and_tmux(hub, alice):
    pid, client = alice
    bridge(hub, pid, folders={})
    bridge(hub, pid, host="box-2", tmux=False)
    # `tool` is also call()'s own parameter: call the tool by hand
    r = run(_client(client, BOSS, lambda c: c.call_tool(
        "work", {"project": "proj-a", "task": "x", "tool": "codex"})))
    answer = r.structured_content["result"]
    assert answer == ("no machine can take proj-a: box-1 (u): no folder for proj-a;"
                      " box-2 (u): no tmux (only Claude Code workers can run there)")


def test_busy_folder_of_another_person_answers_busy(hub, alice):
    pid, client = alice
    b = bridge(hub, pid, sessions=[{"key": "claude:a1", "tool": "claude-code",
                                    "project": "proj-a", "cwd": "/w/proj-a"}])
    hub.store.device_set_shared(b.device_id, True)
    bob_id, bob = member(hub, "bob")
    hub.store.grant_machine("box-1", bob_id, by="test")
    answer = run(call(bob, ident("k:b"), "work", project="proj-a", task="x"))
    assert answer == ("busy: box-1 has alice's session in that folder; ask again later or"
                      " name another machine")
    assert hub.ws.by_name("proj-a-w1") is None and poll(hub, b) == []
    # the same person: a worktree beside their own session
    answer = run(call(client, BOSS, "work", project="proj-a", task="x"))
    assert answer == "starting `proj-a-w1` on box-1 (u) in a worktree, the folder is busy"


def test_who_marks_boss_and_worker(hub, alice):
    _, client = alice
    b = start_worker(hub, alice)
    worker = ident("claude:w1", machine="box-1", tool="claude-code")
    rows = {r["name"]: r for r in run(call(as_token(hub, b.token), worker, "who"))}
    assert rows["laptop/codex/proj-a"]["boss"] is True
    assert rows["box-1/claude-code/proj-a"]["worker"] == "proj-a-w1"
    assert "boss" not in rows["box-1/claude-code/proj-a"]
    rows = {r["name"]: r for r in run(call(client, BOSS, "who"))}
    assert rows["box-1/claude-code/proj-a"]["worker"] == "proj-a-w1"
    assert not any("boss" in r for r in rows.values())  # only a worker sees its boss marked


def brief(hub, token, who):
    h = {f"X-Kaptyorka-{k.capitalize()}": v for k, v in who.items()}
    status, text = http.request("GET", hub.url + "/api/brief?event=start", token, headers=h)
    assert status == 200
    return text


def test_worker_brief_starts_with_task(hub, alice, clock):
    b = start_worker(hub, alice, needs_brief=True)
    worker = ident("claude:w1", machine="box-1", tool="claude-code")
    text = brief(hub, b.token, worker)
    assert text.startswith("You are kaptyorka worker `proj-a-w1` for `laptop/codex/proj-a`"
                           " (alice). Your task:\n\nfix the tests")
    assert "You are `box-1/claude-code/proj-a`, alice, on box-1." in text
    clock[0] += 301  # an older session: a resumed one does not hear its task again
    assert not brief(hub, b.token, worker).startswith("You are kaptyorka worker")


def test_worker_brief_without_needs_brief_has_no_task(hub, alice):
    b = start_worker(hub, alice)  # Claude Code: the task went on the command line
    text = brief(hub, b.token, ident("claude:w1", machine="box-1", tool="claude-code"))
    assert text.startswith("You are `box-1/claude-code/proj-a`, alice, on box-1.")
    assert "fix the tests" not in text


OWN_TOKEN = ("use your own device token: run kaptyorka join with an invite, or ask the admin to"
             " assign this machine")


def test_legacy_token_cannot_start_adopt_or_close_workers(hub, alice, clock):
    _, client = alice
    start_worker(hub, alice)
    hub.store.device_add(admin_id(hub), "admin-own")  # the admin has a token of their own
    bridge(hub, admin_id(hub), host="box-9")  # and a private device the shared token must not reach
    legacy = as_token(hub, ADMIN)
    who = ident("k:x", machine="box-1")  # a self-reported machine name
    assert OWN_TOKEN in call_error(legacy, who, "work", project="proj-a", task="x")
    assert OWN_TOKEN in call_error(legacy, who, "close_worker", name="proj-a-w1")
    clock[0] += ONLINE_S + UNATTENDED_S + 5
    hub.app.state.tick()
    assert OWN_TOKEN in call_error(legacy, who, "adopt", name="proj-a-w1")
    for action in ("close", "adopt"):
        status, text = http.request("POST", f"{hub.url}/api/workers/proj-a-w1/{action}", ADMIN)
        assert (status, text) == (403, OWN_TOKEN)
    assert hub.ws.by_name("proj-a-w1")["state"] == "unattended"
    assert hub.ws.by_name("proj-a-w2") is None
    assert run(call(legacy, who, "workers")) == []  # no admin view through the shared token
    assert http.request("GET", hub.url + "/api/workers", ADMIN)[1] == []


def test_unattended_worker_in_brief_and_adopt(hub, alice, clock):
    _, client = alice
    b = start_worker(hub, alice)
    inbox_texts(client, BOSS)  # the "is up" notice
    clock[0] += ONLINE_S + UNATTENDED_S + 5  # the boss is gone
    worker = ident("claude:w1", machine="box-1", tool="claude-code")
    run(call(as_token(hub, b.token), worker, "send", to="laptop/codex/proj-a", text="done"))
    hub.app.state.tick()
    assert hub.ws.by_name("proj-a-w1")["state"] == "unattended"
    new = ident("k:new", machine="laptop2")
    assert ("Unattended workers: proj-a-w1 (box-1); adopt(name) takes one over."
            in brief(hub, client.token, new))
    _, bob = member(hub, "bob")
    assert "no worker proj-a-w1 of yours" in call_error(bob, ident("k:b"), "adopt",
                                                        name="proj-a-w1")
    assert run(call(client, new, "adopt", name="proj-a-w1")) == \
        "you are now the boss of `proj-a-w1`; 1 held messages delivered"
    msgs = run(call(client, new, "inbox"))
    assert [m["text"] for m in msgs] == ["done"]
    clock[0] += brief_mod.CACHE_S  # past the start brief's cache
    assert "Unattended" not in brief(hub, client.token, new)


def test_admin_brief_lists_warnings(hub, alice, clock):
    pid, _ = alice
    bridge(hub, pid)
    clock[0] += 3600  # the bridge on box-1 is silent
    text = brief(hub, ADMIN, ident("k:x", machine="hubbox"))
    assert "Warnings: bridge on box-1 (u) has been offline for 60 minutes." in text
    _, client = alice
    assert "Warnings" not in brief(hub, client.token, BOSS)


def test_close_worker_queues_close_item(hub, alice):
    pid, client = alice
    b = start_worker(hub, alice)
    _, bob = member(hub, "bob")
    assert "no worker proj-a-w1 of yours" in call_error(bob, ident("k:b"), "close_worker",
                                                        name="proj-a-w1")
    assert http.request("POST", hub.url + "/api/workers/proj-a-w1/close", bob.token)[0] == 400
    assert run(call(client, BOSS, "close_worker", name="proj-a-w1")) == "closing `proj-a-w1`"
    items = poll(hub, b)
    assert [(i["kind"], i["worker"], i["launch"]) for i in items] == [
        ("close", "proj-a-w1", True)]
    order = {k: v for k, v in items[0].items() if k != "launch"}
    assert verify(order, signing_key(b.secret), hub.store.clock(), []) is None
    # w1 still holds the folder until the bridge acks the close: w2 gets a worktree
    assert "in a worktree" in run(call(client, BOSS, "work", project="proj-a", task="y"))
    status, body = http.request("POST", hub.url + "/api/workers/proj-a-w2/close", client.token)
    assert status == 200 and body["note"] == "its worktree /w/proj-a stays in place"
    names = [w["name"] for w in run(call(client, BOSS, "workers"))]
    assert names == ["proj-a-w1", "proj-a-w2"]
    status, body = http.request("GET", hub.url + "/api/workers", client.token)
    assert status == 200 and [w["name"] for w in body] == names
    assert http.request("GET", hub.url + "/api/workers", bob.token)[1] == []


def test_page_adopts_for_the_person(hub, alice, clock):
    _, client = alice
    start_worker(hub, alice)
    clock[0] += ONLINE_S + UNATTENDED_S + 5
    hub.app.state.tick()
    status, body = http.request("POST", hub.url + "/api/workers/proj-a-w1/adopt", client.token)
    assert status == 200 and body["state"] == "up"
    # a web boss hears notices on the page: a failed second worker
    with hub.store.lock:
        hub.store.db.execute("UPDATE workers SET state='failed', error='boom', told=NULL"
                             " WHERE name='proj-a-w1'")
    hub.app.state.tick()
    status, feed = http.request("GET", hub.url + "/api/feed", client.token)
    assert [m["text"] for m in feed["messages"] if m["to"] == "alice"] == [
        "worker `proj-a-w1` failed to start: boom"]


def test_auto_update_route_and_machine_rows(hub, alice):
    pid, client = alice
    b = bridge(hub, pid)
    url = f"{hub.url}/api/devices/{b.device_id}/auto_update"
    assert http.request("POST", url, client.token, {"on": False})[0] == 403
    assert http.request("POST", url, ADMIN, {"on": "yes"})[0] == 400
    assert http.request("POST", url, ADMIN, {"on": False})[0] == 204
    assert http.request("POST", f"{hub.url}/api/devices/999/auto_update", ADMIN,
                        {"on": False})[0] == 404
    status, machines = http.request("GET", hub.url + "/api/machines", client.token)
    assert status == 200
    dev = next(d for m in machines for d in m["devices"] if d["id"] == b.device_id)
    assert dev["auto_update"] is False and dev["facts"]["tmux"] is True
    assert dev["folders"] == {"proj-a": "/w/proj-a"} and dev["online"] is True
    assert heartbeat(hub, b.token, "box-1")[1]["auto_update"] is False
    assert http.request("POST", url, ADMIN, {"on": None})[0] == 204
    assert heartbeat(hub, b.token, "box-1")[1]["auto_update"] is True  # a member's default
    assert http.request("GET", hub.url + "/api/warnings", client.token)[0] == 403
    status, warnings = http.request("GET", hub.url + "/api/warnings", ADMIN)
    assert status == 200 and isinstance(warnings, list)


def test_instructions_name_the_worker_tools():
    from kaptyorka.hub import INSTRUCTIONS
    assert ("work(project, task) starts a worker session for you on another machine; workers()"
            " lists yours; adopt(name) takes over an unattended one; close_worker(name) ends"
            " one.") in INSTRUCTIONS


ON_SHARED = "a worker on a shared account cannot start, adopt or close workers; ask your boss"


def test_worker_on_shared_box_cannot_start_adopt_or_close(hub, alice, clock):
    pid, client = alice
    bob_id, bob = member(hub, "bob")
    b = bridge(hub, bob_id, host="box-2")  # bob's machine, shared with alice
    hub.store.device_set_shared(b.device_id, True)
    hub.store.grant_machine("box-2", pid, by="test")
    assert run(call(client, BOSS, "work", project="proj-a", task="x")) == \
        "starting `proj-a-w1` on box-2 (u)"
    item = poll(hub, b)[0]
    http.request("POST", hub.url + "/bridge/ack", b.token,
                 {"id": item["id"], "ok": True, "launch": True})
    assert http.request("POST", hub.url + "/bridge/worker", b.token,
                        {"worker": "proj-a-w1", "key": "claude:w1"})[0] == 204
    box = as_token(hub, b.token)  # the worker calls with the box owner's token
    worker = ident("claude:w1", machine="box-2", tool="claude-code")
    assert ON_SHARED in call_error(box, worker, "work", project="proj-a", task="y")
    assert ON_SHARED in call_error(box, worker, "close_worker", name="proj-a-w1")
    assert ON_SHARED in call_error(box, worker, "adopt", name="proj-a-w1")
    h = {"X-Kaptyorka-Key": "claude:w1"}
    for action in ("close", "adopt"):
        assert http.request("POST", f"{hub.url}/api/workers/proj-a-w1/{action}", b.token,
                            headers=h) == (403, ON_SHARED)
    assert [w["name"] for w in run(call(box, worker, "workers"))] == ["proj-a-w1"]
    assert [w["name"] for w in http.request("GET", hub.url + "/api/workers", b.token,
                                            headers=h)[1]] == ["proj-a-w1"]
    assert hub.ws.by_name("proj-a-w1")["state"] == "up" and hub.ws.by_name("proj-a-w2") is None
    # bob's own session on box-2 still may
    own = ident("k:bob", machine="box-2")
    assert run(call(box, own, "work", project="proj-a", task="z", machine="box-2")) == \
        "starting `proj-a-w2` on box-2 (u) in a worktree, the folder is busy"
    assert [w["name"] for w in run(call(box, own, "workers"))] == ["proj-a-w2"]
    assert run(call(box, own, "close_worker", name="proj-a-w2")).startswith("closing `proj-a-w2`")


def test_machines_hide_other_peoples_folders_and_commits(hub, alice):
    pid, client = alice
    bob_id, bob = member(hub, "bob")
    git = {"git": {"/w/proj-x": {"branch": "main", "subject": "secret plan"}}}
    b = bridge(hub, bob_id, host="box-2", folders={"proj-x": "/w/proj-x"})
    assert heartbeat(hub, b.token, "box-2", folders={"proj-x": "/w/proj-x"},
                     extra={"facts": {"host": "box-2", "os_user": "u", **git}})[0] == 200
    hub.store.device_set_shared(b.device_id, True)
    hub.store.grant_machine("box-2", pid, by="test")
    admin = as_token(hub, hub.store.device_add(admin_id(hub), "admin-own"))

    def dev(token):
        machines = http.request("GET", hub.url + "/api/machines", token)[1]
        return next(d for m in machines for d in m["devices"] if d["id"] == b.device_id)
    seen = dev(client.token)  # alice may use box-2, but bob's projects are his
    assert seen["folders"] == {} and "git" not in seen["facts"] and seen["os_user"] == "u"
    for token in (bob.token, admin.token):
        seen = dev(token)
        assert seen["folders"] == {"proj-x": "/w/proj-x"} and seen["facts"]["git"] == git["git"]
    assert dev(ADMIN)["folders"] == {}  # the shared token is no admin here


def test_names_that_read_as_ssh_options_are_dropped(hub, alice):
    pid, _ = alice
    code = hub.store.invite_create(pid)
    status, body = http.request("POST", f"{hub.url}/i/{code}", "",
                                {"device_name": "box-1", "host": "-oProxyCommand=x",
                                 "os_user": "-lroot"})
    assert status == 200
    token = body["token"]
    dev = hub.store.authenticate(token).device_id
    row = hub.store.db.execute("SELECT host, os_user FROM devices WHERE id=?", (dev,)).fetchone()
    assert (row["host"], row["os_user"]) == ("", "")
    body = {"machine": "box-1", "sessions": [], "tools": [],
            "facts": {"host": "-oProxyCommand=x", "os_user": "-lroot"}}
    assert http.request("POST", hub.url + "/bridge/heartbeat", token, body)[0] == 200
    row = hub.store.db.execute("SELECT host, os_user FROM devices WHERE id=?", (dev,)).fetchone()
    assert (row["host"], row["os_user"]) == ("", "")
    assert hub.ws.facts(dev).get("os_user", "") == ""
    assert "-o" not in {m["host"][:2] for m in hub.store.machines(
        hub.store.authenticate(ADMIN))}


def remove_project(hub, name="proj-a"):
    """The admin removes (archives) a project, as DELETE /api/projects/<name> does."""
    from kaptyorka import admin_ops
    from kaptyorka.store import Principal

    a = next(p for p in hub.store.people() if p["role"] == "admin")
    admin_ops.project_remove(hub.store, name, Principal(a["id"], a["name"], "admin", 0, "", False,
                                                        False))


def test_work_refused_in_removed_project_even_from_its_folder(hub, alice):
    pid, client = alice
    bridge(hub, pid)
    remove_project(hub)  # alice's grant goes; her boss session still runs in folder proj-a
    err = call_error(client, BOSS, "work", project="proj-a", task="x")
    assert err.endswith(": proj-a was removed; ask the admin to register it again")
    assert hub.ws.list(hub.store.authenticate(client.token)) == []
    hub.store.project_register("proj-b", by="test")  # an unrelated folder still works as before
    assert "write role" in call_error(client, BOSS, "work", project="proj-b", task="x")


def test_removed_project_activity_is_the_admins(hub, alice):
    hub.store.notes_write_from_web("test", "state", "where it stands", project="proj-a")
    remove_project(hub)
    status, body = http.request("GET", hub.url + "/api/projects/proj-a/activity", ADMIN)
    assert status == 200 and body["state"] == "where it stands"
    assert http.request("GET", hub.url + "/api/projects/proj-a/activity", alice[1].token)[0] == 403
