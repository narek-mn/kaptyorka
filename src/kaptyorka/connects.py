"""The session directory and connection requests: a member sees every other online session
without its content and asks to be connected to one; only the admin approves, which opens a
session door between the two. Kept out of store.py; a ConnectStore shares the Store's
database, lock and clock."""
from typing import Callable, List, Optional, Tuple

from kaptyorka.secrets_check import check_secrets
from kaptyorka.store import MAX_ABOUT, PRIVATE, KaptyorkaError, Principal, Store, session_id

SCHEMA = """
CREATE TABLE IF NOT EXISTS connects (
    id INTEGER PRIMARY KEY, person_id INTEGER NOT NULL, from_key TEXT NOT NULL,
    to_key TEXT NOT NULL, to_person INTEGER NOT NULL, why TEXT NOT NULL DEFAULT '',
    created REAL NOT NULL, decided_at REAL, decision TEXT, decided_by INTEGER,
    reason TEXT NOT NULL DEFAULT '', door_id INTEGER);
"""

ADMIN_APPROVES = "only the admin approves connection requests, on the page"
MAX_REASON = 500


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

    def _others(self, pid, now):
        """Online sessions of other people that person `pid` may not see: the directory."""
        s = self.store
        return [r for r in s._online_rows(now)
                if r["person_id"] is not None and r["person_id"] != pid and not s._listed(pid, r)]

    def directory(self, principal: Principal) -> List[dict]:
        """Every online session the viewer may not reach yet, without content: its handle
        (`id`), name and project under the who() rule, label, person, machine, tool, and this
        person's request for it. Empty for the admin and the manager: they reach everyone."""
        if not self._limited(principal):
            return []
        pid, s = principal.person_id, self.store
        with self.lock:
            now = s.clock()
            s._expire_doors(now)
            asked = {}
            for c in self.db.execute("SELECT * FROM connects WHERE person_id=? ORDER BY id",
                                     (pid,)):
                asked[c["to_key"]] = c  # the latest wins
            out = []
            for r in self._others(pid, now):
                name = s._shown(r, pid)
                c = asked.get(r["key"])
                request = None
                if c is not None and c["decided_at"] is None:
                    request = {"id": c["id"], "state": "requested"}
                elif c is not None and c["decision"] == "denied":
                    request = {"id": c["id"], "state": "denied", "reason": c["reason"]}
                out.append({"id": session_id(r["key"]), "name": name, "label": r["label"],
                            "person": s._person_name(r["person_id"]), "machine": r["machine"],
                            "tool": r["tool"], "project": r["project"] if name == r["name"] else PRIVATE,
                            "online": True, "reachable": False, "request": request})
            return out

    def _target(self, pid, to, now):
        """The directory row `to` names: a handle, a name as the asker sees it, or a label.
        Never a hidden project's real name, so asking cannot test whether one exists."""
        s, to = self.store, (to or "").strip()
        rows = [r for r in s._online_rows(now) if r["person_id"] is not None and r["person_id"] != pid]
        for how in (lambda r: session_id(r["key"]) == to, lambda r: s._shown(r, pid) == to,
                    lambda r: bool(r["label"]) and r["label"].lower() == to.lower()):
            match = [r for r in rows if how(r)]
            if len(match) > 1:
                raise KaptyorkaError(f"several sessions are named {to}; use one of: "
                                     + ", ".join(session_id(r["key"]) for r in match))
            if match:
                return match[0]
        return None

    def request(self, principal: Principal, from_key: str, to: str, why: str = "") -> int:
        """Ask for a door between `from_key` (one of the person's online sessions, or their
        page `web:<name>`) and the session `to` names. The manager hears of it; the admin
        decides."""
        why = (why or "").strip()
        if len(why) > MAX_ABOUT:
            raise KaptyorkaError(f"why is too long (max {MAX_ABOUT} characters)")
        check_secrets(why)
        pid, s = principal.person_id, self.store
        if not self._limited(principal):
            raise KaptyorkaError("you can already message every session")
        mid = self.manager_id()
        with self.lock:
            now = s.clock()
            s._expire_doors(now)
            if from_key.startswith("web:"):
                if from_key != f"web:{principal.person_name}":
                    raise KaptyorkaError("that is not your page")
                me = {"key": from_key, "person_id": pid}
            else:
                me = next((r for r in s._online_rows(now) if r["key"] == from_key), None)
                if me is None or me["person_id"] != pid:
                    raise KaptyorkaError("connect one of your own online sessions")
            own = next((r for r in s._online_rows(now) if r["person_id"] == pid
                        and (session_id(r["key"]) == to or r["name"] == to)), None)
            if own is not None:
                raise KaptyorkaError("that is your own session")
            t = self._target(pid, to, now)
            if t is None:
                raise KaptyorkaError(f"no online session named {to} in the directory")
            if mid is not None and t["person_id"] == mid:
                raise KaptyorkaError("the manager is always reachable; send to it")
            if s._may_message(me, t):
                raise KaptyorkaError("already connected: send to it")
            dup = self.db.execute("SELECT id FROM connects WHERE from_key=? AND to_key=? AND"
                                  " decided_at IS NULL", (from_key, t["key"])).fetchone()
            if dup is not None:
                raise KaptyorkaError(f"already requested (#{dup['id']}); the admin decides")
            cid = self.db.execute(
                "INSERT INTO connects (person_id, from_key, to_key, to_person, why, created)"
                " VALUES (?,?,?,?,?,?)", (pid, from_key, t["key"], t["person_id"], why, now)).lastrowid
            them = s._person_name(t["person_id"])
            log = f"#{cid} {from_key if from_key.startswith('web:') else me['name']} -> {t['name']}"
            tell = []
            if mid is not None:
                for r in s._online_rows(now):
                    if r["person_id"] == mid:
                        tell.append((r["key"], (
                            f"Connection request #{cid}: {principal.person_name}"
                            f" ({self._side_name(from_key, mid)}) asks to reach {them}'s session"
                            f" {s._shown(t, mid)}" + (f" ({t['label']})" if t["label"] else "")
                            + (f". Why: {why}" if why else "") + ". Only the admin approves,"
                            " on the page (Admin > Access > Requests); you may tell the admin"
                            " and answer the member.")))
        s.log_add(principal.person_name, "connect_request", log)
        for key, text in tell:
            self.notify(key, text)
        return cid

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

    def decide(self, cid: int, by: Principal, approve: bool, reason: str = "") -> str:
        """The admin approves (a session door between the two) or denies (the reason goes to
        the asker). Returns "approved", "denied", or "offline" when a side went offline and
        no door was opened."""
        if not by.is_admin():
            raise KaptyorkaError(ADMIN_APPROVES)
        reason = (reason or "").strip()[:MAX_REASON]
        s = self.store
        with self.lock:
            now = s.clock()
            s._expire_doors(now)
            c = self.db.execute("SELECT * FROM connects WHERE id=?", (cid,)).fetchone()
            if c is None:
                raise KaptyorkaError("no such request")
            if c["decided_at"] is not None:
                raise KaptyorkaError("request already decided")
            online = {r["key"]: r for r in s._online_rows(now)}
            sides_on = (c["from_key"].startswith("web:") or c["from_key"] in online) \
                and c["to_key"] in online
            decision = ("approved" if sides_on else "offline") if approve else "denied"
            door = None
            if decision == "approved":
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
            if decision == "approved":
                handle = session_id(c["to_key"])
                tell.append((page, f"Connected: the admin approved your request #{cid}."
                                   f" {'Your page' if session is None else 'Your session ' + self._side_name(c['from_key'], pid)}"
                                   f" and {target} can talk until either goes offline or"
                                   " closes the door."))
                if session:
                    tell.append((session, f"Connected: the admin approved your request #{cid}."
                                          f" You can send(to=\"{handle}\") to {target} until"
                                          f" either session goes offline or door_close(\"{handle}\")."))
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
        if door is not None:
            s.log_add(by.person_name, "door_open",
                      f"{c['from_key'] if session is None else from_raw} <-> {to_raw}")
        s.log_add(by.person_name, "connect_decided", f"#{cid} {asker}: {decision}")
        for key, text in tell:
            self.notify(key, text)
        return decision
