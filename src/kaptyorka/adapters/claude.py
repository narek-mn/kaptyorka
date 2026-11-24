"""Claude Code: sessions from ~/.claude/sessions, push through each session's inbox socket."""
import shlex
import json
import os
import re
import shutil
import socket
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, List, Optional

from kaptyorka import offhub, paths, stuck
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


WRITE_TAIL = 64 * 1024  # how much of a transcript's end is read for its last real write
TASK_OUTPUT = re.compile(r"/tasks/([A-Za-z0-9_-]{1,40})\.output$")


def _stamp(value) -> Optional[float]:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def last_write(path: Path, size: int, mtime: float) -> float:
    """When the session itself last wrote its transcript: the newest timestamped line that is
    not a queue-operation (a message delivered to it, the hub's own wake-up included); the
    file's mtime when the tail holds none."""
    with open(path, "rb") as f:
        start = max(0, size - WRITE_TAIL)
        f.seek(start)
        lines = f.read(WRITE_TAIL).decode("utf-8", "replace").splitlines()
    for line in reversed(lines[1:] if start else lines):
        if '"queue-operation"' in line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        t = _stamp(rec.get("timestamp")) if isinstance(rec, dict) else None
        if t is not None:
            return min(t, mtime)
    return mtime


REAL_TAIL = 1024 * 1024  # an idle compaction's summary can be long: look this far back at most


def _real(rec: dict) -> bool:
    """A line the person or the model wrote: an assistant line, or a user line that is no
    meta line, compaction summary, subagent line, or text the tool handed the session (a
    delivered hub message, another session's, a task notice). Claude Code also writes while
    nobody works: an away summary, an idle compaction, metadata blocks (checked on 2.1.331)."""
    if rec.get("isSidechain") is True:
        return False
    if rec.get("type") == "assistant":
        return True
    if rec.get("type") != "user" or rec.get("isMeta") is True or rec.get("isCompactSummary") is True:
        return False
    origin = rec.get("origin")
    if isinstance(origin, dict) and origin.get("kind") not in (None, "human"):
        return False
    msg = rec.get("message")
    content = msg.get("content") if isinstance(msg, dict) else None
    if isinstance(content, str):
        from kaptyorka.overnight import typed

        return typed(content)
    return True  # a tool result: part of the model's own turn


def last_real(path: Path, size: int) -> Optional[float]:
    """When the person or the model last did something in the session (see _real): the newest
    such line's timestamp; None when none is in the last REAL_TAIL bytes."""
    window = WRITE_TAIL
    while True:
        start = max(0, size - window)
        with open(path, "rb") as f:
            f.seek(start)
            lines = f.read(size - start).decode("utf-8", "replace").splitlines()
        for line in reversed(lines[1:] if start else lines):
            if '"assistant"' not in line and '"user"' not in line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if isinstance(rec, dict) and _real(rec):
                t = _stamp(rec.get("timestamp"))
                if t is not None:
                    return t
        if start == 0 or window >= REAL_TAIL:
            return None
        window = REAL_TAIL


LINEAGE_LINES = 40  # derived-from and the entrypoint come within a transcript's first lines


def lineage(path: Path, session_id: str) -> "tuple[str, bool, str]":
    """(the parent session id, a one-shot run, how it was started) from the transcript's
    head: Claude Code's `derived-from` line (undocumented; checked on 2.1.329 and 2.1.331:
    via "launch" for `claude --bg` from a session, "subprocess" for any other child process
    of one, "resume"/"compact" name the session itself) and `entrypoint` ("sdk-cli" for
    `claude -p`). ("", False, "") when the transcript says nothing."""
    parent, oneshot, via = "", False, ""
    try:
        with open(path, "rb") as f:
            for _ in range(LINEAGE_LINES):
                raw = f.readline()
                if not raw:
                    break
                try:
                    rec = json.loads(raw)
                except ValueError:
                    continue
                if not isinstance(rec, dict):
                    continue
                if rec.get("entrypoint") == "sdk-cli":
                    oneshot = True
                if rec.get("type") == "derived-from" and not parent:
                    for s in rec.get("sessions") or []:
                        if isinstance(s, dict) and s.get("via") in ("launch", "subprocess") \
                                and isinstance(s.get("id"), str) and s["id"] != session_id:
                            parent, via = s["id"], s["via"]
                            break
    except OSError:
        pass
    return parent, oneshot, via


def notes_parent(session_id: str, tmp: Optional[Path] = None) -> str:
    """The session that started this one, from the starter's child notes: Claude Code gives
    each session CLAUDE_CODE_CHILD_NOTES_DIR (<tmp>/claude-<uid>/<project>/<its id>/started)
    and adds an empty `<child id>.<random>` file there for each session it starts
    (undocumented; checked on 2.1.329/2.1.331). "" when none names it."""
    if not session_id.replace("-", "").isalnum():
        return ""
    base = (tmp or Path("/tmp")) / f"claude-{os.getuid()}"
    for p in base.glob(f"*/*/started/{session_id}.*"):
        return p.parent.parent.name
    return ""


TOOLS = ("opencode", "codex", "cursor")  # an ancestor process like these started the session


def tool_ancestor(pid: int, depth: int = 8) -> str:
    """The agent tool among the process's ancestors (Linux /proc), "" when none or unknown."""
    for _ in range(depth):
        try:
            stat = Path(f"/proc/{pid}/stat").read_text()
            pid = int(stat[stat.rindex(")") + 2:].split()[1])
            if pid <= 1:
                return ""
            comm = Path(f"/proc/{pid}/comm").read_text().strip().lower()
        except (OSError, ValueError, IndexError):
            return ""
        for t in TOOLS:
            if comm.startswith(t):
                return t
    return ""


def children() -> Optional[dict]:
    """{parent pid: [child pids]} from /proc; None where there is no /proc (macOS)."""
    proc = Path("/proc")
    if not (proc / "self" / "stat").exists():
        return None
    out: dict = {}
    for d in proc.iterdir():
        if not d.name.isdigit():
            continue
        try:
            stat = (d / "stat").read_text()
            ppid = int(stat[stat.rindex(")") + 2:].split()[1])
        except (OSError, ValueError, IndexError):
            continue
        out.setdefault(ppid, []).append(int(d.name))
    return out


def live_tasks(pid: int, kids: dict, now: float) -> List[dict]:
    """The session's running background tasks: its child shells whose output goes to
    .../tasks/<task id>.output (checked on 2.1.331). Silent for: now minus that file's mtime;
    Claude Code creates the file when the task starts, so a task is never silent for longer
    than it has run."""
    out, seen = [], set()
    for child in kids.get(pid, []):
        try:
            target = os.readlink(f"/proc/{child}/fd/1")
        except OSError:
            continue
        m = TASK_OUTPUT.search(target)
        # .../<id>/tasks/: the id the process started with, which a resumed session no longer
        # has in its sessions file; being the session's child is what ties a task to it
        if m is None or m.group(1) in seen:
            continue
        try:
            mtime = os.stat(target).st_mtime
            argv = Path(f"/proc/{child}/cmdline").read_bytes().decode("utf-8", "replace")
        except OSError:
            continue
        seen.add(m.group(1))
        out.append({"id": m.group(1), "silent_s": max(0.0, round(now - mtime, 1)),
                    "command": stuck.safe_command(stuck.command_of(argv.rstrip("\0").split("\0")))})
    return out[:20]


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


def live_children(parent_id: str, notes_dir: Optional[str], home: Optional[Path] = None
                  ) -> "list[tuple[str, str]]":
    """[(session id, name)] of the live `claude --bg` sessions this session started: the
    ones its child notes dir names (CLAUDE_CODE_CHILD_NOTES_DIR, in a Stop hook's environment
    on 2.1.331), else, with no such dir, those whose transcript's derived-from names it."""
    home = home or Path.home()
    named: Optional[set] = None
    if notes_dir and os.path.isdir(notes_dir):
        named = {n.split(".", 1)[0] for n in os.listdir(notes_dir)}
        if not named:
            return []
    out, titles = [], {}
    for f in sorted((home / ".claude" / "sessions").glob("*.json")):
        try:
            pid = int(f.stem)
            os.kill(pid, 0)
            data = json.loads(f.read_text())
            sid, cwd = data["sessionId"], data["cwd"]
            if data.get("kind") != "bg" or sid == parent_id \
                    or _same_start(pid, data.get("procStart")) is False:
                continue
        except (OSError, ValueError, KeyError, TypeError):
            continue
        if named is not None:
            if sid not in named:
                continue
        else:
            path = _transcript(home, sid, cwd, titles)
            if path is None or lineage(path, sid)[0] != parent_id:
                continue
        name = data.get("name")
        out.append((sid, name if isinstance(name, str) and name else sid[:8]))
    return out


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
        self.launched: set = set()  # of those, `claude --bg` runs another session started

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
        out, off, launched = [], set(), set()
        kids: Optional[dict] = None  # read once, only when a session is busy
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
            title, _ = self._title(session_id, cwd)
            active = self._active(session_id, cwd)
            # "nameSource" (undocumented, checked on 2.1.329/2.1.330): "derived" is the automatic
            # <folder>-<2 chars> name, new on every resume; -n and /rename give "user". The
            # automatic one gives way to Claude Code's title; the manager keeps its name
            if data.get("nameSource") == "derived" and title and not paths.in_manager_folder(cwd):
                label = ""
            # "kind" is undocumented: "bg" marks a background job; anything else is a normal one
            if offhub.decide(session_id, pid, data):
                off.add(f"claude:{session_id}")
            status = str(data.get("status", ""))
            # stuck measures (undocumented, checked on 2.1.331): "shell" is a finished turn with
            # a background task still running; "busy" a turn or a background subagent
            tasks, write_s = None, None
            if status in ("busy", "shell"):
                if kids is None:
                    kids = children() or {}
                now = time.time()
                tasks = live_tasks(pid, kids, now) if kids else None
                wrote = self._wrote(session_id, cwd)
                write_s = None if wrote is None else max(0.0, round(now - wrote, 1))
            parent, oneshot, tool, via = self._lineage(session_id, cwd, pid)
            if via == "launch":
                launched.add(f"claude:{session_id}")
            out.append(Session(f"claude:{session_id}", self.tool, os.path.basename(cwd.rstrip("/")),
                               status, label, cwd, data.get("kind") == "bg",
                               title, None if active is None else max(0.0, time.time() - active),
                               write_s, tasks, f"claude:{parent}" if parent else "",
                               oneshot and data.get("kind") != "bg",
                               bool(parent or tool)))
        live = {s.key for s in out}
        for s in out:  # its parent ended: no live session file of it on this machine
            s.parent_gone = bool(s.parent) and s.parent not in live
        self.off_keys = off
        self.launched = launched
        return out

    def _lineage(self, session_id: str, cwd: str, pid: int) -> "tuple[str, bool, str, str]":
        """(parent session id, one-shot, the tool that started it, how the parent started it:
        "launch" for `claude --bg` from it) for left.py; read once per
        session: a transcript's head and the starter's notes never change. Each source is
        undocumented, so a missing one leaves "" (today's behaviour: a session of its own)."""
        cache = self.__dict__.setdefault("_lineages", {})
        if session_id in cache:
            return cache[session_id]
        parent, oneshot, via = "", False, ""
        path = _transcript(self.home, session_id, cwd, self.__dict__.setdefault("_titles", {}))
        if path is not None:
            parent, oneshot, via = lineage(path, session_id)
        try:
            parent = parent or notes_parent(session_id)
        except OSError:
            pass
        tool = "" if parent else tool_ancestor(pid)
        if path is not None:  # no transcript yet: ask again next time
            cache[session_id] = (parent, oneshot, tool, via)
        return parent, oneshot, tool, via

    def _active(self, session_id: str, cwd: str) -> Optional[float]:
        """When the person or the model last did something (last_real), its subagents' work
        too; the transcript's mtime when no such line is found (as before)."""
        cache = self.__dict__.setdefault("_actives", {})
        try:
            path = _transcript(self.home, session_id, cwd, self.__dict__.setdefault("_titles", {}))
            if path is None:
                return None
            st = path.stat()
            old = cache.get(session_id)
            if old is not None and old[0] == (st.st_size, st.st_mtime):
                t = old[1]
            else:
                t = last_real(path, st.st_size)
                t = st.st_mtime if t is None else t
                cache[session_id] = ((st.st_size, st.st_mtime), t)
            for sub in (path.parent / session_id / "subagents").glob("*.jsonl"):
                t = max(t, sub.stat().st_mtime)
            return t
        except (OSError, ValueError):
            return None

    def _wrote(self, session_id: str, cwd: str) -> Optional[float]:
        """When the session last wrote anything of its own: its transcript's last real line,
        or any of its subagents' transcripts; None with no transcript."""
        cache = self.__dict__.setdefault("_writes", {})
        try:
            path = _transcript(self.home, session_id, cwd, self.__dict__.setdefault("_titles", {}))
            if path is None:
                return None
            st = path.stat()
            old = cache.get(session_id)
            if old is not None and old[0] == (st.st_size, st.st_mtime):
                t = old[1]
            else:
                t = last_write(path, st.st_size, st.st_mtime)
                cache[session_id] = ((st.st_size, st.st_mtime), t)
            for sub in (path.parent / session_id / "subagents").glob("*.jsonl"):
                t = max(t, sub.stat().st_mtime)
            return t
        except (OSError, ValueError):
            return None

    def _title(self, session_id: str, cwd: str):
        """(Claude Code's title for the session, its transcript's mtime = its last activity),
        the title read from the end of the transcript only when the file changed. Never raises
        and never reads the whole file; ("", None) with no transcript."""
        cache = self.__dict__.setdefault("_titles", {})
        try:
            path = _transcript(self.home, session_id, cwd, cache)
            if path is None:
                return "", None
            st = path.stat()
            old = cache.get(session_id)
            if old is not None and old[0] == (st.st_size, st.st_mtime):
                return old[1], st.st_mtime
            title = read_title(path, st.st_size) or (old[1] if old else "")
            cache[session_id] = ((st.st_size, st.st_mtime), title, path)
            return title, st.st_mtime
        except (OSError, ValueError):
            return "", None

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
