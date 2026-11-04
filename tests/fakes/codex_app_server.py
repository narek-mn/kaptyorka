"""A fake Codex app-server control socket: WebSocket over a Unix socket, JSON-RPC, recorded."""
import base64
import hashlib
import json
import os
import socket
import struct
import threading

GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def _recv_frame(conn):
    def exact(n):
        data = b""
        while len(data) < n:
            chunk = conn.recv(n - len(data))
            if not chunk:
                raise ConnectionError
            data += chunk
        return data

    b0, b1 = exact(2)
    n = b1 & 0x7F
    if n == 126:
        n = struct.unpack(">H", exact(2))[0]
    elif n == 127:
        n = struct.unpack(">Q", exact(8))[0]
    mask = exact(4) if b1 & 0x80 else b"\0\0\0\0"
    data = exact(n)
    return b0 & 0x0F, bytes(c ^ mask[i % 4] for i, c in enumerate(data))


def _send_frame(conn, obj, b0=0x81):
    p = obj if isinstance(obj, bytes) else json.dumps(obj).encode()
    if len(p) < 126:
        head = bytes([b0, len(p)])
    elif len(p) < 65536:
        head = bytes([b0, 126]) + struct.pack(">H", len(p))
    else:
        head = bytes([b0, 127]) + struct.pack(">Q", len(p))
    conn.sendall(head + p)


class FakeCodex:
    """threads: [{"id", "cwd", "updatedAt"}]; loaded: ids loaded in the daemon."""

    def __init__(self, path, threads, loaded, hooks_json=None):
        self.path, self.threads, self.loaded, self.hooks_json = str(path), threads, loaded, hooks_json
        self.error = None  # a JSON-RPC error object to answer turn/start with
        self.trusted = {}  # hook key -> trusted hash
        self.calls = []  # (method, params)
        self.noisy = False  # before each answer: a ping, a fragmented frame, a 126-length frame
        self.pongs = []
        self.sock = socket.socket(socket.AF_UNIX)
        here = os.getcwd()
        os.chdir(os.path.dirname(self.path))  # AF_UNIX paths over 108 bytes fail
        try:
            self.sock.bind(os.path.basename(self.path))
        finally:
            os.chdir(here)
        self.sock.listen(8)
        self.thread = threading.Thread(target=self._serve)
        self.thread.start()

    def _serve(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            with conn:
                try:
                    self._handle(conn)
                except (ConnectionError, OSError):
                    pass

    def _handle(self, conn):
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = conn.recv(4096)
            if not chunk:
                return
            head += chunk
        key = next(line.split(b":", 1)[1].strip() for line in head.split(b"\r\n")
                   if line.lower().startswith(b"sec-websocket-key:"))
        accept = base64.b64encode(hashlib.sha1(key + GUID).digest())
        conn.sendall(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                     b"Connection: Upgrade\r\nSec-WebSocket-Accept: " + accept + b"\r\n\r\n")
        while True:
            op, data = _recv_frame(conn)
            if op == 8:
                return
            if op == 10:
                self.pongs.append(data)
                continue
            msg = json.loads(data)
            self.calls.append((msg["method"], msg.get("params")))
            if "id" not in msg:
                continue
            _send_frame(conn, {"method": "noise/notification", "params": {}})  # must be skipped
            if self.noisy:
                _send_frame(conn, b"are-you-there", 0x89)  # ping
                part = json.dumps({"method": "noise/fragmented", "params": {}}).encode()
                _send_frame(conn, part[:10], 0x01)  # text, not final
                _send_frame(conn, part[10:], 0x80)  # continuation, final
                _send_frame(conn, {"method": "noise/long", "params": {"pad": "x" * 200}})
            _send_frame(conn, dict(self._answer(msg["method"], msg.get("params") or {}),
                                   jsonrpc="2.0", id=msg["id"]))

    def _answer(self, method, params):
        if method == "initialize":
            return {"result": {"userAgent": "fake"}}
        if method == "thread/loaded/list":
            return {"result": {"data": list(self.loaded), "nextCursor": None}}
        if method == "thread/read":
            found = [t for t in self.threads if t["id"] == params["threadId"]
                     and t["id"] in self.loaded]
            if not found:
                return {"error": {"code": -32600, "message": "thread not loaded"}}
            return {"result": {"thread": found[0]}}
        if method == "turn/start":
            if self.error:
                return {"error": self.error}
            return {"result": {"turn": {"id": "turn-1", "status": "inProgress"}}}
        if method == "hooks/list":
            return {"result": {"data": [{"cwd": "/", "errors": [], "warnings": [],
                                         "hooks": self._hooks()}]}}
        return {"error": {"code": -32601, "message": "method not found"}}

    def _hooks(self):
        try:
            with open(self.hooks_json) as f:
                events = json.load(f)["hooks"]
        except (OSError, TypeError, ValueError, KeyError):
            return []
        out = []
        for event, groups in events.items():
            snake = "".join("_" + c.lower() if c.isupper() else c for c in event).lstrip("_")
            for gi, group in enumerate(groups):
                for hi, hook in enumerate(group["hooks"]):
                    key = f"{self.hooks_json}:{snake}:{gi}:{hi}"
                    current = "sha256:" + hashlib.sha256(hook["command"].encode()).hexdigest()
                    status = ("trusted" if self.trusted.get(key) == current else
                              "modified" if key in self.trusted else "untrusted")
                    out.append({"key": key, "currentHash": current, "trustStatus": status,
                                "handlerType": "command", "command": hook["command"],
                                "eventName": event[0].lower() + event[1:]})
        return out

    def stop(self):
        self.sock.shutdown(socket.SHUT_RDWR)
        self.sock.close()
        self.thread.join()
        os.unlink(self.path)
