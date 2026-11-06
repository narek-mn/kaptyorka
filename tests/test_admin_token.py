"""The admin works with a device of their own: the shared token loses admin rights once that
exists and cannot switch itself off; page logins and doors opened from the page are logged."""
import pytest

from conftest import start_hub
from helpers import person
from kaptyorka import http
from kaptyorka.store import Store

LEGACY = "old-shared-token"


@pytest.fixture
def hub(tmp_path, home):
    store = Store(tmp_path / "hub.db", legacy_token=LEGACY)
    stop = start_hub(store)
    admin = next(p for p in store.people() if p["role"] == "admin")
    yield store, f"http://127.0.0.1:{stop.port}", admin
    stop()
    store.close()


def test_legacy_off_refused_without_an_own_admin_device(hub):
    store, url, _ = hub
    status, body = http.request("POST", url + "/api/legacy/off", LEGACY)
    assert status == 400 and "your own admin token" in body
    assert store.legacy_enabled()


def test_legacy_off_refused_for_the_shared_token(hub):
    store, url, admin = hub
    own = store.device_add(admin["id"], "hub")
    status, body = http.request("POST", url + "/api/legacy/off", LEGACY)
    assert status == 400 and "your own admin token" in body
    assert store.legacy_enabled()
    assert http.request("POST", url + "/api/legacy/off", own)[0] == 204
    assert not store.legacy_enabled()


def test_shared_token_loses_admin_routes_once_admin_has_own_device(hub):
    store, url, admin = hub
    assert http.request("GET", url + "/api/people", LEGACY)[0] == 200
    own = store.device_add(admin["id"], "hub")
    for method, path, body in (("GET", "/api/people", None), ("GET", "/api/log", None),
                               ("POST", "/api/people", {"name": "zed"})):
        status, text = http.request(method, url + path, LEGACY, body)
        assert (status, text) == (403, "use your own admin token"), path
    assert http.request("GET", url + "/api/people", own)[0] == 200
    assert http.request("GET", url + "/api/feed", LEGACY)[0] == 200  # still a working token


def test_page_login_logged_once_a_day(hub):
    store, url, admin = hub
    own = store.device_add(admin["id"], "hub")
    for _ in range(3):
        assert http.request("GET", url + "/api/me", own)[0] == 200
    rows = [r for r in store.log() if r["kind"] == "page_login"]
    assert [(r["person"], r["detail"]) for r in rows] == [(admin["name"], "hub")]


def test_page_send_logs_the_door_it_opens(hub):
    store, url, admin = hub
    _, ann_t = person(store, "ann")
    own = store.device_add(admin["id"], "hub")
    store.seen("a1", "pc", "codex", "p", principal=store.authenticate(ann_t))
    assert http.request("POST", url + "/api/send", own, {"to": "pc/codex/p", "text": "hi"})[0] == 200
    rows = [r for r in store.log() if r["kind"] == "door_open"]
    assert [(r["person"], r["detail"]) for r in rows] == [
        (admin["name"], f"web:{admin['name']} <-> pc/codex/p")]
    http.request("POST", url + "/api/send", own, {"to": "pc/codex/p", "text": "again"})
    assert len([r for r in store.log() if r["kind"] == "door_open"]) == 1  # already open


def test_lock_screen_takes_a_pasted_token(hub):
    _, url, _ = hub
    page = http.request("GET", url + "/", "")[1]
    assert 'id="unlock-token"' in page and 'type="password"' in page
    assert '"#t=" + encodeURIComponent(t)' in page


def test_machine_owner_route(hub):
    store, url, admin = hub
    own = store.device_add(admin["id"], "hub")
    ann, ann_t = person(store, "ann")
    store.heartbeat("ann@pc", [], principal=store.authenticate(ann_t),
                    facts={"host": "pc", "os_user": "ann", "version": "0.2.0"})
    assert not store.may_use("pc", ann)
    assert http.request("POST", url + "/api/machines/pc/owner", ann_t, {"person": "ann"})[0] == 403
    assert http.request("POST", url + "/api/machines/pc/owner", own, {"person": "ann"})[0] == 204
    assert store.may_use("pc", ann)
    assert http.request("POST", url + "/api/machines/pc/owner", own, {"person": "nobody"}) == \
        (400, "no such person")
    assert http.request("POST", url + "/api/machines/nope/owner", own, {"person": "ann"}) == \
        (400, "no such machine: nope")
    assert http.request("POST", url + "/api/machines/pc/owner", own, {"person": ""})[0] == 204
    assert not store.may_use("pc", ann)
    assert [r["detail"] for r in store.log() if r["kind"] == "machine_owner"] == [
        "pc -> nobody", "pc -> ann"]
    page = http.request("GET", url + "/", "")[1]
    assert '"/owner", { person: owner.value }' in page
