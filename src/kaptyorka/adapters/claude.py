"""Claude Code: sessions from ~/.claude/sessions, push through each session's inbox socket."""
import shlex
import json
import os
import shutil
import socket
import subprocess
from pathlib import Path
from typing import Callable, List, Optional

from kaptyorka.adapters.base import DeliveryError, Session, drop_hooks, merge_json

CLAUDE_CODE_TESTED = "2.1.318"


def _newer(version: str) -> bool:
    try:
        return tuple(map(int, version.split("."))) > tuple(map(int, CLAUDE_CODE_TESTED.split(".")))
    except ValueError:
        return False


class ClaudeAdapter:
    tool = "claude-code"
    prefix = "claude"
    restart_note = "Claude Code: open sessions pick up kaptyorka after a restart"

    def __init__(self, home: Optional[Path] = None, log: Callable[[str], None] = print):
        self.home = home or Path.home()
        self.dir = self.home / ".claude" / "sessions"
        self.log = log
        self._warned = False

    def installed(self) -> bool:
        return shutil.which("claude") is not None

    def sessions(self) -> List[Session]:
        out = []
        for f in sorted(self.dir.glob("*.json")):
            try:
                pid = int(f.stem)
                os.kill(pid, 0)
                data = json.loads(f.read_text())
                cwd = data["cwd"]
                if not os.path.exists(data["messagingSocketPath"]):
                    continue  # pid reused by another process, or a session that closed badly
            except (OSError, ValueError, KeyError, TypeError):
                continue  # dead process, or a half-written file
            version = str(data.get("version", ""))
            if not self._warned and _newer(version):
                self._warned = True
                self.log(f"claude-code {version} is newer than tested {CLAUDE_CODE_TESTED}; "
                         "push may break, pull still works")
            name = data.get("name")
            label = name if isinstance(name, str) else ""
            out.append(Session(f"claude:{pid}", self.tool, os.path.basename(cwd.rstrip("/")),
                               data.get("status", ""), label))
        return out

    def deliver(self, key: str, text: str) -> None:
        try:
            pid = int(key.split(":", 1)[1])
            sock_path = json.loads((self.dir / f"{pid}.json").read_text())["messagingSocketPath"]
            keys = sorted(self.dir.glob(f"{pid}.*.key"), key=lambda p: p.stat().st_mtime)
            token = json.loads(keys[-1].read_text())["peerToken"] if keys else None
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                s.settimeout(5)
                s.connect(sock_path)
                if token is not None:
                    s.sendall((json.dumps({"type": "auth", "token": token}) + "\n").encode())
                message = {"type": "user", "message": {"role": "user", "content": text}}
                s.sendall((json.dumps(message) + "\n").encode())
                s.shutdown(socket.SHUT_WR)
                reply = b""
                try:
                    while chunk := s.recv(65536):
                        reply += chunk
                except socket.timeout:
                    pass  # sent; no answer is not a refusal (2.1.318 answers nothing)
        except (OSError, ValueError, KeyError, TypeError, IndexError) as e:
            raise DeliveryError(f"claude session {key}: {e}") from e
        if b"error" in reply.lower():
            answer = reply.decode(errors="replace").strip()
            raise DeliveryError(f"claude session {key} refused: {answer}")

    def configure(self, kaptyorka: str) -> List[str]:
        lines = []
        subprocess.run(["claude", "mcp", "remove", "--scope", "user", "kaptyorka"],
                       capture_output=True)
        r = subprocess.run(["claude", "mcp", "add", "--scope", "user", "kaptyorka", "--",
                            kaptyorka, "mcp"], capture_output=True, text=True)
        if r.returncode:
            self.log(f"claude mcp add failed: {(r.stderr or r.stdout).strip()}")
        else:
            lines.append("Claude Code: kaptyorka MCP server registered (user scope)")

        def add_hooks(data: dict) -> bool:
            hooks = data.setdefault("hooks", {})
            changed = False
            for event, name in (("SessionStart", "start"), ("UserPromptSubmit", "prompt")):
                command = f"{shlex.quote(kaptyorka)} hook claude-code {name}"
                entry = {"hooks": [{"type": "command", "command": command, "timeout": 5}]}
                entries = hooks.setdefault(event, [])
                ours = [i for i, e in enumerate(entries) if any(
                    str(h.get("command", "")).endswith(f" hook claude-code {name}")
                    for h in e.get("hooks", []))]
                if [entries[i] for i in ours] == [entry]:
                    continue
                for i in reversed(ours):  # an older install path: replace, never add a second
                    del entries[i]
                entries.insert(ours[0] if ours else len(entries), entry)
                changed = True
            return changed

        settings = self.home / ".claude" / "settings.json"
        result = merge_json(settings, add_hooks)
        if result == "broken":
            self.log(f"left alone, not valid JSON: {settings}")
        elif result == "changed":
            lines.append(f"Claude Code: kaptyorka hooks added to {settings}")
        return lines

    def unconfigure(self) -> List[str]:
        lines = []
        if self.installed():
            r = subprocess.run(["claude", "mcp", "remove", "--scope", "user", "kaptyorka"],
                               capture_output=True, text=True)
            if r.returncode == 0:
                lines.append("Claude Code: kaptyorka MCP server removed")

        def drop(data: dict) -> bool:
            hooks = data.get("hooks")
            if not isinstance(hooks, dict):
                return False
            changed = drop_hooks(hooks, lambda h: str(h.get("command", "")).endswith(
                (" hook claude-code start", " hook claude-code prompt")))
            if changed and not hooks:
                del data["hooks"]
            return changed

        settings = self.home / ".claude" / "settings.json"
        result = merge_json(settings, drop)
        if result == "broken":
            self.log(f"left alone, not valid JSON: {settings}")
        elif result == "changed":
            lines.append(f"Claude Code: kaptyorka hooks removed from {settings}")
        return lines
