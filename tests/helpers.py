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


def write_session_file(home, pid, cwd="/w/murmur", status="idle", version="2.1.318",
                       sock=None, peer_token=None, name=None, name_source=None, session_id=None):
    """A Claude Code session file (and its .key file when peer_token is given)."""
    import json

    d = home / ".claude" / "sessions"
    d.mkdir(parents=True, exist_ok=True)
    data = {"pid": pid, "sessionId": session_id or f"sid-{pid}", "cwd": cwd, "status": status, "version": version}
    if name is not None:
        data["name"], data["nameSource"] = name, name_source
    if sock is not None:
        data["messagingSocketPath"] = str(sock)
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
