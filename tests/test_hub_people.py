import pytest

from conftest import start_hub
from helpers import person
from kaptyorka import http
from kaptyorka.store import Store

ADMIN = "T"  # the hub fixture's legacy token belongs to an admin


def make_invite(hub, name="ann"):
    status, _ = http.request("POST", hub.url + "/api/people", ADMIN, {"name": name})
    assert status == 200
    status, body = http.request("POST", hub.url + "/api/invites", ADMIN, {"person": name})
    assert status == 200
    return body


def redeem(hub, code, **extra):
    body = {"device_name": "ann-laptop", "host": "pc", "os_user": "ann", **extra}
    return http.request("POST", f"{hub.url}/i/{code}", "", body)


def test_invite_roundtrip(hub):
    inv = make_invite(hub)
    assert len(inv["code"]) == 22 and inv["url"] == f"{hub.url}/i/{inv['code']}"
    status, body = redeem(hub, inv["code"])
    assert status == 200 and body["hub"] == hub.url
    p = hub.store.authenticate(body["token"])
    assert p.person_name == "ann" and p.device_name == "ann-laptop" and not p.is_admin()
    status, _ = http.request("GET", hub.url + "/api/feed", body["token"])
    assert status == 200
    dev = hub.store.devices(p.person_id)[0]
    assert dev["host"] == "pc" and dev["os_user"] == "ann"
    with hub.store.lock:
        row = hub.store.db.execute("SELECT secret_hash FROM devices WHERE id=?",
                                   (dev["id"],)).fetchone()
    import hashlib
    assert row["secret_hash"] == hashlib.sha256(body["secret"].encode()).hexdigest()
    kinds = [r["kind"] for r in hub.store.db.execute("SELECT kind FROM log")]
    assert "join" in kinds


def test_invite_single_use(hub):
    inv = make_invite(hub)
    assert redeem(hub, inv["code"])[0] == 200
    status, body = redeem(hub, inv["code"])
    assert status == 400 and "already used" in body


def test_invite_unknown_code(hub):
    status, body = redeem(hub, "x" * 22)
    assert status == 400 and "no such invite" in body


def test_invite_expired(tmp_path):
    class Clock:
        now = 1000.0

        def __call__(self):
            return self.now

    clock = Clock()
    store = Store(tmp_path / "h.db", clock=clock, legacy_token=ADMIN)
    stop = start_hub(store)
    try:
        from types import SimpleNamespace
        h = SimpleNamespace(url=f"http://127.0.0.1:{stop.port}", store=store)
        inv = make_invite(h)
        clock.now += 8 * 86400
        status, body = redeem(h, inv["code"])
        assert status == 400 and "expired" in body
        status, page = http.request("GET", inv["url"], "")
        assert status == 404
    finally:
        stop()
        store.close()


def test_failed_redeems_are_rate_limited(hub):
    codes = [redeem(hub, "y" * 22)[0] for _ in range(12)]
    assert codes[:10] == [400] * 10 and codes[-1] == 429


def test_invite_page_html(hub):
    inv = make_invite(hub)
    status, page = http.request("GET", inv["url"], "")
    assert status == 200
    assert f"kaptyorka join {inv['url']}" in page and "<pre>" in page
    assert "Join the team hub" in page and "<script" not in page


def test_invite_page_bad_code(hub):
    status, page = http.request("GET", hub.url + "/i/nope", "")
    assert status == 404 and "This invite is not valid." in page
    inv = make_invite(hub)
    redeem(hub, inv["code"])
    assert http.request("GET", inv["url"], "")[0] == 404


def test_member_cannot_create_people(hub):
    _, token = person(hub.store, "ben")
    for method, path, body in [("GET", "/api/people", None),
                               ("POST", "/api/people", {"name": "x"}),
                               ("POST", "/api/invites", {"person": "ben"}),
                               ("POST", "/api/devices/1/revoke", None)]:
        status, text = http.request(method, hub.url + path, token, body)
        assert (status, text) == (403, "admin only"), path


def test_people_validation(hub):
    make_invite(hub)
    assert http.request("POST", hub.url + "/api/people", ADMIN, {"name": "ann"})[0] == 409
    status, text = http.request("POST", hub.url + "/api/people", ADMIN,
                                {"name": "z", "role": "boss"})
    assert status == 400 and "role must be" in text
    status, text = http.request("POST", hub.url + "/api/people", ADMIN,
                                {"name": "z", "role": "admin"})
    assert status == 400 and "already an admin" in text
    assert http.request("POST", hub.url + "/api/invites", ADMIN, {"person": "ghost"})[0] == 404
    assert http.request("POST", hub.url + "/api/people", ADMIN, {})[0] == 400


def test_people_list_has_devices(hub):
    inv = make_invite(hub)
    redeem(hub, inv["code"])
    status, rows = http.request("GET", hub.url + "/api/people", ADMIN)
    ann = next(r for r in rows if r["name"] == "ann")
    assert status == 200 and ann["role"] == "member"
    assert [d["name"] for d in ann["devices"]] == ["ann-laptop"]
    assert "token_hash" not in ann["devices"][0] and "secret_hash" not in ann["devices"][0]


def test_revoke_device(hub):
    inv = make_invite(hub)
    _, body = redeem(hub, inv["code"])
    dev = hub.store.devices(hub.store.authenticate(body["token"]).person_id)[0]
    assert http.request("POST", f"{hub.url}/api/devices/{dev['id']}/revoke", ADMIN)[0] == 204
    assert http.request("GET", hub.url + "/api/feed", body["token"])[0] == 401
