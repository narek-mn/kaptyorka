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
