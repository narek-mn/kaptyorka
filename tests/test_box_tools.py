"""OpenCode, Codex and Cursor inside a personal laptop's box (secure design v2, slice 5): the
helper's per-tool hooks, delivery through each tool's own server (a fake OpenCode server, a fake
Codex app-server socket), state and names, close, box-exec's per-tool plans, and the tools' own
lock-down files. The real tools in the real box run in secure-design/testbox/slice5.sh."""
import base64
import http.server
import json
import os
import socket
import struct
import threading
from pathlib import Path

import pytest

from kaptyorka.secure import kap_box_exec as bx
from kaptyorka.secure import kap_helper as H
from kaptyorka.secure import kap_toolwire as tw
from kaptyorka.secure import kapcommon as kc

FILES = Path(H.__file__).parent / "files" / "box"
SID = "ses_de7cd12ebffewtADFhkxY31Crg"
TID = "01a21831-d19c-72a2-b14b-81db97cccc97"


@pytest.fixture
def box(tmp_path, monkeypatch):
    d = tmp_path / "s"
    d.mkdir()
    rule = d / "rule.md"
    rule.write_text("You are a team chat.")
    monkeypatch.setattr(H, "STANDING", str(rule))
    monkeypatch.setattr(H, "VIEW", str(d / "srv"))
    (d / "srv" / "alice" / "proj-a").mkdir(parents=True)
    return type("Box", (), {"dir": d, "state": str(d / "chats"), "cwd": str(d / "srv" / "alice" / "proj-a"),
                            "home": str(d / "home")})


class Door:
    """A fake relay door (as in test_box_helper)."""

    def __init__(self, path, answer):
        self.path, self.requests, self.answer = str(path), [], answer
        self.sock = socket.socket(socket.AF_UNIX)
        self.sock.bind(self.path)
        self.sock.listen(8)
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        while True:
            try:
                c, _ = self.sock.accept()
            except OSError:
                return
            with c:
                buf = b""
                while b"\n" not in buf:
                    got = c.recv(65536)
                    if not got:
                        break
                    buf += got
                req = json.loads(buf.split(b"\n")[0])
                self.requests.append(req)
                c.sendall(json.dumps({"ok": True, "result": self.answer(req["call"], req["args"])}).encode() + b"\n")


class FakeOpenCode:
    """An OpenCode server on 127.0.0.1: V1 routes (/session/...) and V2 routes (/api/session/...)."""

    def __init__(self, busy=False, title="Fix the login page", password=""):
        self.posts, self.busy, self.title, self.password = [], busy, title, password
        me = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, obj=None):
                raw = json.dumps(obj).encode() if obj is not None else b""
                self.send_response(code)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _auth_ok(self):
                if not me.password:
                    return True
                want = "Basic " + base64.b64encode(f"opencode:{me.password}".encode()).decode()
                return self.headers.get("Authorization") == want

            def do_GET(self):
                path = self.path.split("?")[0]
                if not self._auth_ok():
                    return self._send(401)
                if path == "/session/status":
                    return self._send(200, {SID: {"type": "busy"}} if me.busy else {})
                if path in (f"/session/{SID}",):
                    return self._send(200, {"id": SID, "title": me.title})
                if path == f"/api/session/{SID}":
                    t = {"busy": 2000, "idle": 1000} if me.busy else {"busy": 1000, "idle": 2000}
                    return self._send(200, {"data": {"id": SID, "title": me.title, "time": t}})
                self._send(404)

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"null")
                if not self._auth_ok():
                    return self._send(401)
                me.posts.append((self.path, body))
                self._send(204 if "prompt_async" in self.path else 200, None if "prompt_async" in self.path else {"data": {}})

        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()


class FakeAppServer:
    """A Codex app-server on a Unix socket: WebSocket upgrade, then JSON-RPC in text frames."""

    def __init__(self, path, status="idle", steer_error=False):
        self.path, self.calls, self.status, self.steer_error = str(path), [], status, steer_error
        self.sock = socket.socket(socket.AF_UNIX)
        self.sock.bind(self.path)
        self.sock.listen(8)
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        while True:
            try:
                c, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(c,), daemon=True).start()

    def _serve(self, c):
        with c:
            buf = b""
            while b"\r\n\r\n" not in buf:
                got = c.recv(65536)
                if not got:
                    return
                buf += got
            buf = buf.split(b"\r\n\r\n", 1)[1]
            c.sendall(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n\r\n")
            while True:
                while len(buf) < 2:
                    got = c.recv(65536)
                    if not got:
                        return
                    buf += got
                n, off = buf[1] & 0x7F, 2
                if n == 126:
                    n, off = struct.unpack(">H", buf[2:4])[0], 4
                while len(buf) < off + 4 + n:
                    got = c.recv(65536)
                    if not got:
                        return
                    buf += got
                mask, payload = buf[off:off + 4], buf[off + 4:off + 4 + n]
                buf = buf[off + 4 + n:]
                msg = json.loads(bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))
                if "id" not in msg:
                    continue
                self.calls.append((msg["method"], msg.get("params")))
                if msg["method"] == "thread/read":
                    out = {"result": {"thread": {"id": TID, "status": {"type": self.status, "activeFlags": []},
                                                 "name": "Login page fix"}}}
                elif msg["method"] == "turn/steer" and self.steer_error:
                    out = {"error": {"code": -32600, "message": "expected turn mismatch"}}
                else:
                    out = {"result": {}}
                raw = json.dumps({"jsonrpc": "2.0", "id": msg["id"], **out}).encode()
                head = bytes([0x81, len(raw)]) if len(raw) < 126 else bytes([0x81, 126]) + struct.pack(">H", len(raw))
                c.sendall(head + raw)

    def close(self):
        self.sock.close()


def hook_in(cwd, sid, event, **extra):
    return json.dumps({"session_id": sid, "cwd": cwd, "hook_event_name": event, **extra})


# ---------------------------------------------------------------- hooks per tool

def test_an_opencode1_hook_records_its_window_and_its_own_server(box):
    door = Door(box.dir / "d.sock", lambda call, args: {"text": "BRIEF"})
    env = {"KAP_CHAT_PID": str(os.getppid()), "KAP_OPENCODE_URL": "http://127.0.0.1:41234"}
    out = H.hook("opencode", "start", hook_in(box.cwd, SID, "start"), env, box.state, door.path)
    assert out.startswith("You are a team chat.") and "BRIEF" in out   # plain text: the plugin puts it in
    rec = H.read_record(box.state, f"opencode:{SID}")
    assert rec["tool"] == "opencode" and rec["server"] == "http://127.0.0.1:41234"
    assert rec["pid"] == os.getppid() and rec["project"] == "proj-a"


def test_an_opencode_hook_never_takes_a_process_it_was_not_told_of_nor_a_server_off_loopback(box):
    """An OpenCode 2 hook runs in the box's shared service: closing that would end every chat."""
    door = Door(box.dir / "d.sock", lambda call, args: {})
    H.hook("opencode", "start", hook_in(box.cwd, SID, "start"), {"KAP_OPENCODE_URL": "http://10.0.0.5:80"},
           box.state, door.path)
    rec = H.read_record(box.state, f"opencode:{SID}")
    assert "pid" not in rec and "server" not in rec


def test_an_opencode2_hook_keeps_the_window_box_exec_recorded(box, monkeypatch):
    monkeypatch.setattr(bx, "CHATS", box.state)
    monkeypatch.setattr(bx, "VIEW", H.VIEW)
    bx.write_chat(f"opencode:{SID}", "opencode", os.getpid(), box.cwd)
    door = Door(box.dir / "d.sock", lambda call, args: {"messages": []})
    H.hook("opencode", "prompt", hook_in(box.cwd, SID, "prompt"), {}, box.state, door.path)
    rec = H.read_record(box.state, f"opencode:{SID}")
    assert rec["pid"] == os.getpid() and rec["state"] == "busy" and rec["project"] == "proj-a"


def test_a_codex_hook_records_the_chats_app_server_and_its_turn(box):
    door = Door(box.dir / "d.sock", lambda call, args: {"messages": []})
    env = {"KAP_CHAT_PID": str(os.getppid()), "KAP_CODEX_SOCKET": "/run/kapbox/codex/4242/app.sock"}
    H.hook("codex", "UserPromptSubmit", hook_in(box.cwd, TID, "UserPromptSubmit", turn_id=TID), env, box.state, door.path)
    rec = H.read_record(box.state, f"codex:{TID}")
    assert rec["codex"] == "/run/kapbox/codex/4242/app.sock" and rec["turn"] == TID and rec["pid"] == os.getppid()
    H.hook("codex", "Stop", hook_in(box.cwd, TID, "Stop"), {"KAP_CODEX_SOCKET": "/tmp/evil.sock"}, box.state, door.path)
    assert H.read_record(box.state, f"codex:{TID}")["codex"] == "/run/kapbox/codex/4242/app.sock"


def test_cursor_gets_what_came_meanwhile_at_its_stop_hook_as_its_follow_up(box):
    msgs = [{"id": 1, "text": "PING", "body": "PING please", "person": "bob", "machine": "box-1", "reply": "box-1/x"}]
    door = Door(box.dir / "d.sock", lambda call, args: {"messages": msgs} if call == "inbox" else {})
    data = json.dumps({"conversation_id": "c-1", "workspace_roots": [box.cwd], "hook_event_name": "stop"})
    out = json.loads(H.hook("cursor", "stop", data, {}, box.state, door.path))
    assert "[kaptyorka: from bob on box-1" in out["followup_message"] and "<<<data " in out["followup_message"]
    assert [r["call"] for r in door.requests] == ["inbox"]


# ---------------------------------------------------------------- delivery per tool

def test_opencode1_delivery_posts_prompt_async_to_the_windows_own_server(box):
    oc = FakeOpenCode()
    try:
        rec = {"chat": f"opencode:{SID}", "tool": "opencode", "server": oc.url, "cwd": box.cwd}
        assert H.deliver(rec, "hello", home=box.home) == "opencode server"
        path, body = oc.posts[0]
        assert path.startswith(f"/session/{SID}/prompt_async?directory=")
        assert body == {"parts": [{"type": "text", "text": "hello"}]}
    finally:
        oc.close()


def write_service(home, url, password="pw-1", pid=None):
    d = Path(home) / "state/opencode/state/opencode"
    d.mkdir(parents=True, exist_ok=True)
    (d / "service.json").write_text(json.dumps({"url": url, "pid": pid or os.getpid(), "password": password}))


def test_opencode2_delivery_uses_the_boxs_own_service_with_its_password(box):
    oc = FakeOpenCode(password="pw-1")
    try:
        write_service(box.home, oc.url)
        H.deliver({"chat": f"opencode:{SID}", "tool": "opencode", "cwd": box.cwd}, "hello", home=box.home)
        assert oc.posts == [(f"/api/session/{SID}/prompt", {"text": "hello", "delivery": "queue", "resume": True})]
    finally:
        oc.close()


def test_opencode_delivery_refuses_without_a_service_or_off_loopback(box):
    with pytest.raises(ValueError):
        H.deliver({"chat": f"opencode:{SID}", "tool": "opencode"}, "x", home=box.home)
    write_service(box.home, "http://10.1.2.3:80")
    assert tw.oc_service(box.home) is None
    with pytest.raises(ValueError):
        H.deliver({"chat": "opencode:not-an-id", "tool": "opencode", "server": "http://127.0.0.1:1"}, "x", home=box.home)


def test_codex_delivery_starts_a_turn_when_idle_and_steers_a_running_one(box):
    srv = FakeAppServer(box.dir / "cx.sock")
    try:
        rec = {"chat": f"codex:{TID}", "tool": "codex", "codex": srv.path, "turn": TID}
        assert H.deliver(rec, "hello") == "turn/start"
        assert srv.calls[-1] == ("turn/start", {"threadId": TID, "input": [{"type": "text", "text": "hello"}]})
        srv.status = "active"
        assert H.deliver(rec, "more") == "turn/steer"
        assert srv.calls[-1][1]["expectedTurnId"] == TID
        srv.steer_error = True
        assert H.deliver(rec, "again") == "turn/start"   # the turn ended meanwhile
        assert not any("sandbox" in json.dumps(p).lower() or "approval" in json.dumps(p).lower()
                       for m, p in srv.calls if m.startswith("turn/"))
    finally:
        srv.close()


def test_cursor_and_a_codex_chat_without_its_own_app_server_are_pull_only(box):
    with pytest.raises(ValueError):
        H.deliver({"chat": "cursor:c-1", "tool": "cursor"}, "x")
    with pytest.raises(ValueError):
        H.deliver({"chat": f"codex:{TID}", "tool": "codex"}, "x")


def test_report_delivers_into_an_opencode_chat_and_acks(box):
    oc = FakeOpenCode()
    try:
        H.write_record(box.state, {"chat": f"opencode:{SID}", "tool": "opencode", "server": oc.url, "cwd": box.cwd,
                                   "project": "proj-a", "state": "idle", "last": 1.0})
        msg = {"id": 7, "chat": f"opencode:{SID}", "text": "PING", "person": "bob", "machine": "box-1"}
        door = Door(box.dir / "d.sock", lambda call, args: {"messages": [msg]} if call == "report" else {})
        h = H.Helper("alice", door.path, box.state, home=box.home, agents_json=False)
        h.step()
        assert [a for a in (r["args"] for r in door.requests if r["call"] == "ack")] == [{"id": 7, "ok": True}]
        assert "<<<data " in oc.posts[0][1]["parts"][0]["text"]
    finally:
        oc.close()


# ---------------------------------------------------------------- state, names, close, MCP

def test_the_report_has_each_tools_own_busy_state_and_title(box):
    oc = FakeOpenCode(busy=True, title="Fix the login page")
    srv = FakeAppServer(box.dir / "cx.sock", status="active")
    try:
        H.write_record(box.state, {"chat": f"opencode:{SID}", "tool": "opencode", "server": oc.url, "cwd": box.cwd,
                                   "project": "proj-a", "state": "idle", "last": 1.0})
        H.write_record(box.state, {"chat": f"codex:{TID}", "tool": "codex", "codex": srv.path, "cwd": box.cwd,
                                   "project": "proj-a", "state": "idle", "last": 1.0})
        h = H.Helper("alice", "/nowhere", box.state, home=box.home, agents_json=False, clock=lambda: 100.0)
        rows = {r["chat"]: r for r in h.chats()}
        assert rows[f"opencode:{SID}"]["state"] == "busy" and rows[f"opencode:{SID}"]["name"] == "Fix the login page"
        assert rows[f"codex:{TID}"]["state"] == "busy" and rows[f"codex:{TID}"]["name"] == "Login page fix"
    finally:
        oc.close()
        srv.close()


def test_opencode_placeholder_titles_are_no_names():
    assert tw.oc_title("New session - 2026-11-26T14:08:50.123Z") == ""
    assert tw.oc_title("  PING-1   greeting ") == "PING-1 greeting"


def test_an_opencode2_session_with_no_window_known_ends_after_its_quiet_time(box):
    H.write_record(box.state, {"chat": f"opencode:{SID}", "tool": "opencode", "cwd": box.cwd, "project": "proj-a",
                               "state": "idle", "last": 0.0})
    h = H.Helper("alice", "/nowhere", box.state, home=box.home, agents_json=False, clock=lambda: H.OC_OPEN_S + 10.0)
    assert h.chats()[0]["state"] == "ended"


def test_close_refuses_a_chat_whose_own_process_is_unknown(box, monkeypatch):
    killed = []
    monkeypatch.setattr(H.os, "kill", lambda pid, sig: killed.append((pid, sig)))
    H.write_record(box.state, {"chat": f"opencode:{SID}", "tool": "opencode", "project": "proj-a"})
    H.Helper("alice", "/nowhere", box.state, home=box.home, agents_json=False).close(f"opencode:{SID}")
    assert killed == []


def test_the_mcp_server_finds_its_chat_by_the_process_that_started_it(box, monkeypatch):
    H.write_record(box.state, {"chat": f"codex:{TID}", "tool": "codex", "pid": os.getpid(), "cwd": box.cwd,
                               "project": "proj-a", "state": "idle", "last": 1.0})
    H.write_record(box.state, {"chat": "codex:other", "tool": "codex", "pid": 999999, "cwd": box.cwd,
                               "project": "proj-a", "state": "idle", "last": 2.0})
    monkeypatch.chdir("/")
    assert H.mcp_caller({"KAP_CHAT_PID": str(os.getpid())}, box.state) == {"chat": f"codex:{TID}", "project": "proj-a"}


def test_the_helper_starts_the_boxs_own_opencode2_service_only_for_v2(box, monkeypatch, tmp_path):
    exe = tmp_path / "bin" / "opencode"
    exe.parent.mkdir()
    exe.write_text("")
    monkeypatch.setattr(H, "TOOLS_BIN", str(exe.parent))
    started, version = [], ["opencode v2.4.2"]

    def run(argv, **kw):
        assert argv == [str(exe), "--version"]
        return type("R", (), {"stdout": version[0], "stderr": "", "returncode": 0})()

    class P:
        def poll(self):
            return None

    h = H.Helper("alice", "/nowhere", box.state, home=box.home, agents_json=False, run=run,
                 popen=lambda argv, **kw: started.append((argv, kw["env"])) or P())
    h.keep_service()
    h.keep_service()   # it runs: not again
    assert len(started) == 1
    argv, env = started[0]
    assert argv[1:] == ["serve", "--service", "--hostname", "127.0.0.1", "--port", "0"]
    assert env["XDG_STATE_HOME"] == box.home + "/state/opencode/state" and "CLAUDE_CONFIG_DIR" not in env
    version[0] = "1.19.6"
    h2 = H.Helper("alice", "/nowhere", box.state, home=box.home, agents_json=False, run=run,
                  popen=lambda argv, **kw: started.append(argv) or P())
    h2.keep_service()
    assert len(started) == 1


# ---------------------------------------------------------------- box-exec per tool

HOME = "/var/lib/kap-team/kap-alice/home"


def test_box_exec_names_the_chat_process_for_every_tool_but_git():
    for tool in ("claude", "codex", "opencode", "cursor-agent"):
        assert bx.plan(tool, [], {}, HOME, "alice", "", 4242)[2]["KAP_CHAT_PID"] == "4242"
    assert "KAP_CHAT_PID" not in bx.plan("git", ["status"], {}, HOME, "alice", "", 4242)[2]


def test_box_exec_gives_a_codex_window_its_own_app_server_and_nothing_else_one():
    _, argv, env = bx.plan("codex", ["fix the tests"], {}, HOME, "alice", "", 4242)
    assert argv[1:] == ["--remote", "unix:///run/kapbox/codex/4242/app.sock", "fix the tests"]
    assert env["KAP_CODEX_SOCKET"] == "/run/kapbox/codex/4242/app.sock"
    for args in (["exec", "x"], ["login", "--with-api-key"], ["--version"], ["resume"]):
        _, argv, env = bx.plan("codex", args, {}, HOME, "alice", "", 4242)
        assert "--remote" not in argv and "KAP_CODEX_SOCKET" not in env


def test_box_exec_gives_an_opencode1_window_its_own_loopback_server_and_v2_its_made_session():
    _, argv, env = bx.plan("opencode", [], {}, HOME, "alice", "", 4242, {"major": 1, "port": 41234})
    assert argv[1:] == ["--port", "41234", "--hostname", "127.0.0.1"]
    assert env["KAP_OPENCODE_URL"] == "http://127.0.0.1:41234" and env["XDG_STATE_HOME"] == HOME + "/state/opencode/state"
    assert env["NO_PROXY"] == "127.0.0.1,localhost,::1"   # its own server is never asked through the proxy
    _, argv, env = bx.plan("opencode", [], {}, HOME, "alice", "", 4242, {"major": 2, "session": SID})
    assert argv[1:] == ["-s", SID] and "KAP_OPENCODE_URL" not in env
    assert bx.plan("opencode", [], {}, HOME, "alice", "", 4242, {"major": 2, "session": ""})[1][1:] == []
    assert bx.tui("opencode", []) and not bx.tui("opencode", ["run", "x"]) and not bx.tui("opencode", ["--version"])


def test_box_exec_and_the_helper_use_the_same_opencode_folders():
    assert bx.oc_env(HOME) == tw.oc_env(HOME)


def test_box_exec_writes_a_chat_record_the_helper_reads(tmp_path, monkeypatch):
    monkeypatch.setattr(bx, "CHATS", str(tmp_path / "chats"))
    bx.write_chat(f"opencode:{SID}", "opencode", os.getpid(), "/srv/team/alice/proj-a/sub")
    rec = H.read_record(str(tmp_path / "chats"), f"opencode:{SID}")
    assert rec["pid"] == os.getpid() and rec["project"] == "proj-a" and rec["person"] == "alice"
    assert H.alive(rec)
    bx.write_chat("opencode:ses_x", "opencode", os.getpid(), "/home/alice")   # not a team folder: nothing
    assert H.read_record(str(tmp_path / "chats"), "opencode:ses_x") is None


# ---------------------------------------------------------------- the tools' own lock-down files

def test_the_opencode_managed_file_denies_outside_folders_other_plugins_and_mcp_servers_for_v1_and_v2():
    m = json.loads((FILES / "opencode" / "etc" / "opencode.json").read_text())
    assert m["permission"]["external_directory"] == "deny"                      # V1
    pol = [(p["action"], p["resource"], p["effect"]) for p in m["policies"]]   # V2: last match wins
    assert ("tool.use", "external_directory:*", "deny") in pol
    assert pol.index(("plugin.use", "*", "deny")) < pol.index(("plugin.use", "/etc/opencode/plugins/kaptyorka.js", "allow"))  # its path: measured
    assert pol.index(("mcp.use", "*", "deny")) < pol.index(("mcp.use", "kaptyorka", "allow"))
    assert m["plugin"] == ["file:///etc/kaptyorka/box/opencode/kaptyorka-v1.js"]
    assert (FILES / "opencode" / "kaptyorka-v1.js").exists()
    v2 = (FILES / "opencode" / "etc" / "plugins" / "kaptyorka.js").read_text()
    assert 'id: "kaptyorka"' in v2 and "helper.py" in v2
    v1 = (FILES / "opencode" / "kaptyorka-v1.js").read_text()
    assert '"tool.execute.before"' in v1 and "throw new Error" in v1


def test_the_codex_requirements_allow_no_unsandboxed_mode_and_only_managed_hooks():
    tomllib = pytest.importorskip("tomllib")
    r = tomllib.loads((FILES / "codex" / "requirements.toml").read_text())
    assert set(r["allowed_sandbox_modes"]) == {"read-only", "workspace-write"}
    assert r["allowed_approval_policies"] == ["on-request"]
    assert r["allow_managed_hooks_only"] is True and r["features"]["hooks"] is True
    assert r["allow_remote_control"] is False and r["allow_browser_and_computer_use"] is False
    for ev in ("SessionStart", "UserPromptSubmit", "Stop"):
        cmd = r["hooks"][ev][0]["hooks"][0]["command"]
        assert cmd == f"/usr/bin/python3 -I /opt/kaptyorka/box/current/helper.py hook codex {ev}"
        assert cmd.split()[2].startswith(r["hooks"]["managed_dir"] + "/")
    assert set(r["mcp_servers"]) == {"kaptyorka"}
    c = tomllib.loads((FILES / "codex" / "config.toml").read_text())
    assert "projects" not in c and c["sandbox_mode"] in r["allowed_sandbox_modes"]
    assert json.loads((FILES / "codex" / "hooks.json").read_text()) == {"hooks": {}}


def test_the_cursor_cli_config_denies_the_box_homes_secret_folders():
    d = json.loads((FILES / "cursor" / "cli-config.json").read_text())["permissions"]["deny"]
    for p in ("~/.cursor/**", "~/state/**", "~/.ssh/**"):
        assert f"Read({p})" in d and f"Write({p})" in d


def test_the_box_binds_the_managed_files_and_locks_opencodes_code_folders():
    props = kc.box_props("alice", "alice")
    assert any(p.startswith("BindReadOnlyPaths=") and "/etc/kaptyorka/box/opencode/etc:/etc/opencode" in p
               and "/etc/kaptyorka/box/codex/requirements.toml:/etc/codex/requirements.toml" in p for p in props)
    ro = next(p for p in props if p.startswith("ReadOnlyPaths=-"))
    for d in ("plugin", "plugins", "agent", "agents"):
        assert f"{HOME}/state/opencode/cfg/opencode/{d}" in ro
    assert ("cursor/cli-config.json", ".cursor/cli-config.json") in kc.LOCKED_FILES
