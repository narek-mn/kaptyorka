"""The page per person: /api/me, the filtered feed, web sends and notes as the viewer,
knock answers, image tokens and the invite's web login."""
from types import SimpleNamespace

import pytest

from conftest import start_hub
from helpers import person
from kaptyorka import http
from kaptyorka.store import KaptyorkaError, Store


@pytest.fixture
def app(tmp_path, home):
    store = Store(tmp_path / "hub.db")
    stop = start_hub(store)
    url = f"http://127.0.0.1:{stop.port}"
    ids, tokens = {}, {}
    for name, role in (("ann", "member"), ("ben", "member"), ("boss", "admin")):
        ids[name], tokens[name] = person(store, name, role)
    yield SimpleNamespace(url=url, store=store, ids=ids, tokens=tokens)
    stop()
    store.close()


def online(app, who, key, machine, project="p"):
    """A session of `who`, seen through their device token."""
    return app.store.seen(key, machine, "codex", project,
                          principal=app.store.authenticate(app.tokens[who]))


def get(app, who, path):
    status, body = http.request("GET", app.url + path, app.tokens[who])
    assert status == 200, body
    return body


def post(app, who, path, body):
    return http.request("POST", app.url + path, app.tokens[who], body)


def test_me(app):
    assert get(app, "ann", "/api/me") == {"person": "ann", "role": "member",
                                          "device": "ann-laptop", "person_id": app.ids["ann"]}
    assert get(app, "boss", "/api/me")["role"] == "admin"


def test_feed_filtered_for_member(app):
    s = app.store
    a1, b1, x1 = online(app, "ann", "a1", "pc"), online(app, "ben", "b1", "box-2"), \
        online(app, "boss", "x1", "box-1")
    s.send(x1.key, a1.name, "for ann")
    s.send(x1.key, b1.name, "for ben")
    s.notes_write(x1.key, "team-topic", "everyone")
    s.project_register("proj")
    s.notes_write(x1.key, "plan", "secret plan", "proj")
    kid = s.knock(b1.key, "ann", "can I ask about the api")
    s.knock(a1.key, "ben", "the other way")

    f = get(app, "ann", "/api/feed")
    texts = [m["text"] for m in f["messages"]]
    assert "for ann" in texts and "for ben" not in texts
    assert all(m["kind"] in ("message", "knock", "door") and "file" in m for m in f["messages"])
    assert [(n["project"], n["topic"]) for n in f["notes"]] == [(None, "team-topic")]
    assert [k["id"] for k in f["knocks"]] == [kid]
    assert f["doors"] == []
    for extra in ("people", "requests", "legacy", "machines", "projects", "log", "settings"):
        assert extra not in f

    s.grant_project("proj", app.ids["ann"], "read", "boss")
    f = get(app, "ann", "/api/feed")
    assert {(n["project"], n["topic"]) for n in f["notes"]} == {(None, "team-topic"), ("proj", "plan")}
    assert next(n for n in f["notes"] if n["project"] == "proj")["history"][0]["text"] == "secret plan"


def test_feed_masks_private_sessions(app):
    s = app.store
    online(app, "ann", "a1", "pc", "mine")
    b1 = online(app, "ben", "b1", "box-2", "backend")
    x1 = online(app, "boss", "x1", "box-1", "ops")
    s.send(x1.key, "pc/codex/mine", "hello ann")
    rows = {a["person"]: a for a in get(app, "ann", "/api/feed")["agents"]}
    assert (rows["ann"]["name"], rows["ann"]["project"], rows["ann"]["mine"]) == \
        ("pc/codex/mine", "mine", True)
    assert (rows["ben"]["name"], rows["ben"]["project"], rows["ben"]["mine"]) == \
        ("box-2/codex/(private)", "(private)", False)
    assert rows["ann"]["doors"] == [] and "doors" not in rows["ben"]
    msg = get(app, "ann", "/api/feed")["messages"][-1]
    assert (msg["sender"], msg["to"]) == ("box-1/codex/(private)", "pc/codex/mine")
    assert not any(k.startswith("_") for k in msg)

    s.project_register("backend")
    s.grant_project("backend", app.ids["ann"], "read", "boss")
    rows = {a["person"]: a for a in get(app, "ann", "/api/feed")["agents"]}
    assert (rows["ben"]["name"], rows["ben"]["project"]) == (b1.name, "backend")
    admin_rows = {a["person"]: a for a in get(app, "boss", "/api/feed")["agents"]}
    assert admin_rows["ann"]["project"] == "mine"


def test_feed_admin_extras(app):
    s = app.store
    online(app, "ben", "b1", "box-2")
    s.project_register("proj")
    s.grant_project("proj", app.ids["ann"], "write", "boss")
    s.request_access(app.ids["ben"], "project", "proj", "to help")
    s.open_door_group(app.ids["ann"], app.ids["ben"], "boss")
    f = get(app, "boss", "/api/feed")
    assert [p["name"] for p in f["people"]] == ["ann", "ben", "boss"]
    assert f["people"][0]["devices"][0]["name"] == "ann-laptop"
    assert [r["person"] for r in f["requests"]] == ["ben"]
    assert set(f["legacy"]) == {"unassigned", "enabled", "ready_to_disable"}
    assert isinstance(f["machines"], list)
    proj = next(p for p in f["projects"] if p["name"] == "proj")
    assert proj["members"] == [{"person": "ann", "role": "write"}]
    assert f["log"][0]["kind"] == "door_open" and len(f["log"]) <= 200
    assert f["settings"] == {"retention_days": 180}
    assert len(f["doors"]) == 1
    # a member sees the door too: it is theirs
    assert len(get(app, "ann", "/api/feed")["doors"]) == 1


def test_page_contains_admin_tabs_markup(app):
    status, html = http.request("GET", app.url + "/", "")
    assert status == 200
    for view in ("people", "access", "audit"):
        assert f'data-view="{view}"' in html and f'id="{view}"' in html
    assert "Your name" not in html
    assert "/api/me" in html and "/api/knocks/" in html
    assert "opens a door for all your sessions" in html


def test_web_send_uses_principal(app):
    s = app.store
    online(app, "ann", "a1", "pc")
    s.seen("a2", "pc2", "codex", "p", principal=s.authenticate(app.tokens["ann"]))
    status, body = post(app, "ann", "/api/send", {"to": "pc2/codex/p", "text": "hi", "from": "boss"})
    assert (status, body) == (200, {"sent": 1})
    m = s.inbox("a2")[0]
    assert (m.sender, m.from_web) == ("ann", True)
    with s.lock:
        key = s.db.execute("SELECT sender_key FROM messages").fetchone()[0]
    assert key == "web:ann"


def test_web_send_follows_group_rule(app):
    s = app.store
    online(app, "ann", "a1", "pc")
    b1 = online(app, "ben", "b1", "box-2")
    status, body = post(app, "ann", "/api/send", {"to": b1.name, "text": "hi"})
    assert status == 400 and f"no door to {b1.name}" in body
    assert post(app, "ann", "/api/send", {"to": "all", "text": "hi"}) == (200, {"sent": 1})
    assert post(app, "boss", "/api/send", {"to": b1.name, "text": "hi"})[0] == 200
    s.open_door_group(app.ids["ann"], app.ids["ben"], "boss")
    assert post(app, "ann", "/api/send", {"to": b1.name, "text": "now ok"}) == (200, {"sent": 1})
    status, body = post(app, "ben", "/api/send", {"to": "ann", "text": "to the page"})
    assert status == 200
    status, body = post(app, "ben", "/api/send", {"to": "boss", "text": "to the admin page"})
    assert status == 400 and "no door to boss" in body


def test_web_notes_respects_project_access(app):
    s = app.store
    def note(project=None):
        body = {"topic": "plan", "text": "v", "from": "boss"}
        if project:
            body["project"] = project
        return post(app, "ann", "/api/notes", body)
    assert note()[0] == 204
    assert s.notes_read("plan").author == "ann"
    status, body = note("nowhere")
    assert status == 400 and "not registered" in body
    s.project_register("proj")
    status, body = note("proj")
    assert status == 400 and "no access" in body
    s.grant_project("proj", app.ids["ann"], "read", "boss")
    assert note("proj")[0] == 400
    s.grant_project("proj", app.ids["ann"], "write", "boss")
    assert note("proj")[0] == 204
    assert s.notes_read("plan", project="proj").author == "ann"


def test_files_query_token_only_on_files_route(app):
    s = app.store
    a1, x1 = online(app, "ann", "a1", "pc"), online(app, "boss", "x1", "box-1")
    fid = s.file_add("pic.png", b"\x89PNG", app.ids["boss"])
    s.send(x1.key, a1.name, "look", fid)
    ann = app.tokens["ann"]
    status, data, _ = http.request_raw("GET", f"{app.url}/api/files/{fid}?t={ann}", "")
    assert (status, data) == (200, b"\x89PNG")
    assert get(app, "ann", "/api/feed")["messages"][-1]["file"] == {"id": fid, "name": "pic.png",
                                                                    "size": 4}
    assert http.request_raw("GET", f"{app.url}/api/files/{fid}?t=bad", "")[0] == 401
    for path in ("/api/feed", "/api/me"):
        assert http.request("GET", f"{app.url}{path}?t={ann}", "")[0] == 401
    assert http.request("POST", f"{app.url}/api/send?t={ann}", "", {"to": "all", "text": "x"})[0] == 401
    # ben is in no message carrying the file
    status, _, _ = http.request_raw("GET", f"{app.url}/api/files/{fid}?t={app.tokens['ben']}", "")
    assert status == 404


def test_knock_answer_route(app):
    s = app.store
    a1, b1 = online(app, "ann", "a1", "pc"), online(app, "ben", "b1", "box-2")
    kid = s.knock(b1.key, "ann", "about the api")
    status, body = post(app, "ben", f"/api/knocks/{kid}/answer", {"answer": "yes"})
    assert status == 400 and "only ann or the admin" in body
    assert post(app, "ann", f"/api/knocks/{kid}/answer", {"answer": "maybe"})[0] == 400
    assert post(app, "ann", f"/api/knocks/{kid}/answer", {"answer": "yes"}) == \
        (200, {"door": "session"})
    assert s.may_message(b1.key, a1.key)
    assert get(app, "ann", "/api/feed")["knocks"] == []
    status, body = post(app, "ann", f"/api/knocks/{kid}/answer", {"answer": "yes"})
    assert status == 400 and "already answered" in body

    # carl has no session online: yes opens a door for all his sessions
    carl, app.tokens["carl"] = person(s, "carl")
    kid = s.knock(b1.key, "carl", "and you")
    assert post(app, "carl", f"/api/knocks/{kid}/answer", {"answer": "yes"}) == \
        (200, {"door": "group"})
    c1 = online(app, "carl", "c1", "lab")
    assert s.may_message(b1.key, c1.key)
    kid = s.knock(b1.key, "carl", "once more")
    assert post(app, "carl", f"/api/knocks/{kid}/answer", {"answer": "no"}) == (200, {"door": None})
    log = [(r["person"], r["kind"]) for r in s.log(5)]
    assert ("carl", "knock_answer") in log
    assert post(app, "carl", "/api/knocks/9999/answer", {"answer": "no"})[0] == 400


def test_invite_web_login(app):
    code = app.store.invite_create(app.ids["ann"])
    status, page = http.request("GET", f"{app.url}/i/{code}", "")
    assert status == 200 and "kaptyorka join" in page and "Open the page as this person" in page
    status, body = http.request("POST", f"{app.url}/i/{code}/web", "")
    assert status == 200 and set(body) == {"token"}
    me = http.request("GET", app.url + "/api/me", body["token"])[1]
    assert (me["person"], me["device"]) == ("ann", "web")
    status, body = http.request("POST", f"{app.url}/i/{code}/web", "")
    assert status == 400 and "already used" in body
    assert http.request("POST", f"{app.url}/i/{'x' * 22}/web", "")[0] == 400


def test_invite_web_form_redirects_to_page(app):
    import urllib.request

    code = app.store.invite_create(app.ids["ben"])

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **kw):
            return None

    req = urllib.request.Request(f"{app.url}/i/{code}/web", data=b"", method="POST",
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        urllib.request.build_opener(NoRedirect).open(req)
        raise AssertionError("expected a redirect")
    except urllib.error.HTTPError as e:
        assert e.code == 303
        token = e.headers["Location"].split("#t=", 1)[1]
    assert http.request("GET", app.url + "/api/me", token)[1]["person"] == "ben"


def test_agent_reply_to_page_needs_door(app):
    s = app.store
    a1, b1 = online(app, "ann", "a1", "pc"), online(app, "ben", "b1", "box-2")
    assert post(app, "boss", "/api/send", {"to": a1.name, "text": "please check"})[0] == 200
    assert s.send(a1.key, "boss", "done") == 1  # the admin's page wrote first: a door is open
    assert get(app, "boss", "/api/feed")["messages"][-1]["text"] == "done"
    with pytest.raises(KaptyorkaError, match=r"no door to boss: knock\(to, about\) first"):
        s.send(b1.key, "boss", "cold message")
    # the page's door is a session door: it closes when the session goes offline
    with s.lock:
        s.db.execute("UPDATE agents SET last_seen=last_seen-100 WHERE key='a1'")
    s.expire_doors(s.clock())
    online(app, "ann", "a1", "pc")
    with pytest.raises(KaptyorkaError, match="no door to boss"):
        s.send(a1.key, "boss", "later")


def test_note_author_is_the_person(app):
    s = app.store
    a1 = online(app, "ann", "a1", "pc", "webshop")
    s.notes_write(a1.key, "plan", "v1")
    assert s.notes_read("plan").author == "ann"
    loner = s.seen("x9", "box-1", "codex", "p")  # a session of no person
    s.notes_write(loner.key, "plan", "v2")
    assert s.notes_read("plan").author == "box-1/codex/p"


def test_admin_actions_log_the_admin(app):
    s = app.store
    assert post(app, "boss", "/api/people", {"name": "zed"})[0] == 200
    assert post(app, "boss", "/api/invites", {"person": "zed"})[0] == 200
    dev = s.devices(app.ids["ann"])[0]["id"]
    assert post(app, "boss", f"/api/devices/{dev}/shared", {"shared": True})[0] == 204
    assert post(app, "boss", f"/api/devices/{dev}/revoke", {})[0] == 204
    log = [(r["person"], r["kind"], r["detail"]) for r in s.log(4)]
    assert log == [("boss", "revoke", "device ann-laptop of ann"),
                   ("boss", "device_shared", "device ann-laptop of ann on"),
                   ("boss", "invite", "invite for zed"),
                   ("boss", "person", "zed created as member")]


def test_knock_and_door_sessions_masked(app):
    s = app.store
    a1 = online(app, "ann", "a1", "pc", "mine")
    b1 = online(app, "ben", "b1", "box-2", "secret")
    kid = s.knock(b1.key, "ann", "a question")
    assert get(app, "ann", "/api/feed")["knocks"][0]["from_session"] == "box-2/codex/(private)"
    assert get(app, "ann", "/api/knocks")[0]["from_session"] == "box-2/codex/(private)"
    assert get(app, "boss", "/api/feed")["knocks"][0]["from_session"] == b1.name
    s.answer(kid, a1.key, True)
    door = get(app, "ann", "/api/feed")["doors"][0]
    assert {door["a_session"], door["b_session"]} == {"box-2/codex/(private)", a1.name}
    assert not any(k.startswith("_") for k in door)
    door = get(app, "ben", "/api/feed")["doors"][0]
    assert {door["a_session"], door["b_session"]} == {b1.name, "pc/codex/(private)"}


def test_page_broadcast_opens_no_doors(app):
    s = app.store
    online(app, "ann", "a1", "pc")
    b1 = online(app, "ben", "b1", "box-2")
    assert post(app, "boss", "/api/send", {"to": "all", "text": "news"}) == (200, {"sent": 2})
    assert s.doors() == []  # a broadcast is one-way
    with pytest.raises(KaptyorkaError, match="no door to boss"):
        s.send(b1.key, "boss", "reply to the news")


def test_page_downloads_files_without_token_links(app):
    status, html = http.request("GET", app.url + "/", "")
    assert "URL.createObjectURL" in html and "revokeObjectURL" in html
    assert html.count('?t=" + encodeURIComponent(token)') == 1  # images only
