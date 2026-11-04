"""What every tool adapter provides, and the shared JSON config merge.

Session keys, one per tool, identical in shim, hook and bridge:
  claude:<pid>             project = basename of the session file's cwd
  opencode:<session id>
  codex:<thread id>
  cursor:<workspace path>  workspace = the --workspace argument, else the shim's cwd
"""
import json
import os
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


def merge_json(path: Path, update: Callable[[dict], bool]) -> str:
    """Apply update to a JSON file; "changed", "unchanged", or "broken" (left alone).

    A missing file counts as {}. Writes go to the symlink's target, keeping its mode.
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
    target = path.resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".kaptyorka-tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    if target.exists():
        os.chmod(tmp, target.stat().st_mode & 0o7777)
    os.replace(tmp, target)
    return "changed"
