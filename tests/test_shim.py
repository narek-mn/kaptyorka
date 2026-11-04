import json
import os
import socket
import sys
import time

import anyio
from mcp import Client, StdioServerParameters

from kaptyorka import paths
from kaptyorka.store import Store

from conftest import start_hub

IDENT = {"key": "claude:1", "machine": "pc", "tool": "claude-code", "project": "murmur"}


def shim(home, hub_url, ident=IDENT):
    paths.save_config({"hub": hub_url, "token": "T", "machine": "pc"})
    env = {"HOME": str(home), "KAPTYORKA_HOME": os.environ["KAPTYORKA_HOME"],
           "KAPTYORKA_TEST_IDENTITY": json.dumps(ident)}
    return Client(StdioServerParameters(
        command=sys.executable, args=["-c", "from kaptyorka.cli import entry; entry()", "mcp"],
        env=env))


def text(r) -> str:
    return r.content[0].text


def test_shim_relays_with_identity(hub, home):
    async def go():
        async with shim(home, hub.url) as c:
            names = {t.name for t in (await c.list_tools()).tools}
            assert {"who", "send", "inbox", "status", "notes_list", "notes_read",
                    "notes_write"} <= names
            r = await c.call_tool("who", {})
            assert not r.is_error, text(r)
            assert r.structured_content["result"][0]["you"] is True
    anyio.run(go)
    assert [a.key for a in hub.store.online()] == ["claude:1"]


def test_shim_hub_down(home):
    async def go():
        async with shim(home, "http://127.0.0.1:9") as c:
            names = {t.name for t in (await c.list_tools()).tools}
            assert names == {"who", "send", "inbox", "status", "notes_list", "notes_read",
                             "notes_write"}
            t0 = time.monotonic()
            r = await c.call_tool("who", {})
            assert time.monotonic() - t0 < 5
            assert r.is_error and text(r) == "kaptyorka hub unreachable at http://127.0.0.1:9"
    anyio.run(go)


def test_shim_reconnects_after_hub_restart(home, tmp_path):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    url = f"http://127.0.0.1:{port}"
    db = tmp_path / "restart.db"

    async def go():
        store = Store(db)
        stop = start_hub(store, port)
        try:
            async with shim(home, url) as c:
                assert not (await c.call_tool("who", {})).is_error
                await anyio.to_thread.run_sync(stop)
                store.close()
                r = await c.call_tool("who", {})
                assert text(r) == f"kaptyorka hub unreachable at {url}"
                store2 = Store(db)
                stop2 = start_hub(store2, port)
                try:
                    r = await c.call_tool("who", {})
                    assert not r.is_error, text(r)
                finally:
                    await anyio.to_thread.run_sync(stop2)
                    store2.close()
        finally:
            await anyio.to_thread.run_sync(stop)  # no-op if already stopped
    anyio.run(go)


def test_shim_cursor_heartbeats_itself(hub, home):
    ident = {"key": "cursor:/w/app", "machine": "pc", "tool": "cursor", "project": "app"}

    async def go():
        async with shim(home, hub.url, ident) as c:
            await c.list_tools()  # list_tools does not register the caller
            with anyio.fail_after(5):
                while not hub.store.online():
                    await anyio.sleep(0.05)
    anyio.run(go)
    assert [a.key for a in hub.store.online()] == ["cursor:/w/app"]
