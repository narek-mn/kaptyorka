"""Stuck sessions: a background task that prints nothing, or a busy session that writes nothing.
docs/superpowers/specs/2026-11-24-stuck-sessions.md

Two stages per episode. After WAKE_S the session itself is asked to check (a message reaches a
session whose turn is over at once; a busy one reads it after its current tool call). After
TELL_S its person, its boss (for a worker) and the manager hear it; the manager once more
AGAIN_S later. An episode ends when the task prints, ends, the session writes again, or goes
idle. The bridge measures (Claude Code on Linux only); the hub judges. Nothing is judged when a
measure is missing."""
import re
import shlex
from typing import List, Optional

from kaptyorka.secrets_check import PATTERNS

WAKE_S = 1800
TELL_S = 3600
AGAIN_S = 1800
COMMAND_CAP = 100


def safe_command(text: str) -> str:
    """One line, at most COMMAND_CAP characters; "" when it looks like a secret."""
    line = " ".join(str(text).split())
    line = line[:COMMAND_CAP - 3] + "..." if len(line) > COMMAND_CAP else line
    return "" if any(p.search(line) for _, p in PATTERNS) else line


def command_of(argv: List[str]) -> str:
    """The command a Claude Code background shell runs: its wrapper is
    `bash -c "source <snapshot> ... && eval '<command>' < /dev/null && pwd -P >| ..."`."""
    script = argv[-1] if argv else ""
    m = re.search(r"&& eval (.+?) < /dev/null(?: &&|$)", script, re.S)
    if m:
        try:
            return shlex.split(m.group(1))[0]
        except (ValueError, IndexError):
            pass
    return script


def find(activity: str, write_age: Optional[float], tasks: Optional[list]) -> Optional[dict]:
    """{"kind": "task" | "busy", "age": seconds, "task": {...} for a task} once a session has
    been silent for WAKE_S; None otherwise. `tasks`: [{"id", "silent_s", "command"}] of its
    live background tasks (None: unknown). `write_age`: seconds since its last real write
    (None: unknown). An idle session is never stuck."""
    if activity in ("", "idle"):
        return None
    live = [t for t in tasks or [] if isinstance(t.get("silent_s"), (int, float))]
    if live:
        t = max(live, key=lambda t: t["silent_s"])
        if t["silent_s"] >= WAKE_S:
            return {"kind": "task", "age": float(t["silent_s"]), "task": t}
    if activity == "busy" and write_age is not None and write_age >= WAKE_S:
        return {"kind": "busy", "age": float(write_age)}
    return None


def of_agent(a, now: float) -> Optional[dict]:
    """find() for a hub agents row: `wrote` and each task's `out` are hub times."""
    if not a.online:
        return None
    tasks = None if a.tasks is None else [
        {"id": t["id"], "silent_s": now - t["out"], "command": t.get("command", "")}
        for t in a.tasks]
    return find(a.activity, None if a.wrote is None else now - a.wrote, tasks)


def span(s: float) -> str:
    """32 min, 1 h, 2 h 10 min, 3 h."""
    m = int(s // 60)
    if m < 60:
        return f"{m} min"
    h, m = divmod(m, 60)
    return f"{h} h" + (f" {m} min" if m and h < 3 else "")


def _what(ep: dict) -> str:
    if ep["kind"] == "task":
        cmd = ep["task"].get("command") or ""
        return (f"its background task {ep['task']['id']}" + (f" (`{cmd}`)" if cmd else "")
                + f" has printed nothing for {span(ep['age'])}")
    return f"it has been busy for {span(ep['age'])} without a single new step"


def wake_text(ep: dict) -> str:
    """To the session itself, at WAKE_S."""
    if ep["kind"] == "task":
        cmd = ep["task"].get("command") or ""
        return (f"kaptyorka: your background task {ep['task']['id']}"
                + (f" (`{cmd}`)" if cmd else "")
                + f" has printed nothing for {span(ep['age'])}. Check whether it is stuck (a"
                " wait loop that never ends, a command waiting for input). If it is, stop it"
                " and go on with the work; if it is fine, say in one line what it waits for.")
    return (f"kaptyorka: this session has been busy for {span(ep['age'])} without a new step."
            " If a command or tool is hung, stop it and go on with the work.")


def stop_hint(key: str, background: bool, machine: str, kind: str) -> str:
    sid = key.split(":", 1)[-1]
    if background:
        return f"To stop it: `claude stop {sid[:8]}` on {machine}."
    if kind == "task":
        return "To stop it: open the session and tell it to stop that task."
    return "To stop it: open the session and press Esc."


def people_text(name: str, machine: str, ep: dict, key: str, background: bool) -> str:
    """To the session's person and its boss, at TELL_S."""
    lands = ("A message reaches it, so it was asked to check"
             if ep["kind"] == "task" else
             "A message reaches it only after its current step ends; it was asked to check")
    return (f"Session {name} on {machine} looks stuck: {_what(ep)}. {lands}"
            f" {span(ep['age'] - WAKE_S)} ago and nothing changed. "
            + stop_hint(key, background, machine, ep["kind"]))


def git_line(git: Optional[dict]) -> str:
    """The session folder's git facts as the bridge last read them, in one line; "" unknown."""
    if not isinstance(git, dict) or not git.get("branch"):
        return ""
    dirty = {True: ", uncommitted changes", False: ", nothing uncommitted"}.get(git.get("dirty"), "")
    subject = f", last commit \"{git['subject']}\"" if git.get("subject") else ""
    return f" Git in its folder: branch {git['branch']}{subject}{dirty}."


def manager_text(name: str, machine: str, person: str, ep: dict, key: str, background: bool,
                 git: Optional[dict] = None, again: bool = False) -> str:
    head = "still stuck" if again else "stuck"
    told = f"{person} was told" if person else "nobody could be told"
    return (f"stuck session: {name} of {person or 'nobody'} on {machine} is {head}: {_what(ep)}."
            f" It was asked to check {span(ep['age'] - WAKE_S)} ago; {told}.{git_line(git)} "
            + stop_hint(key, background, machine, ep["kind"]))
