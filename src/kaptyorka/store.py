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

ONLINE_S = 30
LEASE_S = 60
MAX_TEXT = 32_000
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
    kind TEXT NOT NULL DEFAULT 'message');
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
            name = self._project_target(project, person_id)
            self.db.execute("INSERT OR REPLACE INTO project_access (project, person_id, role)"
                            " VALUES (?,?,?)", (project, person_id, role))
        self.log_add(by, "grant", f"project:{project}:{role} -> {name}")

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
                            s.get("label", ""))
                if host:
                    self.db.execute("UPDATE agents SET host=?, os_user=? WHERE key=?",
                                    (host, os_user, s["key"]))
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
            return [self._agent(r, now) for r in self._online_rows(now)]

    def agent(self, key: str) -> Optional[Agent]:
        with self.lock:
            row = self.db.execute("SELECT * FROM agents WHERE key=?", (key,)).fetchone()
            return self._agent(row, self.clock()) if row else None

    def set_status(self, key: str, text: str) -> None:
        with self.lock:
            self.db.execute("UPDATE agents SET status=? WHERE key=?", (text, key))

    # messages

    def send(self, sender_key: str, to: str, text: str) -> int:
        with self.lock:
            now = self.clock()
            sender = self.db.execute("SELECT * FROM agents WHERE key=?", (sender_key,)).fetchone()
            if sender is None:
                raise KaptyorkaError("unknown sender session")
            _check_length(text)
            count, window = RATE
            recent = [t for t in self.sends.get(sender_key, []) if now - t < window]
            if len(recent) >= count:
                raise KaptyorkaError(f"rate limit: at most {count} sends per {window} s")
            n = self._deliver(sender_key, sender["name"], to, text, now)
            recent.append(now)
            self.sends[sender_key] = recent
            return n

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
        targets = self._targets(sender_key, to, now)
        for t in targets:
            # a person reads it on the page, so it is never waiting for delivery
            read_at = now if t["key"].startswith("web:") else None
            self.db.execute(
                "INSERT INTO messages (sender_key, sender_name, to_key, to_name, machine,"
                " tool, project, text, created, read_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (sender_key, sender_name, t["key"], t["name"], t["machine"],
                 t["tool"], t["project"], text, now, read_at))
        return len(targets)

    def _deliverable(self, now):
        """Unread messages paired with the online agent they go to now."""
        online = self._online_rows(now)
        by_key = {r["key"]: r for r in online}
        out = []
        for m in self.db.execute("SELECT * FROM messages WHERE read_at IS NULL ORDER BY id"):
            target = by_key.get(m["to_key"]) or next(
                (r for r in online if r["name"] == m["to_name"]), None)
            if target is not None:
                out.append((m, target))
        return out

    def inbox(self, key: str) -> List[Message]:
        with self.lock:
            now = self.clock()
            rows = [m for m, t in self._deliverable(now) if t["key"] == key]
            for m in rows:
                self.db.execute("UPDATE messages SET read_at=? WHERE id=?", (now, m["id"]))
            return [Message(m["id"], m["sender_name"], m["to_name"], m["text"], m["created"], True,
                            m["sender_key"].startswith("web:")) for m in rows]

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
                    out.append({"id": m["id"], "key": t["key"],
                                "text": f"Kaptyorka message from {_sender(m)}:\n\n{m['text']}"})
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
                            (m["to_key"] or "").startswith("web:"))
                    for m in reversed(rows)]

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
