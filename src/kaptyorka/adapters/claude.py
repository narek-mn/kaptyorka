"""Claude Code: sessions from ~/.claude/sessions, push through each session's inbox socket."""
import shlex
import json
import os
import shutil
import socket
import subprocess
from pathlib import Path
from typing import Callable, List, Optional

from kaptyorka.adapters.base import (DeliveryError, Session, connect_unix, drop_hooks, is_our_hook, login_output,
                                     mentions_hook, merge_json)

CLAUDE_CODE_TESTED = "2.1.319"


def _newer(version: str) -> bool:
    try:
        return tuple(map(int, version.split("."))) > tuple(map(int, CLAUDE_CODE_TESTED.split(".")))
    except ValueError:
        return False


HOOKS = (("SessionStart", "start"), ("UserPromptSubmit", "prompt"), ("Stop", "stop"))


def hook_entry(kaptyorka: str, name: str) -> dict:
    command = f"{shlex.quote(kaptyorka)} hook claude-code {name}"
    return {"hooks": [{"type": "command", "command": command, "timeout": 5}]}


def hooks(kaptyorka: str) -> dict:
    """The settings `hooks` block that `configure` installs, on its own."""
    return {event: [hook_entry(kaptyorka, name)] for event, name in HOOKS}


def _same_start(pid: int, proc_start) -> Optional[bool]:
    """Whether the process started when the session file says (Linux /proc); None when it cannot tell."""
    if proc_start is None:
        return None
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        start = stat[stat.rindex(")") + 2:].split()[19]  # field 22; the name in () may hold spaces
    except (OSError, ValueError, IndexError):
        return None
    return str(proc_start) == start


class ClaudeAdapter:
    tool = "claude-code"
    prefix = "claude"
    restart_note = "Claude Code: open sessions pick up kaptyorka after a restart"

    def __init__(self, home: Optional[Path] = None, log: Callable[[str], None] = print,
                 run: Callable = subprocess.run):
        self.home = home or Path.home()
        self.run = run
        self.dir = self.home / ".claude" / "sessions"
        self.log = log
        self._warned = False

    def installed(self) -> bool:
        return shutil.which("claude") is not None

    def logged_in(self, run: Callable) -> Optional[bool]:
        # logged out it exits 1 with the same JSON on stdout: the JSON decides, not the exit code
        out = login_output("claude", ["auth", "status"], run, any_exit=True, with_stderr=False)
        try:
            state = json.loads(out).get("loggedIn") if out else None
        except (ValueError, AttributeError):
            return None
        return state if isinstance(state, bool) else None

    def sessions(self) -> List[Session]:
        out = []
        for f in sorted(self.dir.glob("*.json")):
            try:
                pid = int(f.stem)
                os.kill(pid, 0)
                data = json.loads(f.read_text())
                cwd = data["cwd"]
                session_id = data["sessionId"]
                started = _same_start(pid, data.get("procStart"))
                if started is False:
                    continue  # pid reused by another process
                sock_path = data.get("messagingSocketPath")
                if sock_path is not None and not started and not os.path.exists(sock_path):
                    continue  # pid reused, or a session that closed badly
            except (OSError, ValueError, KeyError, TypeError):
                continue  # dead process, or a half-written file
            version = str(data.get("version", ""))
            if not self._warned and _newer(version):
                self._warned = True
                self.log(f"claude-code {version} is newer than tested {CLAUDE_CODE_TESTED}; "
                         "push may break, pull still works")
            name = data.get("name")
            label = name if isinstance(name, str) else ""
            out.append(Session(f"claude:{session_id}", self.tool, os.path.basename(cwd.rstrip("/")),
                               data.get("status", ""), label, cwd))
        return out

    def _find(self, key: str):
        """(pid, socket path) of the session file whose sessionId is in the key."""
        wanted = key.split(":", 1)[1]
        for f in self.dir.glob("*.json"):
            try:
                data = json.loads(f.read_text())
                if data["sessionId"] == wanted:
                    return int(f.stem), data.get("messagingSocketPath")
            except (OSError, ValueError, KeyError, TypeError):
                continue
        raise ValueError("no session file with that id")

    def deliver(self, key: str, text: str) -> None:
        try:
            pid, sock_path = self._find(key)
            if sock_path is None:
                raise DeliveryError(f"claude session {key}: no inbox socket (restricted session); "
                                    "it reads the hub on its next turn")
            keys = sorted(self.dir.glob(f"{pid}.*.key"), key=lambda p: p.stat().st_mtime)
            token = json.loads(keys[-1].read_text())["peerToken"] if keys else None
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                s.settimeout(5)
                connect_unix(s, sock_path)
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
        self.run(["claude", "mcp", "remove", "--scope", "user", "kaptyorka"],
                 capture_output=True)
        r = self.run(["claude", "mcp", "add", "--scope", "user", "kaptyorka", "--",
                            kaptyorka, "mcp"], capture_output=True, text=True)
        if r.returncode:
            self.log(f"claude mcp add failed: {(r.stderr or r.stdout).strip()}")
        else:
            lines.append("Claude Code: kaptyorka MCP server registered (user scope)")

        def add_hooks(data: dict) -> bool:
            hooks = data.setdefault("hooks", {})
            changed = False
            for event, name in HOOKS:
                entry = hook_entry(kaptyorka, name)
                entries = hooks.setdefault(event, [])
                cmds = lambda e: [str(h.get("command", "")) for h in e.get("hooks", [])]
                ours = [i for i, e in enumerate(entries) if any(
                    c.endswith(f" hook claude-code {name}") for c in cmds(e))]  # `<path> hook ...` as written by us
                # a line someone wrote by hand (portable shell form) counts as present: leave it alone
                by_hand = any(i not in ours and any(mentions_hook(c, "claude-code", (name,)) for c in cmds(e))
                              for i, e in enumerate(entries))
                if by_hand:
                    for i in reversed(ours):  # our own copy would run the hook twice
                        del entries[i]
                        changed = True
                    continue
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
        manual = "Claude Code: run `claude mcp remove --scope user kaptyorka` to remove the MCP server"
        if not self.installed():
            self.log(f"claude not found; {manual}")
        else:
            try:
                r = self.run(["claude", "mcp", "remove", "--scope", "user", "kaptyorka"],
                             capture_output=True, text=True)
            except OSError as e:  # still remove the hooks below
                self.log(f"claude failed: {e}; {manual}")
            else:
                if r.returncode == 0:
                    lines.append("Claude Code: kaptyorka MCP server removed")

        def drop(data: dict) -> bool:
            hooks = data.get("hooks")
            if not isinstance(hooks, dict):
                return False
            changed = drop_hooks(hooks, lambda h: is_our_hook(
                h.get("command", ""), "claude-code", ("start", "prompt", "stop")))
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
