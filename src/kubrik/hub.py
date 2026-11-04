"""The hub app: MCP tools over streamable HTTP behind a bearer token."""
import functools
import hmac
import inspect
from dataclasses import asdict
from typing import List, Union

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse

from kubrik.store import Agent, KubrikError, Store

INSTRUCTIONS = (
    "kubrik connects you with the other coding agents on this team. "
    "who() lists online sessions; send(to, text) messages one by name or 'all'; "
    "inbox() reads your messages; status(text) says what you are working on; "
    "notes_list/notes_read/notes_write share a team board, one text per topic."
)


class BearerAuth:
    """ASGI wrapper: 401 unless the bearer token matches; GET / is open."""

    def __init__(self, app, token: str):
        self.app = app
        self.expected = f"Bearer {token}".encode()

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and not (scope["method"] == "GET" and scope["path"] == "/"):
            auth = dict(scope["headers"]).get(b"authorization", b"")
            if not hmac.compare_digest(auth, self.expected):
                await PlainTextResponse("unauthorized", status_code=401)(scope, receive, send)
                return
        await self.app(scope, receive, send)


def _tool_errors(fn):
    """Show a KubrikError to the agent as a tool error with its message."""
    if inspect.iscoroutinefunction(fn):
        @functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            try:
                return await fn(*args, **kwargs)
            except KubrikError as e:
                raise ToolError(str(e)) from e
    else:
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            try:
                return fn(*args, **kwargs)
            except KubrikError as e:
                raise ToolError(str(e)) from e
    return wrapper


def build_app(store: Store, token: str) -> Starlette:
    server = MCPServer("kubrik", instructions=INSTRUCTIONS)

    def me(ctx: Context) -> Agent:
        h = ctx.headers or {}
        ident = [h.get(f"x-kubrik-{k}") for k in ("key", "machine", "tool", "project")]
        if not all(ident):
            raise KubrikError("no kubrik identity; run kubrik join")
        return store.seen(*ident)

    @server.tool()
    @_tool_errors
    def who(ctx: Context) -> List[dict]:
        """Online sessions: name, machine, tool, project, status, activity, last seen."""
        key = me(ctx).key
        now = store.clock()
        return [{"name": a.name, "machine": a.machine, "tool": a.tool, "project": a.project,
                 "status": a.status, "activity": a.activity,
                 "last_seen_s": round(now - a.last_seen), "you": a.key == key}
                for a in store.online()]

    @server.tool()
    @_tool_errors
    async def send(to: str, text: str, ctx: Context) -> str:
        """Send text to one session by name, or to 'all'."""
        n = store.send(me(ctx).key, to, text)
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

    app = server.streamable_http_app(host="0.0.0.0")
    app.add_middleware(BearerAuth, token=token)
    return app
