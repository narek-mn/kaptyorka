"""Sign in to the page with GitLab, against a fake GitLab OAuth provider: the full flow, a bad
state, an unknown user, a disabled user, the app file's permissions, the admin's backup link,
expiry and the checks that end a sign-in. The app secret must never show anywhere."""
import http.client
import json
import logging
import os
import re
from types import SimpleNamespace
from urllib.parse import urlparse

import pytest

from conftest import start_hub
from helpers import FakeGitLabOAuth, person
from kaptyorka import http as khttp
from kaptyorka import hub as khub
from kaptyorka import paths, signin
from kaptyorka.gitlab import GitLabSource
from kaptyorka.manager import ManagerStore
from kaptyorka.store import Store

SECRET = "app-secret-value"


def put_app(mode=0o600, text=f"app-id\n{SECRET}\n"):
    p = paths.home() / "gitlab-oauth"
    p.write_text(text)
    os.chmod(p, mode)
    return p


@pytest.fixture
def lab():
    g = FakeGitLabOAuth(users={"alice-gl": {"id": 11, "state": "active"},
                               "bob-gl": {"id": 22, "state": "active"},
                               "dave-gl": {"id": 44, "state": "active"}})
    yield g
    g.close()


@pytest.fixture
def hub(tmp_path, home, lab):
    now = [1_000_000.0]
    store = Store(tmp_path / "hub.db", clock=lambda: now[0])
    ManagerStore(store).set_enabled(False, "test")
    GitLabSource(store).set(lab.url, "team", "alice")
    put_app()
    stop = start_hub(store)
    ids, tokens = {}, {}
    for name, role in (("alice", "admin"), ("bob", "member")):
        ids[name], tokens[name] = person(store, name, role)
    by = store.authenticate(tokens["alice"])
    signin.set_user(store, None, "alice", "alice-gl", by)
    signin.set_user(store, None, "bob", "bob-gl", by)
    yield SimpleNamespace(url=f"http://127.0.0.1:{stop.port}", store=store, ids=ids, tokens=tokens,
                          now=now, si=stop.app.state.signin, by=by)
    stop()
    store.close()


def get(url, cookie=""):
    u = urlparse(url)
    c = http.client.HTTPConnection(u.hostname, u.port, timeout=10)
    c.request("GET", u.path + ("?" + u.query if u.query else ""), headers={"Cookie": cookie} if cookie else {})
    r = c.getresponse()
    out = SimpleNamespace(status=r.status, headers=r.headers, body=r.read().decode())
    c.close()
    return out


def sign_in(hub, mangle=None):
    """The browser's trip: start, GitLab's authorize page, the callback. (callback answer,
    start answer); mangle(callback url, cookie) may change what the browser sends back."""
    start = get(hub.url + "/auth/gitlab/start")
    assert start.status == 302, start.body
    cookie = start.headers["Set-Cookie"].split(";")[0]
    back = get(start.headers["Location"])
    assert back.status == 302, back.body
    url = back.headers["Location"]
    if mangle:
        url, cookie = mangle(url, cookie)
    return get(url, cookie), start


def token_of(answer) -> str:
    m = re.search(r'localStorage\.setItem\("kaptyorka\.token","([^"]+)"\)', answer.body)
    assert m, answer.body
    return m.group(1)


def me(hub, token):
    return khttp.request("GET", hub.url + "/api/me", token)


def test_full_flow_signs_alice_in_as_the_admin(hub, lab):
    assert khttp.request("GET", hub.url + "/auth/gitlab", "")[1] == {"on": True}
    done, start = sign_in(hub)
    assert done.status == 200 and done.headers["Cache-Control"] == "no-store"
    loc = urlparse(start.headers["Location"])
    q = dict(p.split("=", 1) for p in loc.query.split("&"))
    assert loc.path == "/oauth/authorize" and q["scope"] == "read_user" and q["code_challenge_method"] == "S256"
    assert q["redirect_uri"] == f"http%3A%2F%2F127.0.0.1%3A{urlparse(hub.url).port}%2Fauth%2Fgitlab%2Fcallback"
    assert "HttpOnly" in start.headers["Set-Cookie"] and "SameSite=Lax" in start.headers["Set-Cookie"]
    token = token_of(done)
    assert me(hub, token) == (200, {"person": "alice", "role": "admin", "device": "gitlab-web",
                                    "person_id": hub.ids["alice"], "gitlab_connect": False, "page": True})
    dev = [d for d in hub.store.devices(hub.ids["alice"]) if d["name"] == "gitlab-web"][0]
    assert dev["expires"] == hub.now[0] + signin.SESSION_S
    # the token went through the page's storage, never the address bar
    assert token not in done.headers.get("Location", "") and "Location" not in done.headers
    assert "Max-Age=0" in done.headers["Set-Cookie"]
    log = [r["sentence"] for r in khttp.request("GET", hub.url + "/api/feed", token)[1]["log"]]
    assert "alice signed in to the page with GitLab (alice-gl)" in log
    # a member signs in as a member
    lab.who = "bob-gl"
    assert me(hub, token_of(sign_in(hub)[0]))[1]["role"] == "member"


def test_bad_state_is_refused(hub, lab):
    for mangle in (lambda u, c: (re.sub(r"state=[^&]+", "state=forged", u), c),  # forged state
                   lambda u, c: (u, ""),  # no cookie: began in another browser
                   lambda u, c: (u, "kaptyorka_signin=other")):  # someone else's sign-in
        done, _ = sign_in(hub, mangle)
        assert done.status == 403 and "old or was already used" in done.body
    # a callback used twice
    kept = {}
    done, _ = sign_in(hub, lambda u, c: kept.update(u=u, c=c) or (u, c))
    assert done.status == 200
    again = get(kept["u"], kept["c"])
    assert again.status == 403 and "old or was already used" in again.body
    # too late
    start = get(hub.url + "/auth/gitlab/start")
    back = get(start.headers["Location"])
    hub.now[0] += signin.STATE_S
    late = get(back.headers["Location"], start.headers["Set-Cookie"].split(";")[0])
    assert late.status == 403 and "old or was already used" in late.body
    cancelled = get(hub.url + "/auth/gitlab/callback?error=access_denied&state=x")
    assert cancelled.status == 400 and "Sign-in was cancelled." in cancelled.body


def test_redirects_only_to_gitlab_and_the_page(hub):
    start = get(hub.url + "/auth/gitlab/start?next=https://evil.example")
    assert start.headers["Location"].startswith(hub.store.db.execute(
        "SELECT value FROM settings WHERE key='gitlab_url'").fetchone()["value"] + "/oauth/authorize?")
    done, _ = sign_in(hub)
    assert 'location.replace("/")' in done.body and "evil" not in done.body


def test_unknown_user_is_told_to_ask_the_admin(hub, lab):
    lab.who = "dave-gl"
    done, _ = sign_in(hub)
    assert done.status == 403
    assert "Nobody on this hub is linked to the GitLab user dave-gl; ask the admin to add you" in done.body
    assert "kaptyorka.token" not in done.body


def test_a_username_taken_over_by_another_account_is_refused(hub, lab):
    assert me(hub, token_of(sign_in(hub)[0]))[0] == 200  # alice's GitLab id is kept now
    lab.users["alice-gl"]["id"] = 99  # the name moved to another GitLab account
    done, _ = sign_in(hub)
    assert done.status == 403 and "is not the account the admin linked" in done.body


def test_disabled_user_cannot_sign_in_and_is_signed_out(hub, lab):
    token = token_of(sign_in(hub)[0])
    assert hub.si.recheck(force=True) == 0 and me(hub, token)[0] == 200
    lab.users["alice-gl"]["state"] = "blocked"
    done, _ = sign_in(hub)
    assert done.status == 403 and "GitLab refused the sign-in" in done.body
    assert hub.si.recheck() == 0  # not due yet
    hub.now[0] += signin.CHECK_S
    assert hub.si.recheck() == 1
    assert me(hub, token)[0] == 401
    # GitLab answers but the account is no longer active
    lab.users["alice-gl"]["state"] = "active"
    token = token_of(sign_in(hub)[0])
    lab.users["alice-gl"]["state"] = "deactivated"
    assert get(hub.url + "/auth/gitlab/start").status == 302
    assert hub.si.recheck(force=True) == 1 and me(hub, token)[0] == 401
    log = [r["sentence"] for r in khttp.request("GET", hub.url + "/api/feed", hub.tokens["alice"])[1]["log"]]
    assert "alice's GitLab sign-in ended: GitLab user no longer allowed" in log
    assert "alice's GitLab sign-in ended: GitLab refused" in log


def test_gitlab_down_keeps_the_sign_in_until_it_ends(hub, lab):
    token = token_of(sign_in(hub)[0])
    lab.down = True
    assert hub.si.recheck(force=True) == 0 and me(hub, token)[0] == 200
    hub.now[0] += signin.SESSION_S
    assert me(hub, token)[0] == 401  # ended: sign in again
    assert hub.si.recheck() == 1


def test_link_change_and_removal_end_sign_ins(hub, lab):
    alice = token_of(sign_in(hub)[0])
    lab.who = "bob-gl"
    bob = token_of(sign_in(hub)[0])
    assert khttp.request("POST", hub.url + "/api/people/alice/gitlab", hub.tokens["alice"],
                         {"username": "bob-gl"}) == (400, "bob is already linked to the GitLab user bob-gl")
    assert khttp.request("POST", hub.url + "/api/people/bob/gitlab", hub.tokens["bob"],
                         {"username": "x"})[0] == 403
    assert khttp.request("POST", hub.url + "/api/people/bob/gitlab", hub.tokens["alice"],
                         {"username": ""}) == (200, {"gitlab_user": None})
    assert me(hub, bob)[0] == 401 and me(hub, alice)[0] == 200
    done, _ = sign_in(hub)
    assert "Nobody on this hub is linked to the GitLab user bob-gl" in done.body
    # the same name again (other case) changes nothing
    assert khttp.request("POST", hub.url + "/api/people/alice/gitlab", hub.tokens["alice"],
                         {"username": "@Alice-GL"}) == (200, {"gitlab_user": "Alice-GL"})
    assert me(hub, alice)[0] == 200
    assert khttp.request("POST", hub.url + "/api/people/alice/gitlab", hub.tokens["alice"],
                         {"username": "a b"}) == (400, "a GitLab username uses letters, digits, '_', '.' and '-'")


def test_add_person_with_a_gitlab_username(hub, lab):
    assert khttp.request("POST", hub.url + "/api/people", hub.tokens["alice"],
                         {"name": "dave", "gitlab": "bob-gl"}) == (400, "bob is already linked to the GitLab user bob-gl")
    assert hub.store.person_by_name("dave") is None  # nobody added
    assert khttp.request("POST", hub.url + "/api/people", hub.tokens["alice"],
                         {"name": "dave", "gitlab": "dave-gl"})[0] == 200
    lab.who = "dave-gl"
    assert me(hub, token_of(sign_in(hub)[0]))[1]["person"] == "dave"
    rows = khttp.request("GET", hub.url + "/api/feed", hub.tokens["alice"])[1]["people"]
    assert {p["name"]: p["gitlab_user"] for p in rows} == {"alice": "alice-gl", "bob": "bob-gl", "dave": "dave-gl"}


def test_sign_out_ends_a_gitlab_sign_in_only(hub):
    token = token_of(sign_in(hub)[0])
    assert khttp.request("POST", hub.url + "/api/me/signout", token)[0] == 204
    assert me(hub, token)[0] == 401
    # a device token is not a page sign-in: the page route refuses it, and it stays good
    assert khttp.request("POST", hub.url + "/api/me/signout", hub.tokens["alice"])[0] == 403
    assert me(hub, hub.tokens["alice"])[0] == 200


def test_app_file_with_open_permissions_is_refused(hub):
    put_app(mode=0o640)
    assert khttp.request("GET", hub.url + "/auth/gitlab", "")[1] == {"on": False}
    start = get(hub.url + "/auth/gitlab/start")
    assert start.status == 404 and "can be opened by other users" in start.body and "chmod 600" in start.body
    assert SECRET not in start.body
    put_app(text="only-one-line\n")
    assert "must have two lines" in get(hub.url + "/auth/gitlab/start").body
    os.unlink(paths.home() / "gitlab-oauth")
    assert "no GitLab app for sign-in" in get(hub.url + "/auth/gitlab/start").body


def test_admin_backup_link_works_when_gitlab_is_down(hub, lab):
    lab.down = True
    start = get(hub.url + "/auth/gitlab/start")
    assert start.status == 302  # the hub cannot tell until GitLab answers
    # a callback that reaches the hub while GitLab cannot swap the code
    lab.down = False
    back = get(start.headers["Location"])
    lab.down = True
    failed = get(back.headers["Location"], start.headers["Set-Cookie"].split(";")[0])
    assert failed.status == 403 and "GitLab did not answer" in failed.body
    # the admin token (kaptyorka page on the hub machine) still opens the page
    admin = hub.store.device_add(hub.ids["alice"], "hub")
    (paths.home() / "admin-token").write_text(admin + "\n")
    assert me(hub, admin)[1]["role"] == "admin" and me(hub, admin)[1]["page"] is True
    assert khttp.request("GET", hub.url + "/api/log", admin)[0] == 200
    assert get(hub.url + "/").status == 200


def test_secret_never_shows(hub, lab, caplog, capsys, monkeypatch):
    from test_cli import entry_with_exit

    caplog.set_level(logging.DEBUG)
    seen = []
    done, start = sign_in(hub)
    seen += [done.body, str(done.headers), start.body, str(start.headers)]
    lab.who = "dave-gl"
    seen.append(sign_in(hub)[0].body)
    lab.down = True
    lab.who = "alice-gl"
    hub.si.recheck(force=True)
    seen.append(json.dumps(khttp.request("GET", hub.url + "/api/feed", hub.tokens["alice"])[1]))
    seen.append(json.dumps(khttp.request("GET", hub.url + "/api/people", hub.tokens["alice"])[1]))
    lab.down = False
    paths.save_config({"hub": hub.url, "token": hub.tokens["alice"], "machine": "box-1"})
    entry_with_exit(["gitlab", "status"])
    entry_with_exit(["people"])
    out = capsys.readouterr()
    assert "sign-in:    on" in out.out and f"{hub.url}/auth/gitlab/callback" in out.out
    seen += [out.out, out.err, caplog.text]
    with hub.store.lock:
        seen.append(json.dumps([list(r) for t in ("settings", "log", "devices", "gitlab_sessions")
                                for r in hub.store.db.execute(f"SELECT * FROM {t}")]))
    text = "\n".join(seen)
    assert SECRET not in text
    assert all(code not in text for code in lab.codes)  # no code is ever echoed


def test_the_manager_account_cannot_be_linked(hub, capsys):
    from test_cli import entry_with_exit

    person(hub.store, "manager", "manager")
    person(hub.store, "carol", "manager")  # a person with the manager role is not the account
    assert khttp.request("POST", hub.url + "/api/people/manager/gitlab", hub.tokens["alice"],
                         {"username": "mgr-gl"}) == (400, "the manager account cannot sign in with GitLab")
    assert khttp.request("POST", hub.url + "/api/people/carol/gitlab", hub.tokens["alice"],
                         {"username": "carol-gl"})[0] == 200
    paths.save_config({"hub": hub.url, "token": hub.tokens["alice"], "machine": "box-1"})
    assert entry_with_exit(["people", "gitlab", "manager", "mgr-gl"]) == 1
    assert capsys.readouterr().err.strip() == "the manager account cannot sign in with GitLab"


def test_cli_people_counts_live_machines_only(hub, lab, capsys):
    from test_cli import entry_with_exit

    lab.who = "bob-gl"
    token_of(sign_in(hub)[0])  # bob's page sign-in is not a machine
    hub.store.device_add(hub.ids["bob"], "old-box")
    old = [d for d in hub.store.devices(hub.ids["bob"]) if d["name"] == "old-box"][0]
    hub.store.device_revoke(old["id"])
    paths.save_config({"hub": hub.url, "token": hub.tokens["alice"], "machine": "box-1"})
    assert entry_with_exit(["people"]) == 0
    bob = [l for l in capsys.readouterr().out.splitlines() if l.startswith("bob ")][0]
    assert bob.split()[:4] == ["bob", "member", "bob-gl", "1"]


def test_cli_people_gitlab(hub, capsys):
    from test_cli import entry_with_exit

    paths.save_config({"hub": hub.url, "token": hub.tokens["alice"], "machine": "box-1"})
    assert entry_with_exit(["people", "gitlab", "bob", "bob-new"]) == 0
    assert capsys.readouterr().out.strip() == "bob signs in with the GitLab user bob-new"
    assert entry_with_exit(["people", "gitlab", "bob", ""]) == 0
    assert capsys.readouterr().out.strip() == "bob is no longer linked to GitLab"
    assert entry_with_exit(["people", "gitlab", "nobody", "x"]) == 1
    assert capsys.readouterr().err.strip() == "no person named nobody"


# people link their own GitLab: from the invite link, or from their own page

def post(url, cookie="", headers=None, body=b""):
    u = urlparse(url)
    c = http.client.HTTPConnection(u.hostname, u.port, timeout=10)
    h = {"Content-Type": "application/x-www-form-urlencoded", **(headers or {})}
    if cookie:
        h["Cookie"] = cookie
    c.request("POST", u.path, body=body, headers=h)
    r = c.getresponse()
    out = SimpleNamespace(status=r.status, headers=r.headers, body=r.read().decode())
    c.close()
    return out


def finish(start, cookie=None):
    """GitLab's authorize page and the callback, after a start answer that set the cookie."""
    cookie = start.headers["Set-Cookie"].split(";")[0] if cookie is None else cookie
    back = get(start.headers["Location"])
    assert back.status == 302, back.body
    return get(back.headers["Location"], cookie), back.headers["Location"]


def link_invite(hub, code, headers=None):
    start = post(f"{hub.url}/i/{code}/gitlab", headers=headers)
    if start.status != 303:
        return start, None
    return finish(start)


def redeem(hub, code):
    return khttp.request("POST", f"{hub.url}/i/{code}", "", {"device_name": "box-1"})


def linked(hub, name):
    with hub.store.lock:
        r = hub.store.db.execute("SELECT gitlab_user, gitlab_id FROM people WHERE name=?", (name,)).fetchone()
    return r["gitlab_user"], r["gitlab_id"]


def sentences(hub):
    return [r["sentence"] for r in khttp.request("GET", hub.url + "/api/feed", hub.tokens["alice"])[1]["log"]]


@pytest.fixture
def dave(hub, lab):
    """dave: added by the admin with no GitLab name, invited; GitLab knows him as dave-gl."""
    hub.store.person_create("dave", by="alice")
    lab.who = "dave-gl"
    return hub.store.invite_create(hub.store.person_by_name("dave")["id"], by="alice")


def test_invite_link_signs_in_and_links_then_join_still_works(hub, lab, dave):
    page = get(f"{hub.url}/i/{dave}")
    assert page.status == 200 and f"action='/i/{dave}/gitlab'" in page.body
    assert page.body.index("This invite is for dave.") < page.body.index("Sign in with GitLab")
    assert "Sign in with GitLab" in page.body and f"kaptyorka join {hub.url}/i/{dave}" in page.body
    assert "Open the page as this person" not in page.body  # that one would use the invite up
    (done, back), = [link_invite(hub, dave)]
    assert dave not in back  # the invite rides in the server-side state, not the callback
    token = token_of(done)
    assert me(hub, token)[1] == {"person": "dave", "role": "member", "device": "gitlab-web",
                                 "person_id": hub.store.person_by_name("dave")["id"], "gitlab_connect": False,
                                 "page": True}
    assert linked(hub, "dave") == ("dave-gl", 44)
    assert "dave linked the GitLab user dave-gl (from the invite)" in sentences(hub)
    assert "dave signed in to the page with GitLab (dave-gl)" in sentences(hub)
    assert SECRET not in done.body + str(done.headers)
    # the invite is not used up: join works afterwards
    status, got = redeem(hub, dave)
    assert status == 200 and got["person"] == "dave"
    # used and linked: the invite page is done
    assert get(f"{hub.url}/i/{dave}").status == 404
    # a plain sign-in finds dave now
    assert me(hub, token_of(sign_in(hub)[0]))[1]["person"] == "dave"


def test_join_first_then_the_invite_link_still_links(hub, lab, dave):
    assert redeem(hub, dave)[0] == 200
    page = get(f"{hub.url}/i/{dave}")
    assert page.status == 200 and "Sign in with GitLab" in page.body and "kaptyorka join" not in page.body
    assert "This invite is for dave." in page.body
    assert redeem(hub, dave) == (400, "invite already used")  # join still works once only
    done, _ = link_invite(hub, dave)
    assert me(hub, token_of(done))[1]["person"] == "dave" and linked(hub, "dave") == ("dave-gl", 44)
    # once linked, the used link can link nothing more
    assert get(f"{hub.url}/i/{dave}").status == 404
    lab.users["erin-gl"] = {"id": 66, "state": "active"}
    lab.who = "erin-gl"
    done, _ = link_invite(hub, dave)
    assert done.status == 403 and "You are already linked to another GitLab account" in done.body
    assert linked(hub, "dave") == ("dave-gl", 44)


def test_expired_invite_cannot_link(hub, lab, dave):
    from kaptyorka.store import INVITE_TTL_S

    hub.now[0] += INVITE_TTL_S - 100
    start = post(f"{hub.url}/i/{dave}/gitlab")
    assert start.status == 303
    back = get(start.headers["Location"])
    hub.now[0] += 200  # expired while the person was at GitLab
    done = get(back.headers["Location"], start.headers["Set-Cookie"].split(";")[0])
    assert done.status == 403 and "This invite link is no longer valid" in done.body
    assert "kaptyorka.token" not in done.body
    assert get(f"{hub.url}/i/{dave}").status == 404
    late = post(f"{hub.url}/i/{dave}/gitlab")
    assert late.status == 404 and "no longer valid" in late.body
    assert post(f"{hub.url}/i/no-such-code/gitlab").status == 404
    assert linked(hub, "dave") == (None, None)


def test_already_linked_person_and_taken_account_are_refused(hub, lab, dave):
    # a GitLab account someone else has
    lab.who = "bob-gl"
    done, _ = link_invite(hub, dave)
    assert done.status == 403 and "The GitLab user bob-gl is already linked to someone else" in done.body
    assert linked(hub, "dave") == (None, None)
    # bob's GitLab id (kept at his first sign-in), under a new username, is still bob's
    assert sign_in(hub)[0].status == 200
    lab.users["bob-new"] = {"id": 22, "state": "active"}
    lab.who = "bob-new"
    assert link_invite(hub, dave)[0].status == 403 and linked(hub, "dave") == (None, None)
    # the first link wins; the same account again is a plain sign-in
    lab.who = "dave-gl"
    assert link_invite(hub, dave)[0].status == 200
    done, _ = link_invite(hub, dave)
    assert done.status == 200 and me(hub, token_of(done))[1]["person"] == "dave"
    assert sentences(hub).count("dave linked the GitLab user dave-gl (from the invite)") == 1
    # the admin set dave to another account: a link from his invite is refused
    signin.set_user(hub.store, hub.si, "dave", "other-gl", hub.by)
    done, _ = link_invite(hub, dave)
    assert done.status == 403 and "already linked to another GitLab account" in done.body
    assert linked(hub, "dave") == ("other-gl", None)


def test_the_manager_account_cannot_link(hub, lab):
    mid, mtoken = person(hub.store, "manager", "manager")
    with hub.store.lock:  # an invite cannot be made for it, but an old row must still not link
        hub.store.db.execute("INSERT INTO invites (code, person_id, created, expires) VALUES (?,?,?,?)",
                             ("mgr-code", mid, hub.now[0], hub.now[0] + 3600))
    lab.who = "dave-gl"
    done, _ = link_invite(hub, "mgr-code")
    assert done.status == 403 and "The manager account cannot sign in with GitLab" in done.body
    assert me(hub, mtoken)[1]["gitlab_connect"] is False
    assert khttp.request("POST", hub.url + "/api/me/gitlab", mtoken)[0] == 400
    assert linked(hub, "manager") == (None, None)


def test_connect_gitlab_from_the_page(hub, lab):
    dave_id, dave_t = person(hub.store, "dave")  # joined before 0.9.16: a device token, no link
    assert me(hub, dave_t)[1]["gitlab_connect"] is True
    assert me(hub, hub.tokens["bob"])[1]["gitlab_connect"] is False  # linked already
    lab.who = "dave-gl"
    u = urlparse(hub.url)
    c = http.client.HTTPConnection(u.hostname, u.port, timeout=10)
    c.request("POST", "/api/me/gitlab", body=b"{}", headers={"Authorization": f"Bearer {dave_t}",
                                                            "Content-Type": "application/json"})
    r = c.getresponse()
    start = SimpleNamespace(status=r.status, headers=r.headers, body=json.loads(r.read()))
    c.close()
    assert start.status == 200 and "HttpOnly" in start.headers["Set-Cookie"]
    assert start.headers["Set-Cookie"].split(";")[0].split("=", 1)[1] in start.body["url"]
    start.headers = {"Set-Cookie": start.headers["Set-Cookie"], "Location": start.body["url"]}
    done, _ = finish(start)
    token = token_of(done)
    assert me(hub, token)[1]["person"] == "dave" and linked(hub, "dave") == ("dave-gl", 44)
    assert "dave linked the GitLab user dave-gl (from their page)" in sentences(hub)
    # linked: the action is gone, for the old device token too
    assert me(hub, dave_t)[1]["gitlab_connect"] is False and me(hub, token)[1]["gitlab_connect"] is False
    assert khttp.request("POST", hub.url + "/api/me/gitlab", dave_t) == (400, "you cannot connect GitLab here")
    # no sign-in set up: no action
    erin_id, erin_t = person(hub.store, "erin")
    assert me(hub, erin_t)[1]["gitlab_connect"] is True
    os.unlink(paths.home() / "gitlab-oauth")
    assert me(hub, erin_t)[1]["gitlab_connect"] is False
    assert khttp.request("POST", hub.url + "/api/me/gitlab", erin_t)[0] == 400


def test_link_state_cannot_be_tampered_with(hub, lab, dave):
    # another site's form cannot start a link for its own invite in the visitor's browser
    for h in ({"Sec-Fetch-Site": "cross-site"}, {"Sec-Fetch-Site": "same-site"},
              {"Origin": "http://evil.example"}):
        start = post(f"{hub.url}/i/{dave}/gitlab", headers=h)
        assert start.status == 403 and "Location" not in start.headers
    assert post(f"{hub.url}/i/{dave}/gitlab", headers={"Sec-Fetch-Site": "same-origin",
                                                      "Origin": hub.url}).status == 303
    # the callback needs the state its own cookie names
    plain = get(hub.url + "/auth/gitlab/start")
    link = post(f"{hub.url}/i/{dave}/gitlab")
    done, _ = finish(link, cookie=plain.headers["Set-Cookie"].split(";")[0])
    assert done.status == 403 and "old or was already used" in done.body
    link = post(f"{hub.url}/i/{dave}/gitlab")
    back = get(link.headers["Location"])
    forged = re.sub(r"state=[^&]+", "state=forged", back.headers["Location"])
    assert get(forged, link.headers["Set-Cookie"].split(";")[0]).status == 403
    assert linked(hub, "dave") == (None, None)
    # the real callback works once, never twice
    assert get(back.headers["Location"], link.headers["Set-Cookie"].split(";")[0]).status == 200
    assert get(back.headers["Location"], link.headers["Set-Cookie"].split(";")[0]).status == 403
    # GitLab is only ever sent the state; the invite code never leaves the hub
    link = post(f"{hub.url}/i/{dave}/gitlab")
    assert dave not in link.headers["Location"] and dave not in link.headers["Set-Cookie"]


def test_the_admin_cancels_a_used_invite_so_it_can_no_longer_link(hub, lab, dave):
    """A leaked invite that join already used can link GitLab while its person is unlinked: the
    admin can close it, from the list, the command or the API."""
    assert redeem(hub, dave)[0] == 200
    rows = khttp.request("GET", hub.url + "/api/invites", hub.tokens["alice"])[1]
    assert [(r["person"], r["used"]) for r in rows] == [("dave", True)]
    start = post(f"{hub.url}/i/{dave}/gitlab")  # begun before the cancel
    assert start.status == 303
    assert khttp.request("DELETE", f"{hub.url}/api/invites/{dave}", hub.tokens["alice"])[0] == 204
    assert khttp.request("GET", hub.url + "/api/invites", hub.tokens["alice"])[1] == []
    page = get(f"{hub.url}/i/{dave}")
    assert page.status == 404 and "This invite is not valid." in page.body  # dave is still unlinked
    late = post(f"{hub.url}/i/{dave}/gitlab")
    assert late.status == 404 and "no longer valid" in late.body
    done, _ = finish(start)
    assert done.status == 403 and "This invite link is no longer valid" in done.body
    assert linked(hub, "dave") == (None, None)
    assert "alice cancelled the invite link for dave" in sentences(hub)


# 0.9.17: with sign-in on, the page needs a GitLab sign-in (or the admin token); machines and
# the command line keep their device tokens

PAGE_ROUTES = [("GET", "/api/log"), ("GET", "/api/me/account"), ("POST", "/api/me/invite"),
               ("GET", "/api/workers"), ("GET", "/api/warnings"), ("GET", "/api/proposals"),
               ("GET", "/api/me/access"), ("POST", "/api/me/signout"), ("GET", "/api/gitlab/projects")]


def call(hub, method, path, token, body=None):
    return khttp.request(method, hub.url + path, token, body)


def test_the_page_refuses_device_tokens_once_sign_in_is_on(hub):
    for method, path in PAGE_ROUTES:
        assert call(hub, method, path, hub.tokens["alice"]) == (403, khub.SIGN_IN_FIRST), path
        assert call(hub, method, path, hub.tokens["bob"])[0] == 403, path
    assert me(hub, hub.tokens["bob"])[1]["page"] is False
    # a GitLab sign-in opens it
    token = token_of(sign_in(hub)[0])
    assert me(hub, token)[1]["page"] is True
    assert call(hub, "GET", "/api/log", token)[0] == 200
    assert call(hub, "GET", "/api/me/account", token)[0] == 200
    # a device that only carries the sign-in's name is still a device token
    fake = hub.store.device_add(hub.ids["bob"], "gitlab-web")
    assert call(hub, "GET", "/api/me/account", fake)[0] == 403


def test_only_the_admin_token_file_opens_the_page_with_a_device_token(hub):
    admin = hub.store.device_add(hub.ids["alice"], "hub")
    assert call(hub, "GET", "/api/log", admin)[0] == 403  # no file yet: just another device
    (paths.home() / "admin-token").write_text(admin + "\n")
    assert call(hub, "GET", "/api/log", admin)[0] == 200
    assert call(hub, "GET", "/api/log", hub.tokens["alice"])[0] == 403  # the admin's other device
    # the hub key is whichever live device the file names, whatever its person's role
    (paths.home() / "admin-token").write_text(hub.tokens["bob"] + "\n")
    assert call(hub, "GET", "/api/me/account", hub.tokens["bob"])[0] == 200
    assert call(hub, "GET", "/api/log", admin)[0] == 403  # no longer the file's


def test_machine_routes_keep_device_tokens(hub):
    t = hub.tokens["alice"]
    for method, path, body in (("GET", "/api/feed", None), ("GET", "/api/me", None),
                               ("GET", "/api/people", None), ("GET", "/api/invites", None),
                               ("GET", "/api/machines", None), ("GET", "/api/rules", None),
                               ("GET", "/api/settings", None), ("GET", "/api/gitlab", None),
                               ("POST", "/api/invites", {"person": "bob"})):
        assert call(hub, method, path, t, body)[0] == 200, path
    # the hooks and the bridge: refused for their own reasons, never for the page's
    assert call(hub, "GET", "/api/brief", t)[1] != khub.SIGN_IN_FIRST
    assert call(hub, "POST", "/bridge/heartbeat", t, {})[1] != khub.SIGN_IN_FIRST


@pytest.mark.parametrize("method,path", [
    # every route a machine or the command line calls (bridge, shim, hooks, doctor, join,
    # hub-state, the admin commands)
    ("GET", "/api/brief"), ("GET", "/api/done-check"), ("POST", "/api/files"), ("GET", "/api/files/7"),
    ("HEAD", "/api/files/7"), ("GET", "/api/me"), ("GET", "/api/feed"), ("GET", "/api/settings"),
    ("POST", "/api/me/gitlab"), ("GET", "/api/people"), ("POST", "/api/people"),
    ("POST", "/api/people/bob/gitlab"), ("POST", "/api/people/bob/role"), ("DELETE", "/api/people/bob"),
    ("GET", "/api/invites"), ("POST", "/api/invites"), ("DELETE", "/api/invites/abc"),
    ("GET", "/api/rules"), ("POST", "/api/rules"), ("PUT", "/api/skills"), ("GET", "/api/machines"),
    ("POST", "/api/machines/box-1/grant"), ("POST", "/api/machines/box-1/owner"),
    ("POST", "/api/machines/box-1/revoke"), ("POST", "/api/projects"), ("POST", "/api/projects/proj-a"),
    ("DELETE", "/api/projects/proj-a"), ("POST", "/api/projects/proj-a/grant"),
    ("POST", "/api/projects/proj-a/revoke"), ("POST", "/api/devices/3/revoke"),
    ("POST", "/api/legacy/on"), ("POST", "/api/legacy/off"), ("GET", "/api/gitlab"),
    ("POST", "/api/gitlab"), ("DELETE", "/api/gitlab"), ("POST", "/mcp"), ("GET", "/bridge/poll"),
    ("POST", "/api/team-machines"), ("GET", "/api/team/machines"), ("POST", "/api/devices/3/move"),
])
def test_machine_route_list(method, path):
    assert khub.machine_route(method, path)


@pytest.mark.parametrize("method,path", [m for m in PAGE_ROUTES] + [
    ("POST", "/api/send"), ("POST", "/api/notes"), ("POST", "/api/doors"), ("GET", "/api/projects"),
    ("GET", "/api/projects/proj-a/activity"), ("POST", "/api/devices/3/shared"), ("GET", "/api/feed/"),
    ("POST", "/api/settings"), ("POST", "/api/legacy/assign")])
def test_page_route_list(method, path):
    assert not khub.machine_route(method, path)


def test_without_sign_in_nothing_changes(hub):
    os.unlink(paths.home() / "gitlab-oauth")
    for method, path in PAGE_ROUTES:
        for who in ("alice", "bob"):
            assert call(hub, method, path, hub.tokens[who])[1] != khub.SIGN_IN_FIRST, path
    assert call(hub, "GET", "/api/log", hub.tokens["alice"])[0] == 200
    assert call(hub, "GET", "/api/me/account", hub.tokens["bob"])[0] == 200
    assert me(hub, hub.tokens["bob"])[1]["page"] is True
    hub.store.person_create("dave", by="alice")
    code = hub.store.invite_create(hub.store.person_by_name("dave")["id"], by="alice")
    assert post(f"{hub.url}/i/{code}/web").status == 303  # the invite page's own way in


def test_the_invite_page_cannot_make_a_page_device_with_sign_in_on(hub, dave):
    r = post(f"{hub.url}/i/{dave}/web")
    assert r.status == 403 and "Location" not in r.headers
    assert redeem(hub, dave)[0] == 200  # the invite is still good for join


# lost or expired invite links

def test_new_invite_link_ends_the_old_ones(hub, lab, dave):
    used_code = dave
    assert redeem(hub, used_code)[0] == 200  # used, unexpired: it could still link GitLab
    spare = hub.store.invite_create(hub.store.person_by_name("dave")["id"], by="alice")
    bob_code = hub.store.invite_create(hub.ids["bob"], by="alice")
    status, got = call(hub, "POST", "/api/invites", hub.tokens["alice"], {"person": "dave", "replace": True})
    assert status == 200 and got["url"].endswith("/i/" + got["code"])
    assert get(f"{hub.url}/i/{spare}").status == 404 and get(f"{hub.url}/i/{used_code}").status == 404
    assert post(f"{hub.url}/i/{used_code}/gitlab").status == 404
    assert redeem(hub, spare)[0] == 400
    assert redeem(hub, got["code"])[0] == 200
    assert hub.store.invite_valid(bob_code)  # nobody else's
    assert "alice made a new invite link for dave; the old ones no longer work" in sentences(hub)
    # only the admin
    assert call(hub, "POST", "/api/invites", hub.tokens["bob"], {"person": "bob", "replace": True})[0] == 403


def test_add_a_machine_makes_an_invite_for_yourself_only(hub, lab):
    lab.who = "bob-gl"
    bob = token_of(sign_in(hub)[0])
    status, got = call(hub, "POST", "/api/me/invite", bob, {"person": "alice"})  # a name is ignored
    assert status == 200 and got["command"] == f"kaptyorka join {got['url']}"
    code = got["url"].rsplit("/", 1)[1]
    assert signin.invite_person(hub.store, code)["name"] == "bob"
    assert got["expires"] == hub.now[0] + 86400
    assert "bob made a link to add a machine" in sentences(hub)
    # a member still cannot invite anyone through the admin's route
    assert call(hub, "POST", "/api/invites", bob, {"person": "alice"})[0] == 403
    # at most 3 unused at once; a used one frees a place, and so does a day
    assert call(hub, "POST", "/api/me/invite", bob)[0] == 200
    assert call(hub, "POST", "/api/me/invite", bob)[0] == 200
    status, why = call(hub, "POST", "/api/me/invite", bob)
    assert status == 429 and "already have 3 unused links" in why
    assert redeem(hub, code)[0] == 200
    assert call(hub, "POST", "/api/me/invite", bob)[0] == 200
    assert call(hub, "POST", "/api/me/invite", bob)[0] == 429
    hub.now[0] += 86401
    assert call(hub, "POST", "/api/me/invite", bob)[0] == 200


def test_the_manager_account_cannot_add_a_machine(hub):
    os.unlink(paths.home() / "gitlab-oauth")  # off: its device token reaches the page
    _, mtoken = person(hub.store, "manager", "manager")
    assert call(hub, "POST", "/api/me/invite", mtoken)[0] == 403
    assert call(hub, "GET", "/api/me/account", mtoken)[1]["add_machine"] is False


def test_the_account_panel_holds_only_your_own_data(hub, lab):
    from kaptyorka.store import ONLINE_S

    hub.store.device_add(hub.ids["alice"], "alice@box-9")
    with hub.store.lock:
        hub.store.db.execute("UPDATE devices SET os_user='bob', host='box-1', last_used=? WHERE person_id=?",
                             (hub.now[0] - 5, hub.ids["bob"]))
    hub.store.device_add(hub.ids["bob"], "bob@box-2")
    for name in ("proj-a", "proj-b"):
        hub.store.project_register(name, by="alice")
    hub.store.grant_project("proj-a", hub.ids["bob"], "read", by="alice")
    lab.who = "bob-gl"
    bob = token_of(sign_in(hub)[0])
    status, acc = call(hub, "GET", "/api/me/account", bob)
    assert status == 200
    assert (acc["person"], acc["role"], acc["gitlab_user"], acc["add_machine"]) == ("bob", "member", "bob-gl", True)
    # his two machines, not alice's, and not this GitLab sign-in
    assert [(m["name"], m["os_user"], m["online"], m["current"]) for m in acc["machines"]] == [
        ("bob@box-1", "bob", True, False), ("bob@box-2", "", False, False)]
    # one name everywhere: account@computer, unless renamed; the name it joined with stays
    assert (acc["machines"][0]["machine"], acc["machines"][0]["label"], acc["machines"][0]["auto"]) == ("bob-laptop", "", "bob@box-1")
    assert acc["machines"][0]["last_seen"] == hub.now[0] - 5 and ONLINE_S > 5
    assert acc["projects"] == [{"name": "proj-a", "role": "read"}]
    assert [b["current"] for b in acc["browsers"]] == [True]
    assert set(acc) == {"person", "role", "gitlab_user", "machines", "team_machines", "browsers", "projects", "add_machine"}
    assert acc["team_machines"] == []
    assert "alice" not in json.dumps(acc)


def bob_signed_in(hub, lab):
    lab.who = "bob-gl"
    return token_of(sign_in(hub)[0])


def machine_ids(hub, pid):
    return {d["name"]: d["id"] for d in hub.store.devices(pid) if d["expires"] is None}


def test_rename_your_own_machine_only(hub, lab):
    bob = bob_signed_in(hub, lab)
    mine, alices = machine_ids(hub, hub.ids["bob"])["bob-laptop"], machine_ids(hub, hub.ids["alice"])["alice-laptop"]
    assert call(hub, "POST", f"/api/me/machines/{mine}/label", bob, {"label": "  work   box "}) == (200, {"name": "work box"})
    acc = call(hub, "GET", "/api/me/account", bob)[1]
    assert (acc["machines"][0]["name"], acc["machines"][0]["machine"]) == ("work box", "bob-laptop")
    # only the label changed: the machine name the bridge and sessions use stays
    assert hub.store.authenticate(hub.tokens["bob"]).device_name == "bob-laptop"
    assert "bob renamed their machine bob-laptop to work box" in sentences(hub)
    assert call(hub, "POST", f"/api/me/machines/{mine}/label", bob, {"label": "x" * 81})[0] == 400
    assert call(hub, "POST", f"/api/me/machines/{mine}/label", bob, {"label": ""}) == (200, {"name": "bob-laptop"})
    # someone else's machine reads as missing, and stays as it was
    assert call(hub, "POST", f"/api/me/machines/{alices}/label", bob, {"label": "mine now"}) == (404, "no such machine of yours")
    with hub.store.lock:
        assert hub.store.db.execute("SELECT label FROM devices WHERE id=?", (alices,)).fetchone()[0] == ""


def test_remove_your_own_machine_only(hub, lab):
    bob = bob_signed_in(hub, lab)
    box2 = hub.store.device_add(hub.ids["bob"], "bob@box-2")
    ids = machine_ids(hub, hub.ids["bob"])
    alices = machine_ids(hub, hub.ids["alice"])["alice-laptop"]
    assert call(hub, "POST", f"/api/me/machines/{alices}/remove", bob)[0] == 404
    assert hub.store.authenticate(hub.tokens["alice"]) is not None
    # a GitLab sign-in is not a machine to remove here
    session = [d["id"] for d in hub.store.devices(hub.ids["alice"]) if d["name"] == "gitlab-web"]
    assert call(hub, "POST", f"/api/me/machines/{ids['bob@box-2']}/remove", bob)[0] == 204
    assert hub.store.authenticate(box2) is None
    assert "bob turned off bob's device bob@box-2" in sentences(hub)
    assert [m["name"] for m in call(hub, "GET", "/api/me/account", bob)[1]["machines"]] == ["bob-laptop"]
    assert session == []
    # the admin token cannot remove the machine whose token opened the page
    admin = hub.store.device_add(hub.ids["alice"], "hub")
    (paths.home() / "admin-token").write_text(admin + "\n")
    hub_id = machine_ids(hub, hub.ids["alice"])["hub"]
    status, why = call(hub, "POST", f"/api/me/machines/{hub_id}/remove", admin)
    assert status == 400 and "signed in with that machine" in why
    assert hub.store.authenticate(admin) is not None


def test_the_hubs_emergency_device_cannot_be_removed_from_the_page(hub, lab):
    admin = hub.store.device_add(hub.ids["alice"], "hub")
    (paths.home() / "admin-token").write_text(admin + "\n")
    hub_id = machine_ids(hub, hub.ids["alice"])["hub"]
    lab.who = "alice-gl"
    alice = token_of(sign_in(hub)[0])  # a GitLab sign-in, not the hub's token
    machines = {m["name"]: m for m in call(hub, "GET", "/api/me/account", alice)[1]["machines"]}
    assert machines["hub"]["emergency"] is True and machines["alice-laptop"]["emergency"] is False
    status, why = call(hub, "POST", f"/api/me/machines/{hub_id}/remove", alice)
    assert (status, why) == (400, "this is the hub's own emergency sign-in; it can't be removed here")
    assert hub.store.authenticate(admin) is not None
    # her other machine still goes
    laptop = machine_ids(hub, hub.ids["alice"])["alice-laptop"]
    assert call(hub, "POST", f"/api/me/machines/{laptop}/remove", alice)[0] == 204
    # the page offers no Remove for it
    assert "!d.current && !d.emergency" in get(hub.url + "/").body


def test_sign_out_everywhere_else_ends_only_your_other_browsers(hub, lab):
    first, second = bob_signed_in(hub, lab), bob_signed_in(hub, lab)
    lab.who = "alice-gl"
    alices = token_of(sign_in(hub)[0])
    acc = call(hub, "GET", "/api/me/account", second)[1]
    assert sorted(b["current"] for b in acc["browsers"]) == [False, True]
    assert call(hub, "DELETE", "/api/me/browsers/others", second) == (200, {"ended": 1})
    assert me(hub, first)[0] == 401 and me(hub, second)[0] == 200
    assert me(hub, alices)[0] == 200  # nobody else's
    assert hub.store.authenticate(hub.tokens["bob"]) is not None  # machines stay
    assert "bob signed out their other browsers (1)" in sentences(hub)
    assert call(hub, "DELETE", "/api/me/browsers/others", second) == (200, {"ended": 0})
    # a device token reaches none of this with sign-in on
    for method, path in (("DELETE", "/api/me/browsers/others"), ("POST", "/api/me/machines/1/label"),
                         ("POST", "/api/me/machines/1/remove")):
        assert call(hub, method, path, hub.tokens["bob"], {"label": "x"})[0] == 403


def test_cli_page_on_a_member_machine_says_sign_in_with_gitlab(hub, capsys, monkeypatch):
    from kaptyorka import cli

    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.setattr(cli.sys, "platform", "linux")
    paths.save_config({"hub": hub.url, "token": hub.tokens["bob"]})
    assert cli._page(None) == 0
    out = capsys.readouterr().out
    assert out.strip() == f"open {hub.url} and sign in with GitLab" and hub.tokens["bob"] not in out
    # not linked yet: the link's Sign in with GitLab links them
    _, dave_t = person(hub.store, "dave")
    paths.save_config({"hub": hub.url, "token": dave_t})
    cli._page(None)
    assert f"{hub.url}/#t={dave_t}" in capsys.readouterr().out
    # the hub machine: the admin token's link, as before
    admin = hub.store.device_add(hub.ids["alice"], "hub")
    (paths.home() / "admin-token").write_text(admin + "\n")
    paths.save_config({"hub": hub.url, "token": admin})
    cli._page(None)
    assert f"{hub.url}/#t={admin}" in capsys.readouterr().out
    # the hub machine joined again with an invite: its config holds a member device, but the
    # admin token file still gives the emergency link
    _, rejoined = person(hub.store, "frank")
    paths.save_config({"hub": hub.url, "token": rejoined})
    cli._page(None)
    assert f"{hub.url}/#t={admin}" in capsys.readouterr().out
    # a stale admin token file is ignored
    (paths.home() / "admin-token").write_text("not-a-token\n")
    paths.save_config({"hub": hub.url, "token": hub.tokens["bob"]})
    cli._page(None)
    assert capsys.readouterr().out.strip() == f"open {hub.url} and sign in with GitLab"
    (paths.home() / "admin-token").unlink()
    # sign-in off: every machine's link opens the page
    os.unlink(paths.home() / "gitlab-oauth")
    paths.save_config({"hub": hub.url, "token": hub.tokens["bob"]})
    cli._page(None)
    assert f"{hub.url}/#t={hub.tokens['bob']}" in capsys.readouterr().out


def hub_key(hub, pid):
    """A hub key for person `pid`: its token in ~/.kaptyorka/admin-token, 0600."""
    key = hub.store.device_add(pid, "hub")
    p = paths.home() / "admin-token"
    p.write_text(key + "\n")
    os.chmod(p, 0o600)
    return key


def test_the_hub_key_keeps_admin_rights_after_its_person_is_demoted(hub):
    key = hub_key(hub, hub.ids["alice"])
    assert call(hub, "POST", "/api/people/bob/role", key, {"role": "admin"})[0] == 200
    assert call(hub, "POST", "/api/people/alice/role", hub.tokens["bob"], {"role": "member"}) == \
        (200, {"role": "member", "note": ""})
    assert hub.store.person_by_name("alice")["role"] == "member"
    # the emergency link still opens the page, with the admin views
    status, m = me(hub, key)
    assert status == 200 and m["page"] is True and m["role"] == "admin"
    for path in ("/api/me/account", "/api/log", "/api/people", "/api/invites"):
        assert call(hub, "GET", path, key)[0] == 200, path
    # her other device is a member's now
    assert call(hub, "GET", "/api/people", hub.tokens["alice"]) == (403, "admin only")
    assert me(hub, hub.tokens["alice"])[1]["role"] == "member"
    # and the key gives her role back
    assert call(hub, "POST", "/api/people/alice/role", key, {"role": "admin"})[0] == 200
    assert hub.store.person_by_name("alice")["role"] == "admin"


def test_the_hub_key_works_with_no_admin_at_all(hub):
    key = hub_key(hub, hub.ids["alice"])
    with hub.store.lock:
        hub.store.db.execute("UPDATE people SET role='member'")
    assert call(hub, "GET", "/api/people", hub.tokens["bob"]) == (403, "admin only")
    assert me(hub, key)[1]["page"] is True
    assert call(hub, "GET", "/api/people", key)[0] == 200
    assert call(hub, "POST", "/api/people/bob/role", key, {"role": "admin"})[0] == 200
    assert call(hub, "GET", "/api/people", hub.tokens["bob"])[0] == 200


def test_a_copied_hub_key_of_a_revoked_device_gets_nothing(hub):
    key = hub_key(hub, hub.ids["alice"])
    hub.store.device_revoke(hub.store.authenticate(key).device_id)
    for method, path in (("GET", "/api/me"), ("GET", "/api/people"), ("GET", "/api/log")):
        assert call(hub, method, path, key)[0] == 401, path


def test_the_last_gitlab_admin_stays(hub):
    from kaptyorka import admin_ops

    _, carol = person(hub.store, "carol")  # no GitLab link
    alice = hub.tokens["alice"]
    assert call(hub, "POST", "/api/people/carol/role", alice, {"role": "admin"})[0] == 200
    assert call(hub, "POST", "/api/people/alice/role", carol, {"role": "member"}) == \
        (400, "alice is the last admin who signs in with GitLab; make someone with GitLab an"
              " admin first")
    assert call(hub, "POST", "/api/people/carol/role", alice, {"role": "member"})[0] == 200
    # bob is linked: now alice may go
    assert call(hub, "POST", "/api/people/bob/role", alice, {"role": "admin"})[0] == 200
    assert call(hub, "POST", "/api/people/alice/role", alice, {"role": "member"})[0] == 200
    assert admin_ops.admins(hub.store) == ["bob"]


def own_hub(hub):
    """This machine runs the test hub: its hub.json, as kaptyorka start writes it."""
    (paths.home() / "hub.json").write_text(json.dumps({"port": urlparse(hub.url).port}))


def test_cli_people_role_with_the_hub_key_after_demotion(hub, capsys, monkeypatch):
    hub_key(hub, hub.ids["alice"])
    own_hub(hub)
    with hub.store.lock:
        hub.store.db.execute("UPDATE people SET role='member'")
    # not joined at all: the hub's own saved settings give its address
    assert not paths.config_path().exists()
    assert run("people", "role", "alice", "admin", "--hub-key") == 0
    assert "alice is now an admin" in capsys.readouterr().out
    assert hub.store.person_by_name("alice")["role"] == "admin"
    # joined with a member's token: no flag, no retry with the key
    with hub.store.lock:
        hub.store.db.execute("UPDATE people SET role='member'")
    paths.save_config({"hub": hub.url, "token": hub.tokens["bob"]})
    sent = []
    real = khttp.request
    monkeypatch.setattr(khttp, "request", lambda m, u, t, *a, **k: sent.append(t) or real(m, u, t, *a, **k))
    assert run("people", "role", "alice", "admin") == 1
    assert "only the admin can do that" in capsys.readouterr().err
    assert sent == [hub.tokens["bob"]]
    assert run("people", "role", "alice", "admin", "--hub-key") == 0
    assert hub.store.person_by_name("alice")["role"] == "admin"


def test_the_hub_key_is_never_sent_to_another_hub(hub, capsys, monkeypatch):
    key = hub_key(hub, hub.ids["alice"])
    own_hub(hub)
    sent = []
    monkeypatch.setattr(khttp, "request", lambda *a, **k: sent.append(a) or (200, {}))
    paths.save_config({"hub": "http://other.example:8765", "token": hub.tokens["bob"]})
    assert run("people", "role", "alice", "admin", "--hub-key") == 1
    assert "joined to http://other.example:8765, not to the hub it runs" in capsys.readouterr().err
    assert sent == []
    # no hub here at all, or no key file: refused too
    (paths.home() / "hub.json").unlink()
    paths.save_config({"hub": hub.url, "token": hub.tokens["bob"]})
    monkeypatch.setattr("kaptyorka.cli._installed_hub_port", lambda: {})
    assert run("people", "role", "alice", "admin", "--hub-key") == 1
    own_hub(hub)
    (paths.home() / "admin-token").unlink()
    assert run("people", "role", "alice", "admin", "--hub-key") == 1
    assert "no hub key" in capsys.readouterr().err
    assert sent == [] and key


def test_the_manager_runs_through_the_hub_keys_bridge(hub):
    key = hub_key(hub, hub.ids["alice"])
    dev = hub.store.authenticate(key).device_id
    with hub.store.lock:
        hub.store.db.execute("UPDATE devices SET host='box-1', secret_hash='x' WHERE id=?", (dev,))
        hub.store.db.execute("UPDATE people SET role='member' WHERE id=?", (hub.ids["alice"],))
    mm = ManagerStore(hub.store)
    assert mm.admin_device("box-1")["id"] == dev
    (paths.home() / "admin-token").unlink()
    assert mm.admin_device("box-1") is None


def run(*args):
    from unittest import mock

    from kaptyorka import cli

    with mock.patch.object(cli.sys, "argv", ["kaptyorka", *args]):
        try:
            cli.entry()
        except SystemExit as e:
            return e.code


def test_the_hub_key_works_once_its_person_is_a_team_machine(hub, capsys):
    svc = hub.store.person_create("svc")
    key, _ = hub.store.invite_redeem(hub.store.invite_create(svc), "svc@box-1", host="box-1",
                                     os_user="svc")
    p = paths.home() / "admin-token"
    p.write_text(key + "\n")
    os.chmod(p, 0o600)
    paths.save_config({"hub": hub.url, "token": hub.tokens["alice"]})  # from another machine
    assert run("machine", "team", "--from", "bob") == 1  # bob signs in with GitLab
    assert "bob signs in with GitLab, so is a person" in capsys.readouterr().err
    paths.save_config({"hub": hub.url, "token": key})
    assert run("machine", "team", "--from", "svc") == 0
    assert "svc is now a team machine" in capsys.readouterr().out
    assert hub.store.is_team(svc)
    with hub.store.lock:
        hub.store.db.execute("UPDATE people SET role='member'")
    status, m = me(hub, key)
    assert status == 200 and m["page"] is True and m["role"] == "admin"
    assert call(hub, "GET", "/api/team/machines", key)[0] == 200
    assert run("people", "role", "alice", "admin") == 0
    assert hub.store.person_by_name("alice")["role"] == "admin"
    assert run("machine", "team", "--undo", "svc") == 0  # and back
    assert "svc is a person again (member)" in capsys.readouterr().out
    assert not hub.store.is_team(svc)
    assert run("machine", "team", "--undo", "svc") == 1
    assert "svc is not a team machine" in capsys.readouterr().err
    assert run("machine", "team", "x", "--undo", "svc") == 1
