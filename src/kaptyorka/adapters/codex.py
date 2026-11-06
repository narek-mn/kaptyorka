"""Codex: sessions and push through the shared app-server daemon's control socket.

The socket speaks WebSocket over a Unix socket, one JSON-RPC message per text frame.
The TUI uses the daemon only when started as `codex --remote unix://` (the alias `join` adds).
"""
import shlex
import base64
import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional

from kaptyorka import __version__
from kaptyorka.adapters.base import (DeliveryError, Session, add_alias, connect_unix, drop_hooks, is_our_hook, merge_json,
                                     remove_alias, write_atomic)

TIMEOUT_S = 5
RESTART_EVERY_S = 60
ALIAS = "alias codex='codex --remote unix://'"
START, END = "# kaptyorka start", "# kaptyorka end"
APPROVE = "codex: approve the kaptyorka hooks once with /hooks in Codex"
EVENTS = (("SessionStart", "start"), ("UserPromptSubmit", "prompt"))


def codex_home(home: Optional[Path] = None) -> Path:
    env = os.environ.get("CODEX_HOME")
    return Path(env) if env else (home or Path.home()) / ".codex"


def default_socket(home: Optional[Path] = None) -> Path:
    return codex_home(home) / "app-server-control" / "app-server-control.sock"


class RpcError(Exception):
    """The app-server answered with a JSON-RPC error."""


class Rpc:
    """One connection to the control socket, bounded by one deadline; initialized on open."""

    def __init__(self, path: Path, timeout: float = TIMEOUT_S):
        self.deadline = time.monotonic() + timeout
        self.s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.s.settimeout(timeout)
        self.next_id = 0
        try:
            connect_unix(self.s, path)
            self.s.sendall(b"GET / HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\n"
                           b"Connection: Upgrade\r\nSec-WebSocket-Key: "
                           + base64.b64encode(os.urandom(16))
                           + b"\r\nSec-WebSocket-Version: 13\r\n\r\n")
            self.buf = b""
            while b"\r\n\r\n" not in self.buf:
                self.buf += self._recv()
            head, self.buf = self.buf.split(b"\r\n\r\n", 1)
            if not head.startswith(b"HTTP/1.1 101"):
                raise ConnectionError(f"no websocket upgrade: {head[:80]!r}")
            self.call("initialize", {"clientInfo": {"name": "kaptyorka", "version": __version__}})
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
            raise TimeoutError("codex app-server did not answer in time")
        self.s.settimeout(left)
        data = self.s.recv(65536)
        if not data:
            raise ConnectionError("codex app-server closed the connection")
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

    def _frame(self):
        """(fin, opcode, payload) of the next frame from the server."""
        self._need(2)
        n, off = self.buf[1] & 0x7F, 2
        if n == 126:
            self._need(4)
            n, off = struct.unpack(">H", self.buf[2:4])[0], 4
        elif n == 127:
            self._need(10)
            n, off = struct.unpack(">Q", self.buf[2:10])[0], 10
        mask = self.buf[off:off + 4] if self.buf[1] & 0x80 else b""
        off += len(mask)
        self._need(off + n)
        b0, payload, self.buf = self.buf[0], self.buf[off:off + n], self.buf[off + n:]
        if mask:
            payload = bytes(c ^ mask[i % 4] for i, c in enumerate(payload))
        return b0 & 0x80, b0 & 0x0F, payload

    def _message(self) -> dict:
        data = b""
        while True:
            fin, opcode, payload = self._frame()
            if opcode == 8:
                raise ConnectionError("codex app-server closed the connection")
            if opcode == 9:
                self._send(10, payload)  # pong
            elif opcode in (0, 1):
                data += payload
                if fin:
                    return json.loads(data)

    def call(self, method: str, params: dict):
        """The result of one request; RpcError on a JSON-RPC error."""
        self.next_id += 1
        rid = self.next_id
        self._send(1, json.dumps({"jsonrpc": "2.0", "id": rid, "method": method,
                                  "params": params}).encode())
        while True:
            msg = self._message()
            if msg.get("id") != rid or "method" in msg:
                continue  # a notification or a request from the server
            if "error" in msg:
                err = msg["error"]
                raise RpcError(f"{method}: {err.get('message', err) if isinstance(err, dict) else err}")
            if "result" not in msg:
                raise RpcError(f"{method}: no result")
            return msg["result"]


def _loaded_threads(rpc: Rpc) -> List[dict]:
    """Threads loaded in the daemon, newest first, each with its id, cwd and updatedAt.

    thread/read, not thread/list: a thread is in thread/list only after its first turn is saved.
    """
    threads = []
    for tid in rpc.call("thread/loaded/list", {})["data"]:
        try:
            t = rpc.call("thread/read", {"threadId": tid})["thread"]
        except RpcError:
            continue  # unloaded between the two calls
        if t.get("cwd"):
            threads.append(t)
    return sorted(threads, key=lambda t: t.get("updatedAt") or 0, reverse=True)


def session_key(pid: int, cwd: str) -> Optional[str]:
    """identity.RESOLVERS["codex"]: newest loaded thread in the shim's cwd (the MCP server's
    parent is the daemon, not the TUI, so the pid says nothing). None without a daemon."""
    try:
        with Rpc(default_socket()) as rpc:
            here = [t for t in _loaded_threads(rpc)
                    if os.path.realpath(t["cwd"]) == os.path.realpath(cwd)]
    except (OSError, ValueError, KeyError, TypeError, AttributeError, RpcError):
        return None
    return f"codex:{here[0]['id']}" if here else None


class CodexAdapter:
    tool = "codex"
    prefix = "codex"
    restart_note = "Codex: open a new terminal and restart Codex to pick up kaptyorka"

    def __init__(self, home: Optional[Path] = None, socket_path: Optional[Path] = None,
                 log: Callable[[str], None] = print):
        self.home = home or Path.home()
        self.socket_path = socket_path or default_socket(self.home)
        self.log = log
        self._started_at: Optional[float] = None
        self._daemon = ""  # last daemon state logged: up, started, did not start: <why>

    def installed(self) -> bool:
        return shutil.which("codex") is not None

    def _state(self, state: str) -> None:
        if state != self._daemon:
            self._daemon = state
            if state != "up":
                self.log(f"codex: app-server daemon {state}")

    def _start_daemon(self) -> None:
        now = time.monotonic()
        if self._started_at is not None and now - self._started_at < RESTART_EVERY_S:
            return
        self._started_at = now
        cmd = ["codex", "app-server", "daemon", "start"]
        if sys.platform.startswith("linux") and shutil.which("systemd-run"):
            # its own scope: a bridge restart (KillMode=control-group) must not kill the daemon
            cmd = ["systemd-run", "--user", "--scope", "--quiet"] + cmd
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=TIMEOUT_S * 3,
                               start_new_session=True)
        except (OSError, subprocess.SubprocessError) as e:
            self._state(f"did not start: {e}")
            return
        if r.returncode:
            self._state(f"did not start: {(r.stderr or r.stdout).strip()}")
        else:
            self._state("started")

    def sessions(self) -> List[Session]:
        try:
            with Rpc(self.socket_path) as rpc:
                threads = _loaded_threads(rpc)
        except (FileNotFoundError, ConnectionRefusedError):
            self._start_daemon()  # the bridge keeps the daemon running
            return []
        self._state("up")
        return [Session(f"codex:{t['id']}", self.tool, os.path.basename(t["cwd"].rstrip("/")), "", cwd=t["cwd"])
                for t in threads]

    def deliver(self, key: str, text: str) -> None:
        tid = key.partition(":")[2]
        if not tid:
            raise DeliveryError(f"not a codex session key: {key}")
        try:
            with Rpc(self.socket_path) as rpc:
                if tid not in rpc.call("thread/loaded/list", {})["data"]:
                    raise DeliveryError(f"codex session {key}: not loaded in the app-server")
                rpc.call("turn/start", {"threadId": tid,
                                        "input": [{"type": "text", "text": text}]})
        except (OSError, ValueError, KeyError, TypeError, AttributeError, RpcError) as e:
            raise DeliveryError(f"codex session {key}: {e}") from e

    def _trust(self, kaptyorka: str, outside: str) -> Optional[str]:
        """Trust entries for our hooks from the daemon's hooks/list; None if it cannot tell."""
        ours = {f"{shlex.quote(kaptyorka)} hook codex {name}" for _, name in EVENTS}
        try:
            with Rpc(self.socket_path) as rpc:
                listed = [h for entry in rpc.call("hooks/list", {})["data"]
                          for h in entry.get("hooks", []) if h.get("command") in ours]
        except (OSError, ValueError, KeyError, TypeError, AttributeError, RpcError):
            return None
        if len({h["command"] for h in listed}) != len(ours):
            return None
        trust = ""
        for h in listed:
            table = f"[hooks.state.{json.dumps(h['key'])}]"
            if table in outside:  # Codex's own entry from /hooks: a second table breaks the file
                if h.get("trustStatus") != "trusted":
                    return None
                continue
            trust += f'{table}\ntrusted_hash = {json.dumps(h["currentHash"])}\n'
        return trust

    def configure(self, kaptyorka: str) -> List[str]:
        lines = []
        conf = codex_home(self.home)

        index: Dict[str, int] = {}  # event -> our group's index in hooks.json

        def add_hooks(data: dict) -> bool:
            hooks = data.setdefault("hooks", {})
            changed = False
            for event, name in EVENTS:
                group: Dict = {"hooks": [{"type": "command",
                                          "command": f"{shlex.quote(kaptyorka)} hook codex {name}"}]}
                if event == "SessionStart":
                    group = {"matcher": "startup|resume", **group}
                groups = hooks.setdefault(event, [])
                ours = [i for i, g in enumerate(groups) if any(
                    str(h.get("command", "")).endswith(f" hook codex {name}")
                    for h in g.get("hooks", []))]
                index[event] = ours[0] if ours else len(groups)
                if [groups[i] for i in ours] == [group]:
                    continue
                for i in reversed(ours):  # an older install path: replace, never add a second
                    del groups[i]
                groups.insert(ours[0] if ours else len(groups), group)
                changed = True
            return changed

        hooks_json = conf / "hooks.json"
        hooks_changed = merge_json(hooks_json, add_hooks)
        if hooks_changed == "broken":
            self.log(f"left alone, not valid JSON: {hooks_json}")
        elif hooks_changed == "changed":
            lines.append(f"Codex: kaptyorka hooks added to {hooks_json}")

        toml = conf / "config.toml"
        try:
            text = toml.read_text()
        except FileNotFoundError:
            text = ""
        before, old, after = text, "", ""
        if START in text and END in text.split(START, 1)[1]:
            before, rest = text.split(START, 1)
            old, after = rest.split(END, 1)
            old, after = START + old + END + "\n", after[1:] if after.startswith("\n") else after
        if "[mcp_servers.kaptyorka]" in before + after:  # a second table would break the file
            self.log(f"left alone, kaptyorka already set outside the block: {toml}")
            return self._alias(lines)
        inner = old[len(START) + 1:-len(END) - 1]
        i = inner.find("[hooks.state.")
        old_mcp, old_trust = (inner[:i], inner[i:]) if i >= 0 else (inner, "")
        trust = self._trust(kaptyorka, before + after)
        if trust is None:
            trust = old_trust  # the daemon cannot tell now: keep what was there
            keys = [f"{hooks_json}:{snake}:{index[event]}:0" for event, snake in
                    (("SessionStart", "session_start"), ("UserPromptSubmit", "user_prompt_submit"))
                    if event in index]
            if keys and any(f"[hooks.state.{json.dumps(k)}]" not in text for k in keys):
                lines.append(APPROVE)
        mcp = f'[mcp_servers.kaptyorka]\ncommand = {json.dumps(kaptyorka)}\nargs = ["mcp"]\n'
        block = f"{START}\n{mcp}{trust}{END}\n"
        if block != old:
            if old:
                new = before + block + after
            else:
                new = text + ("" if not text or text.endswith("\n\n") else
                              "\n" if text.endswith("\n") else "\n\n") + block
            write_atomic(toml, new)
            if mcp != old_mcp:
                lines.append(f"Codex: kaptyorka MCP server set in {toml}")
            if trust != old_trust:
                lines.append(f"Codex: kaptyorka hooks trusted in {toml}")

        return self._alias(lines)

    def _alias(self, lines: List[str]) -> List[str]:
        line = add_alias(self.home, ALIAS, "Codex", self.log)
        if line:
            lines.append(line)
        return lines

    def unconfigure(self) -> List[str]:
        lines = []
        conf = codex_home(self.home)

        def drop(data: dict) -> bool:
            hooks = data.get("hooks")
            if not isinstance(hooks, dict):
                return False
            changed = drop_hooks(hooks, lambda h: is_our_hook(h.get("command", ""), "codex",
                                                              ("start", "prompt")))
            if changed and not hooks:
                del data["hooks"]
            return changed

        hooks_json = conf / "hooks.json"
        result = merge_json(hooks_json, drop)
        if result == "broken":
            self.log(f"left alone, not valid JSON: {hooks_json}")
        elif result == "changed":
            lines.append(f"Codex: kaptyorka hooks removed from {hooks_json}")
        toml = conf / "config.toml"
        try:
            text = toml.read_text()
        except OSError:
            text = ""
        if START in text and END in text.split(START, 1)[1]:
            before, rest = text.split(START, 1)
            after = rest.split(END, 1)[1]
            write_atomic(toml, before + after.removeprefix("\n"))
            lines.append(f"Codex: kaptyorka MCP server and hook trust removed from {toml}")
        return lines + remove_alias(self.home, ALIAS, "Codex")
