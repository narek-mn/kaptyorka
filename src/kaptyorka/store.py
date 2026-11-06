"""SQLite storage for the hub: agents, messages, notes, people and their access."""
import getpass
import hashlib
import re
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from kaptyorka import paths

ONLINE_S = 30
LEASE_S = 60
MAX_TEXT = 32_000
MAX_ABOUT = 500
MAX_LABEL = 40
MAX_FILE = 5_000_000
FILE_TYPES = ("png", "jpg", "jpeg", "gif", "webp", "svg", "txt", "md", "log", "json", "csv", "pdf")
RATE = (30, 60)
SCHEMA_VERSION = 2
INVITE_TTL_S = 7 * 86400
ROLES = ("admin", "member", "manager")
PROJECT_ROLES = ("read", "write", "manage")
PROJECT_NAME = re.compile(r"^[A-Za-z0-9._-]{1,64}$")

SCHEMA = """
CREATE TABLE IF NOT EXISTS agents (
    key TEXT PRIMARY KEY, name TEXT NOT NULL, machine TEXT NOT NULL,
    tool TEXT NOT NULL, project TEXT NOT NULL, status TEXT NOT NULL DEFAULT '',
    activity TEXT NOT NULL DEFAULT '', last_seen REAL NOT NULL,
    label TEXT NOT NULL DEFAULT '', person_id INTEGER, os_user TEXT NOT NULL DEFAULT '',
    host TEXT NOT NULL DEFAULT '', started REAL);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT, sender_key TEXT NOT NULL,
    sender_name TEXT NOT NULL, to_key TEXT, to_name TEXT NOT NULL,
    machine TEXT NOT NULL, tool TEXT NOT NULL, project TEXT NOT NULL,
    text TEXT NOT NULL, created REAL NOT NULL, read_at REAL,
    push_state TEXT NOT NULL DEFAULT 'new', offered_at REAL, file_id INTEGER,
    kind TEXT NOT NULL DEFAULT 'message', knock_id INTEGER);
CREATE TABLE IF NOT EXISTS notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT, topic TEXT NOT NULL,
    text TEXT NOT NULL, author TEXT NOT NULL, time REAL NOT NULL, project TEXT);
CREATE TABLE IF NOT EXISTS schema (version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS people (
    id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('admin','member','manager')), created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS devices (
    id INTEGER PRIMARY KEY, person_id INTEGER NOT NULL, name TEXT NOT NULL,
    token_hash TEXT UNIQUE NOT NULL, secret_hash TEXT NOT NULL DEFAULT '',
    created REAL NOT NULL, last_used REAL, revoked_at REAL,
    shared INTEGER NOT NULL DEFAULT 0, legacy INTEGER NOT NULL DEFAULT 0,
    host TEXT NOT NULL DEFAULT '', os_user TEXT NOT NULL DEFAULT '',
    version TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS invites (
    code TEXT PRIMARY KEY, person_id INTEGER NOT NULL, created REAL NOT NULL,
    expires REAL NOT NULL, used_at REAL);
CREATE TABLE IF NOT EXISTS projects (
    name TEXT PRIMARY KEY, description TEXT NOT NULL DEFAULT '',
    repo TEXT NOT NULL DEFAULT '', created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS project_access (
    project TEXT NOT NULL, person_id INTEGER NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('read','write','manage')),
    PRIMARY KEY(project, person_id));
CREATE TABLE IF NOT EXISTS machines (
    host TEXT PRIMARY KEY, owner_person_id INTEGER, first_seen REAL NOT NULL);
CREATE TABLE IF NOT EXISTS machine_access (
    host TEXT NOT NULL, person_id INTEGER NOT NULL, PRIMARY KEY(host, person_id));
CREATE TABLE IF NOT EXISTS doors (
    id INTEGER PRIMARY KEY, a_key TEXT NOT NULL, a_person INTEGER NOT NULL,
    b_key TEXT NOT NULL, b_person INTEGER NOT NULL,
    scope TEXT NOT NULL CHECK(scope IN ('session','group')),
    opened_at REAL NOT NULL, closed_at REAL, opened_by INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS knocks (
    id INTEGER PRIMARY KEY, from_key TEXT NOT NULL, from_person INTEGER NOT NULL,
    to_key TEXT, to_person INTEGER NOT NULL, about TEXT NOT NULL, created REAL NOT NULL,
    answered_at REAL, answer TEXT);
CREATE TABLE IF NOT EXISTS requests (
    id INTEGER PRIMARY KEY, person_id INTEGER NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('project','machine')), target TEXT NOT NULL,
    why TEXT NOT NULL DEFAULT '', created REAL NOT NULL, decided_at REAL, decision TEXT,
    decided_by INTEGER);
CREATE TABLE IF NOT EXISTS log (
    id INTEGER PRIMARY KEY, time REAL NOT NULL, person TEXT NOT NULL, kind TEXT NOT NULL,
    detail TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS files (
    id INTEGER PRIMARY KEY, message_id INTEGER, name TEXT NOT NULL, size INTEGER NOT NULL,
    sha256 TEXT NOT NULL, created REAL NOT NULL, person_id INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS rekeys (
    device_name TEXT PRIMARY KEY, token TEXT NOT NULL, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""

# columns added after 0.1: an older database gets them by ALTER TABLE when it is opened
ADDED_COLUMNS = [
    ("agents", "label", "TEXT NOT NULL DEFAULT ''"),
    ("agents", "person_id", "INTEGER"),
    ("agents", "os_user", "TEXT NOT NULL DEFAULT ''"),
    ("agents", "host", "TEXT NOT NULL DEFAULT ''"),
    ("agents", "started", "REAL"),
    ("messages", "file_id", "INTEGER"),
    ("messages", "kind", "TEXT NOT NULL DEFAULT 'message'"),
    ("messages", "knock_id", "INTEGER"),
    ("notes", "project", "TEXT"),
    ("devices", "version", "TEXT NOT NULL DEFAULT ''"),
]


class KaptyorkaError(Exception):
    """Error whose message is shown to the agent."""


def _check_length(text: str) -> None:
    if len(text) > MAX_TEXT:
        raise KaptyorkaError(f"message too long: {len(text)} characters, limit {MAX_TEXT}")


def _sender(m) -> str:
    """Sender as an agent sees it; a person on the page is marked so the agent knows to
    reply to that name."""
    web = m["sender_key"].startswith("web:")
    return m["sender_name"] + (" (web page)" if web else "")


def _one_line(text: str) -> str:
    return " ".join(text.split())


def _person(name: str) -> str:
    name = name.strip()
    if not name or len(name) > 40:
        raise KaptyorkaError("name must be 1 to 40 characters")
    return name


@dataclass
class Agent:
    key: str
    name: str
    machine: str
    tool: str
    project: str
    status: str
    activity: str
    last_seen: float
    online: bool
    label: str = ""
    person_id: Optional[int] = None
    os_user: str = ""
    host: str = ""
    started: Optional[float] = None


@dataclass
class Principal:
    """Who a request is from: the person behind a device token."""
    person_id: int
    person_name: str
    role: str  # admin | member | manager
    device_id: int
    device_name: str
    shared: bool
    legacy: bool

    def is_admin(self) -> bool:
        return self.role == "admin"


@dataclass
class Message:
    id: int
    sender: str
    to: str
    text: str
    created: float
    read: bool
    from_web: bool = False
    to_web: bool = False
    kind: str = "message"
    file: Optional[dict] = None  # {id, name, size} of the attachment


@dataclass
class Note:
    topic: str
    text: str
    author: str
    time: float
    project: Optional[str] = None


class Store:
    def __init__(self, path: Path, clock: Callable[[], float] = time.time,
                 legacy_token: Optional[str] = None):
        self.clock = clock
        self.lock = threading.Lock()
        self.db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        self._migrate(legacy_token)
        self.sends: Dict[str, List[float]] = {}

    def _migrate(self, legacy_token):
        """Bring an older database up to SCHEMA_VERSION. A 0.1 hub had one shared token: it
        becomes the `legacy` device of an admin person, who owns every agent seen so far."""
        self.db.execute("BEGIN IMMEDIATE")
        try:
            for table, column, kind in ADDED_COLUMNS:
                have = {r["name"] for r in self.db.execute(f"PRAGMA table_info({table})")}
                if column not in have:
                    self.db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")
            no_people = self.db.execute("SELECT 1 FROM people LIMIT 1").fetchone() is None
            if legacy_token and no_people:
                now = self.clock()
                admin = self.db.execute(
                    "INSERT INTO people (name, role, created) VALUES (?, 'admin', ?)",
                    (getpass.getuser(), now)).lastrowid
                self.db.execute(
                    "INSERT INTO devices (person_id, name, token_hash, created, legacy)"
                    " VALUES (?, 'legacy', ?, ?, 1)",
                    (admin, hashlib.sha256(legacy_token.encode()).hexdigest(), now))
                self.db.execute("UPDATE agents SET person_id=?", (admin,))
                self.db.execute("UPDATE notes SET project=topic WHERE project IS NULL"
                                " AND topic IN (SELECT DISTINCT project FROM agents)")
            if self._version() != SCHEMA_VERSION:
                self.db.execute("DELETE FROM schema")
                self.db.execute("INSERT INTO schema (version) VALUES (?)", (SCHEMA_VERSION,))
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def _version(self):
        row = self.db.execute("SELECT MAX(version) AS v FROM schema").fetchone()
        return row["v"] or 0

    def schema_version(self) -> int:
        with self.lock:
            return self._version()

    def close(self) -> None:
        with self.lock:
            self.db.close()

    # people

    def people(self) -> List[dict]:
        with self.lock:
            rows = self.db.execute("SELECT id, name, role, created FROM people ORDER BY id")
            return [dict(r) for r in rows]

    def devices(self, person_id: int) -> List[dict]:
        with self.lock:
            rows = self.db.execute("SELECT * FROM devices WHERE person_id=? ORDER BY id",
                                   (person_id,)).fetchall()
            return [{k: r[k] for k in r.keys() if k not in ("token_hash", "secret_hash")}
                    for r in rows]

    def authenticate(self, token: str, machine: Optional[str] = None) -> Optional[Principal]:
        """The principal for a device token, or None when it is unknown or revoked. The 0.1
        shared token (the legacy device) maps a machine to a person through `legacy:<machine>`
        in settings, and to the device's own person otherwise."""
        digest = hashlib.sha256(token.encode()).hexdigest()
        with self.lock:
            dev = self.db.execute("SELECT * FROM devices WHERE token_hash=? AND revoked_at IS NULL",
                                  (digest,)).fetchone()
            if dev is None:
                return None
            person_id, device_name = dev["person_id"], dev["name"]
            who = None
            if dev["legacy"] and machine:
                mapped = self.db.execute("SELECT value FROM settings WHERE key=?",
                                         (f"legacy:{machine}",)).fetchone()
                try:  # a bad value or a deleted person counts as unmapped
                    mapped_id = int(mapped["value"]) if mapped is not None else None
                except ValueError:
                    mapped_id = None
                if mapped_id is not None:
                    who = self.db.execute("SELECT name, role FROM people WHERE id=?",
                                          (mapped_id,)).fetchone()
                    if who is not None:
                        person_id, device_name = mapped_id, machine
            if who is None:
                who = self.db.execute("SELECT name, role FROM people WHERE id=?",
                                      (person_id,)).fetchone()
            if who is None:
                return None
            self.db.execute("UPDATE devices SET last_used=? WHERE id=?", (self.clock(), dev["id"]))
            return Principal(person_id, who["name"], who["role"], dev["id"], device_name,
                             bool(dev["shared"]), bool(dev["legacy"]))

    def person_create(self, name: str, role: str = "member") -> int:
        name = _person(name)
        if role not in ROLES:
            raise KaptyorkaError("role must be admin, member or manager")
        with self.lock:
            if self.db.execute("SELECT 1 FROM people WHERE name=?", (name,)).fetchone():
                raise KaptyorkaError(f"a person named {name} already exists")
            if role == "admin" and self.db.execute(
                    "SELECT 1 FROM people WHERE role='admin'").fetchone():
                raise KaptyorkaError("there is already an admin")
            pid = self.db.execute("INSERT INTO people (name, role, created) VALUES (?,?,?)",
                                  (name, role, self.clock())).lastrowid
        self.log_add(name, "person", f"created as {role}")
        return pid

    def person_by_name(self, name: str) -> Optional[dict]:
        with self.lock:
            row = self.db.execute("SELECT id, name, role, created FROM people WHERE name=?",
                                  (name.strip(),)).fetchone()
            return dict(row) if row else None

    def invite_create(self, person_id: int, ttl_s: float = INVITE_TTL_S) -> str:
        code = secrets.token_urlsafe(16)
        now = self.clock()
        with self.lock:
            row = self.db.execute("SELECT name FROM people WHERE id=?", (person_id,)).fetchone()
            if row is None:
                raise KaptyorkaError("no such person")
            self.db.execute("INSERT INTO invites (code, person_id, created, expires)"
                            " VALUES (?,?,?,?)", (code, person_id, now, now + ttl_s))
        self.log_add(row["name"], "invite", "invite created")
        return code

    def invite_valid(self, code: str) -> bool:
        """True when the code exists, is unused and has not expired."""
        with self.lock:
            row = self.db.execute("SELECT expires, used_at FROM invites WHERE code=?",
                                  (code,)).fetchone()
            return row is not None and row["used_at"] is None and row["expires"] > self.clock()

    def invite_redeem(self, code: str, device_name: str, host: str = "",
                      os_user: str = "") -> Tuple[str, str]:
        """Use an invite once: a new device for its person. Returns (token, signing secret);
        only their hashes are kept."""
        device_name = device_name.strip()
        if not device_name or len(device_name) > 80:
            raise KaptyorkaError("device name must be 1 to 80 characters")
        token, secret = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        now = self.clock()
        with self.lock:
            inv = self.db.execute("SELECT * FROM invites WHERE code=?", (code,)).fetchone()
            if inv is None:
                raise KaptyorkaError("no such invite")
            if inv["used_at"] is not None:
                raise KaptyorkaError("invite already used")
            if inv["expires"] <= now:
                raise KaptyorkaError("invite expired")
            who = self.db.execute("SELECT name FROM people WHERE id=?",
                                  (inv["person_id"],)).fetchone()
            self.db.execute("UPDATE invites SET used_at=? WHERE code=?", (now, code))
            self.db.execute(
                "INSERT INTO devices (person_id, name, token_hash, secret_hash, created, host, os_user)"
                " VALUES (?,?,?,?,?,?,?)",
                (inv["person_id"], device_name, hashlib.sha256(token.encode()).hexdigest(),
                 hashlib.sha256(secret.encode()).hexdigest(), now, host[:80], os_user[:80]))
        self.log_add(who["name"], "join", f"device {device_name}")
        return token, secret

    def device_revoke(self, device_id: int) -> None:
        with self.lock:
            cur = self.db.execute("UPDATE devices SET revoked_at=? WHERE id=? AND revoked_at IS NULL",
                                  (self.clock(), device_id))
            row = self.db.execute("SELECT d.name, p.name AS person FROM devices d"
                                  " JOIN people p ON p.id=d.person_id WHERE d.id=?",
                                  (device_id,)).fetchone()
        if cur.rowcount and row:
            self.log_add(row["person"], "revoke", f"device {row['name']}")

    # machines

    def machines(self, principal: Principal) -> List[dict]:
        """Machines (hosts) with their devices and online session count. An admin sees all; anyone
        else sees the hosts they own, have a device on, or were given."""
        with self.lock:
            now = self.clock()
            out = []
            for m in self.db.execute("SELECT m.host, m.owner_person_id, p.name AS owner FROM machines m"
                                     " LEFT JOIN people p ON p.id=m.owner_person_id ORDER BY m.host"):
                devs = [{"id": d["id"], "name": d["name"], "person": d["person"],
                         "person_id": d["person_id"], "os_user": d["os_user"],
                         "shared": bool(d["shared"]), "version": d["version"],
                         "last_used": d["last_used"]}
                        for d in self.db.execute(
                            "SELECT d.*, p.name AS person FROM devices d JOIN people p"
                            " ON p.id=d.person_id WHERE d.host=? AND d.revoked_at IS NULL"
                            " ORDER BY d.id", (m["host"],))]
                if not (principal.is_admin() or m["owner_person_id"] == principal.person_id
                        or any(d["person_id"] == principal.person_id for d in devs)
                        or self._granted(m["host"], principal.person_id)):
                    continue
                online = self.db.execute("SELECT COUNT(*) FROM agents WHERE host=? AND last_seen>?",
                                         (m["host"], now - ONLINE_S)).fetchone()[0]
                for d in devs:
                    del d["person_id"]
                out.append({"host": m["host"], "owner": m["owner"],
                            "owner_person_id": m["owner_person_id"], "devices": devs,
                            "online": online})
            return out

    def _granted(self, host: str, person_id: int) -> bool:
        return self.db.execute("SELECT 1 FROM machine_access WHERE host=? AND person_id=?",
                               (host, person_id)).fetchone() is not None

    def may_use(self, host: str, person_id: int) -> bool:
        """True for an admin, the machine's owner, or a grant. A device reporting the host is not
        enough: the host name is self-reported."""
        with self.lock:
            p = self.db.execute("SELECT role FROM people WHERE id=?", (person_id,)).fetchone()
            if p is not None and p["role"] == "admin":
                return True
            m = self.db.execute("SELECT owner_person_id FROM machines WHERE host=?",
                                (host,)).fetchone()
            if m is not None and m["owner_person_id"] == person_id:
                return True
            return self._granted(host, person_id)

    def _grant_change(self, host: str, person_id: int, grant: bool, by: str) -> None:
        with self.lock:
            if self.db.execute("SELECT 1 FROM machines WHERE host=?", (host,)).fetchone() is None:
                raise KaptyorkaError(f"no such machine: {host}")
            who = self.db.execute("SELECT name FROM people WHERE id=?", (person_id,)).fetchone()
            if who is None:
                raise KaptyorkaError("no such person")
            if grant:
                self.db.execute("INSERT OR IGNORE INTO machine_access (host, person_id) VALUES (?,?)",
                                (host, person_id))
            else:
                self.db.execute("DELETE FROM machine_access WHERE host=? AND person_id=?",
                                (host, person_id))
        self.log_add(by, "grant" if grant else "revoke", f"machine:{host} -> {who['name']}")

    def grant_machine(self, host: str, person_id: int, by: str) -> None:
        self._grant_change(host, person_id, True, by)

    def _grant_machine(self, host: str, person_id: int) -> str:
        """Lock held by the caller. Returns the person's name."""
        if self.db.execute("SELECT 1 FROM machines WHERE host=?", (host,)).fetchone() is None:
            raise KaptyorkaError(f"no such machine: {host}")
        who = self.db.execute("SELECT name FROM people WHERE id=?", (person_id,)).fetchone()
        if who is None:
            raise KaptyorkaError("no such person")
        self.db.execute("INSERT OR IGNORE INTO machine_access (host, person_id) VALUES (?,?)",
                        (host, person_id))
        return who["name"]

    def revoke_machine(self, host: str, person_id: int, by: str) -> None:
        self._grant_change(host, person_id, False, by)

    def device_set_shared(self, device_id: int, shared: bool) -> None:
        with self.lock:
            cur = self.db.execute("UPDATE devices SET shared=? WHERE id=?", (int(shared), device_id))
        if not cur.rowcount:
            raise KaptyorkaError("no such device")

    # projects

    def project_register(self, name: str, description: str = "", repo: str = "", by: str = "",
                         by_person_id: Optional[int] = None) -> None:
        """A registered project: access to it is by role. `by_person_id` gets `manage`."""
        if not PROJECT_NAME.match(name):
            raise KaptyorkaError("project name must be 1 to 64 letters, digits, '.', '_' or '-'")
        with self.lock:
            if self.db.execute("SELECT 1 FROM projects WHERE name=?", (name,)).fetchone():
                raise KaptyorkaError(f"a project named {name} already exists")
            self.db.execute("INSERT INTO projects (name, description, repo, created) VALUES (?,?,?,?)",
                            (name, description, repo, self.clock()))
            if by_person_id is not None:
                self.db.execute("INSERT OR REPLACE INTO project_access (project, person_id, role)"
                                " VALUES (?,?,'manage')", (name, by_person_id))
        self.log_add(by, "project_register", name)

    def _project_target(self, project: str, person_id: int) -> str:
        """The person's name, after checking both the project and the person exist."""
        if self.db.execute("SELECT 1 FROM projects WHERE name=?", (project,)).fetchone() is None:
            raise KaptyorkaError(f"no project named {project}")
        who = self.db.execute("SELECT name FROM people WHERE id=?", (person_id,)).fetchone()
        if who is None:
            raise KaptyorkaError("no such person")
        return who["name"]

    def grant_project(self, project: str, person_id: int, role: str, by: str) -> None:
        if role not in PROJECT_ROLES:
            raise KaptyorkaError("project role must be read, write or manage")
        with self.lock:
            name = self._grant_project(project, person_id, role)
        self.log_add(by, "grant", f"project:{project}:{role} -> {name}")

    def _grant_project(self, project: str, person_id: int, role: str) -> str:
        """Lock held by the caller. Returns the person's name."""
        name = self._project_target(project, person_id)
        self.db.execute("INSERT OR REPLACE INTO project_access (project, person_id, role)"
                        " VALUES (?,?,?)", (project, person_id, role))
        return name

    def revoke_project(self, project: str, person_id: int, by: str) -> None:
        with self.lock:
            name = self._project_target(project, person_id)
            self.db.execute("DELETE FROM project_access WHERE project=? AND person_id=?",
                            (project, person_id))
        self.log_add(by, "revoke", f"project:{project} -> {name}")

    def _role(self, project, person_id):
        p = self.db.execute("SELECT role FROM people WHERE id=?", (person_id,)).fetchone()
        if p is not None and p["role"] == "admin":
            return "manage"
        row = self.db.execute("SELECT role FROM project_access WHERE project=? AND person_id=?",
                              (project, person_id)).fetchone()
        return row["role"] if row else None

    def project_role(self, project: str, person_id: int) -> Optional[str]:
        """The person's role in a project; an admin always manages."""
        with self.lock:
            return self._role(project, person_id)

    def _registered(self, project) -> bool:
        return self.db.execute("SELECT 1 FROM projects WHERE name=?", (project,)).fetchone() is not None

    def _may(self, project, principal, write) -> bool:
        """A registered project goes by role. An unregistered one is only a folder name, which
        two people can share by chance: it belongs to whoever has a session in it."""
        if principal.is_admin():
            return True
        if self._registered(project):
            role = self._role(project, principal.person_id)
            return role in (("write", "manage") if write else PROJECT_ROLES)
        return self.db.execute("SELECT 1 FROM agents WHERE project=? AND person_id=?",
                               (project, principal.person_id)).fetchone() is not None

    def may_read_project(self, project: str, principal: Principal) -> bool:
        with self.lock:
            return self._may(project, principal, False)

    def may_write_project(self, project: str, principal: Principal) -> bool:
        with self.lock:
            return self._may(project, principal, True)

    def projects_for(self, principal: Principal) -> List[dict]:
        """Registered projects the principal has a role in (an admin: all), with the names of
        the sessions online in each folder."""
        with self.lock:
            online = self._online_rows(self.clock())
            out = []
            for p in self.db.execute("SELECT * FROM projects ORDER BY name").fetchall():
                role = self._role(p["name"], principal.person_id)
                if role is None:
                    continue
                out.append({"name": p["name"], "description": p["description"], "repo": p["repo"],
                            "role": role,
                            "working_now": [r["name"] for r in online if r["project"] == p["name"]]})
            return out

    # access requests

    def _request_rows(self, where: str, args: tuple = ()) -> List[dict]:
        rows = self.db.execute(
            "SELECT r.*, p.name AS person, d.name AS decided_by_name FROM requests r"
            " JOIN people p ON p.id=r.person_id LEFT JOIN people d ON d.id=r.decided_by"
            f" WHERE {where} ORDER BY r.id", args)
        return [{"id": r["id"], "person": r["person"], "person_id": r["person_id"],
                 "kind": r["kind"], "target": r["target"], "why": r["why"],
                 "created": r["created"], "decided_at": r["decided_at"],
                 "decision": r["decision"], "decided_by": r["decided_by_name"]} for r in rows]

    def request_access(self, person_id: int, kind: str, target: str, why: str = "") -> int:
        """Ask for read access to a project or for use of a machine. Asking again for the same
        thing while it is pending returns the same id."""
        if kind not in ("project", "machine"):
            raise KaptyorkaError("kind must be project or machine")
        if len(why) > MAX_ABOUT:
            raise KaptyorkaError(f"why is too long (max {MAX_ABOUT} characters)")
        with self.lock:
            who = self.db.execute("SELECT name FROM people WHERE id=?", (person_id,)).fetchone()
            if who is None:
                raise KaptyorkaError("no such person")
            if kind == "project":
                if not self._registered(target):
                    raise KaptyorkaError(f"no project named {target}")
                has = self._role(target, person_id) is not None
            else:
                m = self.db.execute("SELECT owner_person_id FROM machines WHERE host=?",
                                    (target,)).fetchone()
                if m is None:
                    raise KaptyorkaError(f"no machine named {target}")
                has = (self._is_admin(person_id) or m["owner_person_id"] == person_id
                       or self._granted(target, person_id))
            if has:
                raise KaptyorkaError(f"you already have access to {target}")
            row = self.db.execute("SELECT id FROM requests WHERE person_id=? AND kind=? AND target=?"
                                  " AND decided_at IS NULL", (person_id, kind, target)).fetchone()
            if row:
                if why:
                    self.db.execute("UPDATE requests SET why=? WHERE id=?", (why, row["id"]))
                return row["id"]
            rid = self.db.execute("INSERT INTO requests (person_id, kind, target, why, created)"
                                  " VALUES (?,?,?,?,?)",
                                  (person_id, kind, target, why, self.clock())).lastrowid
        self.log_add(who["name"], "request", f"{kind}:{target}")
        return rid

    def requests(self, pending_only: bool = True) -> List[dict]:
        with self.lock:
            return self._request_rows("r.decided_at IS NULL" if pending_only else "1")

    def pending_for(self, person_id: int) -> List[dict]:
        with self.lock:
            return self._request_rows("r.decided_at IS NULL AND r.person_id=?", (person_id,))

    def denied_since(self, person_id: int, since: float) -> List[dict]:
        with self.lock:
            return self._request_rows("r.decision='denied' AND r.person_id=? AND r.decided_at>?",
                                      (person_id, since))

    def decide_request(self, request_id: int, by_person_id: int, approve: bool) -> None:
        """Claim the request first, so a deny racing an approve cannot both win; if the grant
        then fails, the claim is undone. Approving never lowers a project role already held."""
        decision = "approved" if approve else "denied"
        note = ""
        with self.lock:
            row = self.db.execute("SELECT * FROM requests WHERE id=?", (request_id,)).fetchone()
            if row is None:
                raise KaptyorkaError("no such request")
            by = self.db.execute("SELECT name FROM people WHERE id=?", (by_person_id,)).fetchone()
            who = self.db.execute("SELECT name FROM people WHERE id=?", (row["person_id"],)).fetchone()
            if by is None or who is None:
                raise KaptyorkaError("no such person")
            cur = self.db.execute("UPDATE requests SET decided_at=?, decision=?, decided_by=?"
                                  " WHERE id=? AND decided_at IS NULL",
                                  (self.clock(), decision, by_person_id, request_id))
            if not cur.rowcount:
                raise KaptyorkaError("request already decided")
            if approve:
                try:
                    if row["kind"] == "project":
                        have = self.db.execute(
                            "SELECT role FROM project_access WHERE project=? AND person_id=?",
                            (row["target"], row["person_id"])).fetchone()
                        if have is not None and have["role"] != "read":
                            note = f" (already {have['role']})"
                        else:
                            self._grant_project(row["target"], row["person_id"], "read")
                    else:
                        self._grant_machine(row["target"], row["person_id"])
                except KaptyorkaError:
                    self.db.execute("UPDATE requests SET decided_at=NULL, decision=NULL,"
                                    " decided_by=NULL WHERE id=?", (request_id,))
                    raise
        if approve and not note:
            self.log_add(by["name"], "grant", f"{row['kind']}:{row['target']}"
                         f"{':read' if row['kind'] == 'project' else ''} -> {who['name']}")
        self.log_add(by["name"], "request_decided",
                     f"{row['kind']}:{row['target']} -> {who['name']}: {decision}{note}")

    # log

    def log_add(self, person: str, kind: str, detail: str) -> None:
        with self.lock:
            self.db.execute("INSERT INTO log (time, person, kind, detail) VALUES (?,?,?,?)",
                            (self.clock(), person, kind, detail))

    # agents

    def _agent(self, row, now) -> Agent:
        return Agent(row["key"], row["name"], row["machine"], row["tool"], row["project"],
                     row["status"], row["activity"], row["last_seen"],
                     now - row["last_seen"] < ONLINE_S, row["label"], row["person_id"],
                     row["os_user"], row["host"], row["started"])

    def _online_rows(self, now):
        return self.db.execute("SELECT * FROM agents WHERE last_seen > ? ORDER BY rowid",
                               (now - ONLINE_S,)).fetchall()

    def _free_name(self, key, machine, tool, project, now):
        taken = {r["name"] for r in self._online_rows(now) if r["key"] != key}
        base = f"{machine}/{tool}/{project}"
        name, n = base, 2
        while name in taken:
            name, n = f"{base}-{n}", n + 1
        return name

    def _touch(self, key, machine, tool, project, now, activity=None, label=None):
        row = self.db.execute("SELECT * FROM agents WHERE key=?", (key,)).fetchone()
        if row is None:
            name = self._free_name(key, machine, tool, project, now)
            self.db.execute(
                "INSERT INTO agents (key, name, machine, tool, project, activity, last_seen, label,"
                " started) VALUES (?,?,?,?,?,?,?,?,?)",
                (key, name, machine, tool, project, activity or "", now, label or "", now))
            return
        name = row["name"]
        if now - row["last_seen"] >= ONLINE_S:  # was offline: its name may have been taken
            name = self._free_name(key, row["machine"], row["tool"], row["project"], now)
        if activity is None:
            self.db.execute("UPDATE agents SET name=?, last_seen=? WHERE key=?", (name, now, key))
        else:
            self.db.execute("UPDATE agents SET name=?, last_seen=?, activity=? WHERE key=?",
                            (name, now, activity, key))
        if label is not None:
            self.db.execute("UPDATE agents SET label=? WHERE key=?", (label, key))

    def seen(self, key: str, machine: str, tool: str, project: str,
             principal: Optional[Principal] = None) -> Agent:
        with self.lock:
            now = self.clock()
            self._touch(key, machine, tool, project, now)
            if principal is not None:
                # every call, so a legacy machine handed to another person moves at once
                dev = self.db.execute("SELECT host, os_user FROM devices WHERE id=?",
                                      (principal.device_id,)).fetchone()
                self.db.execute(
                    "UPDATE agents SET person_id=?, host=COALESCE(NULLIF(?, ''), host),"
                    " os_user=COALESCE(NULLIF(?, ''), os_user) WHERE key=?",
                    (principal.person_id, dev["host"] if dev else "",
                     dev["os_user"] if dev else "", key))
            row = self.db.execute("SELECT * FROM agents WHERE key=?", (key,)).fetchone()
            return self._agent(row, now)

    def heartbeat(self, machine: str, sessions: List[dict], tools: Sequence[str] = (),
                  principal: Optional[Principal] = None, facts: Optional[dict] = None) -> None:
        """`tools` are the tools whose sessions this list is complete for: the rest of their agents
        on this machine are gone, so their names are free at once. `facts` (host, os_user,
        version) say which computer the sessions run on; an older bridge sends none."""
        facts = facts or {}
        host = str(facts.get("host") or "")[:80]
        os_user = str(facts.get("os_user") or "")[:80]
        with self.lock:
            now = self.clock()
            seen = {s["key"] for s in sessions}
            for r in self._online_rows(now):
                if r["machine"] == machine and r["tool"] in tools and r["key"] not in seen:
                    self.db.execute("UPDATE agents SET last_seen=? WHERE key=?",
                                    (now - ONLINE_S, r["key"]))
            for s in sessions:
                self._touch(s["key"], machine, s["tool"], s["project"], now, s.get("activity"),
                            str(s.get("label") or "")[:MAX_LABEL])
                if host:
                    self.db.execute("UPDATE agents SET host=?, os_user=? WHERE key=?",
                                    (host, os_user, s["key"]))
                if principal is not None and not principal.legacy:  # until it calls a tool
                    self.db.execute("UPDATE agents SET person_id=COALESCE(person_id, ?)"
                                    " WHERE key=?", (principal.person_id, s["key"]))
            if host:
                self.db.execute("INSERT OR IGNORE INTO machines (host, owner_person_id, first_seen)"
                                " VALUES (?,?,?)",
                                (host, principal.person_id if principal else None, now))
                if principal is not None and not principal.legacy:
                    self.db.execute("UPDATE devices SET host=?, os_user=?, version=? WHERE id=?",
                                    (host, os_user, str(facts.get("version") or "")[:40],
                                     principal.device_id))

    def online(self) -> List[Agent]:
        with self.lock:
            now = self.clock()
            self._expire_doors(now)
            return [self._agent(r, now) for r in self._online_rows(now)]

    def agent(self, key: str) -> Optional[Agent]:
        with self.lock:
            row = self.db.execute("SELECT * FROM agents WHERE key=?", (key,)).fetchone()
            return self._agent(row, self.clock()) if row else None

    def set_status(self, key: str, text: str) -> None:
        with self.lock:
            self.db.execute("UPDATE agents SET status=? WHERE key=?", (text, key))

    # messages

    def send(self, sender_key: str, to: str, text: str, file_id: Optional[int] = None) -> int:
        with self.lock:
            now = self.clock()
            sender = self.db.execute("SELECT * FROM agents WHERE key=?", (sender_key,)).fetchone()
            if sender is None:
                raise KaptyorkaError("unknown sender session")
            _check_length(text)
            if file_id is not None:
                f = self.db.execute("SELECT person_id, message_id FROM files WHERE id=?",
                                    (file_id,)).fetchone()
                if f is None or f["person_id"] != sender["person_id"] or f["message_id"] is not None:
                    raise KaptyorkaError("that file is not yours")
            self._rate_check(sender_key, now)
            self._expire_doors(now)
            targets = self._targets(sender_key, to, now)
            if to == "all":
                targets = [t for t in targets if self._may_message(sender, t)]
            elif not all(t["key"].startswith("web:") or self._may_message(sender, t)
                         for t in targets):  # a person on the web page: Task 11 checks those
                raise KaptyorkaError(f"no door to {to}: knock(to, about) first")
            n = self._insert(sender_key, sender["name"], targets, text, now, file_id)
            self._rate_hit(sender_key, now)
            return n

    def _rate_check(self, key, now):
        count, window = RATE
        recent = [t for t in self.sends.get(key, []) if now - t < window]
        if len(recent) >= count:
            raise KaptyorkaError(f"rate limit: at most {count} sends per {window} s")
        self.sends[key] = recent

    def _rate_hit(self, key, now):
        self.sends.setdefault(key, []).append(now)

    def send_from_web(self, name: str, to: str, text: str) -> int:
        """Send as a person on the web page, not an agent: sender key `web:<name>`."""
        name = _person(name)
        with self.lock:
            _check_length(text)
            return self._deliver(f"web:{name}", name, to, text, self.clock())

    def _targets(self, sender_key, to, now):
        online = self._online_rows(now)
        if to == "all":
            return [r for r in online if r["key"] != sender_key]
        match = [r for r in online if r["name"] == to]
        if not match:
            labeled = [r for r in online if r["label"] and r["label"].lower() == to.lower()]
            if len(labeled) > 1:
                raise KaptyorkaError(f"several sessions are named {to}: "
                                     + ", ".join(r["name"] for r in labeled))
            match = labeled
        match = match or self.db.execute(
            "SELECT * FROM agents WHERE name=? ORDER BY last_seen DESC LIMIT 1",
            (to,)).fetchall()
        if match:
            return match[:1]
        people = self._people()
        person = next((p for p in people if p.lower() == to.lower()), None)
        if person is not None:  # a person on the web page: they read replies there
            return [{"key": f"web:{person}", "name": person, "machine": "", "tool": "", "project": ""}]
        names = ", ".join(r["name"] + (f" ({r['label']})" if r["label"] else "")
                          for r in online) or "none"
        extra = f"; people: {', '.join(people)}" if people else ""
        raise KaptyorkaError(f"no session named {to}; online: {names}{extra}")

    def _people(self):
        """Names of people who have written from the web page, newest first."""
        rows = self.db.execute("SELECT sender_name FROM messages WHERE sender_key LIKE 'web:%'"
                               " GROUP BY sender_name ORDER BY MAX(id) DESC").fetchall()
        return [r["sender_name"] for r in rows]

    def _deliver(self, sender_key, sender_name, to, text, now):
        return self._insert(sender_key, sender_name, self._targets(sender_key, to, now), text, now)

    def _insert(self, sender_key, sender_name, targets, text, now, file_id=None):
        for t in targets:
            # a person reads it on the page, so it is never waiting for delivery
            read_at = now if t["key"].startswith("web:") else None
            self.db.execute(
                "INSERT INTO messages (sender_key, sender_name, to_key, to_name, machine,"
                " tool, project, text, created, read_at, file_id) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (sender_key, sender_name, t["key"], t["name"], t["machine"],
                 t["tool"], t["project"], text, now, read_at, file_id))
            if file_id is not None:  # the file points at the first message that carries it
                self.db.execute("UPDATE files SET message_id=(SELECT last_insert_rowid())"
                                " WHERE id=? AND message_id IS NULL", (file_id,))
        return len(targets)

    def _deliverable(self, now):
        """Unread messages paired with the online agent they go to now. A knock goes to its
        session, or when that is gone (or it was to a person), the person's earliest online
        session."""
        self._expire_doors(now)
        online = self._online_rows(now)
        by_key = {r["key"]: r for r in online}
        out = []
        for m in self.db.execute("SELECT m.*, k.to_person FROM messages m LEFT JOIN knocks k"
                                 " ON k.id=m.knock_id WHERE m.read_at IS NULL ORDER BY m.id"):
            if m["kind"] == "knock":
                target = by_key.get(m["to_key"]) or next(
                    (r for r in online if r["person_id"] == m["to_person"]), None)
            else:
                target = by_key.get(m["to_key"])
                if target is None:  # held: the name may now belong to another person's session
                    target = next((r for r in online if r["name"] == m["to_name"]), None)
                    if target is not None and not m["sender_key"].startswith("web:"):
                        sender = self.db.execute("SELECT * FROM agents WHERE key=?",
                                                 (m["sender_key"],)).fetchone()
                        if sender is None or not self._may_message(sender, target):
                            target = None
            if target is not None:
                out.append((m, target))
        return out

    def _bind(self, m, key):
        """A held knock stays with the session it was handed to."""
        if m["kind"] == "knock" and m["to_key"] != key:
            self.db.execute("UPDATE messages SET to_key=? WHERE id=?", (key, m["id"]))

    def inbox(self, key: str) -> List[Message]:
        with self.lock:
            now = self.clock()
            rows = [m for m, t in self._deliverable(now) if t["key"] == key]
            for m in rows:
                self.db.execute("UPDATE messages SET read_at=? WHERE id=?", (now, m["id"]))
                self._bind(m, key)
            return [Message(m["id"], m["sender_name"], m["to_name"], m["text"], m["created"], True,
                            m["sender_key"].startswith("web:"), kind=m["kind"],
                            file=self._file_info(m["file_id"])) for m in rows]

    def offer(self, machine: str) -> List[dict]:
        with self.lock:
            now = self.clock()
            out = []
            for m, t in self._deliverable(now):
                if t["machine"] != machine:
                    continue
                state = m["push_state"]
                if state == "new" or (state == "offered" and now - m["offered_at"] >= LEASE_S):
                    self.db.execute("UPDATE messages SET push_state='offered', offered_at=?"
                                    " WHERE id=?", (now, m["id"]))
                    self._bind(m, t["key"])
                    text = (f"Kaptyorka message from {_sender(m)}:\n\n{m['text']}"
                            if m["kind"] == "message" else m["text"])  # knocks and doors: as written
                    out.append({"id": m["id"], "key": t["key"], "text": text})
            return out

    def ack(self, msg_id: int, ok: bool) -> None:
        with self.lock:
            if ok:
                self.db.execute("UPDATE messages SET read_at=?, push_state='pushed' WHERE id=?",
                                (self.clock(), msg_id))
            else:
                self.db.execute("UPDATE messages SET push_state='failed' WHERE id=?", (msg_id,))

    def messages(self, limit: int = 500) -> List[Message]:
        with self.lock:
            rows = self.db.execute("SELECT * FROM messages ORDER BY id DESC LIMIT ?",
                                   (limit,)).fetchall()
            return [Message(m["id"], m["sender_name"], m["to_name"], m["text"], m["created"],
                            m["read_at"] is not None, m["sender_key"].startswith("web:"),
                            (m["to_key"] or "").startswith("web:"), m["kind"],
                            self._file_info(m["file_id"]))
                    for m in reversed(rows)]

    # files

    def file_add(self, name: str, data: bytes, person_id: int) -> int:
        ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
        if ext not in FILE_TYPES:
            raise KaptyorkaError(f"file type .{ext} not allowed")
        if len(data) > MAX_FILE:
            raise KaptyorkaError(f"file too large: {len(data)} bytes, limit {MAX_FILE}")
        with self.lock:
            fid = self.db.execute(
                "INSERT INTO files (name, size, sha256, created, person_id) VALUES (?,?,?,?,?)",
                (name[:200], len(data), hashlib.sha256(data).hexdigest(), self.clock(),
                 person_id)).lastrowid
            (paths.files_dir() / str(fid)).write_bytes(data)
            return fid

    def _file_info(self, file_id) -> Optional[dict]:
        row = self.db.execute("SELECT id, name, size FROM files WHERE id=?",
                              (file_id,)).fetchone() if file_id is not None else None
        return dict(row) if row else None

    def _file_visible(self, file_id: int, principal: Principal) -> bool:
        if principal.is_admin():
            return True
        pid = principal.person_id
        return self.db.execute(
            "SELECT 1 FROM messages m LEFT JOIN agents s ON s.key=m.sender_key"
            " LEFT JOIN agents r ON r.key=m.to_key WHERE m.file_id=?"
            " AND (s.person_id=? OR r.person_id=?) LIMIT 1", (file_id, pid, pid)).fetchone() is not None

    def file_visible(self, file_id: int, principal: Principal) -> bool:
        with self.lock:
            return self._file_visible(file_id, principal)

    def file_meta(self, file_id: int, principal: Principal) -> dict:
        with self.lock:
            info = self._file_info(file_id)
            if info is None or not self._file_visible(file_id, principal):
                raise KaptyorkaError("no such file")
            return info

    def file_get(self, file_id: int, principal: Principal) -> Tuple[dict, bytes]:
        info = self.file_meta(file_id, principal)
        try:
            return info, (paths.files_dir() / str(file_id)).read_bytes()
        except OSError:
            raise KaptyorkaError("no such file") from None

    # doors and knocks

    def _is_admin(self, person_id) -> bool:
        p = self.db.execute("SELECT role FROM people WHERE id=?", (person_id,)).fetchone()
        return p is not None and p["role"] == "admin"

    def _person_name(self, person_id) -> str:
        p = self.db.execute("SELECT name FROM people WHERE id=?", (person_id,)).fetchone()
        return p["name"] if p else ""

    def _may_message(self, sender, target) -> bool:
        """Same person, an admin sender, or an open door: one between these two sessions, or a
        group door between their two people. A session of no person reaches only the admin."""
        sp, tp = sender["person_id"], target["person_id"]
        if sp is not None and sp == tp:
            return True
        if sp is not None and self._is_admin(sp):
            return True
        if sp is None or tp is None:
            return False
        return self.db.execute(
            "SELECT 1 FROM doors WHERE closed_at IS NULL AND ("
            "(scope='session' AND ((a_key=? AND b_key=?) OR (a_key=? AND b_key=?))) OR"
            " (scope='group' AND ((a_person=? AND b_person=?) OR (a_person=? AND b_person=?))))",
            (sender["key"], target["key"], target["key"], sender["key"],
             sp, tp, tp, sp)).fetchone() is not None

    def may_message(self, sender_key: str, target_key: str) -> bool:
        with self.lock:
            s = self.db.execute("SELECT * FROM agents WHERE key=?", (sender_key,)).fetchone()
            t = self.db.execute("SELECT * FROM agents WHERE key=?", (target_key,)).fetchone()
            return s is not None and t is not None and self._may_message(s, t)

    def _expire_doors(self, now):
        """A session door lasts while both sessions are online."""
        self.db.execute(
            "UPDATE doors SET closed_at=? WHERE closed_at IS NULL AND scope='session' AND"
            " (a_key NOT IN (SELECT key FROM agents WHERE last_seen > ?)"
            " OR b_key NOT IN (SELECT key FROM agents WHERE last_seen > ?))",
            (now, now - ONLINE_S, now - ONLINE_S))

    def expire_doors(self, now: float) -> None:
        with self.lock:
            self._expire_doors(now)

    def _session(self, key):
        row = self.db.execute("SELECT * FROM agents WHERE key=?", (key,)).fetchone()
        if row is None:
            raise KaptyorkaError("unknown session")
        return row

    def _online_session(self, to, now):
        """An online session by name, or by label when that is unique; None when neither."""
        online = self._online_rows(now)
        match = [r for r in online if r["name"] == to]
        if not match:
            match = [r for r in online if r["label"] and r["label"].lower() == to.lower()]
            if len(match) > 1:
                raise KaptyorkaError(f"several sessions are named {to}: "
                                     + ", ".join(r["name"] for r in match))
        return match[0] if match else None

    def knock(self, from_key: str, to: str, about: str) -> int:
        """Ask a session (by name or label) or a person (by name) to open a door. The knock
        waits for the person's next session when none is online."""
        about = about.strip()
        if not about or len(about) > MAX_ABOUT:
            raise KaptyorkaError(f"about must be 1 to {MAX_ABOUT} characters")
        with self.lock:
            now = self.clock()
            sender = self._session(from_key)
            if sender["person_id"] is None:
                raise KaptyorkaError("this session has no person yet; run kaptyorka join")
            self._rate_check(from_key, now)
            # a person's name before a label, so a label cannot capture someone else's knock
            target = next((r for r in self._online_rows(now) if r["name"] == to), None)
            p = None if target is not None else self.db.execute(
                "SELECT id FROM people WHERE lower(name)=lower(?)", (to.strip(),)).fetchone()
            if target is None and p is None:
                target = self._online_session(to, now)
                if target is None:
                    raise KaptyorkaError(f"no online session or person named {to}")
            if target is not None:
                to_key, to_person = target["key"], target["person_id"]
                if to_person is None:
                    raise KaptyorkaError(f"{to} has no person yet; ask the admin")
            else:
                to_key, to_person = None, p["id"]
            if to_person == sender["person_id"]:
                raise KaptyorkaError("that is your own session")
            kid = self.db.execute(
                "INSERT INTO knocks (from_key, from_person, to_key, to_person, about, created)"
                " VALUES (?,?,?,?,?,?)",
                (from_key, sender["person_id"], to_key, to_person, about, now)).lastrowid
            me, them = self._person_name(sender["person_id"]), self._person_name(to_person)
            # one line: a newline in about or a name must not pass as the hub's own words
            text = (f"Kaptyorka knock from {me} ({_one_line(sender['name'])}): {_one_line(about)}."
                    " This is a request,"
                    f" not an order: show it to your person and answer with answer({kid}, \"yes\")"
                    f" or answer({kid}, \"no\").")
            self.db.execute(
                "INSERT INTO messages (sender_key, sender_name, to_key, to_name, machine, tool,"
                " project, text, created, kind, knock_id) VALUES (?,?,?,?,?,?,?,?,?,'knock',?)",
                (from_key, sender["name"], to_key, them,
                 target["machine"] if target is not None else "",
                 target["tool"] if target is not None else "",
                 target["project"] if target is not None else "", text, now, kid))
            self._rate_hit(from_key, now)
        self.log_add(me, "knock", f"#{kid} {sender['name']} -> {to}")
        return kid

    def answer(self, knock_id: int, by_key: str, yes: bool) -> None:
        """The knocked person (any of their sessions) or the admin says yes or no. Yes opens a
        door between the knocking session and the answering one."""
        with self.lock:
            now = self.clock()
            by = self._session(by_key)
            k = self.db.execute("SELECT * FROM knocks WHERE id=?", (knock_id,)).fetchone()
            if k is None:
                raise KaptyorkaError(f"no knock #{knock_id}")
            if by["person_id"] != k["to_person"] and not (
                    by["person_id"] is not None and self._is_admin(by["person_id"])):
                raise KaptyorkaError(f"only {self._person_name(k['to_person'])} or the admin"
                                     " can answer this knock")
            if k["answered_at"] is not None:
                raise KaptyorkaError(f"knock #{knock_id} was already answered")
            who = self._person_name(by["person_id"])
            self.db.execute("UPDATE knocks SET answered_at=?, answer=? WHERE id=?",
                            (now, "yes" if yes else "no", knock_id))
            if yes and by["person_id"] != k["to_person"]:  # the admin, for someone else
                self._open_group(k["from_person"], k["to_person"], by["person_id"], now)
                them = self._person_name(k["to_person"])
                text = (f"Door open: the admin ({who}) opened a door to {them} for your knock"
                        f" #{knock_id}. You can send to {them}'s sessions until it is closed.")
            elif yes:
                self.db.execute(
                    "INSERT INTO doors (a_key, a_person, b_key, b_person, scope, opened_at,"
                    " opened_by) VALUES (?,?,?,?,'session',?,?)",
                    (k["from_key"], k["from_person"], by_key, by["person_id"], now,
                     by["person_id"]))
                text = (f"Door open: {by['name']} ({who}) said yes to your knock #{knock_id}."
                        f" You can send(to=\"{by['name']}\", ...) until either side closes it.")
            else:
                text = f"Knock #{knock_id} declined by {who}."
            knocker = self.db.execute("SELECT * FROM agents WHERE key=?",
                                      (k["from_key"],)).fetchone()
            self.db.execute(
                "INSERT INTO messages (sender_key, sender_name, to_key, to_name, machine, tool,"
                " project, text, created, kind, knock_id) VALUES (?,?,?,?,?,?,?,?,?,'door',?)",
                (by_key, by["name"], k["from_key"], knocker["name"] if knocker else "",
                 knocker["machine"] if knocker else "", knocker["tool"] if knocker else "",
                 knocker["project"] if knocker else "", text, now, knock_id))
        self.log_add(who, "knock_answer", f"#{knock_id} {'yes' if yes else 'no'} by {by['name']}")

    def door_close(self, by_key: str, to: str) -> None:
        """Close every open door between this session and the one named `to`."""
        with self.lock:
            now = self.clock()
            by = self._session(by_key)
            other = self._online_session(to, now) or self.db.execute(
                "SELECT * FROM agents WHERE name=? ORDER BY last_seen DESC LIMIT 1",
                (to,)).fetchone()
            cur = None
            if other is not None:
                cur = self.db.execute(
                    "UPDATE doors SET closed_at=? WHERE closed_at IS NULL AND scope='session' AND"
                    " ((a_key=? AND b_key=?) OR (a_key=? AND b_key=?))",
                    (now, by_key, other["key"], other["key"], by_key))
            if cur is None or not cur.rowcount:
                raise KaptyorkaError(f"no open door to {to}")
            who = self._person_name(by["person_id"]) or by["name"]
            self.db.execute(
                "INSERT INTO messages (sender_key, sender_name, to_key, to_name, machine, tool,"
                " project, text, created, kind) VALUES (?,?,?,?,?,?,?,?,?,'door')",
                (by_key, by["name"], other["key"], other["name"], other["machine"],
                 other["tool"], other["project"], f"Door closed by {who} ({by['name']}).", now))
        self.log_add(who, "door_close", f"{by['name']} -> {other['name']}")

    def _door_row(self, d, key=None, person_id=None) -> dict:
        """A door as seen from one side (`key`'s session or `person_id`'s group)."""
        mine_a = d["a_key"] == key if d["scope"] == "session" else d["a_person"] == person_id
        other_key, other_person = (d["b_key"], d["b_person"]) if mine_a else (d["a_key"], d["a_person"])
        other = self.db.execute("SELECT name FROM agents WHERE key=?", (other_key,)).fetchone()
        return {"id": d["id"], "with": other["name"] if other else "",
                "person": self._person_name(other_person), "scope": d["scope"],
                "opened_at": d["opened_at"]}

    def doors_for(self, key: str) -> List[dict]:
        """Open doors of a session: its own session doors and its person's group doors."""
        with self.lock:
            self._expire_doors(self.clock())
            row = self.db.execute("SELECT person_id FROM agents WHERE key=?", (key,)).fetchone()
            pid = row["person_id"] if row else None
            rows = self.db.execute(
                "SELECT * FROM doors WHERE closed_at IS NULL AND ((scope='session' AND"
                " (a_key=? OR b_key=?)) OR (scope='group' AND (a_person=? OR b_person=?)))"
                " ORDER BY id", (key, key, pid, pid)).fetchall()
            return [self._door_row(d, key, pid) for d in rows]

    def doors(self) -> List[dict]:
        """Every open door, for the admin."""
        with self.lock:
            self._expire_doors(self.clock())
            out = []
            for d in self.db.execute("SELECT * FROM doors WHERE closed_at IS NULL ORDER BY id"):
                a = self.db.execute("SELECT name FROM agents WHERE key=?", (d["a_key"],)).fetchone()
                b = self.db.execute("SELECT name FROM agents WHERE key=?", (d["b_key"],)).fetchone()
                out.append({"id": d["id"], "scope": d["scope"], "opened_at": d["opened_at"],
                            "a_person": self._person_name(d["a_person"]),
                            "b_person": self._person_name(d["b_person"]),
                            "a_session": a["name"] if a else "", "b_session": b["name"] if b else "",
                            "opened_by": self._person_name(d["opened_by"])})
            return out

    def open_door_group(self, a_person: int, b_person: int, by_person: str) -> int:
        """A door between two people: all their sessions may message each other. Admin only
        (checked by the hub)."""
        with self.lock:
            a, b = self._person_name(a_person), self._person_name(b_person)
            if not a or not b:
                raise KaptyorkaError("no such person")
            if a_person == b_person:
                raise KaptyorkaError("a group door needs two different people")
            by = self.db.execute("SELECT id FROM people WHERE name=?", (by_person,)).fetchone()
            did = self._open_group(a_person, b_person, by["id"] if by else 0, self.clock())
        self.log_add(by_person, "door_open", f"group #{did} {a} <-> {b}")
        return did

    def _open_group(self, a_person, b_person, by_id, now) -> int:
        """The open group door between two people, opened now if there is none."""
        have = self.db.execute(
            "SELECT id FROM doors WHERE closed_at IS NULL AND scope='group' AND"
            " ((a_person=? AND b_person=?) OR (a_person=? AND b_person=?))",
            (a_person, b_person, b_person, a_person)).fetchone()
        if have is not None:
            return have["id"]
        return self.db.execute(
            "INSERT INTO doors (a_key, a_person, b_key, b_person, scope, opened_at, opened_by)"
            " VALUES ('',?,'',?,'group',?,?)", (a_person, b_person, now, by_id)).lastrowid

    def close_door(self, door_id: int, by_person: str) -> None:
        """Close one door by id. Admin only (checked by the hub)."""
        with self.lock:
            cur = self.db.execute("UPDATE doors SET closed_at=? WHERE id=? AND closed_at IS NULL",
                                  (self.clock(), door_id))
        if not cur.rowcount:
            raise KaptyorkaError(f"no open door #{door_id}")
        self.log_add(by_person, "door_close", f"#{door_id}")

    def knocks(self, principal: Principal) -> List[dict]:
        """Unanswered knocks: all for the admin, else the ones to or from the principal."""
        with self.lock:
            rows = self.db.execute(
                "SELECT k.*, s.name AS from_session FROM knocks k LEFT JOIN agents s"
                " ON s.key=k.from_key WHERE k.answered_at IS NULL ORDER BY k.id").fetchall()
            return [{"id": k["id"], "from": self._person_name(k["from_person"]),
                     "from_session": k["from_session"] or "",
                     "to": self._person_name(k["to_person"]), "about": k["about"],
                     "created": k["created"]}
                    for k in rows
                    if principal.is_admin()
                    or principal.person_id in (k["from_person"], k["to_person"])]

    # notes

    def _person_principal(self, person_id) -> Optional[Principal]:
        """A principal for access checks only, from a person id (None when unknown)."""
        p = self.db.execute("SELECT name, role FROM people WHERE id=?", (person_id,)).fetchone()
        return Principal(person_id, p["name"], p["role"], 0, "", False, False) if p else None

    def _check_project(self, project, principal, write=False) -> None:
        """Raise unless the principal may read (or write) the project's notes. No principal
        (an internal caller) means no check. Only a registered project has notes: an
        unregistered folder name can belong to two people by chance."""
        if project is None or principal is None:
            return
        if not self._registered(project):
            raise KaptyorkaError(f"project {project} is not registered; ask the admin to register it")
        if not self._may(project, principal, write):
            raise KaptyorkaError(f"no access to project {project}; ask with request_access")

    def notes_write(self, key: str, topic: str, text: str, project: Optional[str] = None) -> None:
        """Write a note as a session; `project` None or "" is the team board."""
        project = project or None
        with self.lock:
            row = self.db.execute("SELECT name, person_id FROM agents WHERE key=?", (key,)).fetchone()
            if row is None:
                raise KaptyorkaError("unknown session")
            if project is not None and row["person_id"] is None:  # nobody to check access for
                raise KaptyorkaError(f"no access to project {project}; ask with request_access")
            if row["person_id"] is not None:
                self._check_project(project, self._person_principal(row["person_id"]), write=True)
            self._note(topic, text, row["name"], project)

    def notes_write_from_web(self, name: str, topic: str, text: str,
                             project: Optional[str] = None) -> None:
        name, topic = _person(name), topic.strip()
        if not topic:
            raise KaptyorkaError("topic is empty")
        with self.lock:
            self._note(topic, text, name, project or None)

    def _note(self, topic, text, author, project=None):
        self.db.execute("INSERT INTO notes (topic, text, author, time, project) VALUES (?,?,?,?,?)",
                        (topic, text, author, self.clock(), project))

    def notes_list(self, principal: Optional[Principal] = None,
                   project: Optional[str] = None) -> List[dict]:
        """Latest note of each topic on the team board, or with `project`, that project's."""
        project = project or None
        with self.lock:
            self._check_project(project, principal)
            rows = self.db.execute(
                "SELECT topic, author, time, project FROM notes WHERE id IN"
                " (SELECT MAX(id) FROM notes GROUP BY COALESCE(project, ''), topic)"
                " AND project IS ? ORDER BY id DESC", (project,)).fetchall()
            return [dict(r) for r in rows]

    def notes_read(self, topic: str, principal: Optional[Principal] = None,
                   project: Optional[str] = None) -> Optional[Note]:
        history = self.note_history(topic, principal, project)
        return history[0] if history else None

    def note_history(self, topic: str, principal: Optional[Principal] = None,
                     project: Optional[str] = None) -> List[Note]:
        """Every version of one note, newest first. A note is (project, topic)."""
        project = project or None
        with self.lock:
            self._check_project(project, principal)
            rows = self.db.execute(
                "SELECT * FROM notes WHERE topic=? AND project IS ? ORDER BY id DESC",
                (topic, project)).fetchall()
            return [Note(r["topic"], r["text"], r["author"], r["time"], r["project"])
                    for r in rows]
