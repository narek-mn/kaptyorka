"""Memory: what the team's machines did, kept as history. Git facts from heartbeats live
here. Kept out of store.py; a MemoryStore shares the Store's database, lock and clock."""
import os
import re
from typing import Dict, List, Optional

from kaptyorka.store import ONLINE_S, Principal, Store

GIT_KEEP_S = 30 * 86400  # git history older than this is pruned
DONE_KEEP_S = 30 * 86400  # done checks of sessions offline this long, and old state writes
RECENT_LIMIT = 20

SCHEMA = """
CREATE TABLE IF NOT EXISTS git_facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT, project TEXT NOT NULL, host TEXT NOT NULL,
    os_user TEXT NOT NULL, person_id INTEGER, cwd TEXT NOT NULL, branch TEXT NOT NULL,
    dirty INTEGER, subject TEXT NOT NULL, time REAL NOT NULL);
CREATE INDEX IF NOT EXISTS git_facts_cwd ON git_facts (host, os_user, cwd, id);
CREATE INDEX IF NOT EXISTS git_facts_project ON git_facts (project, time);
CREATE TABLE IF NOT EXISTS done_checks (
    key TEXT PRIMARY KEY, project TEXT NOT NULL, started REAL NOT NULL, signal_at REAL,
    signal TEXT NOT NULL DEFAULT '', blocked_at REAL, last_subject TEXT NOT NULL DEFAULT '',
    last_dirty INTEGER);
CREATE TABLE IF NOT EXISTS state_writes (
    id INTEGER PRIMARY KEY AUTOINCREMENT, project TEXT NOT NULL, key TEXT, person_id INTEGER,
    time REAL NOT NULL);
CREATE INDEX IF NOT EXISTS state_writes_project ON state_writes (project, time);
"""

STATE_HEADINGS = ("What it is", "Where it stands", "Decisions", "Open problems",
                  "How to run and test")
_HEADING = re.compile(r"^#{1,3}[ \t]+(.*?)[ \t#]*$")


def check_state(text: str) -> Optional[str]:
    """None when every state heading appears as a Markdown heading, in order."""
    want = [h.lower() for h in STATE_HEADINGS]
    i = 0
    for line in text.splitlines():
        m = _HEADING.match(line.strip())
        if m and i < len(want) and m.group(1).strip().lower() == want[i]:
            i += 1
    if i == len(want):
        return None
    return "the state note needs these headings in order: " + ", ".join(STATE_HEADINGS)

_WORKTREE = re.compile(r"--\d+$")
_DONE = re.compile(r"\bdone\b", re.I)


def ensure_schema(db) -> None:
    db.executescript(SCHEMA)


def _dirty(value) -> Optional[int]:
    return None if value is None else int(bool(value))


class MemoryStore:
    def __init__(self, store: Store):
        self.store = store
        self.db = store.db
        self.lock = store.lock
        self.clock = store.clock

    @staticmethod
    def project_of(cwd: str, folders: dict) -> str:
        """The project of a folder. A `<folder>--N` worktree belongs to its base folder's
        project, checked first, so a learned "proj-a--2" entry never wins over "proj-a"."""
        cwd = cwd.rstrip("/") or cwd
        folders = {p: f.rstrip("/") for p, f in (folders or {}).items()}
        base = _WORKTREE.sub("", cwd)
        if base != cwd:
            for project, folder in folders.items():
                if folder == base:
                    return project
        for project, folder in folders.items():
            if cwd == folder:
                return project
        return _WORKTREE.sub("", os.path.basename(cwd))

    @classmethod
    def session_project(cls, cwd: str, project: str, folders: dict) -> str:
        """A session's project: from its folder when the device's folders are known, else the
        name its identity gave. The brief and the done check both use this."""
        return cls.project_of(cwd, folders) if folders and cwd else project

    def _session(self, key: str):
        return self.db.execute("SELECT project, started, last_seen, person_id, host, os_user, cwd"
                               " FROM agents WHERE key=?", (key,)).fetchone()

    def _latest_git(self, a, before: Optional[float] = None):
        """The newest git fact for a session's folder (at or before `before`). Lock held."""
        if not a["cwd"]:
            return None
        sql = "SELECT subject, dirty, time FROM git_facts WHERE host=? AND os_user=? AND cwd=?"
        args: list = [a["host"], a["os_user"], a["cwd"]]
        if before is not None:
            sql += " AND time<=?"
            args.append(before)
        return self.db.execute(sql + " ORDER BY id DESC LIMIT 1", args).fetchone()

    def _last_write(self, project: str, key: str, person_id: Optional[int]) -> Optional[float]:
        """The last state write for a project by this session, or by its person. Lock held."""
        return self.db.execute(
            "SELECT MAX(time) FROM state_writes WHERE project=? AND (key=? OR"
            " (? IS NOT NULL AND person_id=?))", (project, key, person_id, person_id)).fetchone()[0]

    def _row(self, key: str, a, project: Optional[str] = None):
        """The session's done_checks row, created with the folder's git state at its start.
        A resolved `project` (from the session's folder) is kept on the row; without one the
        row keeps what it has, or takes the session's own project name."""
        row = self.db.execute("SELECT * FROM done_checks WHERE key=?", (key,)).fetchone()
        if row is not None:
            if project and row["project"] != project:
                self.db.execute("UPDATE done_checks SET project=? WHERE key=?", (project, key))
                row = self.db.execute("SELECT * FROM done_checks WHERE key=?", (key,)).fetchone()
            return row
        started = a["started"] or a["last_seen"]
        base = self._latest_git(a, started)
        self.db.execute(
            "INSERT INTO done_checks (key, project, started, last_subject, last_dirty)"
            " VALUES (?,?,?,?,?)", (key, project or a["project"], started,
                                    base["subject"] if base else "", base["dirty"] if base else None))
        return self.db.execute("SELECT * FROM done_checks WHERE key=?", (key,)).fetchone()

    def _signal(self, key: str, kind: str, at: Optional[float] = None,
                project: Optional[str] = None) -> None:
        """Note a work signal for a session. Caller holds the lock. A pending signal (no state
        write since it) keeps its time and takes the newer kind, except that "done" does not
        replace a pending commit or clean."""
        at = self.clock() if at is None else at
        a = self._session(key)
        if a is None:
            return
        row = self._row(key, a, project)
        last = self._last_write(row["project"], key, a["person_id"])
        if row["signal_at"] is None or (last is not None and row["signal_at"] < last):
            self.db.execute("UPDATE done_checks SET signal_at=?, signal=? WHERE key=?",
                            (at, kind, key))
        elif not (kind == "done" and row["signal"] in ("commit", "clean")):
            self.db.execute("UPDATE done_checks SET signal=? WHERE key=?", (kind, key))

    def _git_since_start(self, key: str, a, row, project: Optional[str] = None) -> None:
        """A commit or clean the heartbeats did not signal (the session was offline, or its
        folder's first sighting): compare the folder's newest git fact with the one kept on
        the row, then keep the newest. Lock held."""
        g = self._latest_git(a)
        if g is None:
            return
        if (row["last_subject"] or row["last_dirty"] is not None) and g["time"] > row["started"]:
            kinds = []
            if g["subject"] != row["last_subject"]:
                kinds.append("commit")
            if row["last_dirty"] == 1 and g["dirty"] == 0:
                kinds.append("clean")
            for kind in kinds:
                self._signal(key, kind, g["time"], project)
        self.db.execute("UPDATE done_checks SET last_subject=?, last_dirty=? WHERE key=?",
                        (g["subject"], g["dirty"], key))

    def status_signal(self, key: str, text: str, project: Optional[str] = None) -> None:
        """From the status tool: "done" (a word) is a work signal, unless the state was written
        at or after the last commit or clean (or the start, when there was none); "no state
        change" clears the signal."""
        text = text or ""
        with self.lock:
            a = self._session(key)
            if a is None:
                return
            if _DONE.search(text):
                row = self._row(key, a, project)
                since = (row["signal_at"] if row["signal_at"] is not None
                         and row["signal"] in ("commit", "clean") else row["started"])
                last = self._last_write(row["project"], key, a["person_id"])
                if last is None or last < since:
                    self._signal(key, "done", project=project)
            text = text.lower()
            if "no state change" in text:
                self._row(key, a, project)
                g = self._latest_git(a)
                self.db.execute("UPDATE done_checks SET signal_at=NULL" + (
                    ", last_subject=?, last_dirty=?" if g else "") + " WHERE key=?",
                    ((g["subject"], g["dirty"]) if g else ()) + (key,))

    def done_check(self, key: str, principal: Principal, now: float, commit: bool = True,
                   project: Optional[str] = None) -> dict:
        """Block a session's stop once when it did work in a project it may write and has not
        updated that project's state note since. commit=False only looks: blocked_at stays unset.
        `project` is the session's project resolved from its folder (session_project); without
        it the session's own project name is used."""
        ok = {"decision": "ok"}
        with self.lock:
            a = self._session(key)
        if a is None or a["person_id"] is None or principal is None:
            return ok
        project = project or a["project"]
        may = self.store.may_write_project(project, principal)
        with self.lock:
            if not (may and self.store._registered(project)):
                return ok
            self._git_since_start(key, a, self._row(key, a, project), project)
            row = self._row(key, a, project)
            if row["blocked_at"] is not None or row["signal_at"] is None \
                    or row["signal_at"] <= row["started"]:
                return ok
            last = self._last_write(project, key, a["person_id"])
            if last is not None and last >= row["signal_at"]:
                return ok
            if commit:
                self.db.execute("UPDATE done_checks SET blocked_at=? WHERE key=?", (now, key))
        return {"decision": "block", "reason": (
            f"You did work in {project} ({row['signal']}) but its state note was not updated."
            f" Call state_write(\"{project}\", text) with the five headings (What it is, Where"
            " it stands, Decisions, Open problems, How to run and test), updating what changed,"
            " then stop. If nothing changed in the project, call status(\"done: no state"
            " change\") and stop.")}

    def git_seen(self, principal: Principal, device_id: int, git: dict,
                 folders: dict) -> List[str]:
        now = self.clock()
        changed: List[str] = []
        if not isinstance(git, dict):
            return changed
        with self.lock:
            dev = self.db.execute("SELECT host, os_user FROM devices WHERE id=?",
                                  (device_id,)).fetchone()
            if dev is None:
                return changed
            host, os_user = dev["host"], dev["os_user"]
            person_id = None if principal.shared else principal.person_id
            for cwd, g in git.items():
                if not isinstance(g, dict):
                    continue
                branch, subject = g.get("branch") or "", g.get("subject") or ""
                dirty = _dirty(g.get("dirty"))
                last = self.db.execute(
                    "SELECT branch, dirty, subject FROM git_facts WHERE host=? AND os_user=?"
                    " AND cwd=? ORDER BY id DESC LIMIT 1", (host, os_user, cwd)).fetchone()
                if last is not None and (last["branch"], last["dirty"], last["subject"]) == (
                        branch, dirty, subject):
                    continue
                project = self.project_of(cwd, folders)
                self.db.execute(
                    "INSERT INTO git_facts (project, host, os_user, person_id, cwd, branch,"
                    " dirty, subject, time) VALUES (?,?,?,?,?,?,?,?,?)",
                    (project, host, os_user, person_id, cwd, branch, dirty, subject, now))
                if project not in changed:
                    changed.append(project)
                kinds = []
                if last is not None and last["subject"] != subject:
                    kinds.append("commit")
                if last is not None and last["dirty"] == 1 and dirty == 0:
                    kinds.append("clean")
                keys = [r["key"] for r in self.db.execute(
                    "SELECT key FROM agents WHERE device_id=? AND cwd=? AND last_seen>?",
                    (device_id, cwd, now - ONLINE_S))]
                for key in keys:
                    for kind in kinds:
                        self._signal(key, kind, project=project)
                    # seen live: the row moves on, so a later look never takes this fact
                    # (or a newer one with the same subject) for a fresh commit
                    self.db.execute("UPDATE done_checks SET last_subject=?, last_dirty=?"
                                    " WHERE key=?", (subject, dirty, key))
        return changed

    def git_recent(self, project: str, days: int = 7, now: Optional[float] = None) -> List[dict]:
        """The newest 20 changes of branch or subject (a dirty flip is not activity); a
        folder's first sighting counts."""
        now = self.clock() if now is None else now
        with self.lock:
            rows = self.db.execute(
                "SELECT g.host, g.branch, g.subject, g.dirty, g.time, g.os_user, p.name AS person"
                " FROM (SELECT *, LAG(branch) OVER w AS prev_branch, LAG(subject) OVER w AS"
                " prev_subject, LAG(id) OVER w AS prev_id FROM git_facts"
                " WINDOW w AS (PARTITION BY host, os_user, cwd ORDER BY id)) g"
                " LEFT JOIN people p ON p.id=g.person_id"
                " WHERE g.project=? AND g.time>? AND (g.prev_id IS NULL OR"
                " g.branch IS NOT g.prev_branch OR g.subject IS NOT g.prev_subject)"
                " ORDER BY g.time DESC, g.id DESC LIMIT ?",
                (project, now - days * 86400, RECENT_LIMIT)).fetchall()
        return [{"person": r["person"] or r["os_user"], "host": r["host"], "branch": r["branch"],
                 "subject": r["subject"], "dirty": None if r["dirty"] is None else bool(r["dirty"]),
                 "time": r["time"]} for r in rows]

    def branches_with_sessions(self, project: str, now: Optional[float] = None) -> Dict[str, List[str]]:
        now = self.clock() if now is None else now
        out: Dict[str, List[str]] = {}
        with self.lock:
            rows = self.db.execute(
                "SELECT a.name, (SELECT g.branch FROM git_facts g WHERE g.host=a.host AND"
                " g.os_user=a.os_user AND g.cwd=a.cwd ORDER BY g.id DESC LIMIT 1) AS branch"
                " FROM agents a WHERE a.project=? AND a.last_seen>? ORDER BY a.name",
                (project, now - ONLINE_S)).fetchall()
        for r in rows:
            if r["branch"] is not None:
                out.setdefault(r["branch"], []).append(r["name"])
        return out

    def state_written(self, key: Optional[str], project: str, person_id: Optional[int],
                      now: float) -> None:
        """Record a write to a project's state note, for the done check."""
        with self.lock:
            self.db.execute("INSERT INTO state_writes (project, key, person_id, time)"
                            " VALUES (?,?,?,?)", (project, key, person_id, now))
            self.db.commit()

    def prune(self, now: float) -> Dict[str, int]:
        """Delete git facts older than 30 days, done checks of sessions offline (or gone) more
        than 30 days, and state writes older than 30 days. Counts per table."""
        with self.lock:
            git = self.db.execute("DELETE FROM git_facts WHERE time<?",
                                  (now - GIT_KEEP_S,)).rowcount
            done = self.db.execute(
                "DELETE FROM done_checks WHERE NOT EXISTS (SELECT 1 FROM agents a WHERE"
                " a.key=done_checks.key AND a.last_seen>=?)", (now - DONE_KEEP_S,)).rowcount
            writes = self.db.execute("DELETE FROM state_writes WHERE time<?",
                                     (now - DONE_KEEP_S,)).rowcount
            self.db.commit()
        return {"git_facts": git, "done_checks": done, "state_writes": writes}


def prune(store: Store, now: float) -> Dict[str, int]:
    return MemoryStore(store).prune(now)
