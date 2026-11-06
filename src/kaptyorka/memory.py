"""Memory: what the team's machines did, kept as history. Git facts from heartbeats live
here. Kept out of store.py; a MemoryStore shares the Store's database, lock and clock."""
import os
import re
from typing import Dict, List, Optional

from kaptyorka.store import ONLINE_S, Principal, Store

GIT_KEEP_S = 30 * 86400  # git history older than this is pruned

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
        cwd = cwd.rstrip("/") or cwd
        for project, folder in (folders or {}).items():
            folder = folder.rstrip("/")
            if cwd == folder or (cwd.startswith(folder + "--") and _WORKTREE.sub("", cwd) == folder):
                return project
        return _WORKTREE.sub("", os.path.basename(cwd))

    def _signal(self, key: str, kind: str) -> None:
        """Note a work signal for a session. Caller holds the lock."""
        now = self.clock()
        a = self.db.execute("SELECT project, started, last_seen FROM agents WHERE key=?",
                            (key,)).fetchone()
        if a is None:
            return
        self.db.execute(
            "INSERT INTO done_checks (key, project, started, signal_at, signal) VALUES (?,?,?,?,?)"
            " ON CONFLICT(key) DO UPDATE SET signal_at=excluded.signal_at,"
            " signal=excluded.signal", (key, a["project"], a["started"] or a["last_seen"], now, kind))

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
                if last is None:
                    continue
                kinds = []
                if last["subject"] != subject:
                    kinds.append("commit")
                if last["dirty"] == 1 and dirty == 0:
                    kinds.append("clean")
                if kinds:
                    keys = [r["key"] for r in self.db.execute(
                        "SELECT key FROM agents WHERE device_id=? AND cwd=? AND last_seen>?",
                        (device_id, cwd, now - ONLINE_S))]
                    for key in keys:
                        for kind in kinds:
                            self._signal(key, kind)
        return changed

    def git_recent(self, project: str, days: int = 7, now: Optional[float] = None) -> List[dict]:
        now = self.clock() if now is None else now
        with self.lock:
            rows = self.db.execute(
                "SELECT g.host, g.branch, g.subject, g.dirty, g.time, g.os_user, p.name AS person"
                " FROM git_facts g LEFT JOIN people p ON p.id=g.person_id"
                " WHERE g.project=? AND g.time>? ORDER BY g.time DESC, g.id DESC",
                (project, now - days * 86400)).fetchall()
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

    def prune(self, now: float) -> int:
        with self.lock:
            return self.db.execute("DELETE FROM git_facts WHERE time<?",
                                   (now - GIT_KEEP_S,)).rowcount


def prune(store: Store, now: float) -> int:
    return MemoryStore(store).prune(now)
