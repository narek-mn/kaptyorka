"""The manager's reply stopped by the model's safety filter.

Claude Code marks such a turn in the transcript (compat.py `claude.refusal`); the flagged text
stays in the session's history, so every later turn of that session is stopped too and the
manager falls silent while the hub sees it running. The hub machine's bridge reports the
newest marker of the manager's session (`manager_refused` in its heartbeat); the hub then:

- closes that session, and the usual launch path starts a fresh one with a clean history (no
  crash is counted). At most MAX_RESTARTS such restarts in WINDOW_S: a block after that closes
  the session and waits until the hour is over, so it cannot loop;
- gives the message it was answering, when a person or a session sent it, once to the fresh
  manager (a copy from the same sender). Blocked again on it, or on a copy, its sender hears
  one plain line instead. A hub notice is never given again;
- tells each admin on the page, and shows it on the manager's state line (`kaptyorka status`).

Nothing here rewords or hides content: detection, a fresh start, one redelivery, notices."""
import json
from typing import Callable, List, Optional, Tuple

from kaptyorka.limits import hm

MAX_RESTARTS = 3  # fresh starts after a block within WINDOW_S; the next block stops them
WINDOW_S = 3600
SHOW_S = 3600  # the state line names the last block this long
SEEN = "manager_refusal_seen"  # the newest marker time handled
PENDING = "manager_refusal"  # {"key", "at"}: reported, not handled yet
RESTARTS = "manager_refusal_restarts"  # [time, ...] of the restarts in the window
STOPPED = "manager_refusal_stopped"  # {"until", "n", "at"}: restarts stopped until then
LAST = "manager_refusal_last"  # the last block's time
REDELIVER = "manager_refusal_redeliver"  # {"id", "from"}: the message for the fresh manager
GIVEN = "manager_refusal_given"  # {"originals": [id], "copies": [id]} given once already
SENDER_TEXT = ("The manager could not answer this message: the model's safety filter stopped its"
               " reply. Ask again in other words, or ask the admin.")


def _json(mm, key: str, default):
    try:
        v = json.loads(mm._get(key) or "null")
    except ValueError:
        return default
    return default if v is None else v


def _float(mm, key: str) -> Optional[float]:
    try:
        v = mm._get(key)
        return float(v) if v else None
    except ValueError:
        return None


def seen(store, mm, key: str, at, device_id: int, host: str) -> None:
    """A bridge's report: the manager session `key` has a refusal marker at `at`. Only the
    bridge the manager runs through, only the manager's own session, only a newer marker."""
    if not isinstance(at, (int, float)) or isinstance(at, bool) or not isinstance(key, str):
        return
    dev = mm.admin_device(host)
    if dev is None or dev["id"] != device_id:
        return
    with store.lock:
        row = store.db.execute("SELECT person_id FROM agents WHERE key=?", (key,)).fetchone()
    if row is None or row["person_id"] is None or row["person_id"] != mm.person_id():
        return
    done = _float(mm, SEEN) or 0.0
    pending = _json(mm, PENDING, {})
    if at <= done or at <= float(pending.get("at") or 0):
        return
    mm._set(PENDING, json.dumps({"key": key, "at": float(at)}))


def _answering(store, key: str, at: float) -> Optional[dict]:
    """The message the session was answering: the newest one delivered to it by then."""
    with store.lock:
        row = store.db.execute(
            "SELECT * FROM messages WHERE to_key=? AND created<=? AND (read_at IS NOT NULL"
            " OR push_state!='new') ORDER BY created DESC, id DESC LIMIT 1",
            (key, at + 1)).fetchone()
    return None if row is None else dict(row)


def _quote(text: str) -> str:
    one = " ".join((text or "").split())
    return one if len(one) <= 80 else one[:77] + "..."


def tick(store, mm, ws, tell: Callable[[str, str], None], admins: List[str],
         now: float) -> bool:
    """Before the hub would launch the manager. True: do not launch now (the blocked session
    is being closed, or restarts are stopped for the hour)."""
    redeliver(store, mm, now)
    pending = _json(mm, PENDING, {})
    if pending.get("key"):
        at = float(pending.get("at") or now)
        mm._set(SEEN, repr(at))
        mm._set(PENDING, "")
        mm._set(LAST, repr(at))
        _question(store, mm, ws, tell, pending["key"], at)
        recent = [t for t in _json(mm, RESTARTS, []) if now - float(t) < WINDOW_S]
        closed = ws.close_manager() is not None
        if len(recent) >= MAX_RESTARTS:
            until = float(recent[0]) + WINDOW_S
            mm._set(STOPPED, json.dumps({"until": until, "n": len(recent) + 1, "at": at}))
            store.log_add("hub", "manager", f"reply blocked by the model's safety filter at"
                          f" {hm(at)}, {len(recent) + 1} times within an hour; restarts"
                          f" stopped until {hm(until)}")
            for name in admins:
                tell(f"web:{name}", f"The manager's reply was blocked by the model's safety"
                     f" filter {len(recent) + 1} times within an hour (last at {hm(at)}). The hub"
                     f" stopped it and starts a fresh one at {hm(until)}; messages to it wait.")
        else:
            mm._set(RESTARTS, json.dumps(recent + [now]))
            store.log_add("hub", "manager", f"reply blocked by the model's safety filter at"
                          f" {hm(at)}; restarted with a clean history")
            for name in admins:
                tell(f"web:{name}", f"The manager's reply was blocked by the model's safety"
                     f" filter at {hm(at)}. The hub started a fresh manager.")
        return closed or len(recent) >= MAX_RESTARTS
    stop = _json(mm, STOPPED, {})
    if stop.get("until") is not None:
        if now < float(stop["until"]):
            return True  # the blocked session was closed when it stopped
        mm._set(STOPPED, "")  # the launch now is a fresh start too
        mm._set(RESTARTS, json.dumps([t for t in _json(mm, RESTARTS, [])
                                      if now - float(t) < WINDOW_S] + [now]))
    return False


def _question(store, mm, ws, tell, key: str, at: float) -> None:
    """The message the blocked turn answered: once more for the fresh manager, or, when it
    was given once already, one line to its sender. A hub notice: neither."""
    msg = _answering(store, key, at)
    if msg is None or str(msg["sender_key"]).startswith("hub:"):
        return
    given = _json(mm, GIVEN, {"originals": [], "copies": []})
    if msg["id"] in given.get("copies", []) or msg["id"] in given.get("originals", []):
        tell(msg["sender_key"], f'{SENDER_TEXT} Your message: "{_quote(msg["text"])}"')
        return
    given["originals"] = (given.get("originals", []) + [msg["id"]])[-50:]
    mm._set(GIVEN, json.dumps(given))
    mm._set(REDELIVER, json.dumps({"id": msg["id"], "from": key}))


def redeliver(store, mm, now: float) -> None:
    """The fresh manager's session is online: the waiting message goes to it once, as a copy
    from its own sender."""
    r = _json(mm, REDELIVER, {})
    new = mm.session_key(now) if r.get("id") else None
    if new is None or new == r.get("from"):
        return
    mm._set(REDELIVER, "")
    with store.lock:
        m = store.db.execute("SELECT * FROM messages WHERE id=?", (r["id"],)).fetchone()
        t = store.db.execute("SELECT * FROM agents WHERE key=?", (new,)).fetchone()
        if m is None or t is None:
            return
        copy = store.db.execute(
            "INSERT INTO messages (sender_key, sender_name, to_key, to_name, machine, tool,"
            " project, text, created, file_id) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (m["sender_key"], m["sender_name"], new, t["name"], t["machine"], t["tool"],
             t["project"], m["text"], now, m["file_id"])).lastrowid
    given = _json(mm, GIVEN, {"originals": [], "copies": []})
    given["copies"] = (given.get("copies", []) + [copy])[-50:]
    mm._set(GIVEN, json.dumps(given))
    store.log_add("hub", "manager", f"gave message {r['id']} once more to the fresh manager")


def state(mm, now: float, running: bool) -> Optional[Tuple[str, str]]:
    """(state, reason) for the manager's state line, or None when no block matters now."""
    stop = _json(mm, STOPPED, {})
    if stop.get("until") is not None and now < float(stop["until"]):
        return ("blocked", f"reply blocked by the model's safety filter {stop.get('n')} times"
                f" within an hour (last at {hm(float(stop.get('at') or now))}); restarts stopped"
                f" until {hm(float(stop['until']))}")
    last = _float(mm, LAST)
    if running and last is not None and now - last < SHOW_S:
        return "up", f"reply blocked by the model's safety filter at {hm(last)}, restarted"
    return None
