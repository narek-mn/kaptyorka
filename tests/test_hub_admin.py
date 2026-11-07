"""The admin routes for people, roles, invites and projects; a member asking for access and
seeing their own; request decisions with a role or a reason; log sentences."""
from types import SimpleNamespace

import pytest

from conftest import start_hub
from helpers import person
from kaptyorka import http
from kaptyorka.manager import ManagerStore
from kaptyorka.store import Store


@pytest.fixture
def app(tmp_path):
    store = Store(tmp_path / "hub.db")
    ManagerStore(store).set_enabled(False, "test")
    stop = start_hub(store)
    ids, tokens = {}, {}
    for name, role in (("alice", "admin"), ("bob", "member"), ("carol", "member")):
        ids[name], tokens[name] = person(store, name, role)
    yield SimpleNamespace(url=f"http://127.0.0.1:{stop.port}", store=store, ids=ids,
                          tokens=tokens)
    stop()
    store.close()


def call(app, who, method, path, body=None):
    return http.request(method, app.url + path, app.tokens[who], body)


ADMIN_ROUTES = [("DELETE", "/api/people/carol", None),
                ("POST", "/api/people/carol/role", {"role": "manager"}),
                ("GET", "/api/invites", None),
                ("DELETE", "/api/invites/abc", None),
                ("POST", "/api/projects/proj-a", {"description": "x"}),
                ("DELETE", "/api/projects/proj-a", None),
                ("POST", "/api/requests/1", {"approve": True})]


def test_member_routes_forbidden(app):
    app.store.project_register("proj-a", by="alice")
    for method, path, body in ADMIN_ROUTES:
        status, text = call(app, "bob", method, path, body)
        assert (status, text) == (403, "admin only"), (method, path)
    assert app.store.person_by_name("carol") is not None


def test_remove_person_route(app):
    status, body = call(app, "alice", "DELETE", "/api/people/bob")
    assert status == 200 and body == {"devices": 1, "workers": 0, "doors": 0, "grants": 0,
                                      "requests": 0, "work_requests": 0,
                                      "machines_unowned": [], "still_running": []}
    assert http.request("GET", app.url + "/api/me", app.tokens["bob"])[0] == 401
    status, text = call(app, "alice", "DELETE", "/api/people/bob")
    assert (status, text) == (404, "no person named bob")
    status, text = call(app, "alice", "DELETE", "/api/people/alice")
    assert (status, text) == (400, "you cannot remove yourself")
    names = [p["name"] for p in call(app, "alice", "GET", "/api/people")[1]]
    assert names == ["alice", "carol"]


def test_role_route(app):
    assert call(app, "alice", "POST", "/api/people/bob/role", {"role": "manager"})[0] == 204
    assert app.store.person_by_name("bob")["role"] == "manager"
    status, text = call(app, "alice", "POST", "/api/people/bob/role", {"role": "admin"})
    assert (status, text) == (400, "the admin role cannot be given or taken here")


def test_invite_routes(app):
    status, inv = call(app, "alice", "POST", "/api/invites", {"person": "bob"})
    assert status == 200 and inv["expires"] > app.store.clock() + 6 * 86400
    page = http.request("GET", inv["url"], "")[1]
    assert "This link works for 7 days" in page
    rows = call(app, "alice", "GET", "/api/invites")[1]
    assert [(r["code"], r["person"]) for r in rows] == [(inv["code"], "bob")]
    assert call(app, "alice", "DELETE", f"/api/invites/{inv['code']}")[0] == 204
    assert call(app, "alice", "GET", "/api/invites")[1] == []
    assert call(app, "alice", "DELETE", f"/api/invites/{inv['code']}")[0] == 404


def test_manager_account_cannot_be_invited(app):
    """Whoever redeemed such an invite would act as the manager."""
    person(app.store, "manager", "manager")
    status, text = call(app, "alice", "POST", "/api/invites", {"person": "manager"})
    assert (status, text) == (400, "the manager account cannot be invited; it runs on the hub machine")
    assert call(app, "alice", "GET", "/api/invites")[1] == []


def test_project_routes(app):
    app.store.project_register("proj-a", by="alice")
    assert call(app, "alice", "POST", "/api/projects/proj-a", {"description": "the app"})[0] == 204
    assert call(app, "alice", "GET", "/api/projects")[1][0]["description"] == "the app"
    status, body = call(app, "alice", "DELETE", "/api/projects/proj-a")
    assert status == 200 and body["grants"] == 0
    rows = call(app, "alice", "GET", "/api/projects")[1]
    assert [(p["name"], p.get("archived")) for p in rows] == [("proj-a", True)]
    assert call(app, "bob", "GET", "/api/projects")[1] == []
    assert call(app, "bob", "GET", "/api/projects/proj-a/activity")[0] == 403
    feed = call(app, "alice", "GET", "/api/feed")[1]
    assert feed["projects"] == [] and [p["name"] for p in feed["archived_projects"]] == ["proj-a"]
    assert call(app, "alice", "DELETE", "/api/projects/proj-a") == (404, "no project named proj-a")
    assert call(app, "alice", "POST", "/api/projects", {"name": "proj-a"})[0] == 204
    assert "archived" not in call(app, "alice", "GET", "/api/projects")[1][0]


def test_request_kind_validated(app):
    app.store.project_register("proj-a", by="alice")
    status, text = call(app, "bob", "POST", "/api/requests",
                        {"kind": "folder", "target": "proj-a", "why": ""})
    assert (status, text) == (400, "kind must be project or machine")
    status, text = call(app, "bob", "POST", "/api/requests", {"kind": "project", "target": "nope"})
    assert (status, text) == (400, "no project named nope")
    status, body = call(app, "bob", "POST", "/api/requests",
                        {"kind": "project", "target": "proj-a", "why": "to help"})
    assert status == 200 and isinstance(body["id"], int)
    mine = call(app, "bob", "GET", "/api/me/access")[1]
    assert [(r["target"], r["status"], r["why"]) for r in mine["requests"]] == \
        [("proj-a", "pending", "to help")]


def test_my_access_route(app):
    s = app.store
    s.project_register("proj-a", "the app", by="alice")
    s.grant_project("proj-a", app.ids["bob"], "read", "alice")
    got = call(app, "bob", "GET", "/api/me/access")[1]
    assert got == {"projects": [{"name": "proj-a", "role": "read", "description": "the app"}],
                   "machines": [], "requests": []}
    assert call(app, "carol", "GET", "/api/me/access")[1]["projects"] == []


def test_approve_with_role_and_deny_with_reason(app):
    s = app.store
    s.project_register("proj-a", by="alice")
    s.project_register("proj-b", by="alice")
    s.project_register("proj-c", by="alice")
    ra = s.request_access(app.ids["bob"], "project", "proj-a")
    rb = s.request_access(app.ids["bob"], "project", "proj-b")
    rc = s.request_access(app.ids["bob"], "project", "proj-c")
    status, text = call(app, "alice", "POST", f"/api/requests/{ra}",
                        {"approve": True, "role": "manage"})
    assert (status, text) == (400, "role must be read or write")
    assert call(app, "alice", "POST", f"/api/requests/{ra}", {"approve": True, "role": "write"})[0] == 204
    assert s.project_role("proj-a", app.ids["bob"]) == "write"
    assert call(app, "alice", "POST", f"/api/requests/{rc}", {"approve": True})[0] == 204
    assert s.project_role("proj-c", app.ids["bob"]) == "read"
    s.seen("claude:b1", "pc", "claude-code", "x", principal=s.authenticate(app.tokens["bob"]))
    assert call(app, "alice", "POST", f"/api/requests/{rb}",
                {"approve": False, "reason": "not this month"})[0] == 204
    texts = [(m["to_key"], m["text"]) for m in s.db.execute(
        "SELECT to_key, text FROM messages WHERE sender_key='hub:workers'")]
    want = "Your request for the project proj-b was denied: not this month"
    assert texts == [("web:bob", want), ("claude:b1", want)]
    assert s.project_role("proj-b", app.ids["bob"]) is None


def test_log_rows_have_sentences(app):
    app.store.project_register("proj-a", by="alice")
    rows = call(app, "alice", "GET", "/api/log")[1]
    assert rows[0]["sentence"] == "alice registered the project proj-a"
    assert all("sentence" in r for r in rows)


def test_request_carries_the_wished_role(app):
    s = app.store
    s.project_register("proj-a", by="alice")
    status, body = call(app, "bob", "POST", "/api/requests",
                        {"kind": "project", "target": "proj-a", "role": "write"})
    assert status == 200
    assert call(app, "bob", "POST", "/api/requests",
                {"kind": "project", "target": "proj-a", "role": "manage"}) == (400, "role must be read or write")
    assert [r["role"] for r in call(app, "bob", "GET", "/api/me/access")[1]["requests"]] == ["write"]
    assert [(r["person"], r["role"]) for r in call(app, "alice", "GET", "/api/feed")[1]["requests"]] == \
        [("bob", "write")]
    # asking again with read changes the wish; the same request stays
    assert call(app, "bob", "POST", "/api/requests", {"kind": "project", "target": "proj-a"})[1] == body
    assert [r["role"] for r in s.requests()] == ["read"]


def test_feed_log_rows_have_sentences(app):
    app.store.project_register("proj-a", by="alice")
    rows = call(app, "alice", "GET", "/api/feed")[1]["log"]
    assert "alice registered the project proj-a" in [r["sentence"] for r in rows]


def test_note_delete_and_undo_routes(app):
    app.store.project_register("proj-a", by="alice")
    app.store.grant_project("proj-a", app.ids["bob"], "write", "alice")
    app.store.grant_project("proj-a", app.ids["carol"], "read", "alice")
    note = {"topic": "plan", "text": "v1", "project": "proj-a"}
    assert call(app, "bob", "POST", "/api/notes", note)[0] == 204

    def topics(who):
        return [n["topic"] for n in call(app, who, "GET", "/api/feed")[1]["notes"]]

    assert topics("bob") == ["plan"]
    assert call(app, "bob", "DELETE", "/api/notes?topic=plan&project=proj-a")[0] == 204
    assert topics("bob") == []
    assert call(app, "bob", "POST", "/api/notes/undo", {"topic": "plan", "project": "proj-a"})[0] == 204
    assert topics("bob") == ["plan"]


def test_member_cannot_delete_read_only_note(app):
    app.store.project_register("proj-a", by="alice")
    app.store.grant_project("proj-a", app.ids["carol"], "read", "alice")
    call(app, "alice", "POST", "/api/notes", {"topic": "plan", "text": "v1", "project": "proj-a"})
    status, text = call(app, "carol", "DELETE", "/api/notes?topic=plan&project=proj-a")
    assert status == 400 and text.startswith("no access to project proj-a")
    status, text = call(app, "alice", "DELETE", "/api/notes?topic=nope&project=proj-a")
    assert (status, text) == (400, "no note named nope")


def test_log_kinds_and_paging_routes(app):
    assert call(app, "bob", "GET", "/api/log/kinds") == (403, "admin only")
    assert call(app, "bob", "GET", "/api/log?kind=people")[0] == 403
    status, kinds = call(app, "alice", "GET", "/api/log/kinds")
    assert status == 200 and kinds[0] == {"kind": "people", "label": "People"}
    for who, kind in (("alice", "person"), ("alice", "grant"), ("bob", "role"), ("carol", "grant")):
        app.store.log_add(who, kind, "x")
    all_rows = call(app, "alice", "GET", "/api/log")[1]
    status, page = call(app, "alice", "GET", f"/api/log?kind=people&before={all_rows[0]['id']}")
    assert status == 200 and page and all(r["kind"] in ("person", "role") for r in page)
    assert call(app, "alice", "GET", "/api/log?kind=nope")[0] == 400
    assert kinds[-1] == {"kind": "other", "label": "Other"}
    groups = [k["kind"] for k in kinds]
    assert groups[groups.index("projects") + 1] == "notes"
    assert {"kind": "notes", "label": "Notes"} in kinds
    app.store.log_add("alice", "note_delete", "x")
    rows = call(app, "alice", "GET", "/api/log?kind=notes")[1]
    assert rows and all(r["kind"] in ("note_delete", "note_undo") for r in rows)
    assert call(app, "alice", "GET", "/api/log?kind=other")[0] == 200


def test_settings_show_the_manager_switch(app):
    status, body = call(app, "alice", "GET", "/api/settings")
    assert status == 200 and body == {"retention_days": 180, "manager_enabled": False,
                                      "manager_state": "off",
                                      "manager_reason": "the manager is switched off",
                                      "manager_error": ""}
    ManagerStore(app.store).set_enabled(True, "test")
    assert call(app, "alice", "GET", "/api/settings")[1]["manager_enabled"] is True
    assert call(app, "bob", "GET", "/api/settings")[0] == 403


def test_note_delete_without_topic(app):
    assert call(app, "alice", "DELETE", "/api/notes") == (400, "topic is empty")
    assert call(app, "alice", "DELETE", "/api/notes?topic=%20") == (400, "topic is empty")


def test_attach_refuses_another_persons_file(app):
    fid = app.store.file_add("a.txt", b"hello", app.ids["alice"])
    status, text = call(app, "bob", "POST", "/api/send", {"to": "alice", "text": "x", "file_id": fid})
    assert (status, text) == (400, "that file is not yours to send")
    status, text = call(app, "bob", "POST", "/api/send", {"to": "alice", "text": "x", "file_id": 999})
    assert (status, text) == (400, "no such file")
    assert not [m for m in app.store.messages() if m.file]


def test_attach_sends_own_file_once(app):
    fid = app.store.file_add("a.txt", b"hello", app.ids["alice"])
    app.store.seen("b1", "box-1", "codex", "proj-a", principal=app.store.authenticate(app.tokens["bob"]))
    to = "box-1/codex/proj-a"
    status, body = call(app, "alice", "POST", "/api/send", {"to": to, "text": "see", "file_id": fid})
    assert (status, body) == (200, {"sent": 1})
    assert [(m.text, m.file["id"]) for m in app.store.messages() if m.file] == [("see", fid)]
    status, text = call(app, "alice", "POST", "/api/send", {"to": to, "text": "again", "file_id": fid})
    assert (status, text) == (400, "that file is not yours to send")


def test_rules_history_lists_more_than_twenty(app):
    from kaptyorka.rules import RulesStore
    rs = RulesStore(app.store)
    for i in range(25):
        rs.set_rules(f"rule {i}", by="alice")
    status, rows = call(app, "alice", "GET", "/api/rules/history")
    assert status == 200 and len(rows) == 25
