"""Hub commands suggested from the work, at each prompt (Claude Code and Codex: the tools whose
prompt hook gets the prompt's text). One line of context for the agent, at most once per
session for each target, decided on this machine with no model call and no network: the team
projects from the bridge's team.json, the teammates and team machines the person can see from
the hub-state cache (hubstate.refresh, from the hub's own feed).

- Off the hub in a personal folder (not switched off on purpose, not on a team machine), the
  prompt names a team project, a team machine or a teammate: suggest /hub team [project].
- On the hub, the prompt asks about another session's or teammate's work: who and send; it asks
  to run work on a team machine: work(project, task, machine=...).
Names match as whole words, case-insensitive, 3 characters or more."""
import json
import re
import time
from typing import Iterable, List, Optional

from kaptyorka import offhub, paths

FILE = "suggested.json"  # {session key: {"t": [targets told], "at": time}}
KEEP_S = 30 * 86400
HEAD = "kaptyorka hub: "
# asking about another session's work, without a name: "ask the other session", "what is the
# other agent doing"
OTHER = re.compile(r"\b(?:other|another)\s+(?:\w+\s+)?(?:session|agent)s?\b", re.I)
ASK = re.compile(r"\b(?:ask|tell|message|ping|check with|write to|what (?:is|are|was|were)"
                 r"|how far|progress|status of|working on|doing)\b", re.I)
RUN = re.compile(r"\b(?:run|start|continue|resume|launch|train|do|finish)\b", re.I)


def _word(name: str) -> Optional[re.Pattern]:
    name = name.strip()
    if len(name) < 3:
        return None
    return re.compile(r"(?<![\w-])" + re.escape(name) + r"(?![\w-])", re.I)


def _found(text: str, names: Iterable[str]) -> List[str]:
    out = []
    for n in names:
        p = _word(n)
        if p is not None and p.search(text) and n not in out:
            out.append(n)
    return out


def _seen() -> dict:
    """Names the person can see, from the hub-state cache: {"people", "machines"}."""
    from kaptyorka import hubstate

    c = hubstate.read_cache() or {}
    people = [p for p in c.get("people") or [] if isinstance(p, str)]
    machines = set()
    for m in c.get("machines") or []:
        if isinstance(m, str):
            machines.update({m, m.rsplit("@", 1)[-1]})
    return {"people": people, "machines": sorted(machines)}


def _once(key: str, target: str) -> bool:
    """True the first time this session gets this target; remembers it."""
    f = paths.home() / FILE
    try:
        data = json.loads(f.read_text())
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    now = time.time()
    entry = data.get(key) if isinstance(data.get(key), dict) else {"t": []}
    if target in entry.get("t", []):
        return False
    data = {k: v for k, v in data.items() if isinstance(v, dict)
            and now - float(v.get("at", 0)) < KEEP_S}
    data[key] = {"t": list(entry.get("t", [])) + [target], "at": now}
    try:
        tmp = paths.home() / f".{FILE}.tmp"
        tmp.write_text(json.dumps(data))
        tmp.replace(f)
    except OSError:
        return False  # never repeat a line every prompt because the file cannot be written
    return True


def off_line(key: str, prompt: str) -> str:
    """For a session off the hub only because its folder is personal: one line when the prompt
    names a team project, team machine or teammate; "" otherwise."""
    if not prompt or offhub.shared_machine():
        return ""
    team, _ = offhub.load_team()
    seen = _seen()
    projects = _found(prompt, sorted(team or []))
    others = _found(prompt, seen["machines"] + seen["people"])
    target = (projects or others or [""])[0]
    if not target or not _once(key, target):
        return ""
    from kaptyorka import optin

    hub = optin.command(key)
    mine = offhub.load_mine() or []
    how = (f"`{hub} team {target}` (this session joins project {target})"
           if projects and target in mine else f"`{hub} team` (this session joins as a coordinator)")
    return (f"{HEAD}the user's request names {target}, but this session is off the hub (personal"
            f" folder). If the request is about that, suggest once, in one line, that they type"
            f" {how}; never switch it yourself. Otherwise ignore this.")


def on_lines(key: str, prompt: str) -> str:
    """For a session on the hub: who/send when the prompt asks about another session's or a
    teammate's work, work() when it asks to run work on a team machine; "" otherwise."""
    if not prompt:
        return ""
    seen = _seen()
    lines = []
    machines = _found(prompt, seen["machines"])
    run_on = [m for m in machines if re.search(
        RUN.pattern + r"[^.?!\n]{0,60}\bon\s+(?:the\s+)?" + re.escape(m) + r"(?![\w-])",
        prompt, re.I)]
    if run_on and _once(key, "work"):
        lines.append(f"{HEAD}to run work on {run_on[0]}, use work(project, task,"
                     f" machine=\"{run_on[0]}\"): a worker starts there and reports back.")
    named = _found(prompt, seen["people"]) + [m for m in machines if m not in run_on]
    if (named or OTHER.search(prompt)) and ASK.search(prompt) and _once(key, "who"):
        lines.append(f"{HEAD}to see what other sessions do, call who; to ask one, send(to, text)"
                     " with its name from who. Its answer arrives as a message.")
    return "\n".join(lines)
