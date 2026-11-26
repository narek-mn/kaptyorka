"""How the box helper talks to OpenCode's and Codex's own local servers inside the box (secure
design v2, 5.1; slice 5). Python standard library only: installed as toolwire.py next to
helper.py, read-only in the box. It knows what the team-machine adapters know
(adapters/opencode.py, adapters/codex.py), but imports nothing from kaptyorka.

OpenCode, both on the box's private loopback (the box's network namespace has only lo):
  V1 (1.x): each terminal window runs its own server; box-exec starts it with
     --port <free port> --hostname 127.0.0.1 and names it in KAP_OPENCODE_URL. Routes as on a
     team machine (adapters/opencode.py, checked on 1.19.6): GET /session/status, GET
     /session/<id>, POST /session/<id>/prompt_async {"parts": [{"type": "text", ...}]}.
  V2 (2.x): one background service per user; the helper starts the box user's own one
     (`opencode serve --service`), which writes service.json (url, pid, password) in
     $XDG_STATE_HOME/opencode. Documented routes (opencode.ai/docs/api): GET /api/session/<id>,
     POST /api/session {"id", "location"} ("If a session with this ID already exists, it is
     returned unchanged"), POST /api/session/<id>/prompt {"text", "delivery", "resume"}.
Codex: the app-server protocol (learn.chatgpt.com/docs/app-server): JSON-RPC in WebSocket text
  frames over a Unix socket; turn/start, turn/steer, thread/read.
"""
import base64
import json
import os
import re
import socket
import struct
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional, Tuple

TIMEOUT = 5
SESSION_RX = re.compile(r"ses_[A-Za-z0-9]{8,64}")
THREAD_RX = re.compile(r"[0-9a-fA-F-]{8,64}")
# OpenCode's placeholder title before the first prompt (adapters/opencode.py DEFAULT_TITLE)
OC_DEFAULT_TITLE = re.compile(r"^(New|Child) session - \d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")


# ---------------------------------------------------------------------------- OpenCode

def oc_env(home: str) -> dict:
    """OpenCode's folders in the box (design 5.2, D5): the same for a chat (box-exec) and for
    the helper's service, so both find one service.json and one database."""
    s = f"{home}/state/opencode"
    return {"XDG_CONFIG_HOME": f"{s}/cfg", "XDG_DATA_HOME": f"{s}/data", "XDG_STATE_HOME": f"{s}/state",
            "XDG_CACHE_HOME": f"{s}/cache", "OPENCODE_DISABLE_AUTOUPDATE": "1"}


def oc_major(version_text: str) -> int:
    """1 or 2 from `opencode --version` ("1.19.6", "opencode v2.4.2"); 0 if unknown."""
    m = re.search(r"(\d+)\.\d+(?:\.\d+)?", version_text or "")
    return int(m.group(1)) if m else 0


def oc_service(home: str) -> Optional[dict]:
    """The box's V2 service from its service.json: {"url", "auth", "pid"}; None if none runs."""
    try:
        with open(f"{home}/state/opencode/state/opencode/service.json") as f:
            info = json.load(f)
        url = str(info["url"]).rstrip("/")
        pid = int(info.get("pid") or 0)
        if pid:
            os.kill(pid, 0)
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if not url.startswith("http://127.0.0.1:"):
        return None   # the box's own loopback only
    token = base64.b64encode(f"opencode:{info.get('password') or ''}".encode()).decode()
    return {"url": url, "auth": f"Basic {token}", "pid": pid}


def http(method: str, url: str, body=None, auth: str = "", timeout: float = TIMEOUT) -> Tuple[int, object]:
    """(status, decoded JSON or None). Only loopback: the box has nothing else without its proxy,
    and the proxy must never be used for these calls."""
    if not url.startswith("http://127.0.0.1:"):
        raise ValueError("not the box's own loopback")
    headers = {"Authorization": auth} if auth else {}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=timeout) as r:
            raw = r.read(4 << 20)
            try:
                return r.status, json.loads(raw) if raw else None
            except ValueError:
                return r.status, None
    except urllib.error.HTTPError as e:
        e.close()
        return e.code, None


def oc_title(value) -> str:
    t = " ".join(str(value).split())[:80] if isinstance(value, str) else ""
    return "" if OC_DEFAULT_TITLE.match(t) else t


def oc_state(rec: dict, home: str) -> Tuple[str, str]:
    """(busy|idle|"", title) of an OpenCode chat from its own server; ("", "") if it cannot say."""
    sid = rec["chat"].split(":", 1)[-1]
    if not SESSION_RX.fullmatch(sid):
        return "", ""
    q = "?" + urllib.parse.urlencode({"directory": rec.get("cwd") or ""})
    if rec.get("server"):   # V1: the window's own server
        base = rec["server"]
        st, busy = http("GET", f"{base}/session/status{q}")
        st2, row = http("GET", f"{base}/session/{sid}{q}")
        state = ""
        if st == 200 and isinstance(busy, dict):
            v = busy.get(sid)
            state = "busy" if isinstance(v, dict) and v.get("type") in ("busy", "retry") else "idle"
        return state, oc_title(row.get("title")) if st2 == 200 and isinstance(row, dict) else ""
    svc = oc_service(home)
    if not svc:
        return "", ""
    st, data = http("GET", f"{svc['url']}/api/session/{sid}", auth=svc["auth"])
    row = data.get("data") if st == 200 and isinstance(data, dict) else None
    if not isinstance(row, dict):
        return "", ""
    t = row.get("time") if isinstance(row.get("time"), dict) else {}
    b, i = t.get("busy"), t.get("idle")
    running = isinstance(b, (int, float)) and not (isinstance(i, (int, float)) and i >= b)
    return ("busy" if running else "idle"), oc_title(row.get("title"))


def oc_deliver(rec: dict, text: str, home: str) -> None:
    """One message into an OpenCode chat through its own server. Raises OSError or ValueError."""
    sid = rec["chat"].split(":", 1)[-1]
    if not SESSION_RX.fullmatch(sid):
        raise ValueError("not an OpenCode session id")
    if rec.get("server"):
        q = "?" + urllib.parse.urlencode({"directory": rec.get("cwd") or ""})
        st, _ = http("POST", f"{rec['server']}/session/{sid}/prompt_async{q}",
                     {"parts": [{"type": "text", "text": text}]})
    else:
        svc = oc_service(home)
        if not svc:
            raise ValueError("the box's OpenCode service is not running")
        st, _ = http("POST", f"{svc['url']}/api/session/{sid}/prompt",
                     {"text": text, "delivery": "queue", "resume": True}, svc["auth"])
    if not 200 <= st < 300:
        raise ValueError(f"OpenCode refused it (HTTP {st})")


# ---------------------------------------------------------------------------- Codex

class RpcError(Exception):
    pass


class Rpc:
    """One connection to a Codex app-server's Unix socket: a WebSocket upgrade, then JSON-RPC,
    one message per text frame (the same client as adapters/codex.py). Bounded by one deadline."""

    def __init__(self, path: str, timeout: float = TIMEOUT):
        self.deadline = time.monotonic() + timeout
        self.s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.s.settimeout(timeout)
        self.n, self.buf = 0, b""
        try:
            self.s.connect(path)
            self.s.sendall(b"GET / HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                           b"Sec-WebSocket-Key: " + base64.b64encode(os.urandom(16))
                           + b"\r\nSec-WebSocket-Version: 13\r\n\r\n")
            while b"\r\n\r\n" not in self.buf:
                self.buf += self._recv()
            head, self.buf = self.buf.split(b"\r\n\r\n", 1)
            if not head.startswith(b"HTTP/1.1 101"):
                raise ConnectionError("no websocket upgrade")
            self.call("initialize", {"clientInfo": {"name": "kaptyorka-box", "version": "1"}})
            self._send(1, json.dumps({"jsonrpc": "2.0", "method": "initialized"}).encode())
        except BaseException:
            self.s.close()
            raise

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.s.close()

    def _recv(self) -> bytes:
        left = self.deadline - time.monotonic()
        if left <= 0:
            raise TimeoutError("the Codex app-server did not answer in time")
        self.s.settimeout(left)
        data = self.s.recv(65536)
        if not data:
            raise ConnectionError("the Codex app-server closed the connection")
        return data

    def _send(self, opcode: int, payload: bytes) -> None:
        n = len(payload)
        if n < 126:
            head = bytes([0x80 | opcode, 0x80 | n])
        elif n < 65536:
            head = bytes([0x80 | opcode, 0x80 | 126]) + struct.pack(">H", n)
        else:
            head = bytes([0x80 | opcode, 0x80 | 127]) + struct.pack(">Q", n)
        mask = os.urandom(4)
        self.s.sendall(head + mask + bytes(c ^ mask[i % 4] for i, c in enumerate(payload)))

    def _need(self, n: int) -> None:
        while len(self.buf) < n:
            self.buf += self._recv()

    def _message(self) -> dict:
        data = b""
        while True:
            self._need(2)
            n, off = self.buf[1] & 0x7F, 2
            if n == 126:
                self._need(4)
                n, off = struct.unpack(">H", self.buf[2:4])[0], 4
            elif n == 127:
                self._need(10)
                n, off = struct.unpack(">Q", self.buf[2:10])[0], 10
            if n > 16 << 20:
                raise ConnectionError("a frame too large")
            mask = self.buf[off:off + 4] if self.buf[1] & 0x80 else b""
            off += len(mask)
            self._need(off + n)
            b0, payload, self.buf = self.buf[0], self.buf[off:off + n], self.buf[off + n:]
            if mask:
                payload = bytes(c ^ mask[i % 4] for i, c in enumerate(payload))
            op = b0 & 0x0F
            if op == 8:
                raise ConnectionError("the Codex app-server closed the connection")
            if op == 9:
                self._send(10, payload)
            elif op in (0, 1):
                data += payload
                if b0 & 0x80:
                    return json.loads(data)

    def call(self, method: str, params: dict):
        self.n += 1
        rid = self.n
        self._send(1, json.dumps({"jsonrpc": "2.0", "id": rid, "method": method, "params": params}).encode())
        while True:
            msg = self._message()
            if not isinstance(msg, dict) or msg.get("id") != rid or "method" in msg:
                continue
            if "error" in msg:
                err = msg["error"]
                raise RpcError(f"{method}: {err.get('message', err) if isinstance(err, dict) else err}")
            return msg.get("result")


def cx_state(rec: dict) -> Tuple[str, str]:
    """(busy|idle|"", name) of a Codex thread from its chat's app-server (thread/read: status,
    name; the same fields as adapters/codex.py)."""
    tid = rec["chat"].split(":", 1)[-1]
    if not THREAD_RX.fullmatch(tid) or not rec.get("codex"):
        return "", ""
    with Rpc(rec["codex"]) as rpc:
        t = (rpc.call("thread/read", {"threadId": tid}) or {}).get("thread") or {}
    st = t.get("status") if isinstance(t.get("status"), dict) else {}
    state = {"active": "busy", "idle": "idle"}.get(st.get("type"), "")
    name = t.get("name")
    return state, " ".join(name.split())[:80] if isinstance(name, str) else ""


def cx_deliver(rec: dict, text: str) -> str:
    """One message into a Codex thread: turn/steer into its running turn when one runs ("append
    user input to the active in-flight turn"), else turn/start ("add user input ... and begin
    Codex generation"). Never a sandbox or approval value: the chat's own limits stay. Returns
    the method used. Raises OSError, ValueError or RpcError."""
    tid = rec["chat"].split(":", 1)[-1]
    if not THREAD_RX.fullmatch(tid) or not rec.get("codex"):
        raise ValueError("this Codex chat has no app-server of its own (pull only)")
    inp = [{"type": "text", "text": text}]
    with Rpc(rec["codex"]) as rpc:
        t = (rpc.call("thread/read", {"threadId": tid}) or {}).get("thread") or {}
        st = t.get("status") if isinstance(t.get("status"), dict) else {}
        turn = rec.get("turn")
        if st.get("type") == "active" and isinstance(turn, str) and THREAD_RX.fullmatch(turn):
            try:
                rpc.call("turn/steer", {"threadId": tid, "input": inp, "expectedTurnId": turn})
                return "turn/steer"
            except RpcError:
                pass   # that turn ended meanwhile: a new one
        rpc.call("turn/start", {"threadId": tid, "input": inp})
        return "turn/start"
