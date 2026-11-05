"""`kaptyorka mcp`: a stdio MCP server that relays to the hub with its session's identity."""
import os
from typing import Dict, Optional

import anyio
import httpx2
import mcp_types as types
from mcp import Client, MCPError
from mcp.client.streamable_http import streamable_http_client
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

from kaptyorka import http, identity

TIMEOUT_S = 4.5  # whole call, connection included: the agent hears back within 5 s
HEARTBEAT_S = 10
OPENCODE_META = "ai.opencode/sessionID"  # OpenCode V2 tags each tool call with its session
TRANSPORT_ERRORS = (types.CONNECTION_CLOSED, types.REQUEST_TIMEOUT)  # the client's, not the hub's
FALLBACK_TOOLS = ["who", "send", "inbox", "status", "notes_list", "notes_read", "notes_write"]


class Relay:
    """One hub client, opened on first use and dropped after any failed call."""

    def __init__(self, url: str, headers: dict, tg):
        self.url, self.headers, self.tg = url, headers, tg
        self.client: Optional[Client] = None
        self.stop: Optional[anyio.Event] = None
        self.lock = anyio.Lock()

    async def _hold(self, *, task_status) -> None:
        # The client lives in its own task: anyio needs a context entered and left by one task.
        stop = anyio.Event()
        started = False
        try:
            async with httpx2.AsyncClient(headers=self.headers,
                                         timeout=httpx2.Timeout(TIMEOUT_S, read=None)) as h:
                async with Client(streamable_http_client(self.url + "/mcp", http_client=h)) as c:
                    started = True
                    task_status.started((c, stop))
                    await stop.wait()
        except Exception:
            if not started:
                raise
            # closing a client whose hub is gone fails; nothing to do about it

    async def use(self, fn):
        """Run fn(client) within TIMEOUT_S. On a transport failure drop the client and return
        None. Any other JSON-RPC error (MCPError) is raised to show its message; the client is
        dropped then too, since an expired session after a quick hub restart arrives as one."""
        client = None
        try:
            with anyio.fail_after(TIMEOUT_S):
                async with self.lock:
                    if self.client is None:
                        self.client, self.stop = await self.tg.start(self._hold)
                    client = self.client
                return await fn(client)
        except MCPError as e:
            await self.drop(client)
            if e.code in TRANSPORT_ERRORS:
                return None
            raise
        except Exception:
            await self.drop(client)
            return None

    async def drop(self, client: Optional[Client]) -> None:
        """Close client if it is still the current one, so a late failure keeps a newer client."""
        async with self.lock:
            if client is not None and self.client is client:
                self.stop.set()
                self.client = self.stop = None


async def _heartbeat(cfg: dict, ident: identity.Identity) -> None:
    body = {"machine": ident.machine,
            "sessions": [{"key": ident.key, "tool": ident.tool, "project": ident.project}]}
    while True:
        try:
            await anyio.to_thread.run_sync(lambda: http.request(
                "POST", cfg["hub"] + "/bridge/heartbeat", cfg["token"], body, timeout=TIMEOUT_S))
        except http.HubUnreachable:
            pass
        await anyio.sleep(HEARTBEAT_S)


def _headers(cfg: dict, ident: Optional[identity.Identity]) -> dict:
    return {"Authorization": f"Bearer {cfg['token']}", **(ident.headers() if ident else {})}


async def _serve(cfg: dict, workspace: Optional[str]) -> None:
    url = cfg["hub"]
    unreachable = f"kaptyorka hub unreachable at {url}"

    async with anyio.create_task_group() as tg:
        relay = Relay(url, _headers(cfg, None), tg)
        found = {"ident": None}

        async def identify() -> None:
            # Retried on each call until found: Claude Code may start the shim before it
            # writes its session file.
            # Claude Code rewrites its session id on /clear and /resume, so it is re-read each call.
            if found["ident"] and found["ident"].tool != "claude-code":
                return
            ident = await anyio.to_thread.run_sync(
                lambda: identity.identify(cfg.get("machine", ""), workspace=workspace))
            if ident and ident != found["ident"]:
                found["ident"] = ident
                relay.headers = _headers(cfg, ident)
                await relay.drop(relay.client)
                if ident.tool == "cursor":
                    tg.start_soon(_heartbeat, cfg, ident)  # Cursor has no bridge to find its sessions

        async def list_tools(ctx, params) -> types.ListToolsResult:
            try:
                r = await relay.use(lambda c: c.list_tools())
            except MCPError:
                r = None
            if r is not None:
                return types.ListToolsResult(tools=r.tools)
            return types.ListToolsResult(tools=[
                types.Tool(name=n, description=unreachable, input_schema={"type": "object"})
                for n in FALLBACK_TOOLS])

        per_session: Dict[str, Relay] = {}  # OpenCode V2: one shim serves every session in a folder

        def session_relay(sid: str) -> Relay:
            if sid not in per_session:
                ident = identity.Identity(f"opencode:{sid}", cfg.get("machine", ""), "opencode",
                                          os.path.basename(os.getcwd().rstrip("/")))
                per_session[sid] = Relay(url, _headers(cfg, ident), tg)
            return per_session[sid]

        async def call_tool(ctx, params) -> types.CallToolResult:
            sid = (params.meta or {}).get(OPENCODE_META)
            if sid and isinstance(sid, str):
                use = session_relay(sid)
            else:
                await identify()
                use = relay
            try:
                r = await use.use(lambda c: c.call_tool(params.name, params.arguments or {}))
            except MCPError as e:
                return types.CallToolResult(content=[types.TextContent(text=e.message)],
                                            is_error=True)
            if r is not None:
                return r
            return types.CallToolResult(content=[types.TextContent(text=unreachable)],
                                        is_error=True)

        server = Server("kaptyorka", on_list_tools=list_tools, on_call_tool=call_tool)
        await identify()
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())
        tg.cancel_scope.cancel()


def run(cfg: dict, workspace: Optional[str] = None) -> None:
    anyio.run(_serve, cfg, workspace)
