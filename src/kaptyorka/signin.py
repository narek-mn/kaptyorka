"""Sign in to the hub page with GitLab: the OAuth 2 authorization code flow with PKCE against
the team's own GitLab (the `gitlab_url` setting), scope read_user.

A person is linked to a GitLab user by the admin, or links themselves: from their invite link
(while it has not expired, used by join or not) or from their own page (signed in with a device
token). Either way only someone who already holds that person's hub credential can link, only
while the person is unlinked, and the first link wins. After the callback the hub asks GitLab who
signed in (GET /api/v4/user); a linked, active user gets a page device of their own person, `gitlab-web`, whose token the page keeps like a `#t=` link. That device ends after
SESSION_S, and every CHECK_S the hub uses the sign-in's refresh token to ask GitLab again: a
refused refresh, a user GitLab no longer lets in, or a changed link signs the device out. While
GitLab cannot be reached the device keeps working until it ends.

The app id and secret are read from ~/.kaptyorka/gitlab-oauth (line 1 the id, line 2 the
secret), refused when other users can open it. The secret, the codes and the tokens never go in
a log line, an error or an answer.
"""
import base64
import hashlib
import http.client
import json
import os
import re
import secrets
import stat
import threading
import urllib.error
import urllib.request
from typing import Dict, Optional, Tuple
from urllib.parse import urlencode

from kaptyorka import paths
from kaptyorka.gitlab import settings, shown
from kaptyorka.store import MANAGER_ACCOUNT, KaptyorkaError, Principal

APP_FILE = "gitlab-oauth"
CALLBACK = "/auth/gitlab/callback"
SCOPE = "read_user"
DEVICE = "gitlab-web"  # the page device a GitLab sign-in makes
STATE_S = 600  # a sign-in must come back from GitLab within this
SESSION_S = 7 * 86400  # then the page asks to sign in again
CHECK_S = 900  # how often GitLab is asked again whether the person may still come in
TIMEOUT_S = 10
COOKIE = "kaptyorka_signin"
USERNAME = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,254}$")


def ensure_schema(db) -> None:
    db.execute("CREATE TABLE IF NOT EXISTS gitlab_sessions ("
               " device_id INTEGER PRIMARY KEY, person_id INTEGER NOT NULL,"
               " gitlab_id INTEGER NOT NULL, refresh TEXT NOT NULL, checked REAL NOT NULL)")


class SignInError(KaptyorkaError):
    """One plain sentence for the person signing in. Never holds a secret, code or token."""


class _Refused(Exception):
    """GitLab said no (a 4xx): the grant or the user is no longer good."""


class _Unreachable(Exception):
    """GitLab did not answer, or answered with a server error: try again later."""


def app_path():
    return paths.home() / APP_FILE


def read_app() -> Tuple[str, str]:
    """(application id, secret) from ~/.kaptyorka/gitlab-oauth."""
    p = shown(app_path())
    try:
        st = os.stat(app_path())
    except FileNotFoundError:
        raise SignInError(f"no GitLab app for sign-in: put its id and secret in {p}"
                          f" (two lines), then chmod 600 it") from None
    except OSError:
        raise SignInError(f"cannot read the GitLab app file {p}") from None
    if not stat.S_ISREG(st.st_mode):
        raise SignInError(f"the GitLab app file {p} is not a plain file")
    if st.st_mode & 0o077:
        raise SignInError(f"the GitLab app file {p} can be opened by other users, so it is"
                          f" not used; run: chmod 600 {p}")
    try:
        lines = [line.strip() for line in app_path().read_text().splitlines()]
    except (OSError, UnicodeDecodeError):
        raise SignInError(f"cannot read the GitLab app file {p}") from None
    lines = [line for line in lines if line]
    if len(lines) != 2:
        raise SignInError(f"the GitLab app file {p} must have two lines: the application id,"
                          " then the secret")
    return lines[0], lines[1]


def status(store) -> Optional[str]:
    """None when sign-in with GitLab is ready, else why not (one sentence)."""
    url, _ = settings(store)
    if not url:
        return "no GitLab address: kaptyorka gitlab set <url> <group>"
    try:
        read_app()
    except SignInError as e:
        return str(e)
    return None


def check_username(username: str) -> str:
    username = username.strip().lstrip("@")
    if not USERNAME.match(username):
        raise SignInError("a GitLab username uses letters, digits, '_', '.' and '-'")
    return username


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect would carry the secret or a token to wherever it points: refuse it."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise _Unreachable()


_opener = urllib.request.build_opener(_NoRedirect)


def _call(req) -> dict:
    """GitLab's JSON answer; _Refused on a 4xx, _Unreachable otherwise. No body is ever kept."""
    try:
        with _opener.open(req, timeout=TIMEOUT_S) as r:
            got = json.loads(r.read().decode("utf-8", "replace") or "null")
    except urllib.error.HTTPError as e:
        raise (_Refused() if 400 <= e.code < 500 else _Unreachable()) from None
    except (urllib.error.URLError, OSError, http.client.HTTPException, ValueError):
        raise _Unreachable() from None
    if not isinstance(got, dict):
        raise _Unreachable()
    return got


def _token(base: str, form: dict) -> dict:
    """POST /oauth/token: {access_token, refresh_token, ...}."""
    req = urllib.request.Request(f"{base}/oauth/token", data=urlencode(form).encode(),
                                 headers={"Accept": "application/json",
                                          "Content-Type": "application/x-www-form-urlencoded"})
    got = _call(req)
    if not isinstance(got.get("access_token"), str) or not isinstance(got.get("refresh_token"), str):
        raise _Unreachable()
    return got


def _user(base: str, access: str) -> dict:
    """GET /api/v4/user with the person's own token: {id, username, state}."""
    req = urllib.request.Request(f"{base}/api/v4/user", headers={
        "Authorization": f"Bearer {access}", "Accept": "application/json"})
    got = _call(req)
    if not isinstance(got.get("id"), int) or not isinstance(got.get("username"), str):
        raise _Unreachable()
    return got


def _active(user: dict) -> bool:
    return user.get("state", "active") == "active" and not user.get("locked")


class SignIn:
    """The pending sign-ins (in memory: a hub restart only asks the person to click again) and
    the GitLab sessions (in the database)."""

    def __init__(self, store, clock=None):
        self.store, self.clock = store, clock or store.clock
        self.lock = threading.Lock()
        # state -> (verifier, redirect, at, bind); bind is None for a plain sign-in, or what the
        # sign-in links: ("invite", code) or ("page", person id). It never leaves the hub.
        self.pending: Dict[str, Tuple[str, str, float, Optional[Tuple[str, object]]]] = {}

    def start(self, redirect_uri: str, bind: Optional[Tuple[str, object]] = None) -> Tuple[str, str]:
        """(the GitLab address to send the browser to, the state for its cookie)."""
        url, _ = settings(self.store)
        why = status(self.store)
        if why:
            raise SignInError(f"sign-in with GitLab is not set up on this hub ({why})")
        client_id, _ = read_app()
        state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(64)
        now = self.clock()
        with self.lock:
            self.pending = {k: v for k, v in self.pending.items() if now - v[2] < STATE_S}
            if len(self.pending) > 1000:  # nobody signs in this much: drop the oldest
                for k in sorted(self.pending, key=lambda k: self.pending[k][2])[:500]:
                    del self.pending[k]
            self.pending[state] = (verifier, redirect_uri, now, bind)
        q = urlencode({"client_id": client_id, "redirect_uri": redirect_uri,
                       "response_type": "code", "scope": SCOPE, "state": state,
                       "code_challenge": _b64(hashlib.sha256(verifier.encode()).digest()),
                       "code_challenge_method": "S256"})
        return f"{url}/oauth/authorize?{q}", state

    def finish(self, state: str, code: str, cookie: str) -> Tuple[str, str]:
        """Back from GitLab: (a new page token, the person's name)."""
        with self.lock:
            got = self.pending.pop(state, None) if state else None
        if got is None or not cookie or not secrets.compare_digest(cookie, state) \
                or self.clock() - got[2] >= STATE_S:
            raise SignInError("this sign-in is old or was already used; go back and sign in again")
        if not code:
            raise SignInError("GitLab sent no sign-in code; go back and sign in again")
        verifier, redirect_uri, _, bind = got
        url, _ = settings(self.store)
        why = status(self.store)
        if why:
            raise SignInError(f"sign-in with GitLab is not set up on this hub ({why})")
        client_id, secret = read_app()
        try:
            tokens = _token(url, {"grant_type": "authorization_code", "code": code,
                                  "redirect_uri": redirect_uri, "client_id": client_id,
                                  "client_secret": secret, "code_verifier": verifier})
            user = _user(url, tokens["access_token"])
        except _Refused:
            raise SignInError("GitLab refused the sign-in; try again, and if it keeps failing ask"
                              " the admin to check the GitLab app") from None
        except _Unreachable:
            raise SignInError("GitLab did not answer; try again in a minute") from None
        if not _active(user):
            raise SignInError("your GitLab account is not active, so you cannot sign in here")
        if bind is not None:
            self._link(user, bind)
        return self._open(user, tokens["refresh_token"])

    def _link(self, user: dict, bind: Tuple[str, object]) -> None:
        """Link the bound person to this GitLab user, unless they are linked already (to this
        user: nothing to do, the sign-in goes on; to another: refused)."""
        how, key = bind
        with self.store.lock:
            if how == "invite":
                inv = self.store.db.execute("SELECT person_id, expires FROM invites WHERE code=?",
                                            (key,)).fetchone()
                if inv is None or inv["expires"] <= self.clock():
                    raise SignInError("this invite link is no longer valid; ask the admin for a new one")
                pid = inv["person_id"]
            else:
                pid = key
            row = self.store.db.execute("SELECT * FROM people WHERE id=? AND removed_at IS NULL",
                                        (pid,)).fetchone()
            if row is None:
                raise SignInError("you are no longer on this hub; ask the admin")
            if (row["name"], row["role"]) == (MANAGER_ACCOUNT, "manager"):
                raise SignInError("the manager account cannot sign in with GitLab")
            if row["gitlab_user"]:
                if row["gitlab_user"].lower() == user["username"].lower() \
                        and row["gitlab_id"] in (None, user["id"]):
                    return  # already this user: a plain sign-in
                raise SignInError("you are already linked to another GitLab account;"
                                  " sign in with that one")
            other = self.store.db.execute(
                "SELECT 1 FROM people WHERE removed_at IS NULL AND id<>? AND (gitlab_id=?"
                " OR lower(gitlab_user)=lower(?))", (pid, user["id"], user["username"])).fetchone()
            if other is not None:
                raise SignInError(f"the GitLab user {user['username']} is already linked to someone"
                                  " else on this hub; ask the admin")
            self.store.db.execute("UPDATE people SET gitlab_user=?, gitlab_id=? WHERE id=?"
                                  " AND gitlab_user IS NULL", (user["username"], user["id"], pid))
        where = "the invite" if how == "invite" else "their page"
        self.store.log_add(row["name"], "gitlab_link", f"{user['username']} (from {where})")

    def _open(self, user: dict, refresh: str) -> Tuple[str, str]:
        name, now = user["username"], self.clock()
        token = secrets.token_urlsafe(32)
        with self.store.lock:
            row = self.store.db.execute(
                "SELECT id, name, gitlab_id FROM people WHERE removed_at IS NULL"
                " AND lower(gitlab_user)=lower(?)", (name,)).fetchone()
            if row is None:
                raise SignInError(f"nobody on this hub is linked to the GitLab user {name};"
                                  " ask the admin to add you")
            if row["gitlab_id"] is not None and row["gitlab_id"] != user["id"]:
                # the username now belongs to another GitLab account
                raise SignInError(f"the GitLab user {name} is not the account the admin linked;"
                                  " ask the admin to link you again")
            self.store.db.execute("UPDATE people SET gitlab_id=? WHERE id=?", (user["id"], row["id"]))
            dev = self.store.db.execute(
                "INSERT INTO devices (person_id, name, token_hash, created, expires)"
                " VALUES (?,?,?,?,?)", (row["id"], DEVICE, hashlib.sha256(token.encode()).hexdigest(),
                                        now, now + SESSION_S)).lastrowid
            self.store.db.execute("INSERT INTO gitlab_sessions (device_id, person_id, gitlab_id,"
                                  " refresh, checked) VALUES (?,?,?,?,?)",
                                  (dev, row["id"], user["id"], refresh, now))
        self.store.log_add(row["name"], "gitlab_signin", name)
        return token, row["name"]

    def _end(self, device_id: int, why: str) -> None:
        with self.store.lock:
            row = self.store.db.execute("SELECT p.name FROM devices d JOIN people p"
                                        " ON p.id=d.person_id WHERE d.id=?", (device_id,)).fetchone()
            self.store.db.execute("DELETE FROM gitlab_sessions WHERE device_id=?", (device_id,))
            self.store.db.execute("UPDATE devices SET revoked_at=? WHERE id=? AND revoked_at IS NULL",
                                  (self.clock(), device_id))
        if row is not None:
            self.store.log_add(row["name"], "gitlab_signout", why)

    def end_person(self, person_id: int, why: str) -> None:
        with self.store.lock:
            ids = [r["device_id"] for r in self.store.db.execute(
                "SELECT device_id FROM gitlab_sessions WHERE person_id=?", (person_id,))]
        for d in ids:
            self._end(d, why)

    def signout(self, principal: Principal) -> bool:
        """The page's Sign out: a GitLab page device ends at once; other devices stay."""
        with self.store.lock:
            mine = self.store.db.execute("SELECT 1 FROM gitlab_sessions WHERE device_id=?",
                                         (principal.device_id,)).fetchone()
        if mine is None:
            return False
        self._end(principal.device_id, "signed out")
        return True

    def recheck(self, force: bool = False) -> int:
        """Ask GitLab again about each sign-in due a check; end those it no longer lets in and
        those past their time. Returns how many ended. Runs off the event loop."""
        now = self.clock()
        with self.store.lock:
            rows = [dict(r) for r in self.store.db.execute(
                "SELECT s.*, d.revoked_at, d.expires, p.gitlab_user, p.gitlab_id AS linked_id,"
                " p.removed_at FROM gitlab_sessions s JOIN devices d ON d.id=s.device_id"
                " JOIN people p ON p.id=s.person_id")]
        ended = 0
        for r in rows:
            if r["revoked_at"] is not None or r["removed_at"] is not None \
                    or (r["expires"] is not None and r["expires"] <= now):
                self._end(r["device_id"], "ended")
                ended += 1
                continue
            if not force and now - r["checked"] < CHECK_S:
                continue
            if not r["gitlab_user"] or r["linked_id"] != r["gitlab_id"]:
                self._end(r["device_id"], "link changed")
                ended += 1
                continue
            url, _ = settings(self.store)
            try:
                client_id, secret = read_app()
            except SignInError:
                continue  # the app file is broken: the admin fixes it; sessions still end in time
            if not url:
                continue
            try:
                tokens = _token(url, {"grant_type": "refresh_token", "refresh_token": r["refresh"],
                                      "client_id": client_id, "client_secret": secret})
            except _Refused:
                self._end(r["device_id"], "GitLab refused")
                ended += 1
                continue
            except _Unreachable:
                continue
            with self.store.lock:  # GitLab replaced the refresh token: keep the new one at once
                self.store.db.execute("UPDATE gitlab_sessions SET refresh=? WHERE device_id=?",
                                      (tokens["refresh_token"], r["device_id"]))
            try:
                user = _user(url, tokens["access_token"])
            except _Refused:
                self._end(r["device_id"], "GitLab refused")
                ended += 1
                continue
            except _Unreachable:
                continue
            if user["id"] != r["gitlab_id"] or user["username"].lower() != r["gitlab_user"].lower() \
                    or not _active(user):
                self._end(r["device_id"], "GitLab user no longer allowed")
                ended += 1
                continue
            with self.store.lock:
                self.store.db.execute("UPDATE gitlab_sessions SET checked=? WHERE device_id=?",
                                      (now, r["device_id"]))
        return ended


def set_user(store, si: Optional[SignIn], name: str, username: str, by: Principal) -> Optional[str]:
    """The admin links a person to a GitLab username ("" unlinks). A change ends that person's
    GitLab sign-ins. Returns the username kept, or None."""
    if not by.is_admin():
        raise KaptyorkaError("admin only")
    username = check_username(username) if username.strip() else ""
    with store.lock:
        row = store.db.execute("SELECT * FROM people WHERE name=? AND removed_at IS NULL",
                               (name.strip(),)).fetchone()
        if row is None:
            raise KaptyorkaError(f"no person named {name.strip()}")
        if (row["name"], row["role"]) == (MANAGER_ACCOUNT, "manager"):
            raise KaptyorkaError("the manager account cannot sign in with GitLab")
        if username:
            other = store.db.execute(
                "SELECT name FROM people WHERE removed_at IS NULL AND id<>?"
                " AND lower(gitlab_user)=lower(?)", (row["id"], username)).fetchone()
            if other is not None:
                raise KaptyorkaError(f"{other['name']} is already linked to the GitLab user {username}")
        same = (row["gitlab_user"] or "").lower() == username.lower()
        store.db.execute("UPDATE people SET gitlab_user=?, gitlab_id=? WHERE id=?",
                         (username or None, row["gitlab_id"] if same else None, row["id"]))
    if not same:
        if si is not None:
            si.end_person(row["id"], "link changed")
        store.log_add(by.person_name, "gitlab_user",
                      f"{row['name']} -> {username}" if username else f"{row['name']} unlinked")
    return username or None


def invite_person(store, code: str) -> Optional[dict]:
    """For the invite page: the invite's person while it has not expired, used or not, with
    whether it is used and their GitLab link. None for an unknown or expired code."""
    with store.lock:
        row = store.db.execute(
            "SELECT p.id, p.name, p.role, p.gitlab_user, i.used_at FROM invites i JOIN people p"
            " ON p.id=i.person_id WHERE i.code=? AND i.expires>? AND p.removed_at IS NULL",
            (code, store.clock())).fetchone()
    return dict(row) if row is not None else None


def is_session(store, device_id: int) -> bool:
    """True for a GitLab sign-in's page device (not just any device named gitlab-web)."""
    with store.lock:
        return store.db.execute("SELECT 1 FROM gitlab_sessions WHERE device_id=?",
                                (device_id,)).fetchone() is not None


def can_connect(store, principal: Principal) -> bool:
    """The page's Connect GitLab: a person on their own device token, not linked yet, on a hub
    where sign-in is set up."""
    if principal.shared or principal.legacy or principal.device_name == DEVICE \
            or (principal.person_name, principal.role) == (MANAGER_ACCOUNT, "manager"):
        return False
    with store.lock:
        row = store.db.execute("SELECT gitlab_user FROM people WHERE id=?",
                               (principal.person_id,)).fetchone()
    return row is not None and not row["gitlab_user"] and status(store) is None


def check_free(store, username: str) -> str:
    """For Add person: the username is well formed and nobody has it yet."""
    username = check_username(username)
    with store.lock:
        other = store.db.execute("SELECT name FROM people WHERE removed_at IS NULL"
                                 " AND lower(gitlab_user)=lower(?)", (username,)).fetchone()
    if other is not None:
        raise KaptyorkaError(f"{other['name']} is already linked to the GitLab user {username}")
    return username

