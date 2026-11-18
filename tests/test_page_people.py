"""The page per person: /api/me, the filtered feed, web sends and notes as the viewer,
knock answers, image tokens and the invite's web login."""
import re
from types import SimpleNamespace

import pytest

from conftest import start_hub
from helpers import old_knock, person
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
                                          "device": "ann-laptop", "person_id": app.ids["ann"],
                                          "gitlab_connect": False, "page": True}
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
    old_knock(s, b1.key, app.ids["ann"], "can I ask about the api")
    old_knock(s, a1.key, app.ids["ben"], "the other way")

    f = get(app, "ann", "/api/feed")
    texts = [m["text"] for m in f["messages"]]
    assert "for ann" in texts and "for ben" not in texts
    assert all(m["kind"] in ("message", "knock", "door") and "file" in m for m in f["messages"])
    assert [(n["project"], n["topic"]) for n in f["notes"]] == [(None, "team-topic")]
    assert f["knocks"] == []  # only the admin answers knocks
    assert len(get(app, "boss", "/api/feed")["knocks"]) == 2
    assert [(d["scope"], d["a_person"], d["b_person"]) for d in f["doors"]] == \
        [("session", "boss", "ann")]  # the admin wrote: ann may reply
    for extra in ("people", "requests", "decisions", "legacy", "machines", "projects", "archived_projects",
                  "log", "settings"):
        assert extra not in f

    s.grant_project("proj", app.ids["ann"], "read", "boss")
    f = get(app, "ann", "/api/feed")
    assert {(n["project"], n["topic"]) for n in f["notes"]} == {(None, "team-topic"), ("proj", "plan")}
    assert next(n for n in f["notes"] if n["project"] == "proj")["history"][0]["text"] == "secret plan"


def test_feed_masks_private_sessions(app):
    s = app.store
    online(app, "ann", "a1", "pc", "mine")
    b1 = online(app, "ben", "b1", "box-2", "proj-b")
    x1 = online(app, "boss", "x1", "box-1", "ops")
    s.send(x1.key, "pc/codex/mine", "hello ann")
    rows = {a["person"]: a for a in get(app, "ann", "/api/feed")["agents"]}
    assert (rows["ann"]["name"], rows["ann"]["project"], rows["ann"]["mine"]) == \
        ("pc/codex/mine", "mine", True)
    assert set(rows) == {"ann", "boss"}  # no door to ben; the admin's message opened one
    s.open_door_group(app.ids["ann"], app.ids["ben"], "boss")
    rows = {a["person"]: a for a in get(app, "ann", "/api/feed")["agents"]}
    assert set(rows) == {"ann", "ben", "boss"}
    assert (rows["ben"]["name"], rows["ben"]["project"], rows["ben"]["mine"]) == \
        ("box-2/codex/(private)", "(private)", False)
    assert rows["ann"]["doors"] == ["box-1/codex/(private)", "ben"] and "doors" not in rows["ben"]
    msg = get(app, "ann", "/api/feed")["messages"][-1]
    assert (msg["sender"], msg["to"]) == ("box-1/codex/(private)", "pc/codex/mine")
    assert not any(k.startswith("_") for k in msg)

    s.project_register("proj-b")
    s.grant_project("proj-b", app.ids["ann"], "read", "boss")
    rows = {a["person"]: a for a in get(app, "ann", "/api/feed")["agents"]}
    assert (rows["ben"]["name"], rows["ben"]["project"]) == (b1.name, "proj-b")
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
    # the hub's first tick creates the manager's account
    assert [p["name"] for p in f["people"] if p["role"] == "manager"] == ["manager"]
    people = [p for p in f["people"] if p["role"] != "manager"]
    assert [p["name"] for p in people] == ["ann", "ben", "boss"]
    assert people[0]["devices"][0]["name"] == "ann-laptop"
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


def test_page_contains_team_markup(app):
    status, html = http.request("GET", app.url + "/", "")
    assert status == 200
    assert 'data-view="team"' in html and 'id="team"' in html
    assert "Your name" not in html
    assert "/api/me" in html and "/api/knocks/" in html
    assert "Yes opens a door between them" in html


def test_invites_carry_their_link(app):
    status, made = post(app, "boss", "/api/invites", {"person": "ann"})
    assert status == 200
    rows = get(app, "boss", "/api/invites")
    assert [(r["person"], r["url"]) for r in rows] == [("ann", made["url"])]
    assert rows[0]["url"].endswith("/i/" + rows[0]["code"])


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
    assert status == 400 and body == (f"no session named {b1.name} that you can reach;"
                                      " to reach someone else, message the manager")
    assert post(app, "ann", "/api/send", {"to": "all", "text": "hi"}) == (200, {"sent": 1})
    assert post(app, "boss", "/api/send", {"to": b1.name, "text": "hi"})[0] == 200
    s.open_door_group(app.ids["ann"], app.ids["ben"], "boss")
    assert post(app, "ann", "/api/send", {"to": b1.name, "text": "now ok"}) == (200, {"sent": 1})
    status, body = post(app, "ben", "/api/send", {"to": "ann", "text": "to the page"})
    assert status == 200
    status, body = post(app, "ben", "/api/send", {"to": "boss", "text": "to the admin page"})
    assert status == 400 and "no session named boss that you can reach" in body


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
    kid = old_knock(s, b1.key, app.ids["ann"], "about the api")
    for who in ("ann", "ben"):  # the knocked person and the knocker alike
        assert post(app, who, f"/api/knocks/{kid}/answer", {"answer": "yes"}) == \
            (400, "only the admin opens doors")
    assert post(app, "boss", f"/api/knocks/{kid}/answer", {"answer": "maybe"})[0] == 400
    assert post(app, "boss", f"/api/knocks/{kid}/answer", {"answer": "yes"}) == \
        (200, {"door": "group"})
    assert s.may_message(b1.key, a1.key) and s.may_message(a1.key, b1.key)
    assert get(app, "boss", "/api/feed")["knocks"] == []
    status, body = post(app, "boss", f"/api/knocks/{kid}/answer", {"answer": "yes"})
    assert status == 400 and "already answered" in body
    # a knock to the admin, answered from the admin's own session: a session door
    x1 = online(app, "boss", "x1", "box-1")
    kid = old_knock(s, b1.key, app.ids["boss"], "and you")
    assert post(app, "boss", f"/api/knocks/{kid}/answer", {"answer": "yes"}) == \
        (200, {"door": "session"})
    assert s.may_message(b1.key, x1.key)
    kid = old_knock(s, b1.key, app.ids["ann"], "once more")
    assert post(app, "boss", f"/api/knocks/{kid}/answer", {"answer": "no"}) == (200, {"door": None})
    log = [(r["person"], r["kind"]) for r in s.log(5)]
    assert ("boss", "knock_answer") in log
    assert post(app, "boss", "/api/knocks/9999/answer", {"answer": "no"})[0] == 400


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
    with pytest.raises(KaptyorkaError, match="no session named boss that you can reach; to"
                                             " reach someone else, message the manager"):
        s.send(b1.key, "boss", "cold message")
    # the page's door is a session door: it closes when the session goes offline
    with s.lock:
        s.db.execute("UPDATE agents SET last_seen=last_seen-100 WHERE key='a1'")
    s.expire_doors(s.clock())
    online(app, "ann", "a1", "pc")
    with pytest.raises(KaptyorkaError, match="no session named boss that you can reach"):
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
    online(app, "ann", "a1", "pc", "mine")
    b1 = online(app, "ben", "b1", "box-2", "secret")
    kid = old_knock(s, b1.key, app.ids["ann"], "a question")
    # only the admin answers, so ann's page shows no knock; ben's session is not listed for
    # her, so the knock names no session
    assert get(app, "ann", "/api/feed")["knocks"] == []
    assert get(app, "ann", "/api/knocks")[0]["from_session"] == ""
    assert get(app, "boss", "/api/feed")["knocks"][0]["from_session"] == b1.name
    assert post(app, "boss", f"/api/knocks/{kid}/answer", {"answer": "yes"}) == \
        (200, {"door": "group"})
    door = get(app, "ann", "/api/feed")["doors"][0]
    assert door["scope"] == "group" and not any(k.startswith("_") for k in door)
    rows = {a["person"]: a for a in get(app, "ann", "/api/feed")["agents"]}
    assert rows["ben"]["name"] == "box-2/codex/(private)"  # listed now, the folder masked


def test_page_broadcast_opens_no_doors(app):
    s = app.store
    online(app, "ann", "a1", "pc")
    b1 = online(app, "ben", "b1", "box-2")
    assert post(app, "boss", "/api/send", {"to": "all", "text": "news"}) == (200, {"sent": 2})
    assert s.doors() == []  # a broadcast is one-way
    with pytest.raises(KaptyorkaError, match="no session named boss that you can reach"):
        s.send(b1.key, "boss", "reply to the news")


def test_page_downloads_files_without_token_links(app):
    status, html = http.request("GET", app.url + "/", "")
    assert "URL.createObjectURL" in html and "revokeObjectURL" in html
    assert html.count('?t=" + encodeURIComponent(token)') == 2  # the viewer and the thumbnails/videos only


def page_html() -> str:
    import importlib.resources
    return (importlib.resources.files("kaptyorka") / "web/index.html").read_text()


def test_feed_lists_recent_decisions(app):
    s = app.store
    s.project_register("proj")
    for _ in range(12):
        rid = s.request_access(app.ids["ben"], "project", "proj", "")
        s.decide_request(rid, app.ids["boss"], False)
    s.request_access(app.ids["ann"], "project", "proj", "pending one")
    f = get(app, "boss", "/api/feed")
    assert len(f["decisions"]) == 10 and all(r["decided_at"] for r in f["decisions"])
    times = [r["decided_at"] for r in f["decisions"]]
    assert times == sorted(times, reverse=True)
    assert [r["person"] for r in f["requests"]] == ["ann"]


def test_page_has_no_browser_dialogs():
    import re
    assert not re.search(r"\b(confirm|prompt|alert)\(", page_html())


def test_page_inline_confirm_holds_the_panel():
    html = page_html()
    assert "function confirmInline(anchor, text, yesLabel, onYes, opt)" in html
    assert 'e.key === "Escape"' in html and "holding.isConnected" in html
    assert '$("people-rows").contains(holding)) return;' in html  # a Remove question survives the refresh
    assert '"Remove " + v.name + "? They are signed out everywhere."' in html


def test_page_login_and_sign_out():
    html = page_html()
    assert "Use the link from kaptyorka start, or ask your admin for an invite." in html
    assert "That token did not work." in html and 'id="lock-err"' in html
    unlock = html[html.index('$("unlock").addEventListener'):html.index('$("signout")')]
    assert "location.reload" not in unlock and 'get("/api/me")' in unlock
    assert 'id="signout"' in html and ">Sign out</button>" in html and "lock(false)" in html
    # Sign out sits at the bottom of the Account page, which the person's name opens as a view
    # with its own address (no popover)
    assert 'id="viewer-name" data-view="account"' in html and 'var VIEWS = ["home", "account"]' in html
    assert html.index('id="a-projects"') < html.index('id="signout"') < html.index('<section id="team"')
    assert "vmenu" not in html and "accountOpen" not in html


def test_page_empty_states():
    html = page_html()
    for text in (">No sessions online<", '"No messages yet."', '"No projects yet"'):
        assert text in html, text
    # few words: the old explaining sentences are gone
    for text in ("Click a session on the Team tab", "Notes are shared documents",
                 "Invite a person on the People tab", "Agents file one when"):
        assert text not in html, text


def test_page_remove_says_which_workers_may_still_run():
    html = page_html()
    assert 'id="team-ok"' in html and "removedLine(v.name, r)" in html
    assert '" may still run on " + w.host + "; close it on that machine."' in html


def test_register_again_keeps_description(app):
    from kaptyorka import admin_ops
    s = app.store
    s.project_register("proj", "the web shop")
    admin_ops.project_remove(s, "proj", s.authenticate(app.tokens["boss"]))
    gone = get(app, "boss", "/api/feed")["archived_projects"]
    assert [p["description"] for p in gone if p["name"] == "proj"] == ["the web shop"]
    # what the page's Register again sends
    status, _ = post(app, "boss", "/api/projects", {"name": "proj", "description": gone[0]["description"]})
    assert status == 204
    proj = next(p for p in get(app, "boss", "/api/feed")["projects"] if p["name"] == "proj")
    assert proj["description"] == "the web shop"


def test_page_push_review_fixes():
    html = page_html()
    # the ask field names what to type for the chosen kind
    assert 'id="ask-target" list="ask-names" placeholder="Project name"' in html
    assert '$("ask-target").placeholder = $("ask-kind").value === "machine" ? "Machine name" : "Project name";' in html
