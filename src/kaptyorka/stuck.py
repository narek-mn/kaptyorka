"""Stuck sessions: a background task that prints nothing, or a busy session that writes nothing.
docs/superpowers/specs/2026-11-24-stuck-sessions.md

A silence of WAKE_S or more is an episode. Its stages are timed from when the hub noticed it,
never from the silence's age, one stage per tick at most: first the session itself is asked to
check (a message reaches a session whose turn is over at once; a busy one reads it after its
current tool call). A session that then writes anything has seen it: that task is acknowledged
(a server or a watcher meant to run) and never asked about again while the session lives.
Only a session that did not answer within TELL_S of the wake is reported to its person, its
boss (for a worker) and the manager; the manager once more AGAIN_S later. An episode ends when
the task prints or ends, the session writes again (busy), or it goes idle. The bridge measures
(Claude Code on Linux only); the hub judges. Nothing is judged when a measure is missing."""
import re
import shlex
from typing import List, Optional

from kaptyorka.secrets_check import PATTERNS

WAKE_S = 1800  # silent this long: an episode, and the session is asked
TELL_S = 1800  # no answer this long after the wake: its person, boss and the manager hear it
AGAIN_S = 1800  # still stuck this long after that: the manager once more
COMMAND_CAP = 100
# a re-measured silence start this close to the kept one is the same silence: a heartbeat that
# lands late, a slow read or a hub restart shifts it by seconds; the thresholds are half an hour
STEADY_S = 60


def safe_command(text: str) -> str:
    """One line, at most COMMAND_CAP characters; "" when it looks like a secret."""
    line = " ".join(str(text).split())
    line = line[:COMMAND_CAP - 3] + "..." if len(line) > COMMAND_CAP else line
    return "" if any(p.search(line) for _, p in PATTERNS) else line


def command_of(argv: List[str]) -> str:
    """The command a Claude Code background shell runs. Its wrapper (2.1.33x) is
    `bash -c "source <snapshot> 2>/dev/null || true && ... && eval '<command>' < /dev/null &&
    pwd -P >| ..."`: the command is the one shell word after `eval`, read as the shell would,
    whatever follows it. "" when a wrapper cannot be read: it names the home folder."""
    script = argv[-1] if argv else ""
    m = re.search(r"(?:^|&&|;|\|\|)\s*eval\s+", script)
    if m:
        lex = shlex.shlex(script[m.end():], posix=True)
        lex.whitespace_split, lex.commenters = True, ""
        try:
            return next(iter(lex))
        except (ValueError, StopIteration):
            return ""
    if "shell-snapshots" in script or re.match(r"\s*source\s", script):
        return ""
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
    """find() for a hub agents row: `wrote` and each task's `out` are hub times. The episode
    also carries "start", the silence's kept hub time, so its key never moves with `now`."""
    if not a.online:
        return None
    acked = set(getattr(a, "stuck_acked", ()) or ())  # it said these are meant to run
    tasks = None if a.tasks is None else [
        {"id": t["id"], "silent_s": now - t["out"], "command": t.get("command", ""),
         "out": t["out"]} for t in a.tasks if t["id"] not in acked]
    ep = find(a.activity, None if a.wrote is None else now - a.wrote, tasks)
    if ep is not None:
        ep["start"] = ep["task"]["out"] if ep["kind"] == "task" else a.wrote
    return ep


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
                " and go on with the work. If it is meant to run (a server, a watcher), just say"
                " so in one line; you won't be asked about this task again.")
    return (f"kaptyorka: this session has been busy for {span(ep['age'])} without a new step."
            " If a command or tool is hung, stop it and go on with the work.")


def stop_hint(key: str, background: bool, machine: str, kind: str) -> str:
    sid = key.split(":", 1)[-1]
    if background:
        return f"To stop it: `claude stop {sid[:8]}` on {machine}."
    if kind == "task":
        return "To stop it: open the session and tell it to stop that task."
    return "To stop it: open the session and press Esc."


def people_text(name: str, machine: str, ep: dict, key: str, background: bool,
                asked_s: float) -> str:
    """To the session's person and its boss, TELL_S after the wake went unanswered; asked_s:
    seconds since the hub's wake really went out."""
    lands = ("A message reaches it, yet it has not answered the hub's question"
             if ep["kind"] == "task" else
             "A message reaches it only after its current step ends; it has written nothing"
             " since the hub's question")
    return (f"Session {name} on {machine} looks stuck: {_what(ep)}. {lands}"
            f" {span(asked_s)} ago. " + stop_hint(key, background, machine, ep["kind"]))


def git_line(git: Optional[dict]) -> str:
    """The session folder's git facts as the bridge last read them, in one line; "" unknown."""
    if not isinstance(git, dict) or not git.get("branch"):
        return ""
    dirty = {True: ", uncommitted changes", False: ", nothing uncommitted"}.get(git.get("dirty"), "")
    subject = f", last commit \"{git['subject']}\"" if git.get("subject") else ""
    return f" Git in its folder: branch {git['branch']}{subject}{dirty}."


def manager_text(name: str, machine: str, person: str, ep: dict, key: str, background: bool,
                 asked_s: float, git: Optional[dict] = None, again: bool = False) -> str:
    head = "still stuck" if again else "stuck"
    told = f"{person} was told" if person else "nobody could be told"
    return (f"stuck session: {name} of {person or 'nobody'} on {machine} is {head}: {_what(ep)}."
            f" It was asked to check {span(asked_s)} ago and has not answered; {told}."
            f"{git_line(git)} " + stop_hint(key, background, machine, ep["kind"]))
