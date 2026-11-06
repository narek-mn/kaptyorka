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


def test_machines_route_filtered(hub):
    _, ta = person(hub.store, "ann")
    _, tb = person(hub.store, "ben")
    pc, _ = person(hub.store, "cat")
    for t, host in ((ta, "h-a"), (tb, "h-b")):
        facts = {"host": host, "os_user": "u", "version": "0.2.0"}
        assert http.request("POST", hub.url + "/bridge/heartbeat", t,
                            {"machine": host, "sessions": [], "facts": facts})[0] == 204
    status, rows = http.request("GET", hub.url + "/api/machines", ADMIN)
    assert status == 200 and sorted(r["host"] for r in rows) == ["h-a", "h-b"]
    _, rows = http.request("GET", hub.url + "/api/machines", ta)
    assert [r["host"] for r in rows] == ["h-a"]
    assert http.request("POST", hub.url + "/api/machines/h-b/grant", ta, {"person": "ann"})[0] == 403
    assert http.request("POST", hub.url + "/api/machines/h-b/grant", ADMIN, {"person": "ann"})[0] == 204
    _, rows = http.request("GET", hub.url + "/api/machines", ta)
    assert sorted(r["host"] for r in rows) == ["h-a", "h-b"]
    who = hub.store.db.execute("SELECT person FROM log WHERE kind='grant'").fetchone()["person"]
    assert who == hub.store.authenticate(ADMIN).person_name
    assert http.request("POST", hub.url + "/api/machines/h-b/revoke", ADMIN, {"person": "ann"})[0] == 204
    _, rows = http.request("GET", hub.url + "/api/machines", ta)
    assert [r["host"] for r in rows] == ["h-a"]
    status, text = http.request("POST", hub.url + "/api/machines/nope/grant", ADMIN, {"person": "ann"})
    assert status == 400 and "no such machine" in text
    status, text = http.request("POST", hub.url + "/api/machines/h-b/grant", ADMIN, {"person": "zed"})
    assert status == 400 and "no such person" in text


def test_device_shared_route(hub):
    pid, ta = person(hub.store, "ann")
    dev = hub.store.devices(pid)[0]
    url = f"{hub.url}/api/devices/{dev['id']}/shared"
    assert http.request("POST", url, ta, {"shared": True})[0] == 403
    assert http.request("POST", url, ADMIN, {"shared": True})[0] == 204
    assert hub.store.devices(pid)[0]["shared"] == 1
    assert http.request("POST", url, ADMIN, {"shared": False})[0] == 204
    assert hub.store.devices(pid)[0]["shared"] == 0


# projects, roles and visibility

from types import SimpleNamespace  # noqa: E402

from helpers import call, call_error, run  # noqa: E402


def as_person(hub, token):
    """The hub seen through one person's device token."""
    return SimpleNamespace(url=hub.url, token=token)


def ident(key, project, machine="pc"):
    return {"key": key, "machine": machine, "tool": "codex", "project": project}


def rows_by_name(rows):
    return {r["name"]: r for r in rows}


def test_who_hides_private_project(hub):
    _, ta = person(hub.store, "ann")
    _, tb = person(hub.store, "ben")
    ann, ben = as_person(hub, ta), as_person(hub, tb)
    run(call(ann, ident("k:a", "shop", "pa"), "who"))
    rows = rows_by_name(run(call(ben, ident("k:b", "lab", "pb"), "who")))
    a, b = rows["pa/codex/(private)"], rows["pb/codex/lab"]
    assert a["project"] == "(private)" and a["person"] == "ann" and not a["mine"]
    assert b["project"] == "lab" and b["person"] == "ben" and b["mine"] and b["you"]
    admin_rows = rows_by_name(run(call(hub, ident("k:x", "ops", "px"), "who")))
    assert admin_rows["pa/codex/shop"]["project"] == "shop"
    assert admin_rows["pb/codex/lab"]["project"] == "lab"


def test_same_folder_name_two_people_not_merged(hub):
    _, ta = person(hub.store, "ann")
    _, tb = person(hub.store, "ben")
    ann, ben = as_person(hub, ta), as_person(hub, tb)
    a_id, b_id = ident("k:a", "shop", "pa"), ident("k:b", "shop", "pb")
    run(call(ann, a_id, "who"))
    run(call(ben, b_id, "who"))
    seen_by_ann = rows_by_name(run(call(ann, a_id, "who")))
    seen_by_ben = rows_by_name(run(call(ben, b_id, "who")))
    assert seen_by_ann["pa/codex/shop"]["project"] == "shop"
    assert seen_by_ann["pb/codex/(private)"]["project"] == "(private)"
    assert seen_by_ben["pb/codex/shop"]["project"] == "shop"
    assert seen_by_ben["pa/codex/(private)"]["project"] == "(private)"
    admin_names = {r["name"] for r in run(call(hub, ident("k:x", "ops", "px"), "who"))}
    assert {"pa/codex/shop", "pb/codex/shop"} <= admin_names
    assert http.request("POST", hub.url + "/api/projects", ADMIN, {"name": "shop"})[0] == 204
    for name in ("ann", "ben"):
        assert http.request("POST", hub.url + "/api/projects/shop/grant", ADMIN,
                            {"person": name, "role": "read"})[0] == 204
    for client, i in ((ann, a_id), (ben, b_id)):
        rows = [r for r in run(call(client, i, "who")) if r["person"] in ("ann", "ben")]
        assert {r["project"] for r in rows} == {"shop"}
        assert {r["name"] for r in rows} == {"pa/codex/shop", "pb/codex/shop"}


def test_notes_project_scope_read_write(hub):
    _, ta = person(hub.store, "ann")
    _, tb = person(hub.store, "ben")
    _, tc = person(hub.store, "cat")
    ann, ben, cat = as_person(hub, ta), as_person(hub, tb), as_person(hub, tc)
    a_id, b_id, c_id = ident("k:a", "shop"), ident("k:b", "lab"), ident("k:c", "lab")
    hub.store.project_register("shop", by="root")
    hub.store.grant_project("shop", hub.store.person_by_name("ann")["id"], "write", by="root")
    hub.store.grant_project("shop", hub.store.person_by_name("ben")["id"], "read", by="root")
    run(call(ann, a_id, "notes_write", topic="api", text="v1", project="shop"))
    run(call(ann, a_id, "notes_write", topic="api", text="team v1"))
    note = run(call(ben, b_id, "notes_read", topic="api", project="shop"))
    assert note["text"] == "v1" and note["project"] == "shop"
    assert run(call(ben, b_id, "notes_read", topic="api"))["text"] == "team v1"
    assert [n["topic"] for n in run(call(ben, b_id, "notes_list", project="shop"))] == ["api"]
    err = call_error(ben, b_id, "notes_write", topic="api", text="x", project="shop")
    assert "no access to project shop" in err and "request_access" in err
    assert "no access to project shop" in call_error(cat, c_id, "notes_list", project="shop")
    assert "no access" in call_error(cat, c_id, "notes_read", topic="api", project="shop")
    assert "project nope is not registered" in call_error(ann, a_id, "notes_list", project="nope")
    admin_note = run(call(hub, ident("k:x", "ops"), "notes_read", topic="api", project="shop"))
    assert admin_note["text"] == "v1"


def test_team_notes_for_all(hub):
    _, ta = person(hub.store, "ann")
    _, tb = person(hub.store, "ben")
    ann, ben = as_person(hub, ta), as_person(hub, tb)
    a_id, b_id = ident("k:a", "shop"), ident("k:b", "lab")
    run(call(ann, a_id, "notes_write", topic="howto", text="one"))
    run(call(ben, b_id, "notes_write", topic="howto", text="two"))
    assert run(call(ann, a_id, "notes_read", topic="howto"))["text"] == "two"
    assert run(call(ann, a_id, "notes_read", topic="howto", project=""))["project"] is None
    assert [n["topic"] for n in run(call(ben, b_id, "notes_list"))] == ["howto"]
    hub.store.project_register("shop", by="root")
    hub.store.grant_project("shop", hub.store.person_by_name("ann")["id"], "write", by="root")
    run(call(ann, a_id, "notes_write", topic="secret", text="s", project="shop"))
    assert [n["topic"] for n in run(call(ben, b_id, "notes_list"))] == ["howto"]


def test_projects_for_member(hub):
    pa, ta = person(hub.store, "ann")
    ann = as_person(hub, ta)
    a_id = ident("k:a", "shop", "pa")
    run(call(ann, a_id, "who"))
    for name in ("shop", "lab"):
        assert http.request("POST", hub.url + "/api/projects", ADMIN,
                            {"name": name, "repo": f"https://git.example/{name}",
                             "description": f"the {name}"})[0] == 204
    assert http.request("POST", hub.url + "/api/projects/shop/grant", ADMIN,
                        {"person": "ann", "role": "write"})[0] == 204
    [shop] = run(call(ann, a_id, "projects"))
    assert shop == {"name": "shop", "description": "the shop", "repo": "https://git.example/shop",
                    "role": "write", "working_now": ["pa/codex/shop"]}
    status, rows = http.request("GET", hub.url + "/api/projects", ta)
    assert status == 200 and [r["name"] for r in rows] == ["shop"]
    _, rows = http.request("GET", hub.url + "/api/projects", ADMIN)
    assert [(r["name"], r["role"]) for r in rows] == [("lab", "manage"), ("shop", "manage")]
    # admin only, with clear errors
    assert http.request("POST", hub.url + "/api/projects", ta, {"name": "x"})[0] == 403
    assert http.request("POST", hub.url + "/api/projects/shop/grant", ta,
                        {"person": "ann", "role": "manage"})[0] == 403
    status, text = http.request("POST", hub.url + "/api/projects", ADMIN, {"name": "shop"})
    assert status == 400 and "already exists" in text
    status, text = http.request("POST", hub.url + "/api/projects", ADMIN, {"name": "a b"})
    assert status == 400
    status, text = http.request("POST", hub.url + "/api/projects/shop/grant", ADMIN,
                                {"person": "ann", "role": "boss"})
    assert status == 400 and "role" in text
    status, text = http.request("POST", hub.url + "/api/projects/none/grant", ADMIN,
                                {"person": "ann", "role": "read"})
    assert status == 400 and "no project named none" in text
    status, text = http.request("POST", hub.url + "/api/projects/shop/grant", ADMIN,
                                {"person": "zed", "role": "read"})
    assert status == 400 and "no such person" in text
    assert http.request("POST", hub.url + "/api/projects/shop/revoke", ADMIN,
                        {"person": "ann"})[0] == 204
    assert run(call(ann, a_id, "projects")) == []


def test_unregistered_folder_notes_not_merged(hub):
    pa, ta = person(hub.store, "ann")
    _, tb = person(hub.store, "ben")
    ann, ben = as_person(hub, ta), as_person(hub, tb)
    a_id, b_id = ident("k:a", "shop", "pa"), ident("k:b", "shop", "pb")
    refused = "project shop is not registered; ask the admin to register it"
    for client, i in ((ann, a_id), (ben, b_id), (hub, ident("k:x", "shop", "px"))):
        assert refused in call_error(client, i, "notes_write", topic="t", text="x", project="shop")
        assert refused in call_error(client, i, "notes_list", project="shop")
        assert refused in call_error(client, i, "notes_read", topic="t", project="shop")
    hub.store.project_register("shop", by="root")
    hub.store.grant_project("shop", pa, "write", by="root")
    run(call(ann, a_id, "notes_write", topic="t", text="mine", project="shop"))
    assert run(call(ann, a_id, "notes_read", topic="t", project="shop"))["text"] == "mine"
    assert "no access to project shop" in call_error(ben, b_id, "notes_read", topic="t",
                                                     project="shop")
    assert "no access to project shop" in call_error(ben, b_id, "notes_list", project="shop")


def test_who_masks_name_of_private_session(hub):
    _, ta = person(hub.store, "ann")
    _, tb = person(hub.store, "ben")
    ann, ben = as_person(hub, ta), as_person(hub, tb)
    a1, a2 = ident("k:a1", "shop", "pa"), ident("k:a2", "shop", "pa")
    run(call(ann, a1, "who"))
    run(call(ann, a2, "who"))  # second session, same folder: pa/codex/shop-2
    rows = run(call(ben, ident("k:b", "lab", "pb"), "who"))
    hidden = sorted(r["name"] for r in rows if r["person"] == "ann")
    assert hidden == ["pa/codex/(private)", "pa/codex/(private)-2"]
    assert all(r["project"] == "(private)" for r in rows if r["person"] == "ann")
    assert not any("shop" in str(v) for r in rows for v in r.values())
    admin = {r["name"] for r in run(call(hub, ident("k:x", "ops", "px"), "who"))}
    assert {"pa/codex/shop", "pa/codex/shop-2"} <= admin
    mine = {r["name"] for r in run(call(ann, a1, "who"))}
    assert {"pa/codex/shop", "pa/codex/shop-2"} <= mine


def setup_access(hub):
    """A project `shop` and a machine `h-b`, and a member ann with no access to either."""
    _, ta = person(hub.store, "ann")
    _, tb = person(hub.store, "ben")
    assert http.request("POST", hub.url + "/api/projects", ADMIN, {"name": "shop"})[0] == 204
    facts = {"host": "h-b", "os_user": "u", "version": "0.2.0"}
    assert http.request("POST", hub.url + "/bridge/heartbeat", tb,
                        {"machine": "h-b", "sessions": [], "facts": facts})[0] == 204
    return ta, as_person(hub, ta), ident("k:a", "x")


def test_request_then_approve_grants_read(hub):
    ta, ann, a_id = setup_access(hub)
    assert run(call(ann, a_id, "request_access", kind="project", target="shop",
                    why="to help")) == "requested #1; the admin sees it on the page"
    assert run(call(ann, a_id, "request_access", kind="machine", target="h-b")).startswith("requested #2")
    _, rows = http.request("GET", hub.url + "/api/requests", ADMIN)
    assert [(r["person"], r["kind"], r["target"], r["why"]) for r in rows] == [
        ("ann", "project", "shop", "to help"), ("ann", "machine", "h-b", "")]
    _, mine = http.request("GET", hub.url + "/api/requests", ta)
    assert [r["id"] for r in mine] == [1, 2]
    assert http.request("POST", hub.url + "/api/requests/1", ta, {"approve": True})[0] == 403
    assert http.request("POST", hub.url + "/api/requests/1", ADMIN, {"approve": True})[0] == 204
    assert http.request("POST", hub.url + "/api/requests/2", ADMIN, {"approve": True})[0] == 204
    pid = hub.store.person_by_name("ann")["id"]
    assert hub.store.project_role("shop", pid) == "read" and hub.store.may_use("h-b", pid)
    assert http.request("GET", hub.url + "/api/requests", ADMIN)[1] == []
    admin = hub.store.authenticate(ADMIN).person_name
    log = {r["kind"]: r for r in hub.store.db.execute("SELECT * FROM log WHERE kind LIKE 'request%'")}
    assert log["request"]["person"] == "ann"
    assert log["request_decided"]["person"] == admin
    assert "-> ann: approved" in log["request_decided"]["detail"]
    status, text = http.request("POST", hub.url + "/api/requests/1", ADMIN, {"approve": True})
    assert status == 400 and "already decided" in text
    assert "you already have access to shop" in call_error(ann, a_id, "request_access",
                                                           kind="project", target="shop")


def test_request_deny(hub):
    ta, ann, a_id = setup_access(hub)
    run(call(ann, a_id, "request_access", kind="project", target="shop"))
    assert http.request("POST", hub.url + "/api/requests/1", ADMIN, {"approve": False})[0] == 204
    pid = hub.store.person_by_name("ann")["id"]
    assert hub.store.project_role("shop", pid) is None
    assert http.request("GET", hub.url + "/api/requests", ta)[1] == []
    assert "decision" in hub.store.requests(pending_only=False)[0]
    assert hub.store.requests(pending_only=False)[0]["decision"] == "denied"
    assert "kind must be" in call_error(ann, a_id, "request_access", kind="x", target="shop")
    assert "no project named zz" in call_error(ann, a_id, "request_access", kind="project", target="zz")
    assert "no machine named zz" in call_error(ann, a_id, "request_access", kind="machine", target="zz")
    assert http.request("POST", hub.url + "/api/requests/99", ADMIN, {"approve": True})[0] == 400


def test_duplicate_request_same_id(hub):
    ta, ann, a_id = setup_access(hub)
    run(call(ann, a_id, "request_access", kind="project", target="shop", why="first"))
    assert run(call(ann, a_id, "request_access", kind="project", target="shop",
                    why="second")).startswith("requested #1;")
    assert run(call(ann, a_id, "request_access", kind="project", target="shop")).startswith("requested #1;")
    [r] = hub.store.requests()
    assert r["why"] == "second"
    assert "too long" in call_error(ann, a_id, "request_access", kind="project", target="shop",
                                    why="x" * 501)


def test_brief_mentions_pending(hub):
    ta, ann, a_id = setup_access(hub)
    h = {"X-Kaptyorka-Key": "k:a", "X-Kaptyorka-Machine": "pc", "X-Kaptyorka-Tool": "codex",
         "X-Kaptyorka-Project": "x"}

    def brief():
        return http.request("GET", hub.url + "/api/brief?event=start", ta, headers=h)[1]
    assert "Pending" not in brief() and "Denied" not in brief()
    run(call(ann, a_id, "request_access", kind="project", target="shop"))
    run(call(ann, a_id, "request_access", kind="machine", target="h-b"))
    assert " Pending: access to shop (project), use of h-b (machine)." in brief()
    http.request("POST", hub.url + "/api/requests/1", ADMIN, {"approve": False})
    b = brief()
    assert " Pending: use of h-b (machine)." in b and " Denied: access to shop (project)." in b
    hub.store.clock = lambda t=hub.store.clock(): t + 8 * 86400
    assert "Denied" not in brief()


def test_deny_then_approve_grants_nothing(hub):
    ta, ann, a_id = setup_access(hub)
    run(call(ann, a_id, "request_access", kind="project", target="shop"))
    admin = hub.store.authenticate(ADMIN).person_id
    pid = hub.store.person_by_name("ann")["id"]
    hub.store.decide_request(1, admin, False)
    with pytest.raises(Exception, match="already decided"):
        hub.store.decide_request(1, admin, True)
    assert hub.store.project_role("shop", pid) is None
    assert hub.store.requests(pending_only=False)[0]["decision"] == "denied"


def test_failed_grant_reverts_claim(hub):
    ta, ann, a_id = setup_access(hub)
    run(call(ann, a_id, "request_access", kind="project", target="shop"))
    with hub.store.lock:
        hub.store.db.execute("DELETE FROM projects WHERE name='shop'")
    admin = hub.store.authenticate(ADMIN).person_id
    with pytest.raises(Exception, match="no project named shop"):
        hub.store.decide_request(1, admin, True)
    assert [r["id"] for r in hub.store.requests()] == [1]


def test_approve_never_lowers_role(hub):
    ta, ann, a_id = setup_access(hub)
    run(call(ann, a_id, "request_access", kind="project", target="shop"))
    pid = hub.store.person_by_name("ann")["id"]
    hub.store.grant_project("shop", pid, "write", "t")  # given after the request was made
    assert http.request("POST", hub.url + "/api/requests/1", ADMIN, {"approve": True})[0] == 204
    assert hub.store.project_role("shop", pid) == "write"
    detail = hub.store.db.execute(
        "SELECT detail FROM log WHERE kind='request_decided'").fetchone()["detail"]
    assert detail.endswith("approved (already write)")


def test_retention_setting(hub):
    status, body = http.request("GET", hub.url + "/api/settings", ADMIN)
    assert status == 200 and body == {"retention_days": 180}
    status, _ = http.request("POST", hub.url + "/api/settings", ADMIN, {"retention_days": 30})
    assert status == 200 and hub.store.retention_days() == 30
    for bad in (0, 3651, "x"):
        status, _ = http.request("POST", hub.url + "/api/settings", ADMIN, {"retention_days": bad})
        assert status == 400
    _, tok = person(hub.store, "ann")
    for method, path in (("GET", "/api/settings"), ("GET", "/api/log")):
        status, body = http.request(method, hub.url + path, tok)
        assert status == 403
    status, _ = http.request("POST", hub.url + "/api/settings", tok, {"retention_days": 5})
    assert status == 403
    status, rows = http.request("GET", hub.url + "/api/log?limit=5", ADMIN)
    assert status == 200 and rows[0]["kind"] == "settings"
    status, rows = http.request("GET", hub.url + "/api/log?person=nobody", ADMIN)
    assert rows == []
