"""Headless Chrome driven over the DevTools protocol with the standard library only: a tiny
WebSocket client (RFC 6455, text frames) and a launcher. For the slow page tests."""
import base64
import contextlib
import json
import os
import shutil
import socket
import struct
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path


def chrome_path():
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
        if shutil.which(name):
            return shutil.which(name)
    return None


class Page:
    """One page target. call() sends a DevTools command and returns its result."""

    def __init__(self, ws_url: str):
        rest = ws_url[len("ws://"):]
        hostport, path = rest.split("/", 1)
        host, port = hostport.rsplit(":", 1)
        self.sock = socket.create_connection((host, int(port)), timeout=30)
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall((f"GET /{path} HTTP/1.1\r\nHost: {hostport}\r\nUpgrade: websocket\r\n"
                           f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
                           "Sec-WebSocket-Version: 13\r\n\r\n").encode())
        head = b""
        while not head.endswith(b"\r\n\r\n"):
            head += self._exact(1)
        assert b" 101 " in head.split(b"\r\n", 1)[0], head
        self.n = 0

    def _exact(self, n: int) -> bytes:
        out = b""
        while len(out) < n:
            got = self.sock.recv(n - len(out))
            if not got:
                raise ConnectionError("devtools socket closed")
            out += got
        return out

    def _send(self, text: str) -> None:
        data, mask = text.encode(), os.urandom(4)
        n = len(data)
        head = bytes([0x81]) + (bytes([0x80 | n]) if n < 126 else
                                bytes([0x80 | 126]) + struct.pack(">H", n) if n < 65536 else
                                bytes([0x80 | 127]) + struct.pack(">Q", n))
        self.sock.sendall(head + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

    def _recv(self) -> str:
        parts = []
        while True:
            b0, b1 = self._exact(2)
            n = b1 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._exact(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._exact(8))[0]
            mask = self._exact(4) if b1 & 0x80 else None
            data = self._exact(n)
            if mask:
                data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
            op = b0 & 0x0F
            if op == 8:
                raise ConnectionError("devtools socket closed")
            if op in (9, 10):  # ping, pong
                continue
            parts.append(data)
            if b0 & 0x80:
                return b"".join(parts).decode()

    def call(self, method: str, **params) -> dict:
        self.n += 1
        mid = self.n
        self._send(json.dumps({"id": mid, "method": method, "params": params}))
        while True:
            msg = json.loads(self._recv())
            if msg.get("id") == mid:
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error']}")
                return msg.get("result", {})

    def js(self, expr: str):
        r = self.call("Runtime.evaluate", expression=expr, returnByValue=True, awaitPromise=True)
        if "exceptionDetails" in r:
            raise RuntimeError(r["exceptionDetails"])
        return r["result"].get("value")

    def wait(self, expr: str, timeout: float = 10.0):
        """Poll a JS expression until it is truthy; return its value."""
        end = time.monotonic() + timeout
        while True:
            v = self.js(expr)
            if v:
                return v
            if time.monotonic() > end:
                raise AssertionError(f"timed out waiting for: {expr}")
            time.sleep(0.1)

    def go(self, url: str) -> None:
        self.call("Page.navigate", url=url)

    def screenshot(self, path) -> None:
        Path(path).write_bytes(base64.b64decode(self.call("Page.captureScreenshot", format="png")["data"]))


@contextlib.contextmanager
def chrome(width: int = 1440, height: int = 900):
    """A headless Chrome with its own profile; yields a Page. Everything stops on exit."""
    exe = chrome_path()
    profile = tempfile.mkdtemp(prefix="kaptyorka-chrome-")
    proc = subprocess.Popen([exe, "--headless=new", "--disable-gpu", "--hide-scrollbars",
                             "--no-first-run", "--no-default-browser-check",
                             f"--window-size={width},{height}", "--remote-debugging-port=0",
                             f"--user-data-dir={profile}", "about:blank"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    page = None
    try:
        port_file, end = Path(profile) / "DevToolsActivePort", time.monotonic() + 20
        while not port_file.exists() or not port_file.read_text().strip():
            assert proc.poll() is None and time.monotonic() < end, "chrome did not start"
            time.sleep(0.05)
        port = port_file.read_text().split()[0]
        targets = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=10))
        page = Page(next(t for t in targets if t["type"] == "page")["webSocketDebuggerUrl"])
        page.call("Page.enable")
        page.call("Runtime.enable")
        page.call("Emulation.setDeviceMetricsOverride", width=width, height=height,
                  deviceScaleFactor=1, mobile=width < 720)
        yield page
    finally:
        if page is not None:
            page.sock.close()
        proc.terminate()
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            proc.kill()
        shutil.rmtree(profile, ignore_errors=True)
