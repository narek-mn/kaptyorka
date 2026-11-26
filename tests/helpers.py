import asyncio
from typing import Any

import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

run = asyncio.run


def _headers(hub, ident: dict) -> dict:
    h = {"Authorization": f"Bearer {hub.token}"}
    h.update({f"X-Kaptyorka-{k.capitalize()}": v for k, v in ident.items()})
    return h


async def _client(hub, ident: dict, fn):
    async with httpx2.AsyncClient(headers=_headers(hub, ident)) as http_client:
        async with Client(streamable_http_client(hub.url + "/mcp", http_client=http_client)) as c:
            return await fn(c)


async def call_result(hub, ident: dict, tool: str, **args):
    return await _client(hub, ident, lambda c: c.call_tool(tool, args))


async def call(hub, ident: dict, tool: str, **args) -> Any:
    r = await call_result(hub, ident, tool, **args)
    if r.is_error:
        raise RuntimeError(r.content[0].text)
    return r.structured_content["result"]


def call_error(hub, ident: dict, tool: str, **args) -> str:
    r = run(call_result(hub, ident, tool, **args))
    assert r.is_error
    return r.content[0].text


def tool_names(hub) -> set:
    r = run(_client(hub, {}, lambda c: c.list_tools()))
    return {t.name for t in r.tools}


def write_session_file(home, pid, cwd="/w/proj-a", status="idle", version="2.1.318",
                       sock=None, peer_token=None, name=None, name_source=None, session_id=None, extra=None):
    """A Claude Code session file (and its .key file when peer_token is given)."""
    import json

    d = home / ".claude" / "sessions"
    d.mkdir(parents=True, exist_ok=True)
    data = {"pid": pid, "sessionId": session_id or f"sid-{pid}", "cwd": cwd, "status": status, "version": version}
    if name is not None:
        data["name"], data["nameSource"] = name, name_source
    if sock is not None:
        data["messagingSocketPath"] = str(sock)
    data.update(extra or {})
    (d / f"{pid}.json").write_text(json.dumps(data))
    if peer_token is not None:
        (d / f"{pid}.ab12.key").write_text(json.dumps({"peerToken": peer_token}))


def person(store, name: str, role: str = "member"):
    """Insert a person with one device straight into the store; return (person_id, token)."""
    import hashlib
    import secrets

    token = secrets.token_urlsafe(32)
    now = store.clock()
    with store.lock:
        pid = store.db.execute("INSERT INTO people (name, role, created) VALUES (?,?,?)",
                               (name, role, now)).lastrowid
        store.db.execute("INSERT INTO devices (person_id, name, token_hash, created)"
                         " VALUES (?,?,?,?)",
                         (pid, f"{name}-laptop", hashlib.sha256(token.encode()).hexdigest(), now))
    return pid, token


def old_knock(store, from_key: str, to_person: int, about: str) -> int:
    """A knock a member sent before 0.9.0 (only the admin knocks now), still pending; its id."""
    with store.lock:
        frm = store.db.execute("SELECT person_id FROM agents WHERE key=?", (from_key,)).fetchone()
        return store.db.execute(
            "INSERT INTO knocks (from_key, from_person, to_key, to_person, about, created)"
            " VALUES (?,?,NULL,?,?,?)",
            (from_key, frm["person_id"], to_person, about, store.clock())).lastrowid


class FakeGitLab:
    """A GitLab REST v4 stand-in on 127.0.0.1: GET /api/v4/groups/<group>/projects with
    pagination by X-Next-Page. `projects` is the group's list, `status` a forced answer code,
    `redirect` a URL to send every request to instead; `seen` keeps (path, PRIVATE-TOKEN)."""

    def __init__(self, projects=(), token="glpat-test-token"):
        import http.server
        import json
        import threading

        self.projects, self.token, self.status, self.redirect, self.seen = list(projects), token, None, None, []
        fake = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                from urllib.parse import parse_qs, urlparse

                fake.seen.append((self.path, self.headers.get("PRIVATE-TOKEN")))
                if fake.redirect:
                    self.send_response(302)
                    self.send_header("Location", fake.redirect)
                    self.end_headers()
                    return
                u = urlparse(self.path)
                if fake.status or self.headers.get("PRIVATE-TOKEN") != fake.token:
                    self.send_response(fake.status or 401)
                    self.end_headers()
                    self.wfile.write(b'{"message":"401 Unauthorized"}')
                    return
                if not u.path.startswith("/api/v4/groups/") or not u.path.endswith("/projects"):
                    self.send_response(404)
                    self.end_headers()
                    return
                q = parse_qs(u.query)
                page, per = int(q.get("page", ["1"])[0]), int(q.get("per_page", ["20"])[0])
                rows = fake.projects[(page - 1) * per:page * per]
                body = json.dumps(rows).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("X-Next-Page", str(page + 1) if page * per < len(fake.projects) else "")
                self.end_headers()
                self.wfile.write(body)

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def gitlab_project(path: str, description: str = "", group: str = "team") -> dict:
    return {"path": path, "description": description, "path_with_namespace": f"{group}/{path}",
            "ssh_url_to_repo": f"ssh://git@gitlab.example:2222/{group}/{path}.git",
            "http_url_to_repo": f"https://gitlab.example/{group}/{path}.git"}


class FakeGitLabOAuth:
    """A GitLab OAuth 2 provider on 127.0.0.1, for Sign in with GitLab.

    GET /oauth/authorize signs in as `who` at once and sends the browser back with a code;
    POST /oauth/token swaps a code (checking the app's id and secret, the redirect URI and the
    PKCE verifier) or a refresh token for new tokens, refusing a `blocked` user's refresh;
    GET /api/v4/user names the token's user. `users` maps username -> {"id", "state"}.
    `down` makes every request fail with 503. `seen` keeps every request path and form."""

    def __init__(self, client_id="app-id", secret="app-secret-value", users=None):
        import base64
        import hashlib
        import http.server
        import json
        import secrets
        import threading
        from urllib.parse import parse_qs, urlencode, urlparse

        self.client_id, self.secret = client_id, secret
        self.users = users if users is not None else {"alice-gl": {"id": 11, "state": "active"}}
        self.who, self.down, self.seen = "alice-gl", False, []
        self.codes, self.access, self.refresh = {}, {}, {}  # code -> (user, challenge, redirect)
        fake = self

        def b64(raw):
            return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def answer(self, code, body=None, headers=()):
                self.send_response(code)
                for k, v in headers:
                    self.send_header(k, v)
                data = json.dumps(body).encode() if body is not None else b""
                if body is not None:
                    self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                u = urlparse(self.path)
                fake.seen.append((u.path, dict(parse_qs(u.query))))
                if fake.down:
                    return self.answer(503, {"message": "down"})
                if u.path == "/oauth/authorize":
                    q = {k: v[0] for k, v in parse_qs(u.query).items()}
                    if q.get("client_id") != fake.client_id or q.get("scope") != "read_user" \
                            or q.get("code_challenge_method") != "S256":
                        return self.answer(400, {"error": "invalid_request"})
                    code = secrets.token_urlsafe(12)
                    fake.codes[code] = (fake.who, q["code_challenge"], q["redirect_uri"])
                    back = q["redirect_uri"] + "?" + urlencode({"code": code, "state": q["state"]})
                    return self.answer(302, headers=[("Location", back)])
                if u.path == "/api/v4/user":
                    auth = self.headers.get("Authorization", "")
                    user = fake.access.get(auth[7:]) if auth.startswith("Bearer ") else None
                    if user is None:
                        return self.answer(401, {"message": "401 Unauthorized"})
                    info = fake.users[user]
                    if info["state"] == "blocked":
                        return self.answer(403, {"message": "403 Forbidden"})
                    return self.answer(200, {"id": info["id"], "username": user, "state": info["state"]})
                self.answer(404, {"message": "404"})

            def do_POST(self):
                u = urlparse(self.path)
                n = int(self.headers.get("Content-Length") or 0)
                form = {k: v[0] for k, v in parse_qs(self.rfile.read(n).decode()).items()}
                fake.seen.append((u.path, form))
                if fake.down:
                    return self.answer(503, {"message": "down"})
                if u.path != "/oauth/token":
                    return self.answer(404, {"message": "404"})
                if form.get("client_id") != fake.client_id or form.get("client_secret") != fake.secret:
                    return self.answer(401, {"error": "invalid_client"})
                if form.get("grant_type") == "authorization_code":
                    got = fake.codes.pop(form.get("code", ""), None)
                    if got is None or got[2] != form.get("redirect_uri") or \
                            b64(hashlib.sha256(form.get("code_verifier", "").encode()).digest()) != got[1]:
                        return self.answer(400, {"error": "invalid_grant"})
                    user = got[0]
                elif form.get("grant_type") == "refresh_token":
                    user = fake.refresh.pop(form.get("refresh_token", ""), None)
                    if user is None or fake.users[user]["state"] == "blocked":
                        return self.answer(400, {"error": "invalid_grant"})
                else:
                    return self.answer(400, {"error": "unsupported_grant_type"})
                access, refresh = secrets.token_urlsafe(16), secrets.token_urlsafe(16)
                fake.access[access], fake.refresh[refresh] = user, user
                self.answer(200, {"access_token": access, "refresh_token": refresh,
                                  "token_type": "Bearer", "expires_in": 7200})

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


# ---- real Claude Code sessions in slow tests ----

# A background session cannot ask for permission: a command outside its allow list waits
# forever. Models look around first (`pwd && ls -la && test -e x`), and a compound command needs
# every part allowed, so the read-only commands are always allowed; a test adds what it needs.
READ_ONLY_TOOLS = ("Bash(pwd)", "Bash(pwd *)", "Bash(ls)", "Bash(ls *)", "Bash(cat *)",
                   "Bash(head *)", "Bash(tail *)", "Bash(test *)", "Bash(echo *)",
                   "Bash(wc *)", "Bash(grep *)", "Read", "Glob", "Grep", "LS")


def allowed_tools(*extra: str) -> str:
    """`--allowedTools=...` for a real `claude --bg` in a slow test: READ_ONLY_TOOLS plus extra."""
    return "--allowedTools=" + ",".join((*READ_ONLY_TOOLS, *extra))


def transcript_dir(folder, home=None):
    """The folder Claude Code keeps a session's transcripts in for a session started in
    `folder`: ~/.claude/projects/<absolute path, every character but letters and digits as ->."""
    import re
    from pathlib import Path

    path = str(Path(folder).resolve())
    return (Path(home) if home else Path.home()) / ".claude" / "projects" / re.sub(
        r"[^A-Za-z0-9]", "-", path)


KEEP_FAILURES = 10  # failed slow tests whose transcripts are kept, newest first


def remove_transcripts(folder, home=None) -> None:
    """Remove the transcripts of sessions started in `folder` or in a folder inside it. Called
    from a slow test's `finally`: when the test is failing (an exception on its way out), they
    are copied first to $KAPTYORKA_TEST_KEEP (default ~/.cache/kaptyorka-test-failures)/
    <test>-<time>/, the newest KEEP_FAILURES kept, and the path is printed with the failure."""
    import shutil
    import sys

    d = transcript_dir(folder, home)
    found = [p for p in d.parent.glob(d.name + "*")
             if p.name == d.name or p.name.startswith(d.name + "-")]
    if found and sys.exc_info()[0] is not None:
        line = f"slow test failed: its session transcripts are kept in {keep_failure(found)}"
        print(line, flush=True)  # in the failure's captured output
        print(line, file=sys.stderr, flush=True)
    for p in found:
        shutil.rmtree(p, ignore_errors=True)


def keep_failure(folders):
    """Copy `folders` into a new folder under the keep place, named after the running test and
    the time; prune to the newest KEEP_FAILURES. Returns the new folder."""
    import os
    import re
    import shutil
    import time
    from pathlib import Path

    root = Path(os.environ.get("KAPTYORKA_TEST_KEEP")
                or Path.home() / ".cache" / "kaptyorka-test-failures")
    test = os.environ.get("PYTEST_CURRENT_TEST", "test").split(" ")[0].split("::")[-1]
    base = f"{re.sub(r'[^A-Za-z0-9_.-]', '-', test)}-{time.strftime('%Y%m%d-%H%M%S')}"
    dest, n = root / base, 2
    while dest.exists():
        dest, n = root / f"{base}-{n}", n + 1
    dest.mkdir(parents=True)
    for p in folders:
        shutil.copytree(p, dest / p.name, symlinks=True)
    old = sorted((p for p in root.iterdir() if p.is_dir()), key=lambda p: p.stat().st_mtime,
                 reverse=True)
    for p in old[KEEP_FAILURES:]:
        shutil.rmtree(p, ignore_errors=True)
    return dest


def scratch_dir(prefix: str):
    """A throwaway folder beside the repository, for a real `claude --bg` (Claude Code refuses
    an untrusted folder, and /tmp is not trusted; beside the repo inherits its parent's trust),
    also from a git worktree under .claude/worktrees, which Claude Code does not trust."""
    import tempfile
    from pathlib import Path

    base = Path(__file__).resolve().parents[1]
    while ".claude" in base.parts:
        base = base.parent
    return Path(tempfile.mkdtemp(prefix=prefix, dir=base.parent))


def fenced(text: str, what: str = "message") -> str:
    """`text` in the data fence a delivery puts it in (the tests' fixed boundary)."""
    from conftest import FENCE
    from kaptyorka.secure.kap_fence import fence
    return fence(text, what, boundary=FENCE)
