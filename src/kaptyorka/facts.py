"""What the bridge tells the hub about this computer: load, tools, git state, project folders."""
import getpass
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from kaptyorka import doctor, paths
from kaptyorka.adapters.base import write_atomic

GIT_TTL_S = 30
GIT_BUDGET_S = 2
VERSION_TTL_S = 600
COMMANDS = {"claude-code": "claude", "opencode": "opencode", "codex": "codex", "cursor": "cursor-agent"}
_cache: Dict[Tuple[str, str], Tuple[float, object]] = {}


def _cached(key: Tuple[str, str], ttl: float, make: Callable[[], object]):
    now = time.monotonic()
    hit = _cache.get(key)
    if hit and now - hit[0] < ttl:
        return hit[1]
    value = make()
    _cache[key] = (now, value)
    return value


def git(cwd: str, run=subprocess.run, cached: bool = True,
        budget: float = GIT_BUDGET_S) -> Optional[dict]:
    """Branch, dirty flag and last subject of the repo at cwd; None when it is not one.
    cached=False reads it now (the stop hook), within `budget` seconds."""
    def make() -> Optional[dict]:
        deadline = time.monotonic() + budget  # one budget for all calls on this folder

        def out(*args: str) -> Optional[str]:
            left = max(deadline - time.monotonic(), 0.01)
            r = run(["git", "-C", cwd, *args], capture_output=True, text=True, timeout=left)
            return r.stdout if r.returncode == 0 else None

        try:
            branch = out("rev-parse", "--abbrev-ref", "HEAD")
            if branch is None:
                return None
            status, subject = out("status", "--porcelain"), out("log", "-1", "--format=%s")
        except (subprocess.TimeoutExpired, OSError):
            return None
        return {"branch": branch.strip(),
                "dirty": None if status is None else bool(status.strip()),
                "subject": (subject or "").strip(),
                "read_at": time.time()}  # kept across cached resends; the heartbeat sends it as an age

    if not cached:
        return make()
    return _cached(("git", cwd), GIT_TTL_S, make)  # type: ignore[return-value]


def _tool_version(tool: str, run) -> str:
    def make() -> str:
        exe = shutil.which(COMMANDS.get(tool, tool))
        if not exe:
            return ""
        try:
            r = run([exe, "--version"], capture_output=True, text=True, timeout=5)
        except (subprocess.TimeoutExpired, OSError):
            return ""
        lines = (r.stdout or "").strip().splitlines() if r.returncode == 0 else []
        return lines[0].strip() if lines else ""

    return _cached(("version", tool), VERSION_TTL_S, make)  # type: ignore[return-value]


def _logins(installed: list, run) -> Dict[str, Optional[bool]]:
    """Per installed tool: logged in, not, or None when unknown; at most every 10 minutes."""
    def make() -> Dict[str, Optional[bool]]:
        out: Dict[str, Optional[bool]] = {}
        for a in installed:
            check = getattr(a, "logged_in", None)  # a test double may not have one
            out[a.tool] = check(run) if check else None
        return out

    return _cached(("logins", ""), VERSION_TTL_S, make)  # type: ignore[return-value]


def _rules_in_place(adapter, home: Path) -> bool:
    """The one hook (or plugin) that carries the start brief is installed."""
    tool = adapter.tool
    if tool == "claude-code":
        return doctor._has_hooks(doctor._json(home / ".claude" / "settings.json").get("hooks"),
                                 tool, (("SessionStart", "start"),), True)
    if tool == "codex":
        from kaptyorka.adapters.codex import codex_home

        return doctor._has_hooks(doctor._json(codex_home(home) / "hooks.json").get("hooks"),
                                 tool, (("SessionStart", "start"),), True)
    if tool == "cursor":
        return doctor._has_hooks(doctor._json(home / ".cursor" / "hooks.json").get("hooks"),
                                 tool, (("stop", "stop"),), False)
    if tool == "opencode":
        return (home / ".config" / "opencode" / "plugins" / "kaptyorka.js").exists()
    return False


def harness(adapters: list, home: Path, skills_current: Optional[bool] = False) -> Dict[str, dict]:
    """Per installed tool: is the rules text, the hooks, and the team skills in place.
    Skills are None for a tool that has no skills folder (codex), or while skills_current is unknown."""
    out: Dict[str, dict] = {}
    for a in adapters:
        if not a.installed():
            continue
        h = getattr(a, "home", home)
        out[a.tool] = {"rules": bool(_rules_in_place(a, h)),
                       "hooks": bool(doctor.tool_configured(a, h)),
                       "skills": None if a.tool == "codex" or skills_current is None
                       else bool(skills_current)}
    return out


def machine(adapters: list, run=subprocess.run, platform: str = sys.platform,
            skills_current: Optional[bool] = False) -> dict:
    from kaptyorka import __version__

    try:
        load = float(os.getloadavg()[0])
    except OSError:
        load = 0.0
    installed = [a for a in adapters if a.installed()]
    hooks_ok = {a.tool: doctor.tool_configured(a, getattr(a, "home", Path.home())) for a in installed}
    return {"os_user": getpass.getuser(), "tmux": shutil.which("tmux") is not None,
            "load": load, "cpus": os.cpu_count() or 1,
            "tools": {a.tool: _tool_version(a.tool, run) for a in installed},
            "hooks_ok": hooks_ok, "version": __version__,
            "logins": _logins(installed, run),
            "harness": harness(adapters, Path.home(), skills_current)}


def _learned() -> Dict[str, str]:
    try:
        data = json.loads((paths.home() / "folders.json").read_text())
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, str)} \
        if isinstance(data, dict) else {}


def folders(projects: List[str], sessions: list, home: Path) -> dict:
    """{project: folder}: every folder learned from session cwds (and remembered), registered
    or not, plus home/project for the hub's projects not learned. A remembered folder that is
    gone is dropped."""
    learned = _learned()
    seen = dict(learned)
    for s in sessions:
        cwd = getattr(s, "cwd", "") or ""
        if cwd:
            seen[os.path.basename(cwd.rstrip("/"))] = cwd
    seen = {k: v for k, v in seen.items() if os.path.isdir(v)}
    if seen != learned:
        try:
            write_atomic(paths.home() / "folders.json", json.dumps(seen, indent=2))
        except OSError:
            pass  # remembering is a convenience; never stop the heartbeat
    out = dict(seen)
    for p in projects:
        if p not in out and (home / p).is_dir():
            out[p] = str(home / p)
    return out
