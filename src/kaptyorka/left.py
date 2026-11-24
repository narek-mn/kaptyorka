"""Sessions left behind: ones that have done nothing for days, and ones another session
started whose parent has ended (the bridge reports `parent` and `parent_gone`, Claude Code
only; see adapters/claude.py). The hub, once per tick:

- tells each person once a day per machine, in one line, which of their sessions have done
  nothing for over a day, and which the bridge closed for them;
- tells the owner of a session whose parent ended, once, with the stop command;
- asks a bridge, in its heartbeat answer, to close the narrow case (`close_keys`): a `--bg`
  session started by another session, its parent ended, idle over a day, never a hub message.

Never the hub's workers or the manager. The admin switches closing off (`close_left`).
"""
from typing import Callable, Dict, List, Optional

FRESH_S = 120  # a child or `claude -p` run younger than this, with no hub message: not listed
IDLE_S = 86400  # done nothing for this long: in the daily line, and may be closed
DAY_S = 86400


def short_id(key: str) -> str:
    """The id `claude stop` takes: the first 8 characters of the session id."""
    return key.split(":", 1)[-1][:8]


def _days(s: float) -> str:
    d = int(s // 86400)
    return f"{d} day" + ("" if d == 1 else "s")


def _recipients(store, a, online, live, mid, idle_keys) -> List[str]:
    """Where an owner hears it: their other online sessions (not the idle ones, not workers),
    else their page; a team account's sessions go to the admins' pages."""
    if a.person_id is None:
        return []
    if store.is_team(a.person_id):
        from kaptyorka import admin_ops
        return [f"web:{n}" for n in admin_ops.admins(store)]
    to = [b.key for b in online if b.person_id == a.person_id and b.key not in idle_keys
          and b.key not in live and b.key != a.key]
    if to:
        return to
    name = next((p["name"] for p in store.people() if p["id"] == a.person_id), "")
    return [f"web:{name}"] if name else []


def _ours(a, live: dict, mid: Optional[int]) -> bool:
    """A session these checks may speak about: not a hub worker, not the manager."""
    return a.key not in live and not (mid is not None and a.person_id == mid)


def _idle(a, now: float) -> bool:
    """Done nothing for over IDLE_S: not working now and no live background task (one that
    runs a server or a watcher is meant to)."""
    return a.active is not None and now - a.active > IDLE_S \
        and (a.activity or "idle") == "idle" and not a.tasks


def close_keys(store, device_id: int, live: dict, mid: Optional[int], now: float) -> List[str]:
    """The sessions of this device its bridge may close; [] while `close_left` is off. The
    bridge checks again that it started them itself via launch before it does."""
    if not store.close_left():
        return []
    return [a.key for a in store.online()
            if a.device_id == device_id and a.background and a.parent and a.parent_gone
            and _idle(a, now) and _ours(a, live, mid) and a.tool == "claude-code" and not store.ever_messaged(a.key)]


def closed(store, device_id: int, keys, by: str) -> None:
    """The bridge says it closed these: logged, and kept for the owner's daily line."""
    for key in keys if isinstance(keys, list) else ():
        a = store.agent(str(key))
        if a is None or a.device_id != device_id:
            continue
        store.log_add(by, "left_closed", f"{a.name} on {a.machine}")
        store.closed_add(a.person_id, a.machine, a.name)
        store.offline_now(a.key)


def tick(store, mm, live: dict, tell: Callable[[str, str], None], now: float) -> None:
    online = store.online()
    mid = mm.person_id()
    by_key = {a.key: a for a in online}
    # a child whose parent ended: its owner hears it once
    for a in online:
        if not (a.parent and a.parent_gone and _ours(a, live, mid)):
            continue
        if a.started is not None and now - a.started < FRESH_S:
            continue
        if not mm.push_once(f"left:{a.key}", now):
            continue
        p = by_key.get(a.parent) or store.agent(a.parent)
        parent = f"`{p.name}`" if p is not None else "another session"
        text = (f"Session `{a.name}` on {a.machine} was started by {parent}, which has ended;"
                f" it still runs. To stop it: `claude stop {short_id(a.key)}` on {a.machine}.")
        for k in _recipients(store, a, online, live, mid, set()):
            tell(k, text)
    # the daily line, per person and machine
    groups: Dict[tuple, list] = {}
    for a in online:
        if _ours(a, live, mid) and _idle(a, now):
            groups.setdefault((a.person_id, a.machine), []).append(a)
    for pid, machine in store.closed_pending():
        groups.setdefault((pid, machine), [])
    idle_keys = {a.key for g in groups.values() for a in g}
    for (pid, machine), idle in groups.items():
        if pid is None or not mm.push_once(f"idle:{pid}:{machine}", now, DAY_S):
            continue
        shut = store.closed_take(pid, machine)
        parts = []
        if idle:
            n = len(idle)
            least = min(now - a.active for a in idle)
            names = ", ".join(a.name for a in idle)
            parts.append(f"{n} session{'' if n == 1 else 's'} of yours on {machine}"
                         f" {'has' if n == 1 else 'have'} done nothing for {_days(least)}:"
                         f" {names}; close them or leave them.")
        if shut:
            parts.append(f"Closed on {machine} (started by a session that ended, idle over"
                         f" a day, no hub message): {', '.join(shut)}.")
        if not parts:
            continue
        who = idle[0] if idle else _Owner(pid)
        for k in _recipients(store, who, online, live, mid, idle_keys):
            tell(k, " ".join(parts))


class _Owner:
    """A stand-in session for a person with closed sessions and none online idle."""
    key = ""

    def __init__(self, person_id: int):
        self.person_id = person_id
