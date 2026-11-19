"""Claude Code sessions kept off the hub. Decided on this machine: the bridge never reports an
off session, its hooks stay silent and its MCP shim refuses every tool, so the hub stores
nothing about it.

A session is off when its state file says so; with no state file, when it started with
KAPTYORKA_HUB=off (a normal session only: a background session's environment comes from the
Claude Code daemon, not from whoever typed `claude --bg`). The state file is
~/.kaptyorka/off/<session id>: {"off", "pid", "start", "gone"}. "gone" asks the bridge to tell
the hub to drop a session it may still show. A file whose process has ended is stale and is
removed, so a reused id is never hidden by mistake. A hub-started worker and the manager
session never go off.
"""
import json
import os
import sys
from pathlib import Path
from typing import List, Mapping, Optional, Tuple

from kaptyorka import paths

ENV = "KAPTYORKA_HUB"
TEAM = "team.json"  # the team projects the hub last named, saved by the bridge
COME_BACK = "this session is off the hub; type /hub on to come back"


def _dir() -> Path:
    d = paths.home() / "off"
    d.mkdir(mode=0o700, exist_ok=True)
    return d


def env_off(environ: Mapping) -> bool:
    return str(environ.get(ENV, "")).strip().lower() == "off"


def _proc_env(pid: int) -> dict:
    """The environment a process started with (Linux /proc); {} where it cannot be read."""
    if not sys.platform.startswith("linux"):
        return {}
    try:
        raw = Path(f"/proc/{pid}/environ").read_bytes()
    except OSError:
        return {}
    pairs = (p.decode(errors="replace").partition("=") for p in raw.split(b"\0") if p)
    return {k: v for k, _, v in pairs}


def _alive(pid, start) -> bool:
    from kaptyorka.adapters.claude import _same_start

    try:
        os.kill(int(pid), 0)
    except (OSError, ValueError, TypeError):
        return False
    return _same_start(int(pid), start) is not False


def _load(f: Path) -> Optional[dict]:
    try:
        st = json.loads(f.read_text())
    except (OSError, ValueError):
        return None
    return st if isinstance(st, dict) and isinstance(st.get("off"), bool) else None


def _state(session_id: str, pid: Optional[int] = None, start=None) -> Optional[dict]:
    """The live state for a session, or None. Claude Code gives a session a new id on /clear
    while the process stays: a file of the same process then moves to the new id."""
    f = _dir() / session_id
    st = _load(f) if f.exists() else None
    if st is None and pid is not None:
        for other in _dir().iterdir():
            o = _load(other)
            if o is not None and o.get("pid") == pid and o.get("start") == start:
                st = o
                try:
                    other.replace(f)
                except OSError:
                    pass
                break
    if st is None:
        return None
    if not _alive(st.get("pid"), st.get("start")):
        try:
            f.unlink()
        except OSError:
            pass
        return None
    return st


def exempt(data: dict) -> str:
    """Why this session must stay on the hub ("" when it may go off): the manager, or a worker
    the hub started (a background session named after a worker this bridge launched)."""
    from kaptyorka.launch import NAME

    if paths.in_manager_folder(data.get("cwd")):
        return "the manager session stays on the hub"
    name = data.get("name")
    if data.get("kind") == "bg" and isinstance(name, str) and NAME.match(name) \
            and (paths.home() / "workers" / f"{name}.log").exists():
        return "a worker the hub started stays on the hub"
    return ""


def decide(session_id: str, pid: int, data: dict, environ: Optional[Mapping] = None) -> bool:
    """Whether the session is off. environ None: the one the process started with."""
    if exempt(data):
        return False
    st = _state(session_id, pid, data.get("procStart"))
    if st is not None:
        return st["off"]
    if data.get("kind") == "bg":
        return False
    return env_off(_proc_env(pid) if environ is None else environ)


def session(session_id: str, home: Optional[Path] = None) -> Optional[Tuple[int, dict]]:
    """(pid, session file data) of the live Claude Code session with this id."""
    for f in ((home or Path.home()) / ".claude" / "sessions").glob("*.json"):
        try:
            data = json.loads(f.read_text())
            if data["sessionId"] == session_id:
                pid = int(f.stem)
                os.kill(pid, 0)
                return pid, data
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return None


def off(key: str, environ: Optional[Mapping] = None) -> bool:
    """Whether the session with this key is off; for the shim and hooks (environ: their own)."""
    if not key.startswith("claude:"):
        return False
    sid = key.split(":", 1)[1]
    found = session(sid)
    if found is None:
        return False
    return decide(sid, found[0], found[1], os.environ if environ is None else environ)


def switch(session_id: str, pid: int, data: dict, want_off: bool) -> None:
    """Save the person's choice for this session."""
    was = decide(session_id, pid, data, None if sys.platform.startswith("linux") else os.environ)
    st = _state(session_id, pid, data.get("procStart")) or {}
    tell = (bool(st.get("gone")) or not was) if want_off else False
    body = {"off": want_off, "pid": pid, "start": data.get("procStart"), "gone": tell}
    tmp = _dir() / f".{session_id}.tmp"
    tmp.write_text(json.dumps(body))
    tmp.replace(_dir() / session_id)


def started(session_id: str, pid: int, data: dict, environ: Mapping) -> None:
    """The start hook: remember KAPTYORKA_HUB=off for the bridge, which cannot read another
    process's environment outside Linux. Never overrides a choice already made."""
    if env_off(environ) and data.get("kind") != "bg" and not exempt(data) \
            and _state(session_id, pid, data.get("procStart")) is None:
        body = {"off": True, "pid": pid, "start": data.get("procStart"), "gone": False}
        (_dir() / session_id).write_text(json.dumps(body))


def gone() -> List[str]:
    """Keys of off sessions the hub may still show, for the bridge's heartbeat."""
    out = []
    for f in _dir().iterdir():
        st = _load(f)
        if st and st["off"] and st.get("gone") and not f.name.startswith("."):
            out.append(f"claude:{f.name}")
    return out


def told(keys: List[str]) -> None:
    """The hub dropped these: stop asking."""
    for key in keys:
        f = _dir() / key.split(":", 1)[1]
        st = _load(f)
        if st and st.get("gone"):
            st["gone"] = False
            f.write_text(json.dumps(st))


def clean() -> None:
    """Remove the files of sessions that ended."""
    for f in _dir().iterdir():
        st = _load(f)
        if st is None or not _alive(st.get("pid"), st.get("start")):
            try:
                f.unlink()
            except OSError:
                pass


def save_team(team: Optional[set], keep: set, mine: Optional[list] = None,
              repos: Optional[dict] = None) -> None:
    """The bridge saves the hub's list of team projects (None: the hub sent none), the keys
    of the workers it started, the team projects its person has a role in (for the help a
    refused call gets) and the registered repos' fingerprints ({fingerprint: project}, see
    repos.py), for the shims and hooks of this machine."""
    body = {"team": None if team is None else sorted(team), "keep": sorted(keep),
            "mine": None if mine is None else sorted(mine), "repos": repos or {}}
    try:
        tmp = paths.home() / f".{TEAM}.tmp"
        tmp.write_text(json.dumps(body))
        tmp.replace(paths.home() / TEAM)
    except OSError:
        pass  # the hub refuses a personal folder's calls anyway


def load_team() -> Tuple[Optional[set], set]:
    """The list the bridge saved last: (team project names, or None for no list; worker keys).
    No file: None, as before the hub's first answer."""
    try:
        st = json.loads((paths.home() / TEAM).read_text())
    except (OSError, ValueError):
        return None, set()
    if not isinstance(st, dict):
        return None, set()
    team, keep = st.get("team"), st.get("keep")
    return ({t for t in team if isinstance(t, str)} if isinstance(team, list) else None,
            {k for k in keep if isinstance(k, str)} if isinstance(keep, list) else set())


def load_repos() -> dict:
    """{fingerprint: project} of the registered repos, as the bridge saved them; {} when none."""
    try:
        repos = json.loads((paths.home() / TEAM).read_text()).get("repos")
    except (OSError, ValueError, AttributeError):
        return {}
    return {k: v for k, v in repos.items() if isinstance(k, str) and isinstance(v, str)} \
        if isinstance(repos, dict) else {}


def load_mine() -> Optional[List[str]]:
    """The team projects this machine's person has a role in, as the bridge saved them; None
    when unknown."""
    try:
        mine = json.loads((paths.home() / TEAM).read_text()).get("mine")
    except (OSError, ValueError, AttributeError):
        return None
    return [m for m in mine if isinstance(m, str)] if isinstance(mine, list) else None


def personal(project: str, cwd: str = "", key: str = "") -> str:
    """Why this session's shim and hooks send nothing ("" when they may): outside the team
    projects (automatically off the hub), or the hub named no team projects yet (an older
    hub). Decided from the bridge's last list; with no file saved yet the hub decides (it
    refuses such a session too). The manager and the hub's workers are never personal."""
    if paths.in_manager_folder(cwd):
        return ""
    if project == paths.COORDINATOR:  # only a session that joined with /hub team says so
        from kaptyorka import optin

        return "" if optin.get(key) == paths.COORDINATOR else paths.NOT_TEAM
    if not (paths.home() / TEAM).exists():
        return ""
    team, keep = load_team()
    if key in keep:
        return ""
    if team is None:
        return paths.NO_TEAM_LIST
    return "" if paths.team_name(project) in team else paths.NOT_TEAM


def _tell_now(data: dict) -> None:
    """Ask the hub to drop what it shows of the sessions just switched off, without waiting
    for the bridge's next heartbeat (which asks again until the hub answered)."""
    from kaptyorka import http

    keys = gone()
    cfg = paths.load_config(data.get("cwd"))
    if not keys or not cfg.get("hub") or not cfg.get("token") or not cfg.get("machine"):
        return
    try:
        http.request("POST", cfg["hub"] + "/bridge/heartbeat", cfg["token"],
                     {"machine": cfg["machine"], "sessions": [], "tools": [], "gone": keys},
                     timeout=3)
    except Exception:
        pass  # the bridge asks again on its next heartbeat


def command(words: List[str], environ: Mapping = os.environ) -> Tuple[int, str]:
    """`kaptyorka hub [on|off]`, run by Claude Code's /hub inside the session: (exit code, line)."""
    want = " ".join(words).strip().lower()
    team = bool(words) and words[0].strip().lower() == "team"
    if want not in ("", "on", "off") and not (team and len(words) <= 2):
        return 1, "use /hub, /hub on, /hub off, /hub team, /hub team <project> or /hub team off"
    sid = str(environ.get("CLAUDE_CODE_SESSION_ID") or "")
    found = session(sid) if sid else None
    if found is None:
        return 1, "kaptyorka: /hub works only inside a Claude Code session"
    pid, data = found
    bg = data.get("kind") == "bg"
    if team:
        from kaptyorka import optin

        if paths.in_manager_folder(data.get("cwd")):
            return 1, "kaptyorka: the manager session is on the hub already"
        if decide(sid, pid, data, environ):
            return 1, f"kaptyorka: {COME_BACK}"
        ok, line = optin.join(paths.load_config(data.get("cwd")), f"claude:{sid}", pid,
                              words[1].strip() if len(words) > 1 else "")
        return (0 if ok else 1), line
    if want == "":
        if decide(sid, pid, data, environ):
            return 0, COME_BACK
        note = (f" (a background session ignores {ENV}=off; type /hub off to hide it)"
                if bg and env_off(environ) else "")
        from kaptyorka import optin

        joined = optin.describe(optin.get(f"claude:{sid}"))
        return 0, (joined + note if joined else f"this session is on the hub{note}")
    why = exempt(data)
    if want == "off" and why:
        return 1, f"kaptyorka: {why}"
    switch(sid, pid, data, want == "off")
    if want == "off":
        _tell_now(data)
        return 0, ("this session is now off the hub: nobody on the hub sees it or can message it."
                   " /hub on brings it back")
    return 0, "this session is back on the hub; it shows there within 10 seconds"
