"""`kaptyorka mcp`: a stdio MCP server that relays to the hub with its session's identity."""
import dataclasses
import json
import os
from typing import Dict, Optional

import anyio
import httpx2
import mcp_types as types
from mcp import Client, MCPError
from mcp.client.streamable_http import streamable_http_client
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

from urllib.parse import quote

from kaptyorka import http, identity, paths
from kaptyorka.store import MAX_FILE

TIMEOUT_S = 4.5  # whole call, connection included: the agent hears back within 5 s
HEARTBEAT_S = 10
OPENCODE_META = "ai.opencode/sessionID"  # OpenCode V2 tags each tool call with its session
NO_MANAGER_CONFIG = "kaptyorka: the manager's config is missing on this machine; no hub"
TRANSPORT_ERRORS = (types.CONNECTION_CLOSED, types.REQUEST_TIMEOUT)  # the client's, not the hub's
FALLBACK_TOOLS = ["who", "send", "inbox", "status", "notes_list", "notes_read", "notes_write",
                  "projects", "knock", "answer", "door_close", "request_access", "fetch_file",
                  "work", "workers", "adopt", "close_worker", "state", "state_write", "decide",
                  "rules", "propose_rule", "conflict"]


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
            "sessions": [{"key": ident.key, "tool": ident.tool, "project": ident.project}],
            "facts": identity.facts()}
    while True:
        try:
            await anyio.to_thread.run_sync(lambda: http.request(
                "POST", cfg["hub"] + "/bridge/heartbeat", cfg["token"], body, timeout=TIMEOUT_S))
        except http.HubUnreachable:
            pass
        await anyio.sleep(HEARTBEAT_S)


def _headers(cfg: dict, ident: Optional[identity.Identity]) -> dict:
    return {"Authorization": f"Bearer {cfg['token']}", **(ident.headers() if ident else {})}


def _error(message: str) -> types.CallToolResult:
    return types.CallToolResult(content=[types.TextContent(text=message)], is_error=True)


def _private_place(path: str) -> Optional[str]:
    """The private folder (~/.kaptyorka or ~/.ssh) a path really lies in, or None. Files
    kaptyorka itself saved to the inbox may be passed on."""
    real = os.path.realpath(path)

    def under(folder) -> bool:
        folder = os.path.realpath(folder)
        return real == folder or real.startswith(folder.rstrip(os.sep) + os.sep)

    if under(paths.inbox_dir()):
        return None
    home = os.path.expanduser("~")
    for shown, folder in (("~/.kaptyorka", os.path.join(home, ".kaptyorka")),
                          ("~/.kaptyorka", str(paths.home())), ("~/.ssh", os.path.join(home, ".ssh"))):
        if under(folder):
            return shown
    return None


def _upload(cfg: dict, headers: dict, args: dict) -> Optional[str]:
    """Turn a local path in args["file"] into "id:<n>" by uploading it. Returns an error text."""
    path = os.path.expanduser(args["file"])
    refused = _private_place(path)
    if refused:
        return f"refusing to upload {args['file']}: it is in {refused}, where keys and tokens live"
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError as e:
        return f"cannot read {args['file']}: {e.strerror or e}"
    if len(data) > MAX_FILE:  # the hub refuses early and drops the connection mid-upload
        return f"file too large: {len(data)} bytes, limit {MAX_FILE}"
    status, body, _ = http.request_raw(
        "POST", cfg["hub"] + "/api/files", cfg["token"], data,
        {**headers, "X-File-Name": quote(os.path.basename(path))})
    if status // 100 != 2:
        return body.decode("utf-8", "replace") or f"upload failed ({status})"
    args["file"] = f"id:{json.loads(body)['id']}"
    return None


def _download(cfg: dict, headers: dict, info: dict) -> "dict | str":
    """Save the file the hub described to the inbox folder; return {path, name, size}, or the
    hub's answer as an error text when it refused."""
    status, body, _ = http.request_raw("GET", cfg["hub"] + info["url"], cfg["token"], None, headers)
    if status // 100 != 2:
        return body.decode("utf-8", "replace").strip() or f"download failed ({status})"
    dest = paths.inbox_dir() / f"{info['id']}-{os.path.basename(info['name'])}"
    dest.write_bytes(body)
    return {"path": str(dest), "name": info["name"], "size": info["size"]}


async def _serve(cfg: dict, workspace: Optional[str]) -> None:
    url = cfg["hub"]
    unreachable = f"kaptyorka hub unreachable at {url}"

    async with anyio.create_task_group() as tg:
        relay = Relay(url, _headers(cfg, None), tg)
        found = {"ident": None, "off": False}  # off: the manager's folder without its config

        async def identify() -> None:
            # Retried on each call until found: Claude Code may start the shim before it
            # writes its session file.
            # Claude Code rewrites its session id on /clear and /resume, so it is re-read each call.
            if found["ident"] and found["ident"].tool != "claude-code":
                return
            ident = await anyio.to_thread.run_sync(
                lambda: identity.identify(cfg.get("machine", ""), workspace=workspace))
            if ident and paths.in_manager_folder(ident.cwd):  # only the manager's config
                own = await anyio.to_thread.run_sync(paths.load_config, ident.cwd)
                found["off"] = not (own.get("hub") and own.get("token"))
                if found["off"]:
                    found["ident"] = None
                    await relay.drop(relay.client)
                    return
                if own != cfg:
                    cfg.clear()
                    cfg.update(own)
                    relay.url = cfg["hub"]
                    found["ident"] = None  # new token: reconnect below
                ident = dataclasses.replace(ident, machine=cfg.get("machine", ""))
            if ident and ident != found["ident"]:
                found["ident"] = ident
                relay.headers = _headers(cfg, ident)
                await relay.drop(relay.client)
                if ident.tool == "cursor":
                    tg.start_soon(_heartbeat, cfg, ident)  # Cursor has no bridge to find its sessions

        async def list_tools(ctx, params) -> types.ListToolsResult:
            r = None
            try:
                if not found["off"]:
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
            if found["off"]:
                return _error(NO_MANAGER_CONFIG)
            args = dict(params.arguments or {})
            f = args.get("file")
            try:
                if params.name == "send" and isinstance(f, str) and f and not f.startswith("id:"):
                    err = await anyio.to_thread.run_sync(_upload, cfg, use.headers, args)
                    if err:
                        return _error(err)
                r = await use.use(lambda c: c.call_tool(params.name, args))
                if params.name == "fetch_file" and r is not None and not r.is_error:
                    out = await anyio.to_thread.run_sync(
                        _download, cfg, use.headers, r.structured_content["result"])
                    if isinstance(out, str):
                        return _error(out)
                    return types.CallToolResult(content=[types.TextContent(text=json.dumps(out))],
                                                structured_content={"result": out})
            except MCPError as e:
                return _error(e.message)
            except http.HubUnreachable:
                return _error(unreachable)
            if r is not None:
                return r
            return _error(unreachable)

        server = Server("kaptyorka", on_list_tools=list_tools, on_call_tool=call_tool)
        await identify()
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())
        tg.cancel_scope.cancel()


def run(cfg: dict, workspace: Optional[str] = None) -> None:
    anyio.run(_serve, cfg, workspace)
