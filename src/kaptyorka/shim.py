"""`kaptyorka mcp`: a stdio MCP server that relays to the hub with its session's identity."""
import dataclasses
import json
import os
import re
import sys
import traceback
from pathlib import Path
from typing import Dict, Optional

import anyio
import httpx2
import mcp_types as types
from mcp import Client, MCPError
from mcp.client.streamable_http import streamable_http_client
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

from urllib.parse import quote

import kaptyorka
# Everything a tool call needs is loaded now: an upgrade replaces these files under a running
# shim, and a module first imported after it would be the new one next to old ones in memory.
from kaptyorka import facts, http, identity, launch, offhub, optin, overnight, paths, repos, tls  # noqa: F401
from kaptyorka.adapters.opencode import valid_id as opencode_id
from kaptyorka.store import MAX_FILE

TIMEOUT_S = 4.5  # whole call, connection included: the agent hears back within 5 s
HEARTBEAT_S = 10
OPENCODE_META = "ai.opencode/sessionID"  # OpenCode V2 tags each tool call with its session
UPDATED = ("kaptyorka was updated on this machine; run /mcp and reconnect kaptyorka (or restart"
           " this session) to use the new version")
NO_MANAGER_CONFIG = "kaptyorka: the manager's config is missing on this machine; no hub"
TRANSPORT_ERRORS = (types.CONNECTION_CLOSED, types.REQUEST_TIMEOUT)  # the client's, not the hub's
FALLBACK_TOOLS = ["who", "send", "inbox", "status", "notes_list", "notes_read", "notes_write",
                  "projects", "knock", "answer", "door_close", "request_access", "fetch_file",
                  "work", "workers", "adopt", "close_worker", "state", "state_write", "decide",
                  "rules", "propose_rule", "conflict", "work_decide", "log", "machines",
                  "door_request", "door_decide", "resume_session", "lesson", "reach"]
JOIN = types.Tool(  # answered by the shim itself: it saves the choice on this machine
    name="join_team",
    description=("Put this session on the team hub when its folder is no team project. Only "
                 "when the user asked for it. project: a team project to work for, \"\" to "
                 "coordinate sessions across projects (then name a project for notes and work), "
                 "or \"off\" to leave. Same as /hub team [project] in Claude Code."),
    input_schema={"type": "object", "properties": {"project": {"type": "string"}}})


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
            # the bridge may have moved the hub to https since this shim started (tls.py); an
            # https hub this machine pinned is trusted by that certificate alone
            self.url = await anyio.to_thread.run_sync(tls.current_url, self.url)
            pins = tls.pins_for(self.url) if self.url.startswith("https://") else []
            verify = await anyio.to_thread.run_sync(tls.client_context, self.url, pins) if pins else True
            async with httpx2.AsyncClient(headers=self.headers, verify=verify,
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


async def _heartbeat(cfg: dict, first: identity.Identity) -> None:
    own = identity.project_of(first.cwd) if first.cwd else first.project  # its folder's own
    while True:
        # a join or leave (/hub team) changes the project it reports
        ident = await anyio.to_thread.run_sync(
            optin.apply, dataclasses.replace(first, project=own))
        body = {"machine": ident.machine,
                "sessions": [{"key": ident.key, "tool": ident.tool, "project": ident.project}],
                "facts": identity.facts()}
        if await anyio.to_thread.run_sync(offhub.personal, ident.project, ident.cwd, ident.key) \
                or await anyio.to_thread.run_sync(offhub.off, ident.key):
            await anyio.sleep(HEARTBEAT_S)  # not a team project's folder, or /hub off: never reported
            continue
        try:
            await anyio.to_thread.run_sync(lambda: http.request(
                "POST", cfg["hub"] + "/bridge/heartbeat", cfg["token"], body, timeout=TIMEOUT_S))
        except http.HubUnreachable:
            pass
        await anyio.sleep(HEARTBEAT_S)


def _machine(cfg: dict) -> str:
    """This machine's name as its config says now: a shim lives as long as its tool (OpenCode's
    for days), and a machine that joined again under a new name names its sessions so, like
    the bridge and the hooks do. The manager's shim keeps its own config's name."""
    try:
        now = paths.load_config().get("machine")
    except (OSError, ValueError, AttributeError):
        now = None
    return now if isinstance(now, str) and now else cfg.get("machine", "")


def _headers(cfg: dict, ident: Optional[identity.Identity]) -> dict:
    return {"Authorization": f"Bearer {cfg['token']}", **(ident.headers() if ident else {})}


def _error(message: str) -> types.CallToolResult:
    return types.CallToolResult(content=[types.TextContent(text=message)], is_error=True)


def _installed_version() -> str:
    """The kaptyorka version on disk now ("" when unreadable), not the one loaded."""
    try:
        text = (Path(kaptyorka.__file__).parent / "__init__.py").read_text()
    except OSError:
        return ""
    m = re.search(r"""__version__\s*=\s*["']([^"']+)["']""", text)
    return m.group(1) if m else ""


def _failed(e: Exception) -> types.CallToolResult:
    """A tool call that raised: after an upgrade on disk, a plain line on how to reconnect
    instead of the client's bare "Internal server error"; otherwise raised as it was."""
    installed = _installed_version()
    if installed and installed != kaptyorka.__version__:
        traceback.print_exception(e, file=sys.stderr)
        return _error(UPDATED)
    raise e


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
        beating: set = set()  # Cursor session keys with a heartbeat task

        async def identify() -> None:
            # Retried on each call until found: Claude Code may start the shim before it
            # writes its session file.
            # Claude Code rewrites its session id on /clear and /resume, so it is re-read each call.
            if found["ident"] and found["ident"].tool != "claude-code" \
                    and found["ident"].machine == _machine(cfg):  # a new name: found again
                return
            ident = await anyio.to_thread.run_sync(
                lambda: identity.identify(_machine(cfg), workspace=workspace))
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
                if ident.tool == "cursor" and ident.key not in beating:
                    beating.add(ident.key)
                    tg.start_soon(_heartbeat, cfg, ident)  # Cursor has no bridge to find its sessions

        async def hidden() -> bool:
            """Kept off the hub by its person (`/hub off`): nothing goes to the hub."""
            ident = found["ident"]
            return ident is not None and await anyio.to_thread.run_sync(offhub.off, ident.key)

        async def personal(ident: Optional[identity.Identity]) -> str:
            """Why nothing goes to the hub ("" when it may): not a team project's folder, or
            no team project list yet."""
            return "" if ident is None else await anyio.to_thread.run_sync(
                offhub.personal, ident.project, ident.cwd, ident.key)

        async def list_tools(ctx, params) -> types.ListToolsResult:
            r = None
            mine = await personal(found["ident"])
            try:
                if not found["off"] and not mine and not await hidden():
                    r = await relay.use(lambda c: c.list_tools())
            except MCPError:
                r = None
            if r is not None:
                return types.ListToolsResult(tools=[*r.tools, JOIN])
            return types.ListToolsResult(tools=[
                types.Tool(name=n, description=mine or unreachable,
                           input_schema={"type": "object"})
                for n in FALLBACK_TOOLS] + [JOIN])

        per_session: Dict[str, Relay] = {}  # OpenCode V2: one shim serves every session in a folder

        def meta_ident(sid: str) -> identity.Identity:
            return optin.apply(identity.Identity(
                f"opencode:{sid}", _machine(cfg), "opencode",
                identity.project_of(os.getcwd()), os.getcwd(),
                int(os.environ.get("OPENCODE_PID") or os.getppid())))

        def session_relay(sid: str, ident: identity.Identity) -> Relay:
            if sid not in per_session or per_session[sid].headers != _headers(cfg, ident):
                per_session[sid] = Relay(url, _headers(cfg, ident), tg)  # joined or left
            return per_session[sid]

        async def join(who: Optional[identity.Identity], args: dict) -> types.CallToolResult:
            """join_team: ask the hub, save the choice here, and speak as it from now on."""
            if who is None:
                return _error("kaptyorka: this session is not detected yet; try again")
            if paths.in_manager_folder(who.cwd):
                return _error("kaptyorka: the manager session is on the hub already")
            if await anyio.to_thread.run_sync(offhub.off, who.key):
                return _error(f"kaptyorka: {offhub.come_back(who.key)}")
            project = args.get("project") if isinstance(args.get("project"), str) else ""
            ok, line = await anyio.to_thread.run_sync(optin.join, cfg, who.key, who.pid, project)
            if ok and found["ident"] is not None and found["ident"].key == who.key:
                found["ident"] = None
                await identify()  # found again, with the project it chose (or its own)
            return types.CallToolResult(content=[types.TextContent(text=line)]) if ok \
                else _error(line)

        async def call_tool(ctx, params) -> types.CallToolResult:
            try:
                return await tool_call(params)
            except Exception as e:
                return _failed(e)

        async def tool_call(params) -> types.CallToolResult:
            sid = (params.meta or {}).get(OPENCODE_META)
            if sid and isinstance(sid, str) and opencode_id(sid):
                who = meta_ident(sid)
                use = session_relay(sid, who)
            else:
                await identify()
                use, who = relay, found["ident"]
            if found["off"]:
                return _error(NO_MANAGER_CONFIG)
            if params.name == JOIN.name:
                return await join(who, dict(params.arguments or {}))
            why = await personal(who)
            if why == paths.NOT_TEAM:
                why = paths.not_team_help(await anyio.to_thread.run_sync(offhub.load_mine),
                                         optin.command(who.key) if who else "/hub")
            if why:
                return _error(f"kaptyorka: {why}")
            if (await hidden() if use is relay
                    else await anyio.to_thread.run_sync(offhub.off, who.key)):
                return _error(f"kaptyorka: {offhub.come_back(who.key if who else '')}")
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
