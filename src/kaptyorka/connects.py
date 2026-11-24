"""Door requests: a door between two sessions of different people. The manager files one for
someone who asked it (`door_request`), or a member's session files one for one of its own
sessions; only the admin decides (`door_decide`, the page), which opens a door: between the two
sessions, or between the two people for DOOR_PEOPLE_S. Two sessions of one registered project
whose people both have a role in it get theirs at once, with no request (`auto_doors`).
Requests made before 0.9.11, when a member could browse a directory of other sessions, are
the same rows. A member never sees another person's session this way: they name it, and only
the admin's decision (or the shared project) opens anything. Kept out of store.py; a
ConnectStore shares the Store's database, lock and clock."""
from typing import Callable, List, Optional, Tuple

from kaptyorka import paths
from kaptyorka.store import (RECENT_S, UNREACHABLE, KaptyorkaError, Principal, Store, clean,
                             session_id)

SCHEMA = """
CREATE TABLE IF NOT EXISTS connects (
    id INTEGER PRIMARY KEY, person_id INTEGER NOT NULL, from_key TEXT NOT NULL,
    to_key TEXT NOT NULL, to_person INTEGER NOT NULL, why TEXT NOT NULL DEFAULT '',
    created REAL NOT NULL, decided_at REAL, decision TEXT, decided_by INTEGER,
    reason TEXT NOT NULL DEFAULT '', door_id INTEGER);
"""

ADMIN_APPROVES = "only the admin approves connection requests, on the page"
MAX_REASON = 500
DOOR_PEOPLE_S = 7 * 86400  # "these two people": a group door for a week
REMIND_S = 3600  # an unanswered door request reminds the admin once, after an hour
MAX_FILED = 10  # door requests one session may file per hour
HOWS = ("sessions", "people", "deny")


def ensure_schema(db) -> None:
    db.executescript(SCHEMA)


class ConnectStore:
    def __init__(self, store: Store, notify: Callable[[str, str], bool],
                 manager_id: Callable[[], Optional[int]]):
        """`notify(key, text)` sends a hub notice to a session or a page (`web:<name>`);
        `manager_id()` is the manager person's id or None."""
        self.store, self.db, self.lock = store, store.db, store.lock
        self.notify, self.manager_id = notify, manager_id

    def _side_name(self, key, viewer) -> str:
        """A door end as person `viewer` sees it: a session's name, the project hidden unless
        they may see it; a page as its person's name."""
        if key.startswith("web:"):
            return "the page"
        r = self.db.execute("SELECT * FROM agents WHERE key=?", (key,)).fetchone()
        return self.store._shown(r, viewer) if r else ""

    def _limited(self, principal: Principal) -> bool:
        return principal.role not in ("admin", "manager")

    def _rows(self, where: str, args: tuple = ()) -> List[dict]:
        out = []
        for c in self.db.execute(
                "SELECT c.*, p.name AS person, d.name AS decided_by_name FROM connects c"
                " JOIN people p ON p.id=c.person_id LEFT JOIN people d ON d.id=c.decided_by"
                f" WHERE {where} ORDER BY c.id", args).fetchall():
            out.append({"id": c["id"], "kind": "session", "person": c["person"],
                        "from_session": "" if c["from_key"].startswith("web:")
                        else self._raw_name(c["from_key"]),  # "": their page
                        "target": self._raw_name(c["to_key"]),
                        **self._label_machine(c["to_key"]),
                        "target_person": self.store._person_name(c["to_person"]),
                        "why": c["why"], "created": c["created"], "decided_at": c["decided_at"],
                        "decision": c["decision"], "decided_by": c["decided_by_name"],
                        "reason": c["reason"], "_from": self.store._side(c["from_key"]),
                        "_to": self.store._side(c["to_key"])})
        return out

    def _manager_key(self, mid: Optional[int]) -> Optional[str]:
        """Lock held. The latest session of the manager (person `mid`), online or not."""
        if mid is None:
            return None
        r = self.db.execute("SELECT key FROM agents WHERE person_id=? ORDER BY last_seen DESC"
                            " LIMIT 1", (mid,)).fetchone()
        return r["key"] if r else None

    def _label_machine(self, key) -> dict:
        r = self.db.execute("SELECT label, machine FROM agents WHERE key=?", (key,)).fetchone()
        return {"target_label": r["label"] if r else "", "target_machine": r["machine"] if r else ""}

    def _raw_name(self, key) -> str:
        if key.startswith("web:"):
            return key[4:]
        r = self.db.execute("SELECT name FROM agents WHERE key=?", (key,)).fetchone()
        return r["name"] if r else ""

    def requests(self, pending_only: bool = True) -> List[dict]:
        """Connection requests for the admin, with `_from` and `_to` sides for masking."""
        with self.lock:
            return self._rows("c.decided_at IS NULL AND p.removed_at IS NULL" if pending_only
                              else "c.decided_at IS NOT NULL")

    # door requests

    def _row(self, key):
        return self.db.execute("SELECT * FROM agents WHERE key=?", (key,)).fetchone()

    def _where(self, r) -> str:
        """A session for the admin: its name, its person, machine and project."""
        project = r["project"] if r["project"] != paths.COORDINATOR else "no project"
        return (f"{r['name']} ({self.store._person_name(r['person_id'])}, {r['machine']},"
                f" {project})")

    def _shared_project(self, a, b) -> Optional[str]:
        """Lock held. The registered project both sessions work in, when both people have a
        role in it and neither is a team machine; None otherwise."""
        s = self.store
        p = a["project"]
        if not p or p != b["project"] or p == paths.COORDINATOR or not s._registered(p):
            return None
        if s._is_team(a["person_id"]) or s._is_team(b["person_id"]):
            return None
        if s._role(p, a["person_id"]) is None or s._role(p, b["person_id"]) is None:
            return None
        return p

    def _live(self, pid) -> bool:
        """Lock held. A person still on the team."""
        return self.db.execute("SELECT 1 FROM people WHERE id=? AND removed_at IS NULL",
                               (pid,)).fetchone() is not None

    def _asked(self, asker_key, manager_key, a, b, now) -> bool:
        """Lock held. The manager may open this door itself: the asking session belongs to the
        person of one side and wrote to the manager in the last RECENT_S."""
        r = self._row(asker_key) if asker_key else None
        if r is None or r["person_id"] not in (a["person_id"], b["person_id"]):
            return False
        return self.db.execute(
            "SELECT 1 FROM messages WHERE sender_key=? AND to_key=? AND created>? LIMIT 1",
            (asker_key, manager_key, now - RECENT_S)).fetchone() is not None

    def file(self, a_key: str, b_key: str, why: str, filer: Principal, filer_key: str,
             asker_key: Optional[str] = None, size: str = "sessions") -> dict:
        """A door between sessions `a_key` and `b_key` (resolved by the hub; a member names one
        of their own as `a_key`). What happens, in order:
        {"kind": "auto"}: one shared project (auto_doors on), a session door at once;
        {"kind": "opened"}: the manager filed it for `asker_key`, a session of the person of
        one side that wrote to it in the last 10 minutes (manager_doors on): a door of `size`
        ("sessions", or "people" for DOOR_PEOPLE_S) at once;
        {"kind": "pending", "id"}: a request for this pair waits already;
        {"kind": "filed", "id", "line"}: a new request for the admin.
        Refused: one person's two sessions, a removed person, a pair that may already talk,
        and for a member a hidden team machine (to the manager's request the admin answers)."""
        why = clean(why, MAX_REASON)
        if not why:
            raise KaptyorkaError("say why the two need a door, in why")
        if size not in ("sessions", "people"):
            raise KaptyorkaError('size is "sessions" or "people"')
        s = self.store
        auto, by_manager = s.auto_doors(), filer.role == "manager" and s.manager_doors()
        with self.lock:
            now = s.clock()
            s._expire_doors(now)
            a, b = self._row(a_key), self._row(b_key)
            if a is None or b is None or a_key == b_key:
                raise KaptyorkaError("no such session")
            ap, bp = a["person_id"], b["person_id"]
            if ap is None or bp is None:
                raise KaptyorkaError("no such session")
            if ap == bp:
                raise KaptyorkaError("both are sessions of one person: they need no door")
            if not self._live(ap) or not self._live(bp):
                raise KaptyorkaError("one of them is no longer on the team")
            hidden = (s._is_team(bp) and s._hidden(bp, ap)) or (s._is_team(ap) and s._hidden(ap, bp))
            if hidden and filer.role not in ("admin", "manager"):
                raise KaptyorkaError(UNREACHABLE.format(to=b["name"]))  # hidden stays hidden
            if s._may_message(a, b) and s._may_message(b, a):
                raise KaptyorkaError(f"{a['name']} and {b['name']} can already message each"
                                     " other")
            project = self._shared_project(a, b) if auto and not hidden else None
            asker = self._row(asker_key) if asker_key else None
            if project is not None:
                self.db.execute(
                    "INSERT INTO doors (a_key, a_person, b_key, b_person, scope, opened_at,"
                    " opened_by) VALUES (?,?,?,?,'session',?,0)", (a_key, ap, b_key, bp, now))
                out = {"kind": "auto", "project": project, "detail": f"auto: same project {project}"}
            elif by_manager and not hidden and self._asked(asker_key, filer_key, a, b, now):
                if size == "people":
                    s._open_group(ap, bp, filer.person_id, now, now + DOOR_PEOPLE_S)
                else:
                    self.db.execute(
                        "INSERT INTO doors (a_key, a_person, b_key, b_person, scope, opened_at,"
                        " opened_by) VALUES (?,?,?,?,'session',?,?)",
                        (a_key, ap, b_key, bp, now, filer.person_id))
                out = {"kind": "opened", "size": size, "asker": asker["name"],
                       "detail": f"by manager for {asker['name']}: {why}"}
            else:
                have = self.db.execute(
                    "SELECT id FROM connects WHERE decided_at IS NULL AND ((from_key=? AND"
                    " to_key=?) OR (from_key=? AND to_key=?))",
                    (a_key, b_key, b_key, a_key)).fetchone()
                if have is not None:
                    return {"kind": "pending", "id": have["id"]}
                n = self.db.execute("SELECT COUNT(*) FROM connects WHERE filed_by=? AND"
                                    " created>?", (filer_key, now - 3600)).fetchone()[0]
                if n >= MAX_FILED:
                    raise KaptyorkaError(f"at most {MAX_FILED} door requests an hour")
                who = asker["name"] if asker is not None else (
                    "" if filer.role == "manager" else self._row(filer_key)["name"])
                cid = self.db.execute(
                    "INSERT INTO connects (person_id, from_key, to_key, to_person, why, created,"
                    " asked_by, filed_by) VALUES (?,?,?,?,?,?,?,?)",
                    (ap, a_key, b_key, bp, why, now, who, filer_key)).lastrowid
                out = {"kind": "filed", "id": cid, "line": self._admin_line(cid)}
            out.update(a=a["name"], b=b["name"])
        if out["kind"] == "filed":
            s.log_add(filer.person_name, "door_request", f"#{out['id']} {out['a']} <-> {out['b']}")
            return out
        s.log_add("hub" if out["kind"] == "auto" else "manager", "door_open",
                  f"{out['a']} <-> {out['b']} ({out['detail']})")
        lasts = ("for 7 days" if out.get("size") == "people"
                 else "while both sessions are around")
        for me, other in ((a_key, b), (b_key, a)):
            if out["kind"] == "auto":
                why_line = f"same project {out['project']}"
            elif me == asker_key:
                why_line = "you asked the manager"
            else:
                why_line = f"{out['asker']} asked the manager: {why}"
            self.notify(me, f"Connected: a door is open between this session and"
                            f" {other['name']} ({why_line}). Write with send(to=\"{other['name']}\");"
                            f" it stays open {lasts}. Close it with door_close(\"{other['name']}\").")
        return out

    def _admin_line(self, cid: int) -> str:
        """Lock held. One line for the admin: both sides, why, who asked, how to answer."""
        c = self.db.execute("SELECT * FROM connects WHERE id=?", (cid,)).fetchone()
        a, b = self._row(c["from_key"]), self._row(c["to_key"])
        sides = " and ".join(self._where(r) if r else "a gone session" for r in (a, b))
        asked = f" Asked by {c['asked_by']}." if c["asked_by"] else ""
        return (f"Door request #{cid}: {sides}: {c['why']}.{asked} Answer with"
                f" door_decide({cid}, \"sessions\" | \"people\" | \"deny\"): the two sessions,"
                " the two people for 7 days, or no. Also on the page.")

    def door_requests(self) -> List[dict]:
        """Open door requests for the admin, each with its one line."""
        with self.lock:
            rows = self.db.execute("SELECT id, created FROM connects WHERE decided_at IS NULL"
                                   " ORDER BY id").fetchall()
            return [{"id": r["id"], "created": r["created"], "line": self._admin_line(r["id"])}
                    for r in rows]

    def due_reminders(self, now: float) -> List[str]:
        """The one reminder each open request gets, an hour after it was filed."""
        with self.lock:
            # filed_by: one filed through door_request; a request from before 0.9.11 never
            # reminds (it would come at the first tick after the update, however old)
            rows = self.db.execute("SELECT id FROM connects WHERE decided_at IS NULL AND"
                                   " reminded_at IS NULL AND filed_by!='' AND created<=?",
                                   (now - REMIND_S,)).fetchall()
            out = []
            for r in rows:
                self.db.execute("UPDATE connects SET reminded_at=? WHERE id=?", (now, r["id"]))
                out.append("Still waiting for you: " + self._admin_line(r["id"]))
        return out

    def decide(self, cid: int, by: Principal, approve: bool, reason: str = "",
               how: str = "sessions") -> str:
        """The admin approves (`how` "sessions": a session door between the two; "people": a
        group door between their people for DOOR_PEOPLE_S) or denies (the reason goes to the
        asker). Returns "approved", "denied", or "offline" when a side was gone RECENT_S and
        no session door was opened."""
        if not by.is_admin():
            raise KaptyorkaError(ADMIN_APPROVES)
        if how not in ("sessions", "people"):
            raise KaptyorkaError('how must be "sessions" or "people"')
        reason = (reason or "").strip()[:MAX_REASON]
        s = self.store
        mid = self.manager_id()  # it locks: never inside the block below
        with self.lock:
            now = s.clock()
            s._expire_doors(now)
            c = self.db.execute("SELECT * FROM connects WHERE id=?", (cid,)).fetchone()
            if c is None:
                raise KaptyorkaError("no such request")
            if c["decided_at"] is not None:
                raise KaptyorkaError("request already decided")
            online = {r["key"]: r for r in self.db.execute(
                "SELECT * FROM agents WHERE last_seen > ?", (now - RECENT_S,)).fetchall()}
            sides_on = (c["from_key"].startswith("web:") or c["from_key"] in online) \
                and c["to_key"] in online
            people = approve and how == "people"
            decision = ("approved" if sides_on or people else "offline") if approve else "denied"
            door = None
            if people:
                door = s._open_group(c["person_id"], c["to_person"], by.person_id, now,
                                     now + DOOR_PEOPLE_S)
            elif decision == "approved":
                door = self.db.execute(
                    "INSERT INTO doors (a_key, a_person, b_key, b_person, scope, opened_at,"
                    " opened_by) VALUES (?,?,?,?,'session',?,?)",
                    (c["from_key"], c["person_id"], c["to_key"], c["to_person"], now,
                     by.person_id)).lastrowid
            self.db.execute("UPDATE connects SET decided_at=?, decision=?, decided_by=?, reason=?,"
                            " door_id=? WHERE id=?", (now, decision, by.person_id, reason, door, cid))
            pid, asker = c["person_id"], s._person_name(c["person_id"])
            to_name = self._side_name(c["to_key"], pid)
            to_label = online[c["to_key"]]["label"] if c["to_key"] in online else ""
            target = to_name + (f" ({to_label})" if to_label else "")
            page, session = f"web:{asker}", None if c["from_key"].startswith("web:") else c["from_key"]
            tell: List[Tuple[str, str]] = []
            b_person = s._person_name(c["to_person"])
            if people:
                text = (f"Connected: the admin let {asker} and {b_person} message each other's"
                        f" sessions for 7 days (request #{cid}).")
                tell += [(page, text)] + ([(session, text)] if session else []) \
                    + [(c["to_key"], text)]
            elif decision == "approved":
                handle = session_id(c["to_key"])
                tell.append((page, f"Connected: the admin approved your request #{cid}."
                                   f" {'Your page' if session is None else 'Your session ' + self._side_name(c['from_key'], pid)}"
                                   f" and {target} can talk until either is gone 10 minutes or"
                                   " closes the door."))
                if session:
                    tell.append((session, f"Connected: the admin approved your request #{cid}."
                                          f" You can send(to=\"{handle}\") to {target} until"
                                          f" either session is gone 10 minutes or door_close(\"{handle}\")."))
                back = asker if session is None else session_id(c["from_key"])
                tell.append((c["to_key"], f"Connected: the admin opened a door between this"
                                          f" session and {asker}'s"
                                          f" {'page' if session is None else 'session ' + self._side_name(c['from_key'], c['to_person'])}"
                                          f" (request #{cid}). You can reply with send(to=\"{back}\")."))
            else:
                text = (f"Your connection request #{cid} to {target} was denied"
                        + (f": {reason}" if reason else ".")) if decision == "denied" else (
                    f"Your connection request #{cid} to {target} was approved, but a session went"
                    " offline, so no door was opened. Ask again when both are online.")
                tell += [(page, text)] + ([(session, text)] if session else [])
            from_raw, to_raw = self._raw_name(c["from_key"]), self._raw_name(c["to_key"])
            mkey = self._manager_key(mid) if c["filed_by"] else None
            if mkey is not None and mkey not in (c["from_key"], c["to_key"]):
                outcome = {"approved": "allowed for the two people for 7 days" if people
                           else "allowed for the two sessions", "denied": "denied",
                           "offline": "allowed, but a session was gone, so no door"}[decision]
                tell.append((mkey, f"door request #{cid} ({from_raw} <-> {to_raw}): {outcome}"
                                   + (f": {reason}" if reason and decision == "denied" else "")))
        if door is not None:
            s.log_add(by.person_name, "door_open",
                      f"{c['from_key'] if session is None else from_raw} <-> {to_raw}"
                      + (" (people, 7 days)" if people else ""))
        s.log_add(by.person_name, "connect_decided", f"#{cid} {asker}: {decision}")
        for key, text in tell:
            self.notify(key, text)
        return decision
