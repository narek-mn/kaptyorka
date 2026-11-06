"""The hub's worker side: heartbeat answers, launches in the bridge stream, the worker tools
and routes. A bridge is simulated with raw HTTP calls."""
from types import SimpleNamespace

import pytest

import kaptyorka
from conftest import start_hub
from helpers import _client, call, call_error, person, run
from kaptyorka import http
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


def start_worker(hub, alice, **kw):
    """alice's boss session asks for a worker; the bridge on box-1 takes and links it."""
    pid, client = alice
    b = bridge(hub, pid)
    answer = run(call(client, BOSS, "work", project="proj-a", task="fix the tests", **kw))
    assert answer == "starting `proj-a-w1` on box-1 (u)"
    item = poll(hub, b)[0]
    http.request("POST", hub.url + "/bridge/ack", b.token,
                 {"id": item["id"], "ok": True, "launch": True})
    assert http.request("POST", hub.url + "/bridge/worker", b.token,
                        {"worker": "proj-a-w1", "key": "claude:w1"})[0] == 204
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
                    "projects": ["proj-a"], "you": "alice", "shared": False}
    dev = hub.store.authenticate(token).device_id
    assert hub.ws.facts(dev)["tmux"] is True and hub.ws.folders(dev) == {"proj-a": "/w/proj-a"}
    with hub.store.lock:
        assert hub.store.db.execute("SELECT cwd FROM agents WHERE key='claude:1'"
                                    ).fetchone()["cwd"] == "/w/proj-a"
    # a 0.2 bridge: no facts, no folders, the shared token
    status, body = http.request("POST", hub.url + "/bridge/heartbeat", ADMIN,
                                {"machine": "old", "sessions": []})
    assert status == 200 and body["auto_update"] is False and body["shared"] is False
    assert sorted(body["projects"]) == ["proj-a", "proj-b"]  # the admin reads every project


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
        "worker `proj-a-w1` is up on box-1; attach with `claude attach proj-a-w1`"]
    # linked once only
    assert http.request("POST", hub.url + "/bridge/worker", b.token,
                        {"worker": "proj-a-w1", "key": "claude:w1"})[0] == 404

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


def test_work_refuses_without_write_role(hub, registered):
    pid, client = member(hub, "alice", role="read")
    bridge(hub, pid)
    err = call_error(client, BOSS, "work", project="proj-a", task="x")
    assert "write role" in err
    assert hub.ws.list(hub.store.authenticate(client.token)) == []


def test_work_allows_own_unregistered_folder(hub):
    pid, client = member(hub, "alice", role=None)
    bridge(hub, pid, folders={"proj-b": "/w/proj-b"})
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


def test_admin_cannot_use_members_private_device(hub, alice):
    pid, _ = alice
    bridge(hub, pid)
    admin = as_token(hub, ADMIN)
    answer = run(call(admin, ident("k:x", machine="hubbox"), "work", project="proj-a",
                      task="x", machine="box-1"))
    assert answer == "no machine can take proj-a: no machine named box-1 that you may use"
    answer = run(call(admin, ident("k:x", machine="hubbox"), "work", project="proj-a", task="x"))
    assert answer.startswith("no machine can take proj-a: ")
    assert hub.ws.list(hub.store.authenticate(ADMIN)) == []


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
    b = start_worker(hub, alice)
    worker = ident("claude:w1", machine="box-1", tool="claude-code")
    text = brief(hub, b.token, worker)
    assert text.startswith("You are kaptyorka worker `proj-a-w1` for `laptop/codex/proj-a`"
                           " (alice). Your task:\n\nfix the tests")
    assert "Kaptyorka: you are `box-1/claude-code/proj-a`." in text
    clock[0] += 301  # an older session: a resumed one does not hear its task again
    assert not brief(hub, b.token, worker).startswith("You are kaptyorka worker")


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
