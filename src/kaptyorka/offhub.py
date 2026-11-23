"""Sessions kept off the hub. Decided on this machine: the bridge never reports an off session,
its hooks stay silent and its MCP shim refuses every tool, so the hub stores nothing about it.

Every tool but Claude Code (OpenCode, Codex, Cursor) keeps the choice in
~/.kaptyorka/off/<the percent-encoded session key>: {"off", "gone", "at"}, written by /hub off
and removed by /hub on. Their ids are never reused, so the file needs no process check; one a
finished session left behind is removed after KEEP_S. A Cursor key names a folder, so there
/hub switches the folder.

Claude Code:

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
import time
from pathlib import Path
from typing import List, Mapping, Optional, Tuple
from urllib.parse import quote, unquote

from kaptyorka import paths

ENV = "KAPTYORKA_HUB"
TEAM = "team.json"  # the team projects the hub last named, saved by the bridge
COME_BACK = "this session is off the hub; type /hub on to come back"
KEEP_S = 90 * 86400  # another tool's choice outlives a finished session by this much
SESSION_KEY = "KAPTYORKA_SESSION_KEY"  # set by kaptyorka's Codex and Cursor hooks for /hub
OPENCODE_SESSION = "KAPTYORKA_OPENCODE_SESSION"  # set by kaptyorka's OpenCode plugin


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
        return kept_off(key)
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


def _keyed(name: str) -> bool:
    """The file of another tool's session, named by its whole key (a Claude Code id has no %)."""
    return "%3A" in name


def _file(key: str) -> Path:
    return _dir() / (key.split(":", 1)[1] if key.startswith("claude:") else quote(key, safe=""))


def rename_machine(old: str, new: str) -> None:
    """The machine got a new name: Cursor's keys contain it, so each folder keeps its /hub off
    (and a /hub team join) under the new name; nothing comes back on by the rename."""
    from kaptyorka import optin

    was, now = quote(f"cursor:{old}:", safe=""), quote(f"cursor:{new}:", safe="")
    for d in (_dir(), optin._dir()):
        for f in d.iterdir():
            if f.name.startswith(was):
                f.replace(d / (now + f.name[len(was):]))


def kept_off(key: str) -> bool:
    """Another tool's session (OpenCode, Codex, Cursor) switched off with /hub off."""
    if key.startswith("claude:"):
        return False
    st = _load(_file(key))
    return bool(st and st["off"])


def switch_key(key: str, want_off: bool) -> None:
    """Save the choice for another tool's session; /hub on removes the file."""
    f = _file(key)
    if not want_off:
        try:
            f.unlink()
        except OSError:
            pass
        return
    st = _load(f) or {}
    body = {"off": True, "gone": bool(st.get("gone")) or not st.get("off"), "at": time.time()}
    tmp = _dir() / f".{f.name}.tmp"
    tmp.write_text(json.dumps(body))
    tmp.replace(f)


def gone() -> List[str]:
    """Keys of off sessions the hub may still show, for the bridge's heartbeat."""
    out = []
    for f in _dir().iterdir():
        st = _load(f)
        if st and st["off"] and st.get("gone") and not f.name.startswith("."):
            out.append(unquote(f.name) if _keyed(f.name) else f"claude:{f.name}")
    return out


def told(keys: List[str]) -> None:
    """The hub dropped these: stop asking."""
    for key in keys:
        f = _file(key)
        st = _load(f)
        if st and st.get("gone"):
            st["gone"] = False
            f.write_text(json.dumps(st))


def _ended(f: Path, st: Optional[dict]) -> bool:
    if st is None:
        return True
    if _keyed(f.name):
        at = st.get("at")
        return not isinstance(at, (int, float)) or time.time() - at >= KEEP_S
    return not _alive(st.get("pid"), st.get("start"))


def clean() -> None:
    """Remove the files of sessions that ended."""
    for f in _dir().iterdir():
        st = _load(f)
        if _ended(f, st):
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


def session_key(environ: Mapping) -> str:
    """The key of the session that runs /hub: Claude Code names it, kaptyorka's OpenCode plugin
    and its Codex and Cursor hooks pass it on; "" outside a session."""
    if environ.get("CLAUDE_CODE_SESSION_ID"):
        return f"claude:{environ['CLAUDE_CODE_SESSION_ID']}"
    if environ.get(OPENCODE_SESSION):
        return f"opencode:{environ[OPENCODE_SESSION]}"
    key = str(environ.get(SESSION_KEY) or "")
    return key if key.split(":", 1)[0] in ("opencode", "codex", "cursor") and ":" in key else ""


def come_back(key: str) -> str:
    """COME_BACK as that tool types the command ($hub in Codex)."""
    return COME_BACK.replace("/hub", _where(key))


def _where(key: str) -> str:
    """How the person types the command in that tool: Codex runs skills with $."""
    from kaptyorka import optin

    return optin.command(key)


def command(words: List[str], environ: Mapping = os.environ, cwd: str = "") -> Tuple[int, str]:
    """`kaptyorka hub [on|off|team [project|off]]`, run by /hub inside a session of any tool:
    (exit code, line)."""
    want = " ".join(words).strip().lower()
    team = bool(words) and words[0].strip().lower() == "team"
    key = session_key(environ)
    hub = _where(key)
    if words and words[0].strip().lower() == "overnight":
        return _overnight(key, [w.lower() for w in words[1:]], cwd or os.getcwd())
    if want not in ("", "on", "off") and not (team and len(words) <= 2):
        return 1, (f"use {hub}, {hub} on, {hub} off, {hub} team, {hub} team <project>,"
                   f" {hub} team off, {hub} overnight or {hub} overnight off")
    if not key:
        return 1, "kaptyorka: /hub works only inside a session of an agent tool"
    project = words[1].strip() if team and len(words) > 1 else ""
    if not key.startswith("claude:"):
        return _other(key, "team" if team else want, cwd or os.getcwd(), hub, project)
    sid = key.split(":", 1)[1]
    found = session(sid)
    if found is None:
        return 1, "kaptyorka: /hub works only inside a Claude Code session"
    pid, data = found
    bg = data.get("kind") == "bg"
    if team:
        if paths.in_manager_folder(data.get("cwd")):
            return 1, "kaptyorka: the manager session is on the hub already"
        if decide(sid, pid, data, environ):
            return 1, f"kaptyorka: {COME_BACK}"
        return _join(paths.load_config(data.get("cwd")), key, pid, project)
    if want == "":
        if decide(sid, pid, data, environ):
            return 0, COME_BACK
        note = (f" (a background session ignores {ENV}=off; type /hub off to hide it)"
                if bg and env_off(environ) else "")
        return 0, _team_note(key, str(data.get("cwd") or ""), f"this session is on the hub{note}",
                             note)
    why = exempt(data)
    if want == "off" and why:
        return 1, f"kaptyorka: {why}"
    switch(sid, pid, data, want == "off")
    if want == "off":
        _tell_now(data)
        return 0, ("this session is now off the hub: nobody on the hub sees it or can message it."
                   " /hub on brings it back")
    return 0, _team_note(key, str(data.get("cwd") or ""),
                         "this session is back on the hub; it shows there within 10 seconds")


def _join(cfg: dict, key: str, pid: int, project: str) -> Tuple[int, str]:
    from kaptyorka import optin

    ok, line = optin.join(cfg, key, pid, project)
    return (0 if ok else 1), line


def _team_note(key: str, cwd: str, line: str, note: str = "") -> str:
    """An "on" line, unless the folder keeps the session off the hub anyway. A session that
    joined the team (/hub team) says how it joined."""
    from kaptyorka import optin
    from kaptyorka.identity import project_of

    joined = optin.describe(optin.get(key))
    if joined:
        return joined + note
    project = project_of(cwd) if cwd else ""
    why = personal(project, cwd, key) if project else ""
    return f"this session stays off the hub: {why}" if why else line


def _tool_pid(key: str, environ: Mapping = os.environ) -> int:
    """The process of the tool whose session runs /hub (OpenCode, Codex, Cursor): the nearest
    ancestor of this process that is that tool; 0 when none is found. A join ends with it."""
    from kaptyorka import identity

    tool = key.split(":", 1)[0]
    try:
        for pid, comm in identity.ancestors(os.getpid()):
            if identity._tool(pid, comm) == tool:
                return pid
    except (OSError, ValueError):
        pass
    if tool == "opencode":  # its plugin runs /hub straight from the OpenCode process
        pid = str(environ.get("OPENCODE_PID") or "")
        return int(pid) if pid.isdigit() else os.getppid()
    return 0


def _other(key: str, want: str, cwd: str, hub: str, project: str = "") -> Tuple[int, str]:
    """/hub in OpenCode, Codex or Cursor. Cursor has one key per folder."""
    what = "this folder" if key.startswith("cursor:") else "this session"
    if want == "team":
        if paths.in_manager_folder(cwd):
            return 1, "kaptyorka: the manager session is on the hub already"
        if kept_off(key):
            return 1, f"kaptyorka: {what} is off the hub; type {hub} on to come back"
        pid = _tool_pid(key) if project.lower() != "off" else 0
        return _join(paths.load_config(cwd), key, pid, project)
    if want == "":
        if kept_off(key):
            return 0, f"{what} is off the hub; type {hub} on to come back"
        return 0, _team_note(key, cwd, f"{what} is on the hub")
    if want == "off" and paths.in_manager_folder(cwd):
        return 1, "kaptyorka: the manager session stays on the hub"
    switch_key(key, want == "off")
    if want == "off":
        _tell_now({"cwd": cwd})
        return 0, (f"{what} is now off the hub: nobody on the hub sees it or can message it."
                   f" {hub} on brings it back")
    return 0, _team_note(key, cwd, f"{what} is back on the hub; it shows there within 10 seconds")


def _overnight(key: str, words: List[str], cwd: str) -> Tuple[int, str]:
    """`/hub overnight [on|off]`: Claude Code only. Usually the prompt hook has turned it on
    already (it adds the night rules for Claude); this line confirms it, or turns it on when the
    hook did not run."""
    from kaptyorka import overnight

    if words not in ([], ["on"], ["off"]):
        return 1, "use /hub overnight or /hub overnight off"
    if not key:
        return 1, "kaptyorka: /hub works only inside a session of an agent tool"
    if not key.startswith("claude:"):
        return 1, (f"kaptyorka: overnight mode is not there yet for {key.split(':', 1)[0]};"
                   " it works in Claude Code only")
    sid = key.split(":", 1)[1]
    if words == ["off"]:
        if overnight.state(sid) is None:
            return 0, "kaptyorka: overnight mode is off"
        text = overnight.finish(sid, "turned off with /hub overnight off")
        return 0, ("kaptyorka: overnight mode is off. Morning report: "
                   + str(overnight.save_report(sid, text)))
    if paths.in_manager_folder(cwd):
        return 1, "kaptyorka: the manager session has no overnight mode"
    if overnight.state(sid) is None:
        from kaptyorka.identity import Identity, project_of
        from kaptyorka import optin

        cfg = paths.load_config()
        ident = optin.apply(Identity(key, cfg.get("machine", ""), "claude-code",
                                     project_of(cwd), cwd))
        team = bool(cfg.get("hub") and cfg.get("token")) and not personal(
            ident.project, cwd, key) and not off(key)
        overnight.turn_on(sid, ident.project, cwd, team, key, cfg.get("machine", ""), "", "")
    return 0, "kaptyorka: overnight mode is on for this task. To stop it: /hub overnight off"
