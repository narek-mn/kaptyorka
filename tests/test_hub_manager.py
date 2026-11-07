"""The hub keeps the manager session alive through the admin's own bridge on its machine."""
import json
import os
from types import SimpleNamespace

import pytest

import kaptyorka
from conftest import start_hub
from helpers import person
from kaptyorka import http, identity, manager, paths
from kaptyorka.hub import MANAGER_FAILED, MANAGER_FIRST
from kaptyorka.hub_feed import feed
from kaptyorka.manager import ManagerStore
from kaptyorka.rules import RulesStore
from kaptyorka.store import ONLINE_S, Store
from kaptyorka.workers import GONE_S, WorkerStore

HOST = "box-1"
URL = "http://127.0.0.1:9999"


@pytest.fixture
def clock():
    return [1_000_000.0]


@pytest.fixture
def hub(tmp_path, clock, monkeypatch):
    monkeypatch.setenv("KAPTYORKA_HOME", str(tmp_path / "kh"))
    monkeypatch.setattr(identity, "facts", lambda: {"host": HOST, "os_user": "u",
                                                    "version": kaptyorka.__version__})
    store = Store(tmp_path / "hub.db", clock=lambda: clock[0], legacy_token="T")
    mm = ManagerStore(store)
    mm.set_enabled(False, "test")  # the start-up tick must not race the test
    stop = start_hub(store, manager_url=URL)
    yield SimpleNamespace(url=f"http://127.0.0.1:{stop.port}", store=store, mm=mm,
                          ws=WorkerStore(store), tick=stop.app.state.tick,
                          folder=tmp_path / "kh" / manager.DIR)
    stop()
    store.close()


def admin_bridge(hub):
    pid, _ = person(hub.store, "root", "admin")
    code = hub.store.invite_create(pid)
    token, _ = hub.store.invite_redeem(code, HOST, host=HOST, os_user="u")
    b = SimpleNamespace(token=token, device_id=hub.store.authenticate(token).device_id)
    beat(hub, b)
    return b


def beat(hub, b, sessions=()):
    body = {"machine": HOST, "sessions": list(sessions), "tools": [],
            "facts": {"host": HOST, "os_user": "u", "version": kaptyorka.__version__}}
    assert http.request("POST", hub.url + "/bridge/heartbeat", b.token, body)[0] == 200


def orders(hub, b):
    status, items = http.request("GET", f"{hub.url}/bridge/poll?machine={HOST}", b.token)
    assert status == 200
    return [i for i in items if i.get("launch")]


def ack(hub, b, item, ok=True, error=""):
    assert http.request("POST", hub.url + "/bridge/ack", b.token,
                        {"id": item["id"], "ok": ok, "launch": True, "error": error})[0] == 204


def page_texts(hub, admin="root"):
    with hub.store.lock:
        return [r["text"] for r in hub.store.db.execute(
            "SELECT text FROM messages WHERE to_key=? ORDER BY id", (f"web:{admin}",))]


def state(hub):
    w = hub.ws.by_name("manager")
    return w and w["state"]


def test_disabled_manager_tick_does_nothing(hub):
    admin_bridge(hub)
    hub.tick()
    assert hub.mm.person_id() is None and not hub.folder.exists() and state(hub) is None


def test_manager_tick_writes_files_and_rewrites_on_rules_change(hub):
    hub.mm.set_enabled(True, "test")
    hub.tick()  # no admin bridge yet: the account and files, but no launch
    assert hub.mm.person_id() is not None and state(hub) is None
    assert os.stat(hub.folder).st_mode & 0o777 == 0o700
    cfg = json.loads((hub.folder / "config.json").read_text())
    assert cfg["hub"] == URL and cfg["person"] == "manager"
    assert cfg["machine"] == f"manager@{HOST}"
    assert hub.store.authenticate(cfg["token"]).role == "manager"
    assert "No team rules yet." in (hub.folder / "CLAUDE.md").read_text()
    hub.tick()
    assert json.loads((hub.folder / "config.json").read_text()) == cfg
    RulesStore(hub.store).set_rules("always run the tests", "root")
    hub.tick()
    assert "always run the tests" in (hub.folder / "CLAUDE.md").read_text()
    assert json.loads((hub.folder / "config.json").read_text()) == cfg  # same token
    (hub.folder / "CLAUDE.md").unlink()
    hub.tick()
    assert "always run the tests" in (hub.folder / "CLAUDE.md").read_text()
    (hub.folder / "config.json").unlink()  # a lost token: a new one, the old one revoked
    hub.tick()
    new = json.loads((hub.folder / "config.json").read_text())
    assert new["token"] != cfg["token"] and hub.store.authenticate(cfg["token"]) is None
    assert hub.store.authenticate(new["token"]).role == "manager"


def test_manager_relaunch_limits(hub, clock):
    b = admin_bridge(hub)
    hub.mm.set_enabled(True, "test")
    t0 = clock[0]
    hub.tick()
    [o1] = orders(hub, b)
    assert o1["worker"] == "manager" and o1["manager"] is True and o1["person"] == "root"
    assert o1["folder"] == str(hub.folder) and o1["first_message"] == MANAGER_FIRST
    assert o1["env"] == {"KAPTYORKA_CONFIG": str(hub.folder / "config.json")}
    hub.tick()
    assert orders(hub, b) == [] and state(hub) == "starting"  # one at a time
    ack(hub, b, o1)
    assert http.request("POST", hub.url + "/bridge/worker", b.token,
                        {"worker": "manager", "key": "claude:m1"})[0] == 204
    assert state(hub) == "up" and hub.store.agent("claude:m1").person_id == hub.mm.person_id()
    assert any("worker `manager` is up" in t for t in page_texts(hub))
    # the page shows the manager's session with its role
    admin = hub.store.authenticate(b.token)
    rows = {a["person"]: a for a in feed(hub.store, admin)["agents"]}
    assert rows["manager"]["role"] == "manager"
    # its session goes quiet: closed as "session gone", started again in the same tick
    clock[0] = t0 + ONLINE_S + GONE_S + 130
    beat(hub, b)
    hub.tick()
    [o2] = orders(hub, b)
    assert o2["id"] != o1["id"] and state(hub) == "starting"
    ack(hub, b, o2, ok=False, error="claude: not found")
    hub.tick()
    assert "worker `manager` failed to start: claude: not found" in page_texts(hub)
    clock[0] += 121  # two launches within the hour: no third
    beat(hub, b)
    hub.tick()
    assert orders(hub, b) == [] and state(hub) == "failed"
    assert hub.mm.may_launch(clock[0]) == (False, "the manager was launched twice in the"
                                                  " last hour")
    assert MANAGER_FAILED not in page_texts(hub)  # one failure so far
    # an hour later it may start again; two failures in an hour: the page hears it once
    clock[0] += 3600
    beat(hub, b)
    hub.tick()
    [o3] = orders(hub, b)
    ack(hub, b, o3, ok=False, error="boom")
    hub.tick()
    clock[0] += 121
    beat(hub, b)
    hub.tick()
    [o4] = orders(hub, b)
    ack(hub, b, o4, ok=False, error="boom")
    hub.tick()
    hub.tick()
    clock[0] += 30
    beat(hub, b)
    hub.tick()
    assert page_texts(hub).count(MANAGER_FAILED) == 1


def test_manager_waits_for_the_admin_bridge(hub, clock):
    b = admin_bridge(hub)
    hub.mm.set_enabled(True, "test")
    clock[0] += ONLINE_S + 1  # the bridge went quiet
    hub.tick()
    assert orders(hub, b) == [] and state(hub) is None
    beat(hub, b)
    hub.tick()
    assert len(orders(hub, b)) == 1


def test_manager_failure_never_stops_the_worker_tick(hub, monkeypatch, capsys):
    hub.mm.set_enabled(True, "test")
    seen = []
    monkeypatch.setattr(hub.ws.__class__, "tick", lambda self, now: seen.append(now) or [])

    def boom():
        raise RuntimeError("no host")
    monkeypatch.setattr(identity, "facts", boom)
    hub.tick()
    assert seen and "manager tick failed: no host" in capsys.readouterr().out
