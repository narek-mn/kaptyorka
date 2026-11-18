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
                                    "person_id": hub.ids["alice"], "gitlab_connect": False})
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
    assert khttp.request("POST", hub.url + "/api/me/signout", hub.tokens["alice"])[0] == 204
    assert me(hub, hub.tokens["alice"])[0] == 200  # a device token stays good


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
    assert me(hub, hub.tokens["alice"])[1]["role"] == "admin"  # the #t= link's token
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
                                 "person_id": hub.store.person_by_name("dave")["id"], "gitlab_connect": False}
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
