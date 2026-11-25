"""Sessions left behind: ones that have done nothing for a day, and ones another session
started whose parent has ended (the bridge reports `parent` and `parent_gone`, Claude Code
only; see adapters/claude.py). Nobody is told about them: their owner is not there, so a
message would only cost the reader time. The page shows them in its Idle group, and:

- a person closes one of their sessions from the page (an admin anyone's): `Closing` keeps
  the order and that session's own bridge gets it in its next heartbeat answer
  (`close_asked`), checks it again and ends the session (bridge.py `_close_asked`);
- the hub asks a bridge, in its heartbeat answer, to close the narrow case by itself
  (`close_keys`): a `--bg` session started by another session, its parent ended, idle over a
  day, never a hub message. The admin switches that off (`close_left`).

Never the hub's workers or the manager.
"""
from typing import Dict, List, Optional

FRESH_S = 120  # a child or `claude -p` run younger than this, with no hub message: not listed
IDLE_S = 86400  # done nothing for this long: in the Idle group; closed with no question
CLOSE_WAIT_S = 180  # no answer from its bridge this long: the page says so, Close comes back
SHOWN_S = 600  # a refusal stays on its row this long
CLOSABLE_TOOLS = ("claude-code",)  # the tools whose sessions a bridge can end from outside


def short_id(key: str) -> str:
    """The id `claude stop` takes: the first 8 characters of the session id."""
    return key.split(":", 1)[-1][:8]


def _ours(a, live, mid: Optional[int]) -> bool:
    """A session these checks may act on: not a hub worker, not the manager."""
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
    """The bridge says it closed these by itself: logged, nobody told."""
    for key in keys if isinstance(keys, list) else ():
        a = store.agent(str(key))
        if a is None or a.device_id != device_id:
            continue
        store.log_add(by, "left_closed", f"{a.name} on {a.machine}")
        store.offline_now(a.key)


def refused(principal, a, live, mid: Optional[int], night) -> Optional[str]:
    """Why this viewer may not close this online session from the page; None: they may. Only
    their own sessions, or any for an admin; never the manager, a hub worker (it has its own
    close), a session in overnight mode or a tool its bridge cannot end."""
    if principal.legacy or not (principal.is_admin() or a.person_id == principal.person_id):
        return "no such session"
    if mid is not None and a.person_id == mid:
        return "the manager is never closed from here"
    if a.key in live:
        return "a hub worker: close it with its own close"
    if a.key in night:
        return "overnight mode is on in that session"
    if a.tool not in CLOSABLE_TOOLS or a.device_id is None:
        return "this tool's sessions cannot be closed from the page"
    return None


class Closing:
    """People's orders from the page to close a session, in the hub's memory: the session's
    own bridge gets each once, in its next heartbeat answer, and says next beat whether it
    closed it. A hub restart forgets them (the session just stays open)."""

    def __init__(self):
        self.rows: Dict[str, dict] = {}

    def ask(self, a, by: str, confirmed: bool, now: float) -> None:
        if self.rows.get(a.key, {}).get("error", "x") == "":
            return  # already on its way
        self.rows[a.key] = {"device": a.device_id, "by": by, "confirmed": confirmed, "at": now,
                            "sent": False, "error": ""}

    def for_device(self, device_id: int) -> List[dict]:
        """What this device's bridge is asked to close now; each order only once."""
        out = []
        for key, r in self.rows.items():
            if r["device"] == device_id and not r["sent"] and not r["error"]:
                r["sent"] = True
                out.append({"key": key, "confirmed": r["confirmed"]})
        return out

    def done(self, store, device_id: int, results, by: str) -> None:
        """The bridge's answers, {key: "" when closed, else why not}; another device's are
        ignored."""
        if not isinstance(results, dict):
            return
        for key, why in results.items():
            r = self.rows.get(key)
            if r is None or r["device"] != device_id or r["error"]:
                continue
            if why:
                r["error"], r["at"] = str(why)[:200], store.clock()
                store.log_add(by, "session_close", f"{_name(store, key)}: not closed, {r['error']}")
            else:
                del self.rows[key]
                store.offline_now(key)

    def state(self, key: str, now: float) -> dict:
        """The row's fields for the page: {"closing": True} while on its way, {"close_error":
        why} for a while after it failed, else {}."""
        r = self.rows.get(key)
        if r is None:
            return {}
        if not r["error"] and now - r["at"] > CLOSE_WAIT_S:
            r["error"], r["at"] = "its machine did not answer; try again later", now
        if r["error"]:
            if now - r["at"] > SHOWN_S:
                del self.rows[key]
                return {}
            return {"close_error": r["error"]}
        return {"closing": True}


def _name(store, key: str) -> str:
    a = store.agent(key)
    return f"{a.name} on {a.machine}" if a is not None else key
