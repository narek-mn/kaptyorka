"""The hub app: MCP tools and plain routes over HTTP behind per-device tokens."""
import asyncio
import functools
import html
import importlib.resources
import inspect
import math
from dataclasses import asdict
from typing import List, Mapping, Union
from urllib.parse import unquote

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response

from kaptyorka.auth import Auth, RateLimiter, bearer
from kaptyorka.store import Agent, KaptyorkaError, Principal, Store

MAX_WAIT_S = 25
CSP = ("default-src 'none'; connect-src 'self'; script-src 'unsafe-inline'; "
       "style-src 'unsafe-inline'; img-src 'self' data:")

INVITE_PAGE = ("<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width'>"
               "<title>kaptyorka</title><style>body{{background:#111418;color:#e6e8eb;"
               "font:16px system-ui,sans-serif;max-width:40rem;margin:4rem auto;padding:0 1rem}}"
               "pre{{background:#1b2027;padding:1rem;border-radius:6px;overflow-x:auto}}</style>"
               "{body}")

INSTRUCTIONS = (
    "kaptyorka connects you with the other coding agents on this team. "
    "who() lists online sessions; send(to, text) messages one by name or 'all'; "
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


def _agent_row(a: Agent, now: float) -> dict:
    return {"name": a.name, "machine": a.machine, "tool": a.tool, "project": a.project,
            "status": a.status, "activity": a.activity, "label": a.label,
            "last_seen_s": round(now - a.last_seen)}


def _private_name(a: Agent) -> str:
    """The session name with its project hidden: machine/tool/(private), keeping a -N suffix."""
    base = f"{a.machine}/{a.tool}/{a.project}"
    suffix = a.name[len(base):] if a.name.startswith(base) else ""
    return f"{a.machine}/{a.tool}/(private){suffix}"


def _titled(a: Agent) -> str:
    return f"{a.label} ({a.name})" if a.label else a.name


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
        rows = []
        for a in store.online():
            mine = a.person_id == principal.person_id
            row = {**_agent_row(a, now), "you": a.key == agent.key, "mine": mine,
                   "person": names.get(a.person_id, "")}
            # an unregistered folder is only a name two people can share by chance, so it shows
            # on your own rows only; a registered project shows to anyone with a role in it
            if not mine and store.project_role(a.project, principal.person_id) is None:
                row["project"] = "(private)"
                row["name"] = _private_name(a)
            rows.append(row)
        return rows

    def wake_bridges():
        sent["event"].set()
        sent["event"] = asyncio.Event()

    @server.tool()
    @_tool_errors
    async def send(to: str, text: str, ctx: Context) -> str:
        """Send text to one session by name or label, or to 'all'."""
        n = store.send(me(ctx).key, to, text)
        wake_bridges()
        return f"sent to {n} session" + ("" if n == 1 else "s")

    @server.tool()
    @_tool_errors
    def inbox(ctx: Context) -> List[dict]:
        """Your unread messages; reading marks them read."""
        return [{"from": m.sender, "text": m.text, "time": m.created}
                for m in store.inbox(me(ctx).key)]

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

    @server.custom_route("/bridge/heartbeat", methods=["POST"])
    @_bad_request
    async def bridge_heartbeat(request: Request) -> Response:
        body = await request.json()
        facts = body.get("facts")
        store.heartbeat(body["machine"], body["sessions"], body.get("tools", ()),
                        principal=request.scope.get("state", {}).get("principal"),
                        facts=facts if isinstance(facts, dict) else None)
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
        items = store.offer(machine)
        if not items and wait:
            try:
                await asyncio.wait_for(event.wait(), wait)
                items = store.offer(machine)
            except asyncio.TimeoutError:
                pass
        return JSONResponse(items)

    @server.custom_route("/bridge/ack", methods=["POST"])
    @_bad_request
    async def bridge_ack(request: Request) -> Response:
        body = await request.json()
        store.ack(int(body["id"]), body["ok"] is True)
        return Response(status_code=204)

    @server.custom_route("/api/brief", methods=["GET"])
    async def api_brief(request: Request) -> Response:
        try:
            agent = identify(request.headers)
        except KaptyorkaError as e:
            return PlainTextResponse(str(e), status_code=400)
        unread = store.inbox(agent.key)
        parts = [f"Kaptyorka message from {m.sender}{' (web page)' if m.from_web else ''}:\n\n{m.text}"
                 for m in unread]
        if request.query_params.get("event") == "start":
            others = [a for a in store.online() if a.key != agent.key]
            line = f"Kaptyorka: you are `{_titled(agent)}`. "
            if others:
                names = ", ".join(_titled(a) + (f" ({a.activity})" if a.activity else "")
                                  for a in others)
                line += f"{_plural(len(others), 'other')} online: {names}. "
            else:
                line += "No one else online. "
            line += f"{_plural(len(unread), 'unread message')}."
            topics = [n["topic"] for n in store.notes_list()]
            if topics:
                line += f" Board topics: {', '.join(topics)}."
            parts.insert(0, line)
        return PlainTextResponse("\n\n".join(parts))

    @server.custom_route("/api/feed", methods=["GET"])
    async def api_feed(request: Request) -> Response:
        now = store.clock()
        return JSONResponse({
            "agents": [_agent_row(a, now) for a in store.online()],
            "messages": [asdict(m) for m in store.messages(500)],
            "notes": [{"topic": n["topic"],
                       "history": [asdict(h) for h in store.note_history(n["topic"])]}
                      for n in store.notes_list()],
        })

    @server.custom_route("/api/send", methods=["POST"])
    @_bad_request
    async def api_send(request: Request) -> Response:
        body = await request.json()
        try:
            n = store.send_from_web(str(body["from"]), str(body["to"]), str(body["text"]))
        except KaptyorkaError as e:
            return PlainTextResponse(str(e), status_code=400)
        wake_bridges()
        return JSONResponse({"sent": n})

    @server.custom_route("/api/notes", methods=["POST"])
    @_bad_request
    async def api_notes(request: Request) -> Response:
        body = await request.json()
        try:
            store.notes_write_from_web(str(body["from"]), str(body["topic"]), str(body["text"]))
        except KaptyorkaError as e:
            return PlainTextResponse(str(e), status_code=400)
        return Response(status_code=204)

    limiter = RateLimiter()  # shared by token checks (Auth) and failed invite redeems

    def admin_only(request: Request) -> "Response | None":
        principal = request.scope.get("state", {}).get("principal")
        if principal is None or not principal.is_admin():
            return PlainTextResponse("admin only", status_code=403)
        return None

    def hub_url(request: Request) -> str:
        return f"http://{request.headers.get('host') or request.url.netloc}"

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
            pid = store.person_create(str(body["name"]), str(body.get("role", "member")))
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
        code = store.invite_create(person["id"])
        return JSONResponse({"code": code, "url": f"{hub_url(request)}/i/{code}"})

    @server.custom_route("/api/devices/{device_id:int}/revoke", methods=["POST"])
    async def api_device_revoke(request: Request) -> Response:
        denied = admin_only(request)
        if denied:
            return denied
        store.device_revoke(request.path_params["device_id"])
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
            store.device_set_shared(request.path_params["device_id"], body["shared"])
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

    @server.custom_route("/i/{code}", methods=["GET"])
    async def invite_page(request: Request) -> Response:
        code = request.path_params["code"]
        headers = {"Cache-Control": "no-store"}
        if not store.invite_valid(code):
            return HTMLResponse(INVITE_PAGE.format(body="<p>This invite is not valid.</p>"),
                                status_code=404, headers=headers)
        cmd = html.escape(f"kaptyorka join {hub_url(request)}/i/{code}")
        body = (f"<h1>Join the team hub</h1><pre>{cmd}</pre>"
                "<p>Paste this link to your agent and say: join this.</p>")
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

    @server.custom_route("/", methods=["GET"])
    async def page(request: Request) -> Response:
        html = (importlib.resources.files("kaptyorka") / "web/index.html").read_text("utf-8")
        return HTMLResponse(html, headers={"Cache-Control": "no-store", "Content-Security-Policy": CSP})

    app = server.streamable_http_app(host="0.0.0.0")
    app.add_middleware(Auth, store=store, limiter=limiter)
    return app
