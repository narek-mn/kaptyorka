"""Rules: versioned team rules, skill files, rule proposals and conflicts, kept in the hub.
A RulesStore shares the Store's database, lock and clock."""
import hashlib
import re
from typing import Dict, List, Optional

from kaptyorka.store import KaptyorkaError, Principal, Store

SCOPE = "team"
NAME_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
MAX_NAME = 48
MAX_TEXT = 20000
MAX_SKILLS = 50
MAX_PROPOSAL = 2000
MAX_EVIDENCE = 4000
MAX_CONFLICT = 1000
MAX_OPEN = 20
MAX_CONFLICTS = 20  # per session
HEADING = "Team rules (project rules in this repo override these; these override your personal rules):"
TRAILER = ("If your person corrects you the same way twice, call propose_rule(text, evidence). "
           "If two rules clash and you must ask your person, call conflict(rule_a, rule_b) "
           "so the admin can fix the text.")

SCHEMA = """
CREATE TABLE IF NOT EXISTS rules (
    id INTEGER PRIMARY KEY, scope TEXT NOT NULL, version INTEGER NOT NULL,
    text TEXT NOT NULL, author TEXT NOT NULL, time REAL NOT NULL, UNIQUE(scope, version));
CREATE TABLE IF NOT EXISTS skills (
    scope TEXT NOT NULL, name TEXT NOT NULL, text TEXT NOT NULL, author TEXT NOT NULL,
    time REAL NOT NULL, PRIMARY KEY(scope, name));
CREATE TABLE IF NOT EXISTS proposals (
    id INTEGER PRIMARY KEY, person_id INTEGER NOT NULL, session_key TEXT NOT NULL,
    text TEXT NOT NULL, evidence TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('open','accepted','rejected')),
    reason TEXT NOT NULL DEFAULT '', decided_by TEXT NOT NULL DEFAULT '',
    time REAL NOT NULL, decided_at REAL);
CREATE TABLE IF NOT EXISTS conflicts (
    id INTEGER PRIMARY KEY, person_id INTEGER NOT NULL, session_key TEXT NOT NULL,
    project TEXT NOT NULL, rule_a TEXT NOT NULL, rule_b TEXT NOT NULL, time REAL NOT NULL);
"""


def ensure_schema(db) -> None:
    db.executescript(SCHEMA)


def rules_text(text: str) -> str:
    if not text:
        return ""
    return HEADING + "\n\n" + text.strip() + "\n\n" + TRAILER


def check_name(name: str) -> str:
    if not name or len(name) > MAX_NAME or not NAME_RE.match(name):
        raise KaptyorkaError("skill name must be 1-48 characters of lowercase letters, "
                             "digits and single hyphens")
    return name


def _check_scope(scope: str) -> None:
    if scope != SCOPE:
        raise KaptyorkaError("only team rules are kept in the hub; "
                             "project rules live in the project repo")


def _check_text(text: str, what: str = "rules") -> None:
    if len(text) > MAX_TEXT:
        raise KaptyorkaError(f"{what} text too long: limit {MAX_TEXT}")


class RulesStore:
    def __init__(self, store: Store):
        self.store = store
        self.db = store.db
        self.lock = store.lock
        self.clock = store.clock

    def _current(self, scope: str) -> dict:
        r = self.db.execute("SELECT text, version, author, time FROM rules WHERE scope=?"
                            " ORDER BY version DESC LIMIT 1", (scope,)).fetchone()
        return dict(r) if r else {"text": "", "version": 0, "author": "", "time": 0.0}

    def current(self, scope: str = SCOPE) -> dict:
        with self.lock:
            return self._current(scope)

    def set_rules(self, text: str, by: str, scope: str = SCOPE) -> int:
        _check_scope(scope)
        _check_text(text)
        text = text.strip()
        with self.lock:
            cur = self._current(scope)
            if text == cur["text"].strip():
                return cur["version"]
            n = cur["version"] + 1
            self.db.execute("INSERT INTO rules (scope, version, text, author, time)"
                            " VALUES (?,?,?,?,?)", (scope, n, text, by, self.clock()))
        self.store.log_add(by, "rules", f"team rules v{n} by {by}")
        return n

    def history(self, scope: str = SCOPE, limit: int = 20) -> List[dict]:
        with self.lock:
            rows = self.db.execute(
                "SELECT version, text, author, time FROM rules WHERE scope=?"
                " ORDER BY version DESC LIMIT ?", (scope, limit)).fetchall()
        return [dict(r) for r in rows]

    def _skills(self, scope: str) -> List[dict]:
        rows = self.db.execute("SELECT name, text, author, time FROM skills WHERE scope=?"
                               " ORDER BY name", (scope,)).fetchall()
        return [dict(r) for r in rows]

    def skills(self, scope: str = SCOPE) -> List[dict]:
        with self.lock:
            return self._skills(scope)

    def set_skills(self, items: Dict[str, str], by: str, scope: str = SCOPE) -> dict:
        _check_scope(scope)
        if len(items) > MAX_SKILLS:
            raise KaptyorkaError(f"too many skills: limit {MAX_SKILLS}")
        for name, text in items.items():
            check_name(name)
            _check_text(text, "skill")
        with self.lock:
            old = {r["name"]: r["text"] for r in self._skills(scope)}
            added = sorted(n for n in items if n not in old)
            changed = sorted(n for n in items if n in old and old[n] != items[n])
            removed = sorted(n for n in old if n not in items)
            now = self.clock()
            for n in removed:
                self.db.execute("DELETE FROM skills WHERE scope=? AND name=?", (scope, n))
            for n in added + changed:
                self.db.execute("INSERT OR REPLACE INTO skills (scope, name, text, author, time)"
                                " VALUES (?,?,?,?,?)", (scope, n, items[n], by, now))
        summary = {"added": added, "changed": changed, "removed": removed}
        if added or changed or removed:
            self.store.log_add(by, "skills", str(summary))
        return summary

    def skills_version(self, scope: str = SCOPE) -> str:
        rows = self.skills(scope)
        if not rows:
            return ""
        blob = "\n".join(f"{r['name']}\0{r['text']}" for r in rows)
        return hashlib.sha256(blob.encode()).hexdigest()[:12]

    def propose(self, principal: Principal, session_key: str, text: str, evidence: str) -> int:
        if len(text) > MAX_PROPOSAL:
            raise KaptyorkaError(f"proposal too long: limit {MAX_PROPOSAL}")
        if len(evidence) > MAX_EVIDENCE:
            raise KaptyorkaError(f"evidence too long: limit {MAX_EVIDENCE}")
        with self.lock:
            n = self.db.execute("SELECT COUNT(*) FROM proposals WHERE person_id=?"
                                " AND status='open'", (principal.person_id,)).fetchone()[0]
            if n >= MAX_OPEN:
                raise KaptyorkaError(f"you have {MAX_OPEN} open proposals; wait for the admin")
            pid = self.db.execute(
                "INSERT INTO proposals (person_id, session_key, text, evidence, status, time)"
                " VALUES (?,?,?,?,'open',?)",
                (principal.person_id, session_key, text, evidence, self.clock())).lastrowid
        self.store.log_add(principal.person_name, "propose", f"proposal {pid}: {text[:80]}")
        return pid

    def proposals(self, status: Optional[str] = None, limit: int = 100,
                  person_id: Optional[int] = None) -> List[dict]:
        q = ("SELECT p.*, pe.name AS person FROM proposals p"
             " JOIN people pe ON pe.id = p.person_id")
        where, args = [], []
        if status:
            where.append("p.status=?")
            args.append(status)
        if person_id is not None:
            where.append("p.person_id=?")
            args.append(person_id)
        if where:
            q += " WHERE " + " AND ".join(where)
        q += " ORDER BY p.id DESC LIMIT ?"
        with self.lock:
            rows = self.db.execute(q, args + [limit]).fetchall()
        return [dict(r) for r in rows]

    def decide(self, proposal_id: int, accept: bool, reason: str, by: str) -> dict:
        with self.lock:
            row = self.db.execute(
                "SELECT p.*, pe.name AS person FROM proposals p"
                " JOIN people pe ON pe.id = p.person_id WHERE p.id=?",
                (proposal_id,)).fetchone()
            if row is None:
                raise KaptyorkaError("no such proposal")
            if row["status"] != "open":
                raise KaptyorkaError("proposal already decided")
            row = dict(row)
            cur = self._current(SCOPE)["text"]
        if accept:  # set_rules locks itself; the lock is not reentrant
            self.set_rules((cur.rstrip() + "\n\n" if cur.strip() else "") + row["text"], by)
        status = "accepted" if accept else "rejected"
        now = self.clock()
        with self.lock:
            done = self.db.execute(
                "UPDATE proposals SET status=?, reason=?, decided_by=?, decided_at=?"
                " WHERE id=? AND status='open'", (status, reason, by, now, proposal_id))
            if done.rowcount == 0:
                raise KaptyorkaError("proposal already decided")
        row.update(status=status, reason=reason, decided_by=by, decided_at=now)
        self.store.log_add(by, "proposal", f"proposal {proposal_id} {status}")
        return row

    def conflict(self, principal: Principal, session_key: str, project: str,
                 rule_a: str, rule_b: str) -> int:
        if len(rule_a) > MAX_CONFLICT or len(rule_b) > MAX_CONFLICT:
            raise KaptyorkaError(f"rule text too long: limit {MAX_CONFLICT}")
        with self.lock:
            n = self.db.execute("SELECT COUNT(*) FROM conflicts WHERE session_key=?",
                                (session_key,)).fetchone()[0]
            if n >= MAX_CONFLICTS:
                raise KaptyorkaError(f"this session has logged {MAX_CONFLICTS} conflicts;"
                                     " ask your person to fix the rules")
            cid = self.db.execute(
                "INSERT INTO conflicts (person_id, session_key, project, rule_a, rule_b, time)"
                " VALUES (?,?,?,?,?,?)",
                (principal.person_id, session_key, project, rule_a, rule_b,
                 self.clock())).lastrowid
        self.store.log_add(principal.person_name, "conflict", f"conflict {cid} in {project}")
        return cid

    def conflicts(self, limit: int = 100) -> List[dict]:
        with self.lock:
            rows = self.db.execute(
                "SELECT c.*, pe.name AS person FROM conflicts c"
                " JOIN people pe ON pe.id = c.person_id ORDER BY c.id DESC LIMIT ?",
                (limit,)).fetchall()
        return [dict(r) for r in rows]
