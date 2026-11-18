"""The team's GitLab group as a read-only list of projects (name, description, repo address).
GitLab never decides access: the hub does. The token lives in ~/.kaptyorka/gitlab-token on the
hub machine and never leaves this module: not in a log line, an error, or a page answer."""
import http.client
import json
import os
import stat
import threading
import time
import urllib.error
import urllib.request
from typing import List, Optional, Tuple
from urllib.parse import quote, urlparse

from kaptyorka import paths
from kaptyorka.store import MAX_ABOUT, KaptyorkaError

TOKEN_FILE = "gitlab-token"
URL_KEY, GROUP_KEY = "gitlab_url", "gitlab_group"  # rows in the hub's settings table
CACHE_S = 300  # a good list is kept this long
ERROR_S = 30  # a failure is kept this long, so a refreshing page does not hammer GitLab
MAX_PAGES = 50  # 5,000 projects; a group past that is cut, not looped over for ever
TIMEOUT_S = 10


class GitLabError(KaptyorkaError):
    """One plain sentence for the page and the command line. Never holds the token."""


def token_path():
    return paths.home() / TOKEN_FILE


def shown(p) -> str:
    """A path as people read it: ~ for the home folder."""
    home, p = os.path.expanduser("~"), str(p)
    return "~" + p[len(home):] if home != "/" and p.startswith(home + os.sep) else p


def read_token() -> str:
    p = shown(token_path())
    try:
        st = os.stat(token_path())
    except FileNotFoundError:
        raise GitLabError(f"no GitLab token: put a read_api token in {p}, then chmod 600 it") from None
    except OSError:
        raise GitLabError(f"cannot read the GitLab token file {p}") from None
    if not stat.S_ISREG(st.st_mode):
        raise GitLabError(f"the GitLab token file {p} is not a plain file")
    if st.st_mode & 0o077:
        raise GitLabError(f"the GitLab token file {p} can be opened by other users, so it is"
                          f" not used; run: chmod 600 {p}")
    try:
        token = token_path().read_text().strip()
    except (OSError, UnicodeDecodeError):
        raise GitLabError(f"cannot read the GitLab token file {p}") from None
    if not token:
        raise GitLabError(f"the GitLab token file {p} is empty")
    return token


def check_url(url: str) -> str:
    url = url.strip().rstrip("/")
    try:
        u = urlparse(url)
    except ValueError:
        u = None
    if u is None or u.scheme not in ("http", "https") or not u.hostname or u.query or u.fragment \
            or u.username or u.password:
        raise GitLabError("the GitLab address must look like https://gitlab.example")
    return url


def check_group(group: str) -> str:
    group = group.strip().strip("/")
    if not group or len(group) > 255 or any(c.isspace() for c in group):
        raise GitLabError("the GitLab group must be its path, like team or team/sub")
    return group


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect would carry the token to wherever it points: refuse it instead."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise GitLabError(f"GitLab answered with a redirect to {newurl.split('?')[0]};"
                          " set that address with kaptyorka gitlab set")


_opener = urllib.request.build_opener(_NoRedirect)


def _get(url: str, token: str, timeout: float):
    req = urllib.request.Request(url, headers={"PRIVATE-TOKEN": token, "Accept": "application/json"})
    with _opener.open(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace") or "null"), r.headers.get("X-Next-Page", "")


def fetch(base: str, group: str, token: str, timeout: float = TIMEOUT_S) -> List[dict]:
    """Every project of the group and its subgroups: [{name, description, repo, path}], where
    name is the GitLab `path` (a clone's folder name) and path the full namespaced path."""
    out, page = [], "1"
    host = urlparse(base).hostname
    for _ in range(MAX_PAGES):
        url = (f"{base}/api/v4/groups/{quote(group, safe='')}/projects"
               f"?include_subgroups=true&per_page=100&page={quote(page, safe='')}")
        try:
            rows, page = _get(url, token, timeout)
        except urllib.error.HTTPError as e:
            if e.code == 401:
                raise GitLabError("GitLab refused the token (401): it is wrong or expired;"
                                  f" put a new one in {shown(token_path())}") from None
            if e.code == 403:
                raise GitLabError("GitLab says the token may not read the group (403);"
                                  " it needs the read_api scope") from None
            if e.code == 404:
                raise GitLabError(f"GitLab has no group {group}, or the token cannot see it") from None
            raise GitLabError(f"GitLab answered with an error ({e.code})") from None
        except GitLabError:
            raise
        except (urllib.error.URLError, OSError, http.client.HTTPException):
            raise GitLabError(f"cannot reach GitLab at {host}") from None
        except ValueError:
            raise GitLabError("GitLab sent something that is not a project list") from None
        if not isinstance(rows, list):
            raise GitLabError("GitLab sent something that is not a project list")
        for r in rows:
            if not isinstance(r, dict) or not r.get("path"):
                continue
            out.append({"name": str(r["path"]),
                        "description": str(r.get("description") or "").strip()[:MAX_ABOUT],
                        "repo": str(r.get("http_url_to_repo") or r.get("web_url") or ""),
                        "path": str(r.get("path_with_namespace") or r["path"])})
        page = (page or "").strip()
        if not page:
            break
    return sorted(out, key=lambda p: (p["name"], p["path"]))


def settings(store) -> Tuple[Optional[str], Optional[str]]:
    with store.lock:
        rows = dict(store.db.execute("SELECT key, value FROM settings WHERE key IN (?, ?)",
                                     (URL_KEY, GROUP_KEY)).fetchall())
    return rows.get(URL_KEY), rows.get(GROUP_KEY)


class GitLabSource:
    """The group's project list, cached for a few minutes, for the admin's page."""

    def __init__(self, store, clock=time.monotonic):
        self.store, self.clock = store, clock
        self.lock = threading.Lock()  # one fetch at a time; others wait and take its answer
        self._cache = None  # (key, until, projects, error)

    def set(self, url: str, group: str, by: str) -> None:
        url, group = check_url(url), check_group(group)
        with self.store.lock:
            for k, v in ((URL_KEY, url), (GROUP_KEY, group)):
                self.store.db.execute("INSERT INTO settings (key, value) VALUES (?, ?)"
                                      " ON CONFLICT(key) DO UPDATE SET value=excluded.value", (k, v))
        self._cache = None
        self.store.log_add(by, "gitlab", f"{url} {group}")

    def off(self, by: str) -> None:
        with self.store.lock:
            self.store.db.execute("DELETE FROM settings WHERE key IN (?, ?)", (URL_KEY, GROUP_KEY))
        self._cache = None
        self.store.log_add(by, "gitlab", "off")

    def projects(self, fresh: bool = False) -> dict:
        """{"on", "url", "group", "projects": [...], "error": one sentence or None}."""
        url, group = settings(self.store)
        if not url or not group:
            return {"on": False, "url": None, "group": None, "projects": [], "error": None}
        key = (url, group)
        with self.lock:
            c = self._cache
            if fresh or c is None or c[0] != key or c[1] <= self.clock():
                try:
                    found, error = fetch(url, group, read_token()), None
                except GitLabError as e:
                    found, error = [], str(e)
                c = self._cache = (key, self.clock() + (ERROR_S if error else CACHE_S), found, error)
        return {"on": True, "url": url, "group": group, "projects": c[2], "error": c[3]}

    def status(self) -> dict:
        """For `kaptyorka gitlab status`: the settings, the token file's state, and a fresh count."""
        out = self.projects(fresh=True)
        try:
            read_token()
            token = "ok"
        except GitLabError as e:
            token = str(e)
        return {"on": out["on"], "url": out["url"], "group": out["group"],
                "token_file": shown(token_path()), "token": token,
                "count": len(out["projects"]) if out["on"] and not out["error"] else None,
                "error": out["error"]}
