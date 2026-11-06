"""Legacy (0.1 shared token) machines: assign each to a person, then rekey its bridge."""
import hashlib
import secrets
from types import SimpleNamespace

import pytest

from conftest import start_hub
from helpers import person
from kaptyorka import http
from kaptyorka.store import Store

LEGACY = "old-shared-token"
MACHINE = "mo@lab"
NEW = {"version": "0.2.0", "host": "lab", "os_user": "mo"}
OLD = {"version": "0.1.9", "host": "lab", "os_user": "mo"}


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def store(tmp_path, clock):
    s = Store(tmp_path / "hub.db", clock=clock, legacy_token=LEGACY)
    yield s
    s.close()


def admin_token(store):
    """A device of its own for the admin, so admin routes work once the legacy token dies."""
    admin = next(p for p in store.people() if p["role"] == "admin")
    token = secrets.token_urlsafe(16)
    with store.lock:
        store.db.execute("INSERT INTO devices (person_id, name, token_hash, created)"
                         " VALUES (?, 'admin-pc', ?, 0)",
                         (admin["id"], hashlib.sha256(token.encode()).hexdigest()))
    return token


@pytest.fixture
def app(store):
    kim_id, kim_token = person(store, "kim")
    stop = start_hub(store)
    url = f"http://127.0.0.1:{stop.port}"
    yield SimpleNamespace(url=url, kim_id=kim_id, kim_token=kim_token, admin=admin_token(store))
    stop()


def legacy_seen(store, machine, key="codex:1"):
    store.seen(key, machine, "codex", "proj", principal=store.authenticate(LEGACY, machine=machine))


def beat(app, machine=MACHINE, facts=NEW, token=LEGACY, key="codex:1"):
    status, _ = http.request("POST", app.url + "/bridge/heartbeat", token,
                             {"machine": machine, "sessions": [
                                 {"key": key, "tool": "codex", "project": "proj"}],
                              "facts": facts})
    assert status == 200


def poll(app, machine=MACHINE, token=LEGACY):
    status, items = http.request("GET", f"{app.url}/bridge/poll?machine={machine}&wait=0", token)
    assert status == 200
    return items


def assign(app, machine=MACHINE, who="kim", token=None):
    return http.request("POST", app.url + "/api/legacy/assign", token or app.admin,
                        {"machine": machine, "person": who})


def rekeys(store):
    with store.lock:
        return [dict(r) for r in store.db.execute("SELECT * FROM rekeys")]


def test_legacy_machine_unassigned_then_assigned(store):
    kim, _ = person(store, "kim")
    legacy_seen(store, MACHINE)
    assert store.legacy_unassigned() == [MACHINE]
    assert store.authenticate(LEGACY, machine=MACHINE).is_admin()
    store.assign_legacy(MACHINE, kim, "root")
    p = store.authenticate(LEGACY, machine=MACHINE)
    assert (p.person_id, p.person_name, p.legacy) == (kim, "kim", True)  # immediately
    assert store.agent("codex:1").person_id == kim
    assert store.legacy_unassigned() == []
    assert [d["name"] for d in store.devices(kim)] == ["kim-laptop", MACHINE]
    row = rekeys(store)[0]
    assert row["device_name"] == MACHINE and row["token"] and row["secret"]
    assert store.authenticate(row["token"]).device_name == MACHINE
    entry = next(r for r in store.log() if r["kind"] == "legacy_assign")
    assert (entry["person"], entry["detail"]) == ("root", f"{MACHINE} -> kim")
    with pytest.raises(Exception, match="not an unassigned legacy machine"):
        store.assign_legacy(MACHINE, kim, "root")
    with pytest.raises(Exception, match="no such person"):
        store.assign_legacy("x", 9999, "root")


def test_legacy_used_logged_once_per_machine_per_day(store, clock):
    legacy_seen(store, MACHINE)
    store.authenticate(LEGACY, machine=MACHINE)
    store.authenticate(LEGACY, machine="made-up")  # never seen: no log row, no settings row
    used = lambda: [r["detail"] for r in store.log() if r["kind"] == "legacy_used"]  # noqa: E731
    assert used() == [MACHINE]
    with store.lock:
        assert store.db.execute("SELECT COUNT(*) FROM settings WHERE key LIKE 'legacy_seen:%'"
                                ).fetchone()[0] == 1
    clock.now += 86400
    store.authenticate(LEGACY, machine=MACHINE)
    assert used() == [MACHINE, MACHINE]


def test_revoking_the_new_device_drops_its_rekey(store):
    kim, _ = person(store, "kim")
    legacy_seen(store, MACHINE)
    store.assign_legacy(MACHINE, kim, "root")
    store.device_revoke(next(d["id"] for d in store.devices(kim) if d["name"] == MACHINE))
    assert rekeys(store) == []


def test_rekey_offered_once_and_consumed(app, store):
    beat(app)
    assert poll(app) == []
    assert assign(app)[0] == 204
    assert all(i.get("kind") != "rekey" for i in poll(app))  # no version known for it yet
    beat(app)
    store.send_from_web("root", store.agent("codex:1").name, "hi")
    items = poll(app)
    assert [i["kind"] for i in items] == ["rekey", "message"]
    token, secret = items[0]["token"], items[0]["secret"]
    assert items[1]["key"] == "codex:1" and "hi" in items[1]["text"]
    assert [i["kind"] for i in poll(app, token=app.kim_token, machine=MACHINE)] == []  # not kim's laptop
    assert http.request("POST", app.url + "/bridge/rekeyed", LEGACY, {"machine": MACHINE})[0] == 403
    assert http.request("POST", app.url + "/bridge/rekeyed", app.kim_token,
                        {"machine": MACHINE})[0] == 403
    assert http.request("POST", app.url + "/bridge/rekeyed", token, {"machine": MACHINE})[0] == 204
    assert rekeys(store) == []
    assert poll(app, token=token) == []
    assert all(i.get("kind") != "rekey" for i in poll(app))  # the shared token still works
    p = store.authenticate(token)
    assert (p.person_name, p.device_name, p.legacy) == ("kim", MACHINE, False)
    [entry] = [r for r in store.log() if r["kind"] == "rekeyed"]
    assert (entry["person"], entry["detail"]) == ("kim", MACHINE)
    assert secret


def test_old_bridge_gets_no_rekey(app, store):
    beat(app, facts=OLD)
    assert assign(app)[0] == 204
    beat(app, facts=OLD)
    store.send_from_web("root", store.agent("codex:1").name, "hi")
    items = poll(app)
    assert [i["kind"] for i in items] == ["message"]
    assert set(items[0]) >= {"id", "key", "text"}
    assert len(rekeys(store)) == 1  # kept for when it upgrades
    assert store.agent("codex:1").person_id == app.kim_id  # the mapping alone attributes it


def test_legacy_bridge_routes_attribute_to_assigned_person(app, store):
    beat(app)
    admin = next(p for p in store.people() if p["role"] == "admin")
    assert store.agent("codex:1").person_id in (None, admin["id"])
    assert assign(app)[0] == 204
    beat(app, key="codex:2")
    assert store.agent("codex:1").person_id == app.kim_id
    assert store.agent("codex:2").person_id == app.kim_id
    dev = next(d for d in store.devices(app.kim_id) if d["name"] == MACHINE)
    assert (dev["version"], dev["host"]) == ("0.2.0", "lab")


def test_legacy_routes(app, store):
    beat(app)
    status, body = http.request("GET", app.url + "/api/legacy", app.admin)
    assert (status, body) == (200, {"unassigned": [MACHINE], "enabled": True,
                                    "ready_to_disable": False})
    for method, path, payload in (("GET", "/api/legacy", None),
                                  ("POST", "/api/legacy/off", {}),
                                  ("POST", "/api/legacy/on", {}),
                                  ("POST", "/api/legacy/assign", {"machine": MACHINE,
                                                                 "person": "kim"})):
        assert http.request(method, app.url + path, app.kim_token, payload) == (403, "admin only")
    assert assign(app, who="nobody") == (400, "no such person")
    assert assign(app, machine="nowhere")[0] == 400


READY = "All old machines have their own tokens; switch the shared token off on the page."


def start_brief(app, token, key="claude:7"):
    h = {"X-Kaptyorka-Key": key, "X-Kaptyorka-Machine": "admin-pc",
         "X-Kaptyorka-Tool": "claude-code", "X-Kaptyorka-Project": "proj"}
    status, text = http.request("GET", app.url + "/api/brief?event=start", token, headers=h)
    assert status == 200
    return text


def ready(app):
    return http.request("GET", app.url + "/api/legacy", app.admin)[1]["ready_to_disable"]


def test_legacy_accepted_until_switched_off(app, store):
    assert store.authenticate(LEGACY) is not None  # nothing seen yet
    beat(app, machine="m1")
    beat(app, machine="m2", key="codex:2")
    assert not ready(app)
    assert assign(app, machine="m1")[0] == 204
    assert assign(app, machine="m2")[0] == 204
    assert not ready(app)  # their bridges have not taken their tokens yet
    for machine in ("m1", "m2"):
        token = next(r["token"] for r in rekeys(store) if r["device_name"] == machine)
        assert http.request("POST", app.url + "/bridge/rekeyed", token, {"machine": machine})[0] == 204
    assert ready(app)
    assert store.authenticate(LEGACY, machine="m1") is not None  # never switched off by itself
    assert READY in start_brief(app, app.admin)
    assert READY not in start_brief(app, app.kim_token, "claude:8")
    beat(app, machine="m3", key="codex:3")
    assert not ready(app)
    assert assign(app, machine="m3")[0] == 204
    assert http.request("POST", app.url + "/api/legacy/off", app.admin, {})[0] == 204
    assert store.authenticate(LEGACY) is None
    assert rekeys(store) == []  # fetched with the shared token, so dropped with it
    assert http.request("GET", app.url + "/api/legacy", LEGACY)[0] == 401
    body = http.request("GET", app.url + "/api/legacy", app.admin)[1]
    assert (body["enabled"], body["ready_to_disable"]) == (False, False)
    assert READY not in start_brief(app, app.admin, "claude:9")  # a new session: no cached brief
    assert http.request("POST", app.url + "/api/legacy/on", app.admin, {})[0] == 204
    assert store.authenticate(LEGACY) is not None
    assert [r["detail"] for r in store.log() if r["kind"] == "legacy_toggle"] == ["on", "off"]
