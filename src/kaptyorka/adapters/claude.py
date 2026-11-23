"""Claude Code: sessions from ~/.claude/sessions, push through each session's inbox socket."""
import shlex
import json
import os
import re
import shutil
import socket
import subprocess
from pathlib import Path
from typing import Callable, List, Optional

from kaptyorka import offhub, paths
from kaptyorka.adapters.base import (NIGHT_GUARD, DeliveryError, Session, connect_unix, drop_hooks, is_our_hook, login_output,
                                     mentions_hook, merge_json, write_atomic)

CLAUDE_CODE_TESTED = "2.1.319"
TITLE_TAIL = 256 * 1024  # how much of a transcript's end is read for its title
TITLE_HEAD = 64 * 1024  # the first read also looks at the start, where the automatic title is
MAX_TITLE = 80


def clean_title(value) -> str:
    """One line of printable text, at most MAX_TITLE characters."""
    if not isinstance(value, str):
        return ""
    return " ".join("".join(c if c.isprintable() else " " for c in value).split())[:MAX_TITLE]


def parse_title(text: str) -> str:
    """The newest custom-title (/rename), else the newest ai-title, in transcript lines; lines
    that are not JSON or not those records are skipped."""
    custom = auto = ""
    for line in text.splitlines():
        if '"custom-title"' not in line and '"ai-title"' not in line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if not isinstance(rec, dict):
            continue
        if rec.get("type") == "custom-title" and clean_title(rec.get("customTitle")):
            custom = clean_title(rec.get("customTitle"))
        elif rec.get("type") == "ai-title" and clean_title(rec.get("aiTitle")):
            auto = clean_title(rec.get("aiTitle"))
    return custom or auto


def read_title(path: Path, size: int) -> str:
    """Parse the last TITLE_TAIL bytes of the file (a first partial line dropped); when they hold
    no title, the first TITLE_HEAD bytes too."""
    with open(path, "rb") as f:
        start = max(0, size - TITLE_TAIL)
        f.seek(start)
        text = f.read(TITLE_TAIL).decode("utf-8", "replace")
        if start:
            text = text.split("\n", 1)[-1]
        found = parse_title(text)
        if not found and start:
            f.seek(0)
            found = parse_title(f.read(TITLE_HEAD).decode("utf-8", "replace").rsplit("\n", 1)[0])
    return found


def _transcript(home: Path, session_id: str, cwd: str, cache: dict) -> Optional[Path]:
    """The session's transcript: ~/.claude/projects/<folder with / and . as ->/<id>.jsonl."""
    old = cache.get(session_id)
    if old is not None and old[2].exists():
        return old[2]
    root = home / ".claude" / "projects"
    if not session_id.replace("-", "").isalnum():
        return None
    guess = root / re.sub(r"[^A-Za-z0-9]", "-", cwd) / f"{session_id}.jsonl"
    if guess.exists():
        return guess
    for p in root.glob(f"*/{session_id}.jsonl"):
        return p
    return None


def _newer(version: str) -> bool:
    try:
        return tuple(map(int, version.split("."))) > tuple(map(int, CLAUDE_CODE_TESTED.split(".")))
    except ValueError:
        return False


HOOKS = (("SessionStart", "start"), ("UserPromptSubmit", "prompt"), ("Stop", "stop"),
         # overnight mode (overnight.py); each answers at once while it is off
         ("PreToolUse", "pretool"), ("PermissionRequest", "permission"),
         ("StopFailure", "failure"), ("SessionEnd", "end"))


COMMAND_MARK = "<!-- written by kaptyorka; it rewrites this file -->"


def command_text(kaptyorka: str) -> str:
    """~/.claude/commands/hub.md: `/hub`, `/hub on`, `/hub off`, `/hub team [project|off]`.
    The `!` line runs before the model sees anything; the model cannot run the command itself."""
    exe = shlex.quote(kaptyorka)
    return (f"---\ndescription: Show or switch whether this session is on the kaptyorka hub,"
            f" or put it on the team (team: coordinate; team <project>: work for one)\n"
            f"argument-hint: \"[on|off|team [project|off]|overnight [off]]\"\nallowed-tools: Bash({exe} hub:*)\n"
            f"disable-model-invocation: true\n---\n{COMMAND_MARK}\n"
            f"!`{exe} hub $ARGUMENTS`\n\n"
            "Tell the user the line above, word for word, and nothing else.\n")


GUARDED = ("pretool", "permission")  # run on every tool call: skipped in the shell while off


def hook_entry(kaptyorka: str, name: str) -> dict:
    command = f"{shlex.quote(kaptyorka)} hook claude-code {name}"
    if name in GUARDED:
        command = NIGHT_GUARD + command
    return {"hooks": [{"type": "command", "command": command, "timeout": 5}]}


def hooks(kaptyorka: str) -> dict:
    """The settings `hooks` block that `configure` installs, on its own."""
    return {event: [hook_entry(kaptyorka, name)] for event, name in HOOKS}


STATUS_SAVE = "statusline.json"  # under ~/.kaptyorka: the status line join replaced, for leave


def _saved_status() -> Optional[dict]:
    try:
        st = json.loads((paths.home() / STATUS_SAVE).read_text())
    except (OSError, ValueError):
        return None
    return st if isinstance(st, dict) and isinstance(st.get("ours"), str) else None


def _status_argv(command) -> List[str]:
    try:
        return shlex.split(command) if isinstance(command, str) else []
    except ValueError:
        return []


def _our_status(command, kaptyorka: str = "") -> bool:
    """A status line command `join` wrote: `<kaptyorka> statusline [-- <their command>]`."""
    argv = _status_argv(command)
    saved = _saved_status()
    return (saved is not None and command == saved["ours"]) or (
        len(argv) >= 2 and argv[1] == "statusline"
        and (argv[0] == kaptyorka or os.path.basename(argv[0]) == "kaptyorka"))


def status_command(kaptyorka: str, original: Optional[str]) -> str:
    exe = shlex.quote(kaptyorka)
    return f"{exe} statusline -- {shlex.quote(original)}" if original else f"{exe} statusline"


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
        self.off_keys: set = set()  # sessions of the last sessions() kept off the hub

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
        """Every live session, those kept off the hub too (in off_keys): the bridge reports
        only the rest, but its own decisions (a busy folder) count them all."""
        out, off = [], set()
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
            # "kind" is undocumented: "bg" marks a background job; anything else is a normal one
            if offhub.decide(session_id, pid, data):
                off.add(f"claude:{session_id}")
            out.append(Session(f"claude:{session_id}", self.tool, os.path.basename(cwd.rstrip("/")),
                               data.get("status", ""), label, cwd, data.get("kind") == "bg",
                               self._title(session_id, cwd)))
        self.off_keys = off
        return out

    def _title(self, session_id: str, cwd: str) -> str:
        """Claude Code's title for the session, read from the end of its transcript only when
        the file changed. Never raises and never reads the whole file."""
        cache = self.__dict__.setdefault("_titles", {})
        try:
            path = _transcript(self.home, session_id, cwd, cache)
            if path is None:
                return ""
            st = path.stat()
            old = cache.get(session_id)
            if old is not None and old[0] == (st.st_size, st.st_mtime):
                return old[1]
            title = read_title(path, st.st_size) or (old[1] if old else "")
            cache[session_id] = ((st.st_size, st.st_mtime), title, path)
            return title
        except (OSError, ValueError):
            return ""

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
        if key in self.off_keys:
            raise DeliveryError(f"claude session {key}: that session is off the hub")
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

    def _mcp_registered(self, kaptyorka: str) -> bool:
        """Already registered with this command and args. Detected from ~/.claude.json, the file
        `claude mcp add --scope user` writes (and `claude mcp get` reads): no claude process needed."""
        try:
            entry = json.loads((self.home / ".claude.json").read_text())["mcpServers"]["kaptyorka"]
        except (OSError, ValueError, KeyError, TypeError):
            return False
        return isinstance(entry, dict) and entry.get("command") == kaptyorka and entry.get("args") == ["mcp"]

    def install_command(self, kaptyorka: str) -> List[str]:
        """Write the /hub command; a file someone else wrote at that path is left alone."""
        f = self.home / ".claude" / "commands" / "hub.md"
        want = command_text(kaptyorka)
        try:
            old = f.read_text() if f.exists() else None
            if old == want:
                return []
            if old is not None and COMMAND_MARK not in old:
                self.log(f"left alone, not written by kaptyorka: {f}")
                return []
            f.parent.mkdir(parents=True, exist_ok=True)
            write_atomic(f, want)
        except OSError as e:
            self.log(f"could not write {f}: {e}")
            return []
        return [f"Claude Code: /hub command written to {f}"]

    def configure(self, kaptyorka: str) -> List[str]:
        lines = []
        if not self._mcp_registered(kaptyorka):
            self.run(["claude", "mcp", "remove", "--scope", "user", "kaptyorka"],
                     capture_output=True)
            r = self.run(["claude", "mcp", "add", "--scope", "user", "kaptyorka", "--",
                          kaptyorka, "mcp"], capture_output=True, text=True)
            if r.returncode:
                self.log(f"claude mcp add failed: {(r.stderr or r.stdout).strip()}")
            else:
                lines.append("Claude Code: kaptyorka MCP server registered (user scope)")
        return lines + self.add_hooks(kaptyorka) + self.install_hub(kaptyorka)

    def add_hooks(self, kaptyorka: str) -> List[str]:
        """kaptyorka's hooks in ~/.claude/settings.json, each once; also at bridge start, so
        hooks a newer kaptyorka adds (overnight mode) reach machines that joined earlier.
        Claude Code's file watcher picks them up in running sessions (docs/en/hooks)."""
        lines = []

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

    def install_hub(self, kaptyorka: str) -> List[str]:
        """The /hub command, the hub field in the status line and the hooks; also run at bridge
        start."""
        return self.wire_status(kaptyorka) + self.install_command(kaptyorka) \
            + self.add_hooks(kaptyorka)

    def wire_status(self, kaptyorka: str) -> List[str]:
        """Put the hub field in the status line. With none, the line shows only the field; with
        one of the person's own, `kaptyorka statusline -- <theirs>` runs it and appends the field.
        What it replaced is kept in ~/.kaptyorka/statusline.json, for leave."""
        said: List[str] = []

        def update(data: dict) -> bool:
            cur = data.get("statusLine")
            if cur is not None and not isinstance(cur, dict):
                return False  # a shape we do not know: leave it alone
            cmd = cur.get("command") if cur else None
            saved = _saved_status()
            if cur and _our_status(cmd, kaptyorka):
                if saved is not None:
                    original = saved.get("original")
                    theirs = original.get("command") if isinstance(original, dict) else None
                else:  # the saved file is gone: what we wrap is in the command itself
                    argv = _status_argv(cmd)
                    theirs = " ".join(argv[3:]) if argv[2:3] == ["--"] else None
                    original = {"type": "command", "command": theirs} if theirs else None
                want = status_command(kaptyorka, theirs)
                if cmd == want and saved is not None:
                    return False
                self._save_status(want, original)
                cur["command"] = want
                return True
            if cur and (cur.get("type", "command") != "command" or not isinstance(cmd, str) or not cmd.strip()):
                return False
            want = status_command(kaptyorka, cmd if cur else None)
            self._save_status(want, dict(cur) if cur else None)
            if cur:
                data["statusLine"] = {**cur, "command": want}
                said.append("Claude Code: added the hub field after your status line;"
                            " kaptyorka leave puts yours back as it was")
            else:
                data["statusLine"] = {"type": "command", "command": want, "padding": 0}
                said.append("Claude Code: the status line shows the hub field (kaptyorka leave removes it)")
            return True

        settings = self.home / ".claude" / "settings.json"
        if merge_json(settings, update) == "broken":
            return []  # configure already said so
        return said

    @staticmethod
    def _save_status(ours: str, original: Optional[dict]) -> None:
        write_atomic(paths.home() / STATUS_SAVE, json.dumps({"ours": ours, "original": original}))

    def unwire_status(self) -> List[str]:
        """Put back the status line `join` replaced, exactly; one changed since is left alone."""
        said: List[str] = []
        saved = _saved_status()

        def update(data: dict) -> bool:
            cur = data.get("statusLine")
            cmd = cur.get("command") if isinstance(cur, dict) else None
            if not cur or not _our_status(cmd):
                return False
            if saved is not None and cmd == saved["ours"]:
                original = saved.get("original")
            else:  # no saved file: unwrap the command itself
                argv = _status_argv(cmd)
                theirs = " ".join(argv[3:]) if argv[2:3] == ["--"] else None
                original = {**cur, "command": theirs} if theirs else None
            if isinstance(original, dict):
                data["statusLine"] = original
                said.append("Claude Code: your status line is back as it was")
            else:
                del data["statusLine"]
                said.append("Claude Code: hub field removed from the status line")
            return True

        settings = self.home / ".claude" / "settings.json"
        merge_json(settings, update)
        try:
            (paths.home() / STATUS_SAVE).unlink()
        except OSError:
            pass
        return said

    def unconfigure(self) -> List[str]:
        lines = []
        command = self.home / ".claude" / "commands" / "hub.md"
        try:
            if COMMAND_MARK in command.read_text():
                command.unlink()
                lines.append(f"Claude Code: /hub command removed ({command})")
        except OSError:
            pass
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
                h.get("command", ""), "claude-code", tuple(name for _, name in HOOKS)))
            if changed and not hooks:
                del data["hooks"]
            return changed

        settings = self.home / ".claude" / "settings.json"
        result = merge_json(settings, drop)
        if result == "broken":
            self.log(f"left alone, not valid JSON: {settings}")
        elif result == "changed":
            lines.append(f"Claude Code: kaptyorka hooks removed from {settings}")
        return lines + self.unwire_status()
