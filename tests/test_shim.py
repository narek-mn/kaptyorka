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

IDENT = {"key": "claude:1", "machine": "pc", "tool": "claude-code", "project": "proj-a"}


def shim(home, hub_url, ident=IDENT):
    paths.save_config({"hub": hub_url, "token": "T", "machine": "pc"})
    env = {"HOME": str(home), "KAPTYORKA_HOME": os.environ["KAPTYORKA_HOME"]}
    if ident:
        env["KAPTYORKA_TEST_IDENTITY"] = json.dumps(ident)
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
                             "notes_write", "projects", "knock", "answer", "door_close",
                             "request_access", "fetch_file", "work", "workers", "adopt",
                             "close_worker"}
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
        store = Store(db, legacy_token="T")
        stop = start_hub(store, port)
        try:
            async with shim(home, url) as c:
                assert not (await c.call_tool("who", {})).is_error
                await anyio.to_thread.run_sync(stop)
                store.close()
                r = await c.call_tool("who", {})
                assert text(r) == f"kaptyorka hub unreachable at {url}"
                store2 = Store(db, legacy_token="T")
                stop2 = start_hub(store2, port)
                try:
                    r = await c.call_tool("who", {})
                    assert not r.is_error, text(r)
                    # quick restart, no call while down: the stale session heals on the next call
                    await anyio.to_thread.run_sync(stop2)
                    stop2 = start_hub(store2, port)
                    first = await c.call_tool("who", {})
                    assert text(first) != f"kaptyorka hub unreachable at {url}"
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


def test_shim_non_ascii_identity(hub, home):
    ident = {"key": "cursor:/w/мурмур", "machine": "пк", "tool": "cursor", "project": "мурмур"}

    async def go():
        async with shim(home, hub.url, ident) as c:
            r = await c.call_tool("who", {})
            assert not r.is_error, text(r)
            assert r.structured_content["result"][0]["project"] == "мурмур"
    anyio.run(go)
    [a] = hub.store.online()
    assert (a.key, a.machine, a.project) == ("cursor:/w/мурмур", "пк", "мурмур")


def test_shim_finds_claude_session_file_written_late(hub, home):
    # The shim's parent is this test process; its session file appears after the shim started.
    async def go():
        async with shim(home, hub.url, ident=None) as c:
            r = await c.call_tool("who", {})
            assert r.is_error and "no kaptyorka identity" in text(r)
            d = home / ".claude" / "sessions"
            d.mkdir(parents=True)
            (d / f"{os.getpid()}.json").write_text(json.dumps({"cwd": "/w/proj-a", "sessionId": "sid-x"}))
            r = await c.call_tool("who", {})
            assert not r.is_error, text(r)
    anyio.run(go)
    [a] = hub.store.online()
    assert (a.key, a.tool, a.project) == ("claude:sid-x", "claude-code", "proj-a")


def test_shim_passes_hub_errors_through(hub, home):
    async def go():
        async with shim(home, hub.url) as c:
            r = await c.call_tool("no_such_tool", {})
            assert r.is_error and "unreachable" not in text(r), text(r)
            assert not (await c.call_tool("who", {})).is_error
    anyio.run(go)


def test_shim_opencode_v2_session_from_call_meta(hub, home):
    # OpenCode V2 runs one shim per folder and tags each call with the calling session.
    async def go():
        async with shim(home, hub.url, ident=None) as c:
            for sid in ("ses_a", "ses_b"):
                r = await c.call_tool("who", {}, meta={"ai.opencode/sessionID": sid})
                assert not r.is_error, text(r)
    anyio.run(go)
    assert sorted((a.key, a.tool) for a in hub.store.online()) == [
        ("opencode:ses_a", "opencode"), ("opencode:ses_b", "opencode")]


def test_shim_follows_claude_session_id_change(hub, home):
    d = home / ".claude" / "sessions"
    d.mkdir(parents=True)
    f = d / f"{os.getpid()}.json"
    f.write_text(json.dumps({"cwd": "/w/proj-a", "sessionId": "old"}))

    async def go():
        async with shim(home, hub.url, ident=None) as c:
            assert not (await c.call_tool("who", {})).is_error
            f.write_text(json.dumps({"cwd": "/w/proj-a", "sessionId": "new"}))  # /clear
            assert not (await c.call_tool("who", {})).is_error
    anyio.run(go)
    assert {a.key for a in hub.store.online()} == {"claude:old", "claude:new"}


def test_shim_send_uploads_local_path(hub, home, tmp_path):
    pid, tok = __import__("helpers").person(hub.store, "ann", "admin")
    src = tmp_path / "shot.png"
    src.write_bytes(b"PNGDATA")
    hub.store.seen("claude:2", "pc", "claude-code", "other")
    other = hub.store.agent("claude:2").name

    async def go():
        async with shim(home, hub.url) as c:
            r = await c.call_tool("send", {"to": other, "text": "hi", "file": str(src)})
            assert not r.is_error, text(r)
            r = await c.call_tool("send", {"to": other, "text": "hi", "file": str(tmp_path / "no.png")})
            assert r.is_error and "cannot read" in text(r)
            big = tmp_path / "big.png"
            big.write_bytes(b"x" * 5_000_001)
            r = await c.call_tool("send", {"to": other, "text": "hi", "file": str(big)})
            assert r.is_error and text(r) == "file too large: 5000001 bytes, limit 5000000"
            bad = tmp_path / "x.exe"
            bad.write_bytes(b"x")
            r = await c.call_tool("send", {"to": other, "text": "hi", "file": str(bad)})
            assert r.is_error and "file type .exe not allowed" in text(r)
    anyio.run(go)
    with hub.store.lock:
        rows = hub.store.db.execute("SELECT * FROM files").fetchall()
    assert [(r["name"], r["size"]) for r in rows] == [("shot.png", 7)]
    assert (paths.files_dir() / str(rows[0]["id"])).read_bytes() == b"PNGDATA"


def test_shim_fetch_file_saves_to_inbox(hub, home):
    pid, tok = __import__("helpers").person(hub.store, "ann", "admin")
    fid = hub.store.file_add("a b/notes.txt", b"hello", pid)

    async def go():
        async with shim(home, hub.url) as c:
            r = await c.call_tool("fetch_file", {"id": fid})
            assert not r.is_error, text(r)
            out = json.loads(text(r))
            assert out["name"] == "a b/notes.txt" and out["size"] == 5
            assert out["path"] == str(paths.inbox_dir() / f"{fid}-notes.txt")
            with open(out["path"], "rb") as f:
                assert f.read() == b"hello"
            r = await c.call_tool("fetch_file", {"id": 999})
            assert r.is_error and "no such file" in text(r)
    anyio.run(go)
