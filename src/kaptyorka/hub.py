"""The hub app: MCP tools and plain routes over HTTP behind per-device tokens."""
import asyncio
import functools
import html
import importlib.resources
import inspect
import math
import mimetypes
import re
from dataclasses import asdict
from typing import Dict, List, Mapping, Union
from urllib.parse import unquote

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response

from kaptyorka.auth import Auth, RateLimiter, bearer
from kaptyorka.hub_feed import feed, session_row
from kaptyorka.hub_feed import knocks as hub_knocks
from kaptyorka.store import MAX_FILE, MAX_IDENT, Agent, KaptyorkaError, Principal, Store, clean

MAX_WAIT_S = 25
CSP = ("default-src 'none'; connect-src 'self'; script-src 'unsafe-inline'; "
       "style-src 'unsafe-inline'; img-src 'self' data:")

INVITE_PAGE = ("<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width'>"
               "<title>kaptyorka</title><style>body{{background:#111418;color:#e6e8eb;"
               "font:16px system-ui,sans-serif;max-width:40rem;margin:4rem auto;padding:0 1rem}}"
               "pre{{background:#1b2027;padding:1rem;border-radius:6px;overflow-x:auto}}</style>"
               "{body}")

# the invite page's second way in, without script: the form's POST redirects to the page
INVITE_WEB = ("<form method=post action='{action}'><button style='font:inherit;padding:.5rem 1rem;"
              "border-radius:4px;border:1px solid #3d71d9;background:#3d71d9;color:#fff;"
              "cursor:pointer'>Open the page as this person</button></form>")

INSTRUCTIONS = (
    "kaptyorka connects you with the other coding agents on this team. "
    "who() lists online sessions; send(to, text) messages one by name or 'all'. "
    "You may message your own person's sessions; for anyone else, knock(to, about) first "
    "and wait for a yes; answer(knock_id, 'yes' or 'no') replies to a knock, only after "
    "asking your person; door_close(to) ends a door; "
    "inbox() reads your messages; status(text) says what you are working on; "
    "notes_list/notes_read/notes_write share a team board, one text per topic; "
    "pass project to use that project's notes. projects() lists the projects you have a role in."
)


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" + ("" if n == 1 else "s")


def _bad_request(fn):
    """Answer 400 instead of 500 when a client sends a malformed request."""
    @functools.wraps(fn)
    async def wrapper(request):
        try:
            return await fn(request)
        except (ValueError, KeyError, TypeError):  # JSONDecodeError is a ValueError
            return PlainTextResponse("bad request", status_code=400)
    return wrapper


def _wanted(r: dict) -> str:
    return f"access to {r['target']} (project)" if r["kind"] == "project" \
        else f"use of {r['target']} (machine)"


def _request_lines(store: Store, principal) -> str:
    """The start line's tail: the caller's pending and recently denied access requests."""
    if principal is None:
        return ""
    out = ""
    pending = store.pending_for(principal.person_id)
    if pending:
        out += f" Pending: {', '.join(_wanted(r) for r in pending)}."
    denied = store.denied_since(principal.person_id, store.clock() - 7 * 86400)
    if denied:
        out += f" Denied: {', '.join(_wanted(r) for r in denied)}."
    return out


def _titled(a: Agent, name: str = "") -> str:
    name = name or a.name
    return f"{a.label} ({name})" if a.label else name


def _tool_errors(fn):
    """Show a KaptyorkaError to the agent as a tool error with its message."""
    if inspect.iscoroutinefunction(fn):
        @functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            try:
                return await fn(*args, **kwargs)
            except KaptyorkaError as e:
                raise ToolError(str(e)) from e
    else:
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            try:
                return fn(*args, **kwargs)
            except KaptyorkaError as e:
                raise ToolError(str(e)) from e
    return wrapper


def build_app(store: Store) -> Starlette:
    server = MCPServer("kaptyorka", instructions=INSTRUCTIONS)
    sent = {"event": asyncio.Event()}  # set and replaced after each send; wakes bridge polls

    def principal_of(headers: Mapping) -> Principal:
        """The caller's principal from its bearer token (the Auth middleware already let it in)."""
        machine = unquote(headers.get("x-kaptyorka-machine") or "") or None
        principal = store.authenticate(bearer(headers), machine=machine)
        if principal is None:
            raise KaptyorkaError("unauthorized")
        return principal

    def identify(h, principal: "Principal | None" = None) -> Agent:
        # values are percent-encoded by Identity.headers(), since headers must be ASCII
        ident = [unquote(h.get(f"x-kaptyorka-{k}") or "") for k in ("key", "machine", "tool", "project")]
        ident[1:] = [clean(v, MAX_IDENT) for v in ident[1:]]  # one line: names and push texts
        if not all(ident):
            raise KaptyorkaError("no kaptyorka identity; run kaptyorka join")
        return store.seen(*ident, principal=principal or principal_of(h))

    def me(ctx: Context) -> Agent:
        return identify(ctx.headers or {})

    def caller(ctx: Context) -> "tuple[Agent, Principal]":
        principal = principal_of(ctx.headers or {})
        return identify(ctx.headers or {}, principal), principal

    @server.tool()
    @_tool_errors
    def who(ctx: Context) -> List[dict]:
        """Online sessions: name, label, person, machine, tool, project, status, activity, last
        seen. A project you may not see shows as (private), in the name too."""
        agent, principal = caller(ctx)
        now = store.clock()
        names = {p["id"]: p["name"] for p in store.people()}
        return [{**session_row(store, a, principal, names, now), "you": a.key == agent.key}
                for a in store.online()]

    def wake_bridges():
        sent["event"].set()
        sent["event"] = asyncio.Event()

    @server.tool()
    @_tool_errors
    async def send(to: str, text: str, ctx: Context, file: str = "") -> str:
        """Send text to one session by name or label, or to 'all'. file: the local path of one
        file to attach (png jpg gif webp svg txt md log json csv pdf, up to 5 MB)."""
        file_id = None
        if file:
            m = re.fullmatch(r"id:(\d+)", file)
            if m is None:
                raise KaptyorkaError("attach a file by passing its local path; "
                                     "the kaptyorka tool uploads it")
            file_id = int(m.group(1))
        n = store.send(me(ctx).key, to, text, file_id)
        wake_bridges()
        return f"sent to {n} session" + ("" if n == 1 else "s")

    @server.tool()
    @_tool_errors
    async def knock(to: str, about: str, ctx: Context) -> str:
        """Ask another person's session (name or label), or the person by name, to open a door
        so you can message it. Say in about what you want."""
        kid = store.knock(me(ctx).key, to, about)
        wake_bridges()
        return f"knocked (#{kid}); waiting for an answer"

    @server.tool()
    @_tool_errors
    async def answer(knock_id: int, answer: str, ctx: Context) -> str:
        """Answer a knock with "yes" (opens a door with this session) or "no". Ask your person
        first."""
        if answer not in ("yes", "no"):
            raise KaptyorkaError('answer must be "yes" or "no"')
        store.answer(knock_id, me(ctx).key, answer == "yes")
        wake_bridges()
        return "door open" if answer == "yes" else "knock declined"

    @server.tool()
    @_tool_errors
    async def door_close(to: str, ctx: Context) -> str:
        """Close the door between this session and another one."""
        store.door_close(me(ctx).key, to)
        wake_bridges()
        return f"door to {to} closed"

    @server.tool()
    @_tool_errors
    def inbox(ctx: Context) -> List[dict]:
        """Your unread messages; reading marks them read."""
        return [{"from": m.sender, "text": m.text, "time": m.created, "kind": m.kind,
                 "file": m.file} for m in store.inbox(me(ctx).key)]

    @server.tool()
    @_tool_errors
    def fetch_file(id: int, ctx: Context) -> Dict[str, Union[int, str]]:
        """Download a file attached to a message you sent or received (id from inbox)."""
        _, principal = caller(ctx)
        info = store.file_meta(id, principal)
        return {**info, "url": f"/api/files/{id}"}

    @server.tool()
    @_tool_errors
    def status(text: str, ctx: Context) -> str:
        """Say what you are working on; others see it in who()."""
        store.set_status(me(ctx).key, text)
        return "status set"

    @server.tool()
    @_tool_errors
    def notes_list(ctx: Context, project: str = "") -> List[dict]:
        """Topics on the team board, newest first; with project, that project's notes."""
        _, principal = caller(ctx)
        return store.notes_list(principal, project or None)

    @server.tool()
    @_tool_errors
    def notes_read(topic: str, ctx: Context, project: str = "") -> Union[dict, str]:
        """Read the latest text of a note (a team note, or with project, that project's)."""
        _, principal = caller(ctx)
        note = store.notes_read(topic, principal, project or None)
        return asdict(note) if note else f"no note named {topic}"

    @server.tool()
    @_tool_errors
    def notes_write(topic: str, text: str, ctx: Context, project: str = "") -> str:
        """Write a new version of a note (older versions are kept); with project, that
        project's note."""
        store.notes_write(me(ctx).key, topic, text, project or None)
        return f"saved note {topic}"

    @server.tool()
    @_tool_errors
    def projects(ctx: Context) -> List[dict]:
        """Projects you have a role in: name, description, repo, your role, sessions in it now."""
        _, principal = caller(ctx)
        return store.projects_for(principal)

    @server.tool()
    @_tool_errors
    def request_access(kind: str, target: str, ctx: Context, why: str = "") -> str:
        """Ask the admin for read access to a project (kind "project") or for use of a machine
        (kind "machine"), with a short reason."""
        _, principal = caller(ctx)
        rid = store.request_access(principal.person_id, kind, target, why)
        return f"requested #{rid}; the admin sees it on the page"

    @server.custom_route("/bridge/heartbeat", methods=["POST"])
    @_bad_request
    async def bridge_heartbeat(request: Request) -> Response:
        body = await request.json()
        facts = body.get("facts")
        principal = request.scope.get("state", {}).get("principal")
        if principal is not None and principal.legacy:
            # the machine is only in the body, which Auth does not read: map it to its person
            principal = store.authenticate(bearer(request.headers),
                                           machine=str(body["machine"])) or principal
        store.heartbeat(body["machine"], body["sessions"], body.get("tools", ()),
                        principal=principal, facts=facts if isinstance(facts, dict) else None)
        return Response(status_code=204)

    @server.custom_route("/bridge/poll", methods=["GET"])
    @_bad_request
    async def bridge_poll(request: Request) -> Response:
        machine = request.query_params["machine"]
        wait = float(request.query_params.get("wait", 0))
        if not math.isfinite(wait):
            raise ValueError("wait must be a finite number")
        wait = min(max(wait, 0), MAX_WAIT_S)
        event = sent["event"]  # taken before offer, so a send after offer still wakes us
        principal = request.scope["state"]["principal"]
        version = store.bridge_version(principal, machine)
        items = store.offer(machine, version, principal)
        if not items and wait:
            try:
                await asyncio.wait_for(event.wait(), wait)
                items = store.offer(machine, version, principal)
            except asyncio.TimeoutError:
                pass
        return JSONResponse(items)

    @server.custom_route("/bridge/ack", methods=["POST"])
    @_bad_request
    async def bridge_ack(request: Request) -> Response:
        body = await request.json()
        if not store.ack(int(body["id"]), body["ok"] is True, request.scope["state"]["principal"]):
            return PlainTextResponse("no such message", status_code=404)
        return Response(status_code=204)

    @server.custom_route("/bridge/rekeyed", methods=["POST"])
    @_bad_request
    async def bridge_rekeyed(request: Request) -> Response:
        body = await request.json()
        if not store.rekeyed(str(body["machine"]), request.scope["state"]["principal"]):
            return PlainTextResponse("not this machine's new device", status_code=403)
        return Response(status_code=204)

    @server.custom_route("/api/brief", methods=["GET"])
    async def api_brief(request: Request) -> Response:
        try:
            agent = identify(request.headers)
        except KaptyorkaError as e:
            return PlainTextResponse(str(e), status_code=400)
        unread = store.inbox(agent.key)
        parts = [m.text if m.kind != "message" else
                 f"Kaptyorka message from {m.sender}{' (web page)' if m.from_web else ''}:\n\n{m.text}"
                 for m in unread]
        if request.query_params.get("event") == "start":
            principal = request.scope.get("state", {}).get("principal")
            others = [a for a in store.online() if a.key != agent.key]
            line = f"Kaptyorka: you are `{_titled(agent)}`. "
            if others:
                now, people = store.clock(), {p["id"]: p["name"] for p in store.people()}
                # masked like who(): another person's private folder is not named
                names = ", ".join(_titled(a, session_row(store, a, principal, people, now)["name"])
                                  + (f" ({a.activity})" if a.activity else "") for a in others)
                line += f"{_plural(len(others), 'other')} online: {names}. "
            else:
                line += "No one else online. "
            line += f"{_plural(len(unread), 'unread message')}."
            topics = [n["topic"] for n in store.notes_list()]
            if topics:
                line += f" Board topics: {', '.join(topics)}."
            line += _request_lines(store, principal)
            if principal is not None and principal.is_admin() and store.legacy_ready_to_disable():
                line += (" All old machines have their own tokens;"
                         " switch the shared token off on the page.")
            parts.insert(0, line)
        return PlainTextResponse("\n\n".join(parts))

    @server.custom_route("/api/me", methods=["GET"])
    async def api_me(request: Request) -> Response:
        p = request.scope["state"]["principal"]
        store.page_seen(p)  # only the page asks who it is
        return JSONResponse({"person": p.person_name, "role": p.role, "device": p.device_name,
                             "person_id": p.person_id})

    @server.custom_route("/api/feed", methods=["GET"])
    async def api_feed(request: Request) -> Response:
        return JSONResponse(feed(store, request.scope["state"]["principal"]))

    @server.custom_route("/api/files", methods=["POST"])
    async def api_file_upload(request: Request) -> Response:
        principal = request.scope["state"]["principal"]
        name = unquote(request.headers.get("x-file-name") or "") or "file"
        def too_large(n):
            return PlainTextResponse(f"file too large: {n} bytes, limit {MAX_FILE}", status_code=400)

        declared = request.headers.get("content-length") or ""
        if declared.isdigit() and int(declared) > MAX_FILE:
            return too_large(int(declared))
        data = b""
        async for chunk in request.stream():
            data += chunk
            if len(data) > MAX_FILE:
                return too_large(len(data))
        try:
            fid = store.file_add(name, data, principal.person_id)
        except KaptyorkaError as e:
            return PlainTextResponse(str(e), status_code=400)
        return JSONResponse({"id": fid, "name": name[:200], "size": len(data)})

    @server.custom_route("/api/files/{id}", methods=["GET"])
    async def api_file_get(request: Request) -> Response:
        try:
            info, data = store.file_get(int(request.path_params["id"]),
                                        request.scope["state"]["principal"])
        except (KaptyorkaError, ValueError):
            return PlainTextResponse("no such file", status_code=404)
        safe = re.sub(r"[^A-Za-z0-9._-]", "", info["name"].replace("\\", "/").rsplit("/", 1)[-1])
        return Response(data, media_type=mimetypes.guess_type(info["name"])[0]
                        or "application/octet-stream",
                        headers={"Content-Disposition": f'attachment; filename="{safe or "file"}"',
                                 "X-Content-Type-Options": "nosniff"})

    @server.custom_route("/api/send", methods=["POST"])
    @_bad_request
    async def api_send(request: Request) -> Response:
        body = await request.json()
        p = request.scope["state"]["principal"]  # the body's "from" (0.1 pages) is ignored
        try:
            n = store.send_from_web(p.person_name, str(body["to"]), str(body["text"]),
                                    person_id=p.person_id)
        except KaptyorkaError as e:
            return PlainTextResponse(str(e), status_code=400)
        wake_bridges()
        return JSONResponse({"sent": n})

    @server.custom_route("/api/notes", methods=["POST"])
    @_bad_request
    async def api_notes(request: Request) -> Response:
        body = await request.json()
        p = request.scope["state"]["principal"]
        try:
            store.notes_write_from_web(p.person_name, str(body["topic"]), str(body["text"]),
                                       str(body.get("project") or "") or None, principal=p)
        except KaptyorkaError as e:
            return PlainTextResponse(str(e), status_code=400)
        return Response(status_code=204)

    limiter = RateLimiter()  # shared by token checks (Auth) and failed invite redeems

    def admin_only(request: Request) -> "Response | None":
        principal = request.scope.get("state", {}).get("principal")
        if principal is None or not principal.is_admin():
            return PlainTextResponse("admin only", status_code=403)
        if principal.legacy and store.has_own_device(principal.person_id):
            # anyone holding the shared token would be the admin: once the admin has a token
            # of their own, admin work takes that one
            return PlainTextResponse("use your own admin token", status_code=403)
        return None

    def hub_url(request: Request) -> str:
        return f"http://{request.headers.get('host') or request.url.netloc}"

    @server.custom_route("/api/log", methods=["GET"])
    @_bad_request
    async def api_log(request: Request) -> Response:
        denied = admin_only(request)
        if denied:
            return denied
        q = request.query_params
        limit = max(1, min(int(q.get("limit") or 200), 1000))
        return JSONResponse(store.log(limit, q.get("person") or None))

    @server.custom_route("/api/settings", methods=["GET"])
    async def api_settings(request: Request) -> Response:
        denied = admin_only(request)
        if denied:
            return denied
        return JSONResponse({"retention_days": store.retention_days()})

    @server.custom_route("/api/settings", methods=["POST"])
    @_bad_request
    async def api_settings_set(request: Request) -> Response:
        denied = admin_only(request)
        if denied:
            return denied
        body = await request.json()
        try:
            store.set_retention_days(body.get("retention_days"),
                                     request.scope["state"]["principal"].person_name)
        except KaptyorkaError as e:
            return PlainTextResponse(str(e), status_code=400)
        return JSONResponse({"retention_days": store.retention_days()})

    @server.custom_route("/api/legacy", methods=["GET"])
    async def api_legacy(request: Request) -> Response:
        denied = admin_only(request)
        if denied:
            return denied
        return JSONResponse({"unassigned": store.legacy_unassigned(),
                             "enabled": store.legacy_enabled(),
                             "ready_to_disable": store.legacy_ready_to_disable()})

    @server.custom_route("/api/legacy/assign", methods=["POST"])
    @_bad_request
    async def api_legacy_assign(request: Request) -> Response:
        denied = admin_only(request)
        if denied:
            return denied
        body = await request.json()
        person = store.person_by_name(str(body["person"]))
        try:
            if person is None:
                raise KaptyorkaError("no such person")
            store.assign_legacy(str(body["machine"]), person["id"],
                                request.scope["state"]["principal"].person_name)
        except KaptyorkaError as e:
            return PlainTextResponse(str(e), status_code=400)
        wake_bridges()  # its bridge may pick up the new token on its next heartbeat and poll
        return Response(status_code=204)

    async def legacy_toggle(request: Request, on: bool) -> Response:
        denied = admin_only(request)
        if denied:
            return denied
        store.set_legacy_enabled(on, request.scope["state"]["principal"].person_name)
        return Response(status_code=204)

    @server.custom_route("/api/legacy/off", methods=["POST"])
    async def api_legacy_off(request: Request) -> Response:
        principal = request.scope.get("state", {}).get("principal")
        if principal is not None and principal.is_admin() and (
                principal.legacy or not store.has_own_device(principal.person_id)):
            # switched off with itself, the shared token would lock the admin out
            return PlainTextResponse(
                "switch the shared token off with your own admin token: run kaptyorka start"
                " on the hub machine and open the page link it prints", status_code=400)
        return await legacy_toggle(request, False)

    @server.custom_route("/api/legacy/on", methods=["POST"])
    async def api_legacy_on(request: Request) -> Response:
        return await legacy_toggle(request, True)

    @server.custom_route("/api/people", methods=["GET"])
    async def api_people(request: Request) -> Response:
        denied = admin_only(request)
        if denied:
            return denied
        return JSONResponse([{**p, "devices": store.devices(p["id"])} for p in store.people()])

    @server.custom_route("/api/people", methods=["POST"])
    @_bad_request
    async def api_people_create(request: Request) -> Response:
        denied = admin_only(request)
        if denied:
            return denied
        body = await request.json()
        try:
            pid = store.person_create(str(body["name"]), str(body.get("role", "member")),
                                      by=request.scope["state"]["principal"].person_name)
        except KaptyorkaError as e:
            status = 409 if "already exists" in str(e) else 400
            return PlainTextResponse(str(e), status_code=status)
        return JSONResponse({"id": pid})

    @server.custom_route("/api/invites", methods=["POST"])
    @_bad_request
    async def api_invites(request: Request) -> Response:
        denied = admin_only(request)
        if denied:
            return denied
        body = await request.json()
        person = store.person_by_name(str(body["person"]))
        if person is None:
            return PlainTextResponse("no such person", status_code=404)
        code = store.invite_create(person["id"], by=request.scope["state"]["principal"].person_name)
        return JSONResponse({"code": code, "url": f"{hub_url(request)}/i/{code}"})

    @server.custom_route("/api/devices/{device_id:int}/revoke", methods=["POST"])
    async def api_device_revoke(request: Request) -> Response:
        denied = admin_only(request)
        if denied:
            return denied
        store.device_revoke(request.path_params["device_id"],
                            by=request.scope["state"]["principal"].person_name)
        return Response(status_code=204)

    @server.custom_route("/api/devices/{device_id:int}/shared", methods=["POST"])
    @_bad_request
    async def api_device_shared(request: Request) -> Response:
        denied = admin_only(request)
        if denied:
            return denied
        body = await request.json()
        if not isinstance(body["shared"], bool):
            raise ValueError("shared must be true or false")
        try:
            store.device_set_shared(request.path_params["device_id"], body["shared"],
                                    by=request.scope["state"]["principal"].person_name)
        except KaptyorkaError as e:
            return PlainTextResponse(str(e), status_code=400)
        return Response(status_code=204)

    @server.custom_route("/api/machines", methods=["GET"])
    async def api_machines(request: Request) -> Response:
        principal = request.scope.get("state", {}).get("principal")
        if principal is None:
            return PlainTextResponse("unauthorized", status_code=401)
        return JSONResponse(store.machines(principal))

    async def machine_grant(request: Request, grant: bool) -> Response:
        denied = admin_only(request)
        if denied:
            return denied
        body = await request.json()
        person = store.person_by_name(str(body["person"]))
        try:
            if person is None:
                raise KaptyorkaError("no such person")
            host = request.path_params["host"]
            principal = request.scope["state"]["principal"]
            (store.grant_machine if grant else store.revoke_machine)(
                host, person["id"], principal.person_name)
        except KaptyorkaError as e:
            return PlainTextResponse(str(e), status_code=400)
        return Response(status_code=204)

    @server.custom_route("/api/machines/{host}/grant", methods=["POST"])
    @_bad_request
    async def api_machine_grant(request: Request) -> Response:
        return await machine_grant(request, True)

    @server.custom_route("/api/machines/{host}/revoke", methods=["POST"])
    @_bad_request
    async def api_machine_revoke(request: Request) -> Response:
        return await machine_grant(request, False)

    @server.custom_route("/api/projects", methods=["GET"])
    async def api_projects(request: Request) -> Response:
        principal = request.scope.get("state", {}).get("principal")
        if principal is None:
            return PlainTextResponse("unauthorized", status_code=401)
        return JSONResponse(store.projects_for(principal))

    @server.custom_route("/api/requests", methods=["GET"])
    async def api_requests(request: Request) -> Response:
        principal = request.scope.get("state", {}).get("principal")
        if principal is None:
            return PlainTextResponse("unauthorized", status_code=401)
        if principal.is_admin():
            return JSONResponse(store.requests())
        return JSONResponse(store.pending_for(principal.person_id))

    @server.custom_route("/api/requests/{rid:int}", methods=["POST"])
    @_bad_request
    async def api_request_decide(request: Request) -> Response:
        denied = admin_only(request)
        if denied:
            return denied
        body = await request.json()
        if not isinstance(body["approve"], bool):
            raise ValueError("approve must be true or false")
        try:
            store.decide_request(request.path_params["rid"],
                                 request.scope["state"]["principal"].person_id, body["approve"])
        except KaptyorkaError as e:
            return PlainTextResponse(str(e), status_code=400)
        return Response(status_code=204)

    @server.custom_route("/api/projects", methods=["POST"])
    @_bad_request
    async def api_project_create(request: Request) -> Response:
        denied = admin_only(request)
        if denied:
            return denied
        body = await request.json()
        try:
            store.project_register(str(body["name"]), str(body.get("description", "")),
                                   str(body.get("repo", "")),
                                   by=request.scope["state"]["principal"].person_name)
        except KaptyorkaError as e:
            return PlainTextResponse(str(e), status_code=400)
        return Response(status_code=204)

    async def project_grant(request: Request, grant: bool) -> Response:
        denied = admin_only(request)
        if denied:
            return denied
        body = await request.json()
        person = store.person_by_name(str(body["person"]))
        project = request.path_params["name"]
        by = request.scope["state"]["principal"].person_name
        try:
            if person is None:
                raise KaptyorkaError("no such person")
            if grant:
                store.grant_project(project, person["id"], str(body["role"]), by)
            else:
                store.revoke_project(project, person["id"], by)
        except KaptyorkaError as e:
            return PlainTextResponse(str(e), status_code=400)
        return Response(status_code=204)

    @server.custom_route("/api/projects/{name}/grant", methods=["POST"])
    @_bad_request
    async def api_project_grant(request: Request) -> Response:
        return await project_grant(request, True)

    @server.custom_route("/api/projects/{name}/revoke", methods=["POST"])
    @_bad_request
    async def api_project_revoke(request: Request) -> Response:
        return await project_grant(request, False)

    @server.custom_route("/api/doors", methods=["GET"])
    async def api_doors(request: Request) -> Response:
        denied = admin_only(request)
        if denied:
            return denied
        return JSONResponse(store.doors())

    @server.custom_route("/api/doors", methods=["POST"])
    @_bad_request
    async def api_door_open(request: Request) -> Response:
        denied = admin_only(request)
        if denied:
            return denied
        body = await request.json()
        a, b = store.person_by_name(str(body["a_person"])), store.person_by_name(str(body["b_person"]))
        try:
            if a is None or b is None:
                raise KaptyorkaError("no such person")
            did = store.open_door_group(a["id"], b["id"],
                                        request.scope["state"]["principal"].person_name)
        except KaptyorkaError as e:
            return PlainTextResponse(str(e), status_code=400)
        return JSONResponse({"id": did})

    @server.custom_route("/api/doors/{door_id:int}/close", methods=["POST"])
    async def api_door_close(request: Request) -> Response:
        denied = admin_only(request)
        if denied:
            return denied
        try:
            store.close_door(request.path_params["door_id"],
                             request.scope["state"]["principal"].person_name)
        except KaptyorkaError as e:
            return PlainTextResponse(str(e), status_code=404)
        return Response(status_code=204)

    @server.custom_route("/api/knocks", methods=["GET"])
    async def api_knocks(request: Request) -> Response:
        principal = request.scope.get("state", {}).get("principal")
        if principal is None:
            return PlainTextResponse("unauthorized", status_code=401)
        return JSONResponse(hub_knocks(store, principal))

    @server.custom_route("/api/knocks/{kid:int}/answer", methods=["POST"])
    @_bad_request
    async def api_knock_answer(request: Request) -> Response:
        body = await request.json()
        if body["answer"] not in ("yes", "no"):
            raise ValueError('answer must be "yes" or "no"')
        p, kid, yes = request.scope["state"]["principal"], request.path_params["kid"], \
            body["answer"] == "yes"
        # the viewer's earliest online session answers; with none, a yes opens a group door
        mine = [a for a in store.online() if a.person_id == p.person_id]
        try:
            scope = (store.answer(kid, mine[0].key, yes) if mine
                     else store.answer_by_person(kid, p.person_id, yes))
        except KaptyorkaError as e:
            return PlainTextResponse(str(e), status_code=400)
        wake_bridges()
        return JSONResponse({"door": scope or None})

    @server.custom_route("/i/{code}", methods=["GET"])
    async def invite_page(request: Request) -> Response:
        code = request.path_params["code"]
        headers = {"Cache-Control": "no-store"}
        if not store.invite_valid(code):
            return HTMLResponse(INVITE_PAGE.format(body="<p>This invite is not valid.</p>"),
                                status_code=404, headers=headers)
        cmd = html.escape(f"kaptyorka join {hub_url(request)}/i/{code}")
        body = (f"<h1>Join the team hub</h1><pre>{cmd}</pre>"
                "<p>Paste this link to your agent and say: join this.</p>"
                + INVITE_WEB.format(action=html.escape(f"/i/{code}/web")))
        return HTMLResponse(INVITE_PAGE.format(body=body), headers=headers)

    @server.custom_route("/i/{code}", methods=["POST"])
    @_bad_request
    async def invite_redeem(request: Request) -> Response:
        code = request.path_params["code"]
        key = (request.client.host if request.client else "?", code)
        if limiter.blocked(key):
            return PlainTextResponse("too many failed attempts", status_code=429)
        body = await request.json()
        try:
            token, secret = store.invite_redeem(code, str(body["device_name"]),
                                                str(body.get("host", "")),
                                                str(body.get("os_user", "")))
        except KaptyorkaError as e:
            limiter.hit(key)
            return PlainTextResponse(str(e), status_code=400)
        return JSONResponse({"token": token, "secret": secret, "hub": hub_url(request)},
                            headers={"Cache-Control": "no-store"})

    @server.custom_route("/i/{code}/web", methods=["POST"])
    async def invite_web(request: Request) -> Response:
        """Use the invite for this browser: a device named `web` whose token the page keeps."""
        code = request.path_params["code"]
        key = (request.client.host if request.client else "?", code)
        if limiter.blocked(key):
            return PlainTextResponse("too many failed attempts", status_code=429)
        try:
            token, _ = store.invite_redeem(code, "web")
        except KaptyorkaError as e:
            limiter.hit(key)
            return PlainTextResponse(str(e), status_code=400)
        headers = {"Cache-Control": "no-store"}
        if request.headers.get("content-type", "").startswith("application/x-www-form-urlencoded"):
            # the invite page's form: open the page, which keeps the token like a #t= link
            return Response(status_code=303, headers={**headers, "Location": f"/#t={token}"})
        return JSONResponse({"token": token}, headers=headers)

    @server.custom_route("/", methods=["GET"])
    async def page(request: Request) -> Response:
        html = (importlib.resources.files("kaptyorka") / "web/index.html").read_text("utf-8")
        return HTMLResponse(html, headers={"Cache-Control": "no-store", "Content-Security-Policy": CSP})

    app = server.streamable_http_app(host="0.0.0.0")
    app.add_middleware(Auth, store=store, limiter=limiter)
    return app
