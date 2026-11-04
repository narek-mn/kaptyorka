"""Which agent session started this process, found from its parent processes.

Session keys, one per tool, identical in shim, hook and bridge:
  claude:<pid>             project = basename of the session file's cwd
  opencode:<session id>
  codex:<thread id>
  cursor:<workspace path>  workspace = the --workspace argument, else the shim's cwd
"""
import json
import os
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple
from urllib.parse import quote


@dataclass
class Identity:
    key: str
    machine: str
    tool: str
    project: str

    def headers(self) -> dict:
        """Percent-encoded, since header values must be ASCII; the hub unquotes them."""
        return {f"X-Kaptyorka-{k.capitalize()}": quote(v, safe="")
                for k, v in asdict(self).items()}


def _ps(pid: int) -> Optional[Tuple[int, str]]:
    r = subprocess.run(["ps", "-o", "ppid=,comm=", "-p", str(pid)],
                       capture_output=True, text=True)
    parts = r.stdout.strip().split(None, 1)
    if r.returncode or len(parts) != 2:
        return None
    return int(parts[0]), parts[1]


def ancestors(pid: int) -> List[Tuple[int, str]]:
    """Parent processes as (pid, command name), nearest first."""
    out = []
    me = _ps(pid)
    parent = me[0] if me else 0
    while parent > 0:
        row = _ps(parent)
        if not row:
            break
        out.append((parent, row[1]))
        parent = row[0]
    return out


def proc_cwd(pid: int) -> str:
    if sys.platform.startswith("linux"):
        return os.readlink(f"/proc/{pid}/cwd")
    r = subprocess.run(["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"],
                       capture_output=True, text=True)
    return next(line[1:] for line in r.stdout.splitlines() if line.startswith("n"))


def _session_file(pid: int) -> Path:
    return Path.home() / ".claude" / "sessions" / f"{pid}.json"


def _opencode_key(pid: int, cwd: str) -> Optional[str]:
    from kaptyorka.adapters.opencode import session_key

    return session_key(pid, cwd)


def _codex_key(pid: int, cwd: str) -> Optional[str]:
    from kaptyorka.adapters.codex import session_key

    return session_key(pid, cwd)


RESOLVERS: Dict[str, Callable[[int, str], Optional[str]]] = {
    "claude-code": lambda pid, cwd: f"claude:{pid}",
    "opencode": _opencode_key,
    "codex": _codex_key,
    "cursor": lambda pid, cwd: f"cursor:{cwd}",
}


def _tool(pid: int, comm: str) -> Optional[str]:
    if _session_file(pid).exists():
        return "claude-code"
    name = os.path.basename(comm)
    if name in ("opencode", "codex"):
        return name
    if "cursor" in name.lower():
        return "cursor"
    return None


def identify(machine: str, pid: Optional[int] = None,
             workspace: Optional[str] = None) -> Optional[Identity]:
    """The session this process belongs to, or None (not in a tool, or not detectable yet)."""
    test = os.environ.get("KAPTYORKA_TEST_IDENTITY")
    if test:
        return Identity(**json.loads(test))
    try:
        return _identify(machine, os.getpid() if pid is None else pid, workspace)
    except (OSError, ValueError, KeyError, StopIteration):
        return None  # e.g. a half-written session file or a process that just exited


def _identify(machine: str, pid: int, workspace: Optional[str]) -> Optional[Identity]:
    sid = os.environ.get("KAPTYORKA_OPENCODE_SESSION")  # set by the OpenCode plugin
    if sid:
        project = os.path.basename(os.getcwd().rstrip("/"))
        return Identity(f"opencode:{sid}", machine, "opencode", project) if project else None
    for tool_pid, comm in ancestors(pid):
        tool = _tool(tool_pid, comm)
        if tool:
            break
        if os.path.basename(comm) == "claude":
            return None  # Claude Code before it wrote its session file; the caller retries
    else:
        if not os.environ.get("OPENCODE_PID"):
            return None
        tool_pid, tool = int(os.environ["OPENCODE_PID"]), "opencode"  # set for OpenCode's MCP servers
    if tool == "claude-code":
        cwd = json.loads(_session_file(tool_pid).read_text())["cwd"]
    elif tool == "cursor":
        cwd = workspace or os.getcwd()
    elif tool == "codex":
        cwd = os.getcwd()  # under the daemon the parent is the daemon, not the TUI in the project
    else:
        cwd = proc_cwd(tool_pid)
    resolve = RESOLVERS.get(tool)
    key = resolve(tool_pid, cwd) if resolve else None
    project = os.path.basename(cwd.rstrip("/"))
    if not key or not project:
        return None
    return Identity(key, machine, tool, project)
