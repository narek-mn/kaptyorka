from types import SimpleNamespace

import pytest

from conftest import start_hub
from helpers import call, person, run
from kaptyorka import http
from kaptyorka.store import ONLINE_S, Store

LEGACY = "old-shared-token"
ANN = {"key": "claude:1", "machine": "pc", "tool": "claude-code", "project": "murmur"}
BEN = {"key": "claude:2", "machine": "dgx", "tool": "claude-code", "project": "backend"}


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


@pytest.fixture
def app(store):
    ann_id, ann_token = person(store, "ann")
    ben_id, ben_token = person(store, "ben")
    stop = start_hub(store)
    url = f"http://127.0.0.1:{stop.port}"
    yield SimpleNamespace(url=url, store=store,
                          ann=SimpleNamespace(id=ann_id, url=url, token=ann_token),
                          ben=SimpleNamespace(id=ben_id, url=url, token=ben_token),
                          legacy=SimpleNamespace(url=url, token=LEGACY))
    stop()


def device_id(store, person_id):
    return store.devices(person_id)[0]["id"]


def test_unknown_token_401(app):
    assert http.request("GET", app.url + "/api/feed", "nope")[0] == 401
    assert http.request("GET", app.url + "/api/feed", app.ann.token)[0] == 200


def test_revoked_device_401_and_sessions_vanish(app, store, clock):
    run(call(app.ben, BEN, "who"))
    store.device_revoke(device_id(store, app.ben.id))
    assert http.request("GET", app.url + "/api/feed", app.ben.token)[0] == 401
    with pytest.raises(Exception):
        run(call(app.ben, BEN, "who"))
    clock.now += ONLINE_S + 1
    names = [r["name"] for r in run(call(app.ann, ANN, "who"))]
    assert names == ["pc/claude-code/murmur"]


def test_ten_failures_then_429(app):
    for _ in range(10):
        assert http.request("GET", app.url + "/api/feed", "bad")[0] == 401
    assert http.request("GET", app.url + "/api/feed", "bad")[0] == 429
    assert http.request("GET", app.url + "/api/feed", app.ann.token)[0] == 429


def test_open_routes_need_no_token(app):
    assert http.request("GET", app.url + "/", "")[0] == 200
    assert http.request("GET", app.url + "/i/nope", "")[0] != 401
    assert http.request("POST", app.url + "/i/nope", "", {})[0] != 401


def test_legacy_token_is_admin(app, store):
    rows = run(call(app.legacy, ANN, "who"))
    me = next(r for r in rows if r["you"])
    admin = next(p for p in store.people() if p["role"] == "admin")
    assert store.agent(ANN["key"]).person_id == admin["id"]
    assert me["name"] == "pc/claude-code/murmur"


def test_legacy_machine_mapped_to_person(app, store):
    with store.lock:
        store.db.execute("INSERT INTO settings (key, value) VALUES (?, ?)",
                         ("legacy:pc", str(app.ann.id)))
    p = store.authenticate(LEGACY, machine="pc")
    assert (p.person_id, p.person_name, p.legacy, p.device_name) == (app.ann.id, "ann", True, "pc")
    assert store.authenticate(LEGACY, machine="other").is_admin()
    run(call(app.legacy, ANN, "who"))
    assert store.agent(ANN["key"]).person_id == app.ann.id


def test_principal_on_agent_row(app, store, clock):
    run(call(app.ann, ANN, "who"))
    a = store.agent(ANN["key"])
    assert a.person_id == app.ann.id and a.started == clock.now
    p = store.authenticate(app.ann.token)
    assert (p.person_name, p.role, p.shared, p.legacy, p.is_admin()) == ("ann", "member", False, False, False)


def test_authenticate_updates_last_used(store, clock):
    pid, token = person(store, "ann")
    clock.now = 2000.0
    assert store.authenticate(token).device_id == device_id(store, pid)
    assert store.devices(pid)[0]["last_used"] == 2000.0
    assert store.authenticate("nope") is None

