"""The start brief: ordered sections within a character budget, cached per session."""
import time
from typing import Callable, Dict, List, Optional, Tuple

from kaptyorka import __version__
from kaptyorka.hub_feed import knocks as hub_knocks
from kaptyorka.hub_feed import session_row
from kaptyorka.store import Agent, KaptyorkaError, Store

BUDGET = 6000  # characters
CACHE_S = 10
WORKER_BRIEF_S = 300  # a worker session this young hears its task in the start brief
RULES_POINTER = "Team rules are longer than fit here; read them with rules()."
STATE_POINTER = "… (read the rest with state(project))"

_CACHE: Dict[str, Tuple[float, int, str]] = {}


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" + ("" if n == 1 else "s")


def _titled(a: Agent, name: str = "") -> str:
    name = name or a.name
    return f"{a.label} ({name})" if a.label else name


def _wanted(r: dict) -> str:
    return f"access to {r['target']} (project)" if r["kind"] == "project" \
        else f"use of {r['target']} (machine)"


def _request_lines(store: Store, principal) -> str:
    """The start line's tail: the caller's pending and recently denied access requests."""
    if principal is None:
        return ""
    out = ""
    pending = store.pending_for(principal.person_id)
    if pending:
        out += f" Pending: {', '.join(_wanted(r) for r in pending)}."
    denied = store.denied_since(principal.person_id, store.clock() - 7 * 86400)
    if denied:
        out += f" Denied: {', '.join(_wanted(r) for r in denied)}."
    return out


def message_text(m) -> str:
    return m.text if m.kind != "message" else \
        f"Kaptyorka message from {m.sender}{' (web page)' if m.from_web else ''}:\n\n{m.text}"


def _ago(s: float) -> str:
    for unit, n in (("day", 86400), ("hour", 3600), ("minute", 60)):
        if s >= n:
            return f"{_plural(int(s // n), unit)} ago"
    return "just now"


def _you(store, ws, agent: Agent, principal, unread: int) -> str:
    """Section 2: who you are, who else is here, and what waits for you."""
    person = principal.person_name if principal is not None else ""
    line = f"You are `{_titled(agent)}`" + (f", {person}" if person else "") \
        + f", on {agent.host or agent.machine}. "
    others = [a for a in store.online() if a.key != agent.key]
    if others and principal is not None:
        now, people = store.clock(), {p["id"]: p["name"] for p in store.people()}
        # masked like who(): another person's private folder is not named
        names = ", ".join(_titled(a, session_row(store, a, principal, people, now)["name"])
                          + (f" ({a.activity})" if a.activity else "") for a in others)
        line += f"{_plural(len(others), 'other')} online: {names}. "
    elif others:
        line += f"{_plural(len(others), 'other')} online. "
    else:
        line += "No one else online. "
    line += f"{_plural(unread, 'unread message')}."
    topics = [n["topic"] for n in store.notes_list()]
    if topics:
        line += f" Board topics: {', '.join(topics)}."
    if principal is None:
        return line
    knocked = hub_knocks(store, principal, to_me_only=True)
    if knocked:
        line += " Knocks for you: " + "; ".join(
            k["from"] + (f" ({k['from_session']})" if k["from_session"] else "")
            + f' about "{k["about"]}"' for k in knocked) + "."
    line += _request_lines(store, principal)
    if principal.is_admin() and store.legacy_ready_to_disable():
        line += " All old machines have their own tokens; switch the shared token off on the page."
    waiting = ws.unattended_for(principal.person_id)
    if waiting:
        line += (" Unattended workers: " + ", ".join(f"{w['name']} ({w['host']})" for w in waiting)
                 + "; adopt(name) takes one over.")
    if principal.is_admin():
        warned = ws.warnings(store.clock(), __version__)[:3]
        if warned:
            line += " Warnings: " + "; ".join(w["text"] for w in warned) + "."
    return line


def _projects(store, principal) -> str:
    rows = store.projects_for(principal) if principal is not None else []
    lines = [f"- {p['name']}" + (f": {p['description']}" if p["description"] else "")
             + (f" (working now: {', '.join(p['working_now'])})" if p["working_now"] else "")
             for p in rows]
    return "Projects you may read:\n" + "\n".join(lines) if lines else ""


def _here(store, ws, ms, agent: Agent, principal) -> Tuple[str, str, str]:
    """Section 4 for the session's project: (project, state note text, git block)."""
    if principal is None:
        return "", "", ""
    folders = ws.folders(principal.device_id)
    project = ms.session_project(agent.cwd, agent.project, folders)
    if not project:  # "" would read the team board's notes
        return "", "", ""
    try:
        note = store.notes_read("state", principal, project)
    except KaptyorkaError:  # not registered, or no access: nothing about it here
        return project, "", ""
    now = store.clock()
    lines = [f'- {g["person"]} on {g["branch"]}: "{g["subject"]}" ({_ago(now - g["time"])})'
             for g in ms.git_recent(project, days=7)]
    branches = ms.branches_with_sessions(project, now)
    git = "Recent git activity (7 days):\n" + "\n".join(lines) if lines else ""
    if branches:
        git += ("\n" if git else "") + "Branches with open sessions: " + ", ".join(
            f"{b or '?'} ({', '.join(names)})" for b, names in branches.items())
    return project, note.text if note is not None else "", git


def build(store, ws, ms, agent: Agent, principal, rules_text: str = "") -> str:
    return compose(store, ws, ms, agent, principal, rules_text)[0]


def compose(store, ws, ms, agent: Agent, principal, rules_text: str = "") -> Tuple[str, int]:
    """The brief, and how many unread messages it delivered (they are now marked read)."""
    worker = ws.by_key(agent.key)
    young = agent.started is not None and agent.started > store.clock() - WORKER_BRIEF_S
    # some tools cannot take the worker's task on the command line
    first = ws.first_message(worker["name"]) if worker is not None and young else ""
    project, state, git = _here(store, ws, ms, agent, principal)
    projects = _projects(store, principal)
    unread = store.inbox(agent.key)
    you = _you(store, ws, agent, principal, len(unread))
    mail = "Unread messages:\n\n" + "\n\n".join(message_text(m) for m in unread) if unread else ""
    head = f"State of {project}:\n"

    def text(rules, projects, git, state) -> str:
        parts = [first, rules, you, projects, head + state if state else "", git, mail]
        return "\n\n".join(p for p in parts if p)

    return _fit(text, rules_text, projects, git, state), len(unread)


def _fit(text, rules_text: str, projects: str, git: str, state: str) -> str:
    """Cut to BUDGET: rules, then projects, then git, then the state note's tail."""
    out = text(rules_text, projects, git, state)
    if len(out) <= BUDGET:
        return out
    rules = RULES_POINTER if rules_text else ""
    for projects, git in ((projects, git), ("", git), ("", "")):
        out = text(rules, projects, git, state)
        if len(out) <= BUDGET:
            return out
    paragraphs: List[str] = state.split("\n\n")
    while paragraphs:  # the state note last, at a paragraph boundary
        out = text(rules, "", "", "\n\n".join(paragraphs) + "\n\n" + STATE_POINTER)
        if len(out) <= BUDGET:
            return out
        paragraphs.pop()
    return text(rules, "", "", STATE_POINTER if state else "")  # section 2 and mail stay whole


def unread_count(store: Store, key: str) -> int:
    """Messages waiting for the session, without reading them."""
    with store.lock:
        return sum(1 for _, t in store._deliverable(store.clock()) if t["key"] == key)


def cached(key: str, builder: Callable[[], Tuple[str, int]],
           unread: Callable[[], int] = lambda: 0, now: Optional[float] = None,
           cache: Optional[Dict[str, Tuple[float, int, str]]] = None) -> str:
    """The session's brief from the last `CACHE_S` seconds, unless its unread count changed
    since (so a new message is never hidden); else a fresh one from `builder`, which returns
    the text and how many messages it delivered. A brief that delivered messages is not kept:
    served again, it would repeat them as unread."""
    cache = _CACHE if cache is None else cache
    now = time.monotonic() if now is None else now
    for k in [k for k, (t, _, _) in cache.items() if now - t >= CACHE_S]:
        del cache[k]
    count = unread()
    hit = cache.get(key)
    if hit is not None and hit[1] == count:
        return hit[2]
    out, delivered = builder()
    if delivered:
        cache.pop(key, None)
    else:  # the count from before the build: a message that came during it breaks the cache
        cache[key] = (now, count, out)
    return out
