"""What every tool adapter provides, and the shared JSON config merge.

Session keys, one per tool, identical in shim, hook and bridge:
  claude:<pid>             project = basename of the session file's cwd
  opencode:<session id>
  codex:<thread id>
  cursor:<workspace path>  workspace = the --workspace argument, else the shim's cwd
"""
import json
import os
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional, Protocol


@dataclass
class Session:
    key: str
    tool: str
    project: str
    activity: str = ""


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
