"""What every tool adapter provides, and the shared JSON config merge.

Session keys, one per tool, identical in shim, hook and bridge:
  claude:<session id>      Claude's sessionId from ~/.claude/sessions/<pid>.json (kept on resume);
                           project = basename of the session file's cwd
  opencode:<session id>
  codex:<thread id>
  cursor:<machine>:<path>  path = the --workspace argument, else the shim's cwd
"""
import json
import os
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional, Protocol


@dataclass
class Session:
    key: str
    tool: str
    project: str
    activity: str = ""
    label: str = ""  # the session name Claude Code shows, if any
    cwd: str = ""  # the folder the session runs in; "" when unknown


class DeliveryError(Exception):
    """The tool's door did not accept the message."""


class Adapter(Protocol):
    """One coding tool. Constructors take log: Callable[[str], None] = print."""

    tool: str
    prefix: str  # session key prefix: claude, opencode, codex, cursor
    restart_note: Optional[str]

    def installed(self) -> bool: ...

    def sessions(self) -> List[Session]: ...

    def deliver(self, key: str, text: str) -> None:
        """Push text into the session; raise DeliveryError if the door refuses."""

    def configure(self, kaptyorka: str) -> List[str]:
        """Wire the tool to the kaptyorka executable; return change lines; idempotent."""

    def unconfigure(self) -> List[str]:
        """Undo configure: remove only our entries; return one line per thing removed."""

    def logged_in(self, run: Callable) -> Optional[bool]:
        """Whether the tool has a login; None when unknown."""
        return None


TOOL_NAMES = {"claude-code": "Claude Code", "codex": "Codex", "opencode": "OpenCode",
              "cursor": "Cursor"}
LOGIN_COMMANDS = {"claude-code": "claude auth login", "codex": "codex login",
                  "opencode": "opencode auth login"}


def login_output(command: str, args: List[str], run: Callable, any_exit: bool = False) -> Optional[str]:
    """Stdout of the tool's login status command; None if it is missing, fails or hangs.
    any_exit: stdout and stderr together, whatever the exit code.
    The output may name the account: callers keep only a yes or no from it."""
    exe = shutil.which(command)
    if not exe:
        return None
    try:
        r = run([exe, *args], capture_output=True, text=True, timeout=5)
    except (subprocess.TimeoutExpired, OSError):
        return None
    if any_exit:
        return (r.stdout or "") + (r.stderr or "")
    return (r.stdout or "") if r.returncode == 0 else None


_cwd_lock = threading.Lock()


def connect_unix(sock: socket.socket, path) -> None:
    """Connect a Unix socket; a long path is reached from its own folder."""
    path = str(path)
    if len(os.fsencode(path)) < 100:  # limit is 108 bytes on Linux, 104 on macOS
        sock.connect(path)
        return
    with _cwd_lock:
        here = os.getcwd()
        os.chdir(os.path.dirname(path))
        try:
            sock.connect(os.path.basename(path))
        finally:
            os.chdir(here)


def write_atomic(path: Path, text: str) -> None:
    """Replace the file in one step; a new file is 0600, an existing one keeps its mode.
    Writes go to the symlink's target."""
    target = path.resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=target.name + ".")  # 0600
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        if target.exists():
            os.chmod(tmp, target.stat().st_mode & 0o7777)
        os.replace(tmp, target)
    except BaseException:
        os.unlink(tmp)
        raise


def merge_json(path: Path, update: Callable[[dict], bool]) -> str:
    """Apply update to a JSON file; "changed", "unchanged", or "broken" (left alone).

    A missing file counts as {} and is created 0600. Writes go to the symlink's target,
    keeping its mode.
    """
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        data = {}
    except ValueError:
        return "broken"
    if not isinstance(data, dict):
        return "broken"
    if not update(data):
        return "unchanged"
    write_atomic(path, json.dumps(data, indent=2) + "\n")
    return "changed"


def add_alias(home: Path, alias: str, label: str, log: Callable[[str], None]) -> Optional[str]:
    """Append alias to the shell startup file once; the change line, or None if already there."""
    shell = os.path.basename(os.environ.get("SHELL", ""))
    bash_rc = ".bash_profile" if sys.platform == "darwin" else ".bashrc"  # macOS: login shells
    rc = {"bash": bash_rc, "zsh": ".zshrc"}.get(shell)
    if rc is None:
        log(f"{label}: add `{alias}` to your shell startup file so kaptyorka can push")
        return None
    rc_path = home / rc
    try:
        current = rc_path.read_text()
    except FileNotFoundError:
        current = ""
    if alias in current.splitlines():
        return None
    with open(rc_path, "a") as f:
        f.write(("" if current.endswith("\n") or not current else "\n") + alias + "\n")
    return f"{label}: `{alias}` added to {rc_path}, so kaptyorka can push into new {label} windows"


def remove_alias(home: Path, alias: str, label: str) -> List[str]:
    """Delete our alias line from the shell startup files; one line per file changed."""
    lines = []
    for rc in (".bashrc", ".bash_profile", ".zshrc"):
        path = home / rc
        try:
            text = path.read_text()
        except OSError:
            continue
        kept = [line for line in text.splitlines(keepends=True) if line.rstrip("\n") != alias]
        if len(kept) != len(text.splitlines(keepends=True)):
            write_atomic(path, "".join(kept))
            lines.append(f"{label}: `{alias}` removed from {path}")
    return lines


def is_our_hook(command: object, tool: str, events: tuple) -> bool:
    """True only for `<path>/kaptyorka hook <tool> <event>`, quoted or not (older installs)."""
    command = str(command)
    for event in events:
        suffix = f" hook {tool} {event}"
        if command.endswith(suffix):
            exe = command[:-len(suffix)]
            try:
                parts = shlex.split(exe)
            except ValueError:
                return False
            exe = parts[0] if len(parts) == 1 else exe  # unquoted path with spaces
            return os.path.basename(exe) == "kaptyorka"
    return False


def drop_hooks(hooks: dict, is_ours: Callable[[dict], bool], nested: bool = True) -> bool:
    """Remove our hook entries from a {event: [entry, ...]} dict, keeping foreign ones.

    nested: entries hold a "hooks" list of commands (Claude Code, Codex); else an entry is the command.
    """
    changed = False
    for event in list(hooks):
        entries = hooks[event]
        if not isinstance(entries, list):
            continue
        kept, was = [], changed
        changed = False
        for e in entries:
            if nested and isinstance(e, dict) and isinstance(e.get("hooks"), list):
                rest = [h for h in e["hooks"] if not (isinstance(h, dict) and is_ours(h))]
                if len(rest) != len(e["hooks"]):
                    changed = True
                    if rest:
                        kept.append({**e, "hooks": rest})
                    continue
            elif not nested and isinstance(e, dict) and is_ours(e):
                changed = True
                continue
            kept.append(e)
        if changed:
            if kept:
                hooks[event] = kept
            else:
                del hooks[event]
        changed = changed or was
    return changed
