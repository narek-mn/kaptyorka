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
                                    "person_id": hub.ids["alice"]})
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


def test_cli_people_gitlab(hub, capsys):
    from test_cli import entry_with_exit

    paths.save_config({"hub": hub.url, "token": hub.tokens["alice"], "machine": "box-1"})
    assert entry_with_exit(["people", "gitlab", "bob", "bob-new"]) == 0
    assert capsys.readouterr().out.strip() == "bob signs in with the GitLab user bob-new"
    assert entry_with_exit(["people", "gitlab", "bob", ""]) == 0
    assert capsys.readouterr().out.strip() == "bob is no longer linked to GitLab"
    assert entry_with_exit(["people", "gitlab", "nobody", "x"]) == 1
    assert capsys.readouterr().err.strip() == "no person named nobody"
