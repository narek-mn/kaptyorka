"""The hub app: MCP tools and plain routes over HTTP behind per-device tokens."""
import asyncio
import functools
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

from kaptyorka.auth import Auth, bearer
from kaptyorka.store import Agent, KaptyorkaError, Principal, Store

MAX_WAIT_S = 25
CSP = ("default-src 'none'; connect-src 'self'; script-src 'unsafe-inline'; "
       "style-src 'unsafe-inline'; img-src 'self' data:")

INSTRUCTIONS = (
    "kaptyorka connects you with the other coding agents on this team. "
    "who() lists online sessions; send(to, text) messages one by name or 'all'; "
    "inbox() reads your messages; status(text) says what you are working on; "
    "notes_list/notes_read/notes_write share a team board, one text per topic."
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

    def identify(h) -> Agent:
        # values are percent-encoded by Identity.headers(), since headers must be ASCII
        ident = [unquote(h.get(f"x-kaptyorka-{k}") or "") for k in ("key", "machine", "tool", "project")]
        if not all(ident):
            raise KaptyorkaError("no kaptyorka identity; run kaptyorka join")
        return store.seen(*ident, principal=principal_of(h))

    def me(ctx: Context) -> Agent:
        return identify(ctx.headers or {})

    @server.tool()
    @_tool_errors
    def who(ctx: Context) -> List[dict]:
        """Online sessions: name, label, machine, tool, project, status, activity, last seen."""
        key = me(ctx).key
        now = store.clock()
        return [{**_agent_row(a, now), "you": a.key == key} for a in store.online()]

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
    def notes_list(ctx: Context) -> List[dict]:
        """Topics on the team board, newest first."""
        me(ctx)
        return store.notes_list()

    @server.tool()
    @_tool_errors
    def notes_read(topic: str, ctx: Context) -> Union[dict, str]:
        """Read the latest text of a note."""
        me(ctx)
        note = store.notes_read(topic)
        return asdict(note) if note else f"no note named {topic}"

    @server.tool()
    @_tool_errors
    def notes_write(topic: str, text: str, ctx: Context) -> str:
        """Write a new version of a note (older versions are kept)."""
        store.notes_write(me(ctx).key, topic, text)
        return f"saved note {topic}"

    @server.custom_route("/bridge/heartbeat", methods=["POST"])
    @_bad_request
    async def bridge_heartbeat(request: Request) -> Response:
        body = await request.json()
        store.heartbeat(body["machine"], body["sessions"], body.get("tools", ()))
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

    @server.custom_route("/", methods=["GET"])
    async def page(request: Request) -> Response:
        html = (importlib.resources.files("kaptyorka") / "web/index.html").read_text("utf-8")
        return HTMLResponse(html, headers={"Cache-Control": "no-store", "Content-Security-Policy": CSP})

    app = server.streamable_http_app(host="0.0.0.0")
    app.add_middleware(Auth, store=store)
    return app
