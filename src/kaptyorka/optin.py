"""Sessions that joined the team from a folder that is no team project: `/hub team [project]`
in Claude Code, or the join_team tool. Decided on this machine, one session at a time.

`/hub team` makes the session a coordinator: on the hub as its person's session with no
project (paths.COORDINATOR). `/hub team <project>` gives it that project, as if it ran in the
project's folder. Either way nothing about its folder leaves the machine: no folder name, no
path, no git facts. The hub checks the person's role first (POST /bridge/join-team).

State: ~/.kaptyorka/team/<quoted session key>: {"project", "pid", "start"}. It ends with the
session's process: a file whose process has ended (or whose pid was reused) is removed.

On a team machine (the hub says its account is shared) every session is on the hub without
asking: one in a folder that is no team project counts as a coordinator, as if it had typed
`/hub team` (auto). Nothing is saved for it, so each new session starts on again; /hub off
still takes one session off (offhub).
"""
import json
import os
import sys
from dataclasses import replace
from pathlib import Path
from typing import List, Optional, Tuple
from urllib.parse import quote, unquote

from kaptyorka import paths

DIR = "team"
LABEL = "coordinator"  # how a coordinator shows on Home when it has no name of its own
JOIN_S = 4  # the hub's role check; below the hooks' limit for `hub team` (hook.HUB_TEAM_S)


def _dir() -> Path:
    d = paths.home() / DIR
    d.mkdir(mode=0o700, exist_ok=True)
    return d


def _file(key: str) -> Path:
    return _dir() / quote(key, safe="")


def proc_start(pid: int) -> Optional[str]:
    """The process's start time in clock ticks (Linux /proc, field 22), as Claude Code's
    session file has it; None where it cannot be read."""
    if not sys.platform.startswith("linux"):
        return None
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        return stat[stat.rindex(")") + 2:].split()[19]
    except (OSError, ValueError, IndexError):
        return None


def _alive(pid, start) -> bool:
    try:
        if int(pid) <= 0:
            return False
        os.kill(int(pid), 0)
    except PermissionError:
        pass
    except (OSError, ValueError, TypeError):
        return False
    return start is None or proc_start(int(pid)) in (None, str(start))


def save(key: str, project: str, pid: int) -> None:
    body = {"project": project, "pid": pid, "start": proc_start(pid)}
    f = _file(key)
    tmp = f.with_name("." + f.name + ".tmp")
    tmp.write_text(json.dumps(body))
    tmp.replace(f)


def clear(key: str) -> bool:
    """Leave the team; True when the session had joined."""
    try:
        _file(key).unlink()
        return True
    except OSError:
        return False


def get(key: str) -> Optional[str]:
    """The project this session joined: a project name, paths.COORDINATOR for `/hub team`
    alone, or None when it did not join (or its process ended)."""
    if not key:
        return None
    f = _file(key)
    try:
        st = json.loads(f.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(st, dict) or not isinstance(st.get("project"), str):
        return None
    if not _alive(st.get("pid"), st.get("start")):
        clear(key)
        return None
    return st["project"] or paths.COORDINATOR


def joined() -> List[Tuple[str, str]]:
    """(key, project) of every live joined session on this machine."""
    out = []
    for f in _dir().iterdir():
        if f.name.startswith("."):
            continue
        key = unquote(f.name)
        project = get(key)
        if project is not None:
            out.append((key, project))
    return out


TEAM_MACHINE = "; this is a team machine, so every session here is on the hub"


def auto(key: str, project: str, cwd: str) -> bool:
    """A team machine's session in a folder that is no team project: on the hub as a
    coordinator by itself. Never the manager's folder or the hub's workers (on the hub as they
    are), and not before the bridge saved the hub's team list."""
    from kaptyorka import offhub

    if project == paths.COORDINATOR or paths.in_manager_folder(cwd) or not offhub.shared_machine():
        return False
    team, keep = offhub.load_team()
    return team is not None and key not in keep and paths.team_name(project) not in team


def effective(key: str, project: str, cwd: str) -> Optional[str]:
    """What get() says, or paths.COORDINATOR when auto() puts the session on the hub."""
    joined = get(key)
    if joined is None and auto(key, project, cwd):
        return paths.COORDINATOR
    return joined


def apply(ident):
    """The identity as the hub sees it: a joined session carries its chosen project, a team
    machine's session in a personal folder is a coordinator."""
    if ident is None:
        return None
    project = effective(ident.key, ident.project, ident.cwd)
    return ident if project is None else replace(ident, project=project)


def describe(project: Optional[str]) -> str:
    if project is None:
        return ""
    if project == paths.COORDINATOR:
        return "this session is on the hub as a coordinator (no project)"
    return f"this session is on the hub for project {project}"


def command(key: str) -> str:
    """How the person types /hub in the session's tool: Codex runs skills with $."""
    return "$hub" if key.startswith("codex:") else "/hub"


def join(cfg: dict, key: str, pid: int, project: str) -> Tuple[bool, str]:
    """Ask the hub, then save the choice. project "" joins as a coordinator, "off" leaves.
    (ok, the line to show)."""
    from kaptyorka import http, offhub

    project = (project or "").strip()
    if project.lower() == "off":
        was = clear(key)
        if was:
            tell_gone(cfg, key)
        if not was and offhub.shared_machine():
            return True, (f"this is a team machine: every session here is on the hub; {command(key)}"
                          " off takes this session off")
        return True, ("this session left the team: it is back to this folder's own rules"
                      if was else "this session had not joined the team")
    if not cfg.get("hub") or not cfg.get("token"):
        return False, "kaptyorka: this machine has not joined a hub"
    if pid <= 0:
        return False, "kaptyorka: could not find this session's process; try again"
    try:
        status, answer = http.request("POST", cfg["hub"] + "/bridge/join-team", cfg["token"],
                                      {"project": project}, timeout=JOIN_S)
    except http.HubUnreachable:
        return False, f"kaptyorka hub unreachable at {cfg['hub']}"
    if status != 200 or not isinstance(answer, dict):
        why = answer.strip() if isinstance(answer, str) and answer.strip() else f"hub answered {status}"
        return False, f"kaptyorka: {why}"
    save(key, project, pid)
    return True, (describe(project or paths.COORDINATOR)
                  + ("; name a project for notes and work" if not project else "")
                  + f". {command(key)} team off (or join_team(\"off\")) leaves it")


def tell_gone(cfg: dict, key: str) -> None:
    """Ask the hub to drop the session at once; it would go offline on its own anyway."""
    from kaptyorka import http

    if not cfg.get("hub") or not cfg.get("token") or not cfg.get("machine"):
        return
    try:
        http.request("POST", cfg["hub"] + "/bridge/heartbeat", cfg["token"],
                     {"machine": cfg["machine"], "sessions": [], "tools": [], "gone": [key]},
                     timeout=3)
    except Exception:
        pass
