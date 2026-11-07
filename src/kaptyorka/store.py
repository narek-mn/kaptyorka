"""SQLite storage for the hub: agents, messages, notes, people and their access."""
import getpass
import hashlib
import re
import secrets
import sqlite3
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from kaptyorka import paths
from kaptyorka.secrets_check import TEXT_TYPES, check_secrets

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
PRIVATE = "(private)"

SCHEMA = """
CREATE TABLE IF NOT EXISTS agents (
    key TEXT PRIMARY KEY, name TEXT NOT NULL, machine TEXT NOT NULL,
    tool TEXT NOT NULL, project TEXT NOT NULL, status TEXT NOT NULL DEFAULT '',
    activity TEXT NOT NULL DEFAULT '', last_seen REAL NOT NULL,
    label TEXT NOT NULL DEFAULT '', person_id INTEGER, os_user TEXT NOT NULL DEFAULT '',
    host TEXT NOT NULL DEFAULT '', started REAL, device_id INTEGER);
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
    device_name TEXT PRIMARY KEY, token TEXT NOT NULL, created REAL NOT NULL,
    secret TEXT NOT NULL DEFAULT '');
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
    ("agents", "device_id", "INTEGER"),
    ("rekeys", "secret", "TEXT NOT NULL DEFAULT ''"),
    ("agents", "cwd", "TEXT NOT NULL DEFAULT ''"),
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


MAX_IDENT = 100  # machine, tool and project: they go into session names and push texts
MAX_ACTIVITY = 200
MAX_CWD = 200


def clean(value, cap: int) -> str:
    """A client-supplied name or text as the hub keeps it: one line, at most `cap` characters."""
    return _one_line(str(value if value is not None else ""))[:cap]


def private_name(name: str, machine: str, tool: str, project: str) -> str:
    """The session name with its project hidden: machine/tool/(private), keeping a -N suffix."""
    base = f"{machine}/{tool}/{project}"
    suffix = name[len(base):] if name.startswith(base) else ""
    return f"{machine}/{tool}/{PRIVATE}{suffix}"


def _owns(row, principal) -> bool:
    """True when an agent row is the caller's: its person, and its device where one is set.
    No principal (an internal caller) owns everything."""
    if principal is None or (row["device_id"] is not None and row["device_id"] == principal.device_id):
        return True  # the hosting bridge delivers to the sessions on its box
    if row["person_id"] is not None and row["person_id"] != principal.person_id:
        return False
    if row["device_id"] is not None and row["device_id"] != principal.device_id:
        return False
    return row["person_id"] is not None or row["device_id"] is not None


def _version_at_least(version: str, floor: Tuple[int, int]) -> bool:
    m = re.match(r"(\d+)\.(\d+)", version or "")
    return m is not None and (int(m.group(1)), int(m.group(2))) >= floor


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
    cwd: str = ""
    device_id: Optional[int] = None


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
        from kaptyorka import memory, rules, workers  # here: workers imports this module
        workers.ensure_schema(self.db)
        memory.ensure_schema(self.db)
        rules.ensure_schema(self.db)
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
                legacy = self.db.execute(
                    "INSERT INTO devices (person_id, name, token_hash, created, legacy)"
                    " VALUES (?, 'legacy', ?, ?, 1)",
                    (admin, hashlib.sha256(legacy_token.encode()).hexdigest(), now)).lastrowid
                self.db.execute("UPDATE agents SET person_id=?, device_id=?", (admin, legacy))
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
        machine = clean(machine, MAX_IDENT) or None
        used, confirmed = False, False
        with self.lock:
            dev = self.db.execute("SELECT * FROM devices WHERE token_hash=? AND revoked_at IS NULL",
                                  (digest,)).fetchone()
            if dev is None:
                return None
            if dev["legacy"]:
                if not self._legacy_enabled():
                    return None
                used = bool(machine) and self._legacy_used(machine)
            else:
                # a rekeyed bridge calling with its new token: its confirm may have been lost
                confirmed = self._rekey_done(dev)
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
            principal = Principal(person_id, who["name"], who["role"], dev["id"], device_name,
                                  bool(dev["shared"]), bool(dev["legacy"]))
        if used:
            self.log_add("system", "legacy_used", machine)
        if confirmed:
            self.log_add(principal.person_name, "rekeyed", principal.device_name)
        return principal

    def person_create(self, name: str, role: str = "member", by: str = "") -> int:
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
        self.log_add(by or name, "person", f"{name} created as {role}")
        return pid

    def person_by_name(self, name: str) -> Optional[dict]:
        with self.lock:
            row = self.db.execute("SELECT id, name, role, created FROM people WHERE name=?",
                                  (name.strip(),)).fetchone()
            return dict(row) if row else None

    def invite_create(self, person_id: int, ttl_s: float = INVITE_TTL_S, by: str = "") -> str:
        code = secrets.token_urlsafe(16)
        now = self.clock()
        with self.lock:
            row = self.db.execute("SELECT name FROM people WHERE id=?", (person_id,)).fetchone()
            if row is None:
                raise KaptyorkaError("no such person")
            self.db.execute("INSERT INTO invites (code, person_id, created, expires)"
                            " VALUES (?,?,?,?)", (code, person_id, now, now + ttl_s))
        self.log_add(by or row["name"], "invite", f"invite for {row['name']}")
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

    def device_add(self, person_id: int, name: str, by: str = "") -> str:
        """A new device of the person's own (not the shared token); returns its token."""
        token = secrets.token_urlsafe(32)
        with self.lock:
            who = self.db.execute("SELECT name FROM people WHERE id=?", (person_id,)).fetchone()
            if who is None:
                raise KaptyorkaError("no such person")
            self.db.execute("INSERT INTO devices (person_id, name, token_hash, created)"
                            " VALUES (?,?,?,?)",
                            (person_id, name, hashlib.sha256(token.encode()).hexdigest(),
                             self.clock()))
        self.log_add(by or who["name"], "device", f"device {name} of {who['name']}")
        return token

    def has_own_device(self, person_id: int) -> bool:
        """True when the person has a working device that is not the 0.1 shared token."""
        with self.lock:
            return self.db.execute("SELECT 1 FROM devices WHERE person_id=? AND legacy=0 AND"
                                   " revoked_at IS NULL LIMIT 1", (person_id,)).fetchone() is not None

    def page_seen(self, principal: Principal) -> None:
        """Log `page_login` the first time in a day a device opens the page."""
        key, now = f"page_seen:{principal.device_id}", self.clock()
        with self.lock:
            row = self.db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
            try:
                if row is not None and now - float(row["value"]) < 86400:
                    return
            except ValueError:
                pass
            self.db.execute("INSERT INTO settings (key, value) VALUES (?, ?)"
                            " ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(now)))
        self.log_add(principal.person_name, "page_login", principal.device_name)

    def device_revoke(self, device_id: int, by: str = "") -> None:
        with self.lock:
            dev = self.db.execute("SELECT * FROM devices WHERE id=?", (device_id,)).fetchone()
            if dev is not None and self._is_rekey_device(dev):  # its token must not be handed out
                self.db.execute("DELETE FROM rekeys WHERE device_name=?", (dev["name"],))
            cur = self.db.execute("UPDATE devices SET revoked_at=? WHERE id=? AND revoked_at IS NULL",
                                  (self.clock(), device_id))
            row = self.db.execute("SELECT d.name, p.name AS person FROM devices d"
                                  " JOIN people p ON p.id=d.person_id WHERE d.id=?",
                                  (device_id,)).fetchone()
        if cur.rowcount and row:
            self.log_add(by or row["person"], "revoke", f"device {row['name']} of {row['person']}")

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
            return self._may_use(host, person_id)

    def _may_use(self, host: str, person_id: int) -> bool:
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

    def set_machine_owner(self, host: str, person_id: Optional[int], by: str) -> None:
        """The admin names a machine's owner (None: no owner)."""
        with self.lock:
            if self.db.execute("SELECT 1 FROM machines WHERE host=?", (host,)).fetchone() is None:
                raise KaptyorkaError(f"no such machine: {host}")
            who = None
            if person_id is not None:
                who = self.db.execute("SELECT name FROM people WHERE id=?", (person_id,)).fetchone()
                if who is None:
                    raise KaptyorkaError("no such person")
            self.db.execute("UPDATE machines SET owner_person_id=? WHERE host=?", (person_id, host))
        self.log_add(by, "machine_owner", f"{host} -> {who['name'] if who else 'nobody'}")

    def revoke_machine(self, host: str, person_id: int, by: str) -> None:
        self._grant_change(host, person_id, False, by)

    def device_set_shared(self, device_id: int, shared: bool, by: str = "") -> None:
        with self.lock:
            cur = self.db.execute("UPDATE devices SET shared=? WHERE id=?", (int(shared), device_id))
            row = self.db.execute("SELECT d.name, p.name AS person FROM devices d"
                                  " JOIN people p ON p.id=d.person_id WHERE d.id=?",
                                  (device_id,)).fetchone()
        if not cur.rowcount:
            raise KaptyorkaError("no such device")
        if row is not None:
            self.log_add(by or row["person"], "device_shared",
                         f"device {row['name']} of {row['person']} {'on' if shared else 'off'}")

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

    # legacy devices

    def _legacy_unassigned(self) -> List[str]:
        return [r["machine"] for r in self.db.execute(
            "SELECT DISTINCT a.machine FROM agents a JOIN devices d ON d.id=a.device_id"
            " WHERE d.legacy=1 AND NOT EXISTS (SELECT 1 FROM settings s"
            " WHERE s.key='legacy:' || a.machine) ORDER BY a.machine")]

    def _legacy_enabled(self) -> bool:
        row = self.db.execute("SELECT value FROM settings WHERE key='legacy_enabled'").fetchone()
        return row is None or row["value"] != "0"

    def _is_rekey_device(self, dev) -> bool:
        """True when `dev` is the device assign_legacy made and its rekey is still pending."""
        if dev["legacy"] or not self.db.execute("SELECT 1 FROM rekeys WHERE device_name=?",
                                                (dev["name"],)).fetchone():
            return False
        assigned = self._assigned_device(dev["name"], dev["person_id"])
        return assigned is not None and assigned["id"] == dev["id"]

    def _rekey_done(self, dev) -> bool:
        """Forget a pending rekey once its new device calls; True when one was forgotten."""
        if not self.db.execute("SELECT 1 FROM rekeys LIMIT 1").fetchone():
            return False
        if not self._is_rekey_device(dev):
            return False
        self.db.execute("DELETE FROM rekeys WHERE device_name=?", (dev["name"],))
        # the machine's sessions now belong to its new device (see _owns), not the shared one
        self.db.execute("UPDATE agents SET device_id=? WHERE machine=? AND person_id=? AND"
                        " device_id IN (SELECT id FROM devices WHERE legacy=1)",
                        (dev["id"], dev["name"], dev["person_id"]))
        return True

    def _legacy_used(self, machine: str) -> bool:
        """True (and remembered) when this machine's use of the shared token is first in a day.
        Only machines the hub knows as legacy count, so made-up names leave no rows."""
        known = self.db.execute(
            "SELECT 1 FROM agents a JOIN devices d ON d.id=a.device_id"
            " WHERE d.legacy=1 AND a.machine=? LIMIT 1", (machine,)).fetchone() or \
            self.db.execute("SELECT 1 FROM settings WHERE key=?", (f"legacy:{machine}",)).fetchone()
        if not known:
            return False
        key, now = f"legacy_seen:{machine}", self.clock()
        row = self.db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        try:
            if row is not None and now - float(row["value"]) < 86400:
                return False
        except ValueError:
            pass
        self.db.execute("INSERT INTO settings (key, value) VALUES (?, ?)"
                        " ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(now)))
        return True

    def _assigned_device(self, machine: str, person_id: int):
        """The device assign_legacy made for `machine`, when it is still mapped to that person."""
        mapped = self.db.execute("SELECT value FROM settings WHERE key=?",
                                 (f"legacy:{machine}",)).fetchone()
        if mapped is None or mapped["value"] != str(person_id):
            return None
        return self.db.execute(
            "SELECT * FROM devices WHERE name=? AND person_id=? AND legacy=0 AND revoked_at IS NULL"
            " ORDER BY id DESC LIMIT 1", (machine, person_id)).fetchone()

    def legacy_unassigned(self) -> List[str]:
        """Machines last seen with the 0.1 shared token that no person has been given yet."""
        with self.lock:
            return self._legacy_unassigned()

    def legacy_enabled(self) -> bool:
        with self.lock:
            return self._legacy_enabled()

    def legacy_ready_to_disable(self) -> bool:
        """The shared token is on but no machine needs it: every one seen with it is assigned
        and has taken its own token."""
        with self.lock:
            return (self._legacy_enabled() and not self._legacy_unassigned()
                    and not self.db.execute("SELECT 1 FROM rekeys LIMIT 1").fetchone()
                    and self.db.execute("SELECT 1 FROM settings WHERE substr(key, 1, 7)='legacy:'"
                                        " LIMIT 1").fetchone() is not None)

    def set_legacy_enabled(self, on: bool, by: str) -> None:
        """Switched off, the shared token stops working and pending rekeys are dropped (they
        are fetched with that token)."""
        with self.lock:
            self.db.execute("INSERT INTO settings (key, value) VALUES ('legacy_enabled', ?)"
                            " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                            ("1" if on else "0",))
            if not on:
                self.db.execute("DELETE FROM rekeys")
        self.log_add(by, "legacy_toggle", "on" if on else "off")

    def assign_legacy(self, machine: str, person_id: int, by: str) -> None:
        """Give a legacy machine to a person: the shared token now speaks for them there, and
        a device of their own waits in `rekeys` for the machine's bridge to pick up."""
        token, secret = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        with self.lock:
            who = self.db.execute("SELECT name FROM people WHERE id=?", (person_id,)).fetchone()
            if who is None:
                raise KaptyorkaError("no such person")
            if machine not in self._legacy_unassigned():
                raise KaptyorkaError(f"{machine} is not an unassigned legacy machine")
            now = self.clock()
            self.db.execute(
                "INSERT INTO devices (person_id, name, token_hash, secret_hash, created)"
                " VALUES (?,?,?,?,?)",
                (person_id, machine, hashlib.sha256(token.encode()).hexdigest(),
                 hashlib.sha256(secret.encode()).hexdigest(), now))
            self.db.execute("INSERT INTO settings (key, value) VALUES (?, ?)"
                            " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                            (f"legacy:{machine}", str(person_id)))
            self.db.execute("INSERT OR REPLACE INTO rekeys (device_name, token, secret, created)"
                            " VALUES (?,?,?,?)", (machine, token, secret, now))
            self.db.execute("UPDATE agents SET person_id=? WHERE machine=?", (person_id, machine))
        self.log_add(by, "legacy_assign", f"{machine} -> {who['name']}")

    def bridge_version(self, principal: Principal, machine: str) -> str:
        """The version a rekey may be offered on: only a legacy bridge on an assigned machine
        gets one, judged by what it reported for its new device; "" otherwise."""
        if principal is None or not principal.legacy:
            return ""
        machine = clean(machine, MAX_IDENT)
        with self.lock:
            dev = self._assigned_device(machine, principal.person_id)
            return dev["version"] if dev is not None else ""

    def rekeyed(self, machine: str, principal: Principal) -> bool:
        """The bridge saved its new token and confirms with it. Authenticating that token has
        already forgotten the raw values (see _rekey_done); this checks the caller is that
        machine's new device. False when it is not."""
        machine = clean(machine, MAX_IDENT)
        with self.lock:
            dev = self._assigned_device(machine, principal.person_id)
            if principal.legacy or dev is None or dev["id"] != principal.device_id:
                return False
            done = self.db.execute("DELETE FROM rekeys WHERE device_name=?", (machine,)).rowcount
        if done:
            self.log_add(principal.person_name, "rekeyed", machine)
        return True

    # log

    def log_add(self, person: str, kind: str, detail: str) -> None:
        with self.lock:
            self.db.execute("INSERT INTO log (time, person, kind, detail) VALUES (?,?,?,?)",
                            (self.clock(), person, kind, detail))

    def log(self, limit: int = 200, person: Optional[str] = None) -> List[dict]:
        """Newest first."""
        with self.lock:
            rows = self.db.execute(
                "SELECT id, time, person, kind, detail FROM log WHERE (? IS NULL OR person=?)"
                " ORDER BY id DESC LIMIT ?", (person, person, limit)).fetchall()
            return [dict(r) for r in rows]

    # retention

    def retention_days(self) -> int:
        with self.lock:
            row = self.db.execute("SELECT value FROM settings WHERE key='retention_days'").fetchone()
            return int(row["value"]) if row else 180

    def retention_s(self) -> int:
        return self.retention_days() * 86400

    def set_retention_days(self, days: int, by: str) -> None:
        if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= 3650:
            raise KaptyorkaError("retention_days must be a whole number from 1 to 3650")
        with self.lock:
            self.db.execute("INSERT INTO settings (key, value) VALUES ('retention_days', ?)"
                            " ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(days),))
        self.log_add(by, "settings", f"retention_days={days}")

    def prune(self, now: float) -> dict:
        """Delete old messages and their files, and unsent uploads older than a day.
        Notes, the log, requests, doors and knocks are never touched."""
        cutoff = now - self.retention_s()
        with self.lock:
            old = [r["id"] for r in self.db.execute(
                "SELECT id FROM messages WHERE created < ?", (cutoff,))]
            file_ids = set()
            for i in range(0, len(old), 500):
                chunk = old[i:i + 500]
                marks = ",".join("?" * len(chunk))
                file_ids.update(r["id"] for r in self.db.execute(
                    f"SELECT id FROM files WHERE message_id IN ({marks})", chunk))
                self.db.execute(f"DELETE FROM messages WHERE id IN ({marks})", chunk)
            file_ids.update(r["id"] for r in self.db.execute(
                "SELECT id FROM files WHERE message_id IS NULL AND created < ?", (now - 86400,)))
            for fid in file_ids:
                self.db.execute("DELETE FROM files WHERE id=?", (fid,))
                try:
                    (paths.files_dir() / str(fid)).unlink()
                except FileNotFoundError:
                    pass
            return {"messages": len(old), "files": len(file_ids)}

    # agents

    def _agent(self, row, now) -> Agent:
        return Agent(row["key"], row["name"], row["machine"], row["tool"], row["project"],
                     row["status"], row["activity"], row["last_seen"],
                     now - row["last_seen"] < ONLINE_S, row["label"], row["person_id"],
                     row["os_user"], row["host"], row["started"], row["cwd"], row["device_id"])

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

    def _touch(self, key, machine, tool, project, now, activity=None, label=None, cwd=None):
        row = self.db.execute("SELECT * FROM agents WHERE key=?", (key,)).fetchone()
        if row is None:
            name = self._free_name(key, machine, tool, project, now)
            self.db.execute(
                "INSERT INTO agents (key, name, machine, tool, project, activity, last_seen, label,"
                " started, cwd) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (key, name, machine, tool, project, activity or "", now, label or "", now,
                 cwd or ""))
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
        if cwd is not None:
            self.db.execute("UPDATE agents SET cwd=? WHERE key=?", (cwd, key))

    def seen(self, key: str, machine: str, tool: str, project: str,
             principal: Optional[Principal] = None) -> Agent:
        machine, tool, project = (clean(v, MAX_IDENT) for v in (machine, tool, project))
        with self.lock:
            now = self.clock()
            if principal is not None:
                # online or not: taking the key would take its unread messages and history.
                # The same device may (a legacy machine's rows move with assign_legacy).
                held = self.db.execute("SELECT person_id, device_id FROM agents WHERE key=?",
                                       (key,)).fetchone()
                if held is not None and held["person_id"] not in (None, principal.person_id) \
                        and held["device_id"] != principal.device_id:
                    raise KaptyorkaError("session key in use by another person")
            self._touch(key, machine, tool, project, now)
            if principal is not None:
                # every call, so a legacy machine handed to another person moves at once
                dev = self.db.execute("SELECT host, os_user FROM devices WHERE id=?",
                                      (principal.device_id,)).fetchone()
                self.db.execute(
                    "UPDATE agents SET person_id=COALESCE(CASE WHEN ? THEN person_id END, ?),"
                    " device_id=?, host=COALESCE(NULLIF(?, ''), host),"
                    " os_user=COALESCE(NULLIF(?, ''), os_user) WHERE key=?",
                    (principal.shared, principal.person_id, principal.device_id,
                     dev["host"] if dev else "", dev["os_user"] if dev else "", key))
            row = self.db.execute("SELECT * FROM agents WHERE key=?", (key,)).fetchone()
            return self._agent(row, now)

    def heartbeat(self, machine: str, sessions: List[dict], tools: Sequence[str] = (),
                  principal: Optional[Principal] = None, facts: Optional[dict] = None) -> None:
        """`tools` are the tools whose sessions this list is complete for: the rest of their agents
        on this machine are gone, so their names are free at once. `facts` (host, os_user,
        version) say which computer the sessions run on; an older bridge sends none."""
        facts = facts or {}
        machine = clean(machine, MAX_IDENT)
        sessions = [{**s, "tool": clean(s["tool"], MAX_IDENT),
                     "project": clean(s["project"], MAX_IDENT),
                     "label": clean(s.get("label"), MAX_LABEL),
                     "cwd": None if s.get("cwd") is None else clean(s["cwd"], MAX_CWD),
                     "activity": (None if s.get("activity") is None
                                  else clean(s["activity"], MAX_ACTIVITY))}
                    for s in sessions]
        host, os_user, version = (clean(facts.get(k), 80) for k in ("host", "os_user", "version"))
        with self.lock:
            now = self.clock()
            # a legacy bridge on an assigned machine: its sessions and facts go to that person
            assigned = (self._assigned_device(machine, principal.person_id)
                        if principal is not None and principal.legacy else None)
            seen = {s["key"] for s in sessions}
            for r in self._online_rows(now):
                if r["machine"] == machine and r["tool"] in tools and r["key"] not in seen \
                        and _owns(r, principal):
                    self.db.execute("UPDATE agents SET last_seen=? WHERE key=?",
                                    (now - ONLINE_S, r["key"]))
            for s in sessions:
                held = self.db.execute("SELECT person_id, device_id FROM agents WHERE key=?",
                                       (s["key"],)).fetchone()
                if principal is not None and held is not None and not (
                        principal.shared and held["device_id"] == principal.device_id) \
                        and held["person_id"] not in (None, principal.person_id):
                    continue  # another person's session: a bridge speaks only for its own
                self._touch(s["key"], machine, s["tool"], s["project"], now, s["activity"],
                            s["label"], s["cwd"])
                if host:
                    self.db.execute("UPDATE agents SET host=?, os_user=? WHERE key=?",
                                    (host, os_user, s["key"]))
                if principal is not None:
                    self.db.execute("UPDATE agents SET device_id=? WHERE key=?",
                                    (principal.device_id, s["key"]))
                if assigned is not None:
                    self.db.execute("UPDATE agents SET person_id=? WHERE key=?",
                                    (principal.person_id, s["key"]))
                elif principal is not None and not principal.legacy:  # until it calls a tool
                    self.db.execute("UPDATE agents SET person_id=COALESCE(person_id, ?)"
                                    " WHERE key=?", (principal.person_id, s["key"]))
            if host:
                # no owner on first sight: the host name is self-reported; the admin sets one
                self.db.execute("INSERT OR IGNORE INTO machines (host, owner_person_id, first_seen)"
                                " VALUES (?,NULL,?)", (host, now))
                device_id = (assigned["id"] if assigned is not None else
                             principal.device_id if principal is not None and not principal.legacy
                             else None)
                if device_id is not None:
                    self.db.execute("UPDATE devices SET host=?, os_user=?, version=? WHERE id=?",
                                    (host, os_user, version, device_id))

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
            self.db.execute("UPDATE agents SET status=? WHERE key=?",
                            (clean(text, MAX_ACTIVITY), key))

    # messages

    def send(self, sender_key: str, to: str, text: str, file_id: Optional[int] = None) -> int:
        check_secrets(text)
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
            targets = self._targets(sender_key, to, now, sender["person_id"])
            if to == "all":
                targets = [t for t in targets if self._may_message(sender, t)]
            elif not all(self._may_message(sender, self._as_target(t)) for t in targets):
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

    def send_from_web(self, name: str, to: str, text: str,
                      person_id: Optional[int] = None) -> int:
        """Send as a person on the web page, not an agent: sender key `web:<name>`. With
        `person_id` the page counts as a session of that person and follows the group rule."""
        name = _person(name)
        check_secrets(text)
        opened = []
        with self.lock:
            _check_length(text)
            now = self.clock()
            key = f"web:{name}"
            targets = self._targets(key, to, now, person_id)
            if person_id is not None:
                self._expire_doors(now)
                me = {"key": key, "person_id": person_id}
                if to == "all":
                    targets = [t for t in targets if self._may_message(me, self._as_target(t))]
                elif not all(self._may_message(me, self._as_target(t)) for t in targets):
                    raise KaptyorkaError(f"no door to {to}: knock(to, about) first")
                if to != "all":  # so the session can reply to the page; a broadcast is one-way
                    opened = [t["name"] for t in targets if self._web_door(key, person_id, t, now)]
            n = self._insert(key, name, targets, text, now)
        for other in opened:
            self.log_add(name, "door_open", f"{key} <-> {other}")
        return n

    def _as_target(self, t) -> dict:
        """A target row as _may_message reads it; a person on the web page by their name."""
        if t["key"].startswith("web:"):
            p = self.db.execute("SELECT id FROM people WHERE name=?", (t["key"][4:],)).fetchone()
            return {"key": t["key"], "person_id": p["id"] if p else None}
        return {"key": t["key"], "person_id": t["person_id"]}

    def _web_door(self, web_key, person_id, t, now):
        """A session door between a person's page and a session it wrote to, unless one is
        open or they are the same person. It closes when the session goes offline. True when
        a door was opened."""
        tp = None if t["key"].startswith("web:") else t["person_id"]
        if tp is None or tp == person_id:
            return False
        if self.db.execute(
                "SELECT 1 FROM doors WHERE closed_at IS NULL AND scope='session' AND"
                " ((a_key=? AND b_key=?) OR (a_key=? AND b_key=?))",
                (web_key, t["key"], t["key"], web_key)).fetchone() is None:
            self.db.execute(
                "INSERT INTO doors (a_key, a_person, b_key, b_person, scope, opened_at, opened_by)"
                " VALUES (?,?,?,?,'session',?,?)", (web_key, person_id, t["key"], tp, now, person_id))
            return True
        return False

    def _shown(self, row, viewer) -> str:
        """A session's name as person `viewer` may see it (the who() rule): the project is
        hidden unless the session is theirs or in a registered project they have a role in."""
        if viewer is not None and (row["person_id"] == viewer
                                   or self._role(row["project"], viewer) is not None):
            return row["name"]
        return private_name(row["name"], row["machine"], row["tool"], row["project"])

    def _titles(self, rows, viewer) -> str:
        return ", ".join(self._shown(r, viewer) + (f" ({r['label']})" if r["label"] else "")
                         for r in rows)

    def _targets(self, sender_key, to, now, viewer=None):
        """`viewer` is the sender's person, for whom names in errors are masked."""
        online = self._online_rows(now)
        if to == "all":
            return [r for r in online if r["key"] != sender_key]
        match = [r for r in online if r["name"] == to]
        if not match:
            labeled = [r for r in online if r["label"] and r["label"].lower() == to.lower()]
            if len(labeled) > 1:
                raise KaptyorkaError(f"several sessions are named {to}: "
                                     + ", ".join(self._shown(r, viewer) for r in labeled))
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
        names = self._titles(online, viewer) or "none"
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
                    if target is not None:
                        sender = (self._as_target({"key": m["sender_key"], "person_id": None})
                                  if m["sender_key"].startswith("web:") else self.db.execute(
                                      "SELECT * FROM agents WHERE key=?",
                                      (m["sender_key"],)).fetchone())
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

    def offer(self, machine: str, version: str = "",
              principal: Optional[Principal] = None) -> List[dict]:
        """Items for a machine's bridge: a pending rekey first, when the bridge's `version` is
        0.2 or later (0.1.9 reads every item as a message and would crash), then messages.
        With `principal`, only what is the caller's: messages to its own sessions (see _owns),
        and a rekey only for the shared token speaking for the machine's assigned person."""
        machine = clean(machine, MAX_IDENT)
        with self.lock:
            now = self.clock()
            out = []
            rekey = self.db.execute("SELECT token, secret FROM rekeys WHERE device_name=?",
                                    (machine,)).fetchone()
            if rekey is not None and principal is not None:
                dev = self._assigned_device(machine, principal.person_id)
                if not principal.legacy or dev is None:
                    rekey = None
            if rekey is not None and _version_at_least(version, (0, 2)):
                out.append({"kind": "rekey", "token": rekey["token"], "secret": rekey["secret"]})
            for m, t in self._deliverable(now):
                if t["machine"] != machine or not _owns(t, principal):
                    continue
                state = m["push_state"]
                if state == "new" or (state == "offered" and now - m["offered_at"] >= LEASE_S):
                    self.db.execute("UPDATE messages SET push_state='offered', offered_at=?"
                                    " WHERE id=?", (now, m["id"]))
                    self._bind(m, t["key"])
                    text = (f"Kaptyorka message from {_one_line(_sender(m))}:\n\n{m['text']}"
                            if m["kind"] == "message" else m["text"])  # knocks and doors: as written
                    out.append({"kind": "message", "id": m["id"], "key": t["key"], "text": text})
            return out

    def ack(self, msg_id: int, ok: bool, principal: Optional[Principal] = None) -> bool:
        """Mark a pushed message done or failed. With `principal`, only a message to one of the
        caller's own sessions; False (nothing changed) otherwise."""
        with self.lock:
            if principal is not None:
                row = self.db.execute("SELECT a.person_id, a.device_id FROM messages m JOIN agents a"
                                      " ON a.key=m.to_key WHERE m.id=?", (msg_id,)).fetchone()
                if row is None or not _owns(row, principal):
                    return False
            if ok:
                self.db.execute("UPDATE messages SET read_at=?, push_state='pushed' WHERE id=?",
                                (self.clock(), msg_id))
            else:
                self.db.execute("UPDATE messages SET push_state='failed' WHERE id=?", (msg_id,))
            return True

    def messages_for(self, principal: Principal, limit: int = 500) -> List[dict]:
        """Messages the principal's person sent or received (an admin: all), oldest first. Each
        is a Message as a dict plus `_from` and `_to`: the session behind each side
        ({person_id, machine, tool, project}) or None, for the caller to mask names with."""
        side = "COALESCE({a}.person_id, {w}.id, CASE m.kind WHEN 'knock' THEN k.{k1}" \
               " WHEN 'door' THEN k.{k2} END)"
        sender = side.format(a="s", w="sp", k1="from_person", k2="to_person")
        receiver = side.format(a="r", w="rp", k1="to_person", k2="from_person")
        with self.lock:
            rows = self.db.execute(
                "SELECT m.*, s.person_id AS s_pid, s.machine AS s_machine, s.tool AS s_tool,"
                " s.project AS s_project, r.person_id AS r_pid, r.machine AS r_machine,"
                " r.tool AS r_tool, r.project AS r_project FROM messages m"
                " LEFT JOIN agents s ON s.key=m.sender_key LEFT JOIN agents r ON r.key=m.to_key"
                " LEFT JOIN people sp ON m.sender_key='web:' || sp.name"
                " LEFT JOIN people rp ON m.to_key='web:' || rp.name"
                " LEFT JOIN knocks k ON k.id=m.knock_id"
                f" WHERE ? OR {sender}=? OR {receiver}=? ORDER BY m.id DESC LIMIT ?",
                (principal.is_admin(), principal.person_id, principal.person_id,
                 limit)).fetchall()
            out = []
            for m in reversed(rows):
                d = asdict(Message(m["id"], m["sender_name"], m["to_name"], m["text"],
                                   m["created"], m["read_at"] is not None,
                                   m["sender_key"].startswith("web:"),
                                   (m["to_key"] or "").startswith("web:"), m["kind"],
                                   self._file_info(m["file_id"])))
                for k, p in (("_from", "s_"), ("_to", "r_")):
                    d[k] = None if m[p + "machine"] is None else {
                        "person_id": m[p + "pid"], "machine": m[p + "machine"],
                        "tool": m[p + "tool"], "project": m[p + "project"]}
                out.append(d)
            return out

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
        if ext in TEXT_TYPES:
            check_secrets(data.decode("utf-8", "replace"))
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
        pid, web = principal.person_id, f"web:{principal.person_name}"
        # a party on the page (web:<person>) counts as that person
        return self.db.execute(
            "SELECT 1 FROM messages m LEFT JOIN agents s ON s.key=m.sender_key"
            " LEFT JOIN agents r ON r.key=m.to_key WHERE m.file_id=?"
            " AND (s.person_id=? OR r.person_id=? OR m.sender_key=? OR m.to_key=?) LIMIT 1",
            (file_id, pid, pid, web, web)).fetchone() is not None

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
        """A session door lasts while both sessions are online (a person's page counts as
        always online)."""
        self.db.execute(
            "UPDATE doors SET closed_at=? WHERE closed_at IS NULL AND scope='session' AND"
            " ((a_key NOT LIKE 'web:%' AND a_key NOT IN (SELECT key FROM agents WHERE last_seen > ?))"
            " OR (b_key NOT LIKE 'web:%' AND b_key NOT IN (SELECT key FROM agents WHERE last_seen > ?)))",
            (now, now - ONLINE_S, now - ONLINE_S))

    def expire_doors(self, now: float) -> None:
        with self.lock:
            self._expire_doors(now)

    def _session(self, key):
        row = self.db.execute("SELECT * FROM agents WHERE key=?", (key,)).fetchone()
        if row is None:
            raise KaptyorkaError("unknown session")
        return row

    def _online_session(self, to, now, viewer=None):
        """An online session by name, or by label when that is unique; None when neither.
        `viewer` is the asking person, for whom names in errors are masked."""
        online = self._online_rows(now)
        match = [r for r in online if r["name"] == to]
        if not match:
            match = [r for r in online if r["label"] and r["label"].lower() == to.lower()]
            if len(match) > 1:
                raise KaptyorkaError(f"several sessions are named {to}: "
                                     + ", ".join(self._shown(r, viewer) for r in match))
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
                target = self._online_session(to, now, sender["person_id"])
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

    def answer(self, knock_id: int, by_key: str, yes: bool) -> str:
        """The knocked person (any of their sessions) or the admin says yes or no. Yes opens a
        door between the knocking session and the answering one. Returns the door's scope
        ("session" or "group"), or "" for no."""
        with self.lock:
            by = self._session(by_key)
            scope, who = self._answer(knock_id, by["person_id"], by_key, by["name"], yes)
        self.log_add(who, "knock_answer", f"#{knock_id} {'yes' if yes else 'no'} by {by['name']}")
        return scope

    def answer_by_person(self, knock_id: int, person_id: int, yes: bool) -> str:
        """Answer from the web page when the person has no session online: yes opens a group
        door, so all their sessions may talk with the knocker's person."""
        with self.lock:
            name = self._person_name(person_id)
            scope, who = self._answer(knock_id, person_id, f"web:{name}", name, yes)
        self.log_add(who, "knock_answer", f"#{knock_id} {'yes' if yes else 'no'} on the web page")
        return scope

    def _answer(self, knock_id, by_pid, by_key, by_name, yes):
        """Lock held by the caller. Returns (door scope or "", the answering person's name)."""
        now = self.clock()
        k = self.db.execute("SELECT * FROM knocks WHERE id=?", (knock_id,)).fetchone()
        if k is None:
            raise KaptyorkaError(f"no knock #{knock_id}")
        if by_pid != k["to_person"] and not (by_pid is not None and self._is_admin(by_pid)):
            raise KaptyorkaError(f"only {self._person_name(k['to_person'])} or the admin"
                                 " can answer this knock")
        if k["answered_at"] is not None:
            raise KaptyorkaError(f"knock #{knock_id} was already answered")
        who = self._person_name(by_pid)
        self.db.execute("UPDATE knocks SET answered_at=?, answer=? WHERE id=?",
                        (now, "yes" if yes else "no", knock_id))
        scope = ""
        if yes and by_pid != k["to_person"]:  # the admin, for someone else
            self._open_group(k["from_person"], k["to_person"], by_pid, now)
            them = self._person_name(k["to_person"])
            scope = "group"
            text = (f"Door open: the admin ({who}) opened a door to {them} for your knock"
                    f" #{knock_id}. You can send to {them}'s sessions until it is closed.")
        elif yes and by_key.startswith("web:"):  # on the page, with no session to pair with
            self._open_group(k["from_person"], k["to_person"], by_pid, now)
            scope = "group"
            text = (f"Door open: {who} said yes to your knock #{knock_id} on the web page."
                    f" You can send to {who}'s sessions until it is closed.")
        elif yes:
            self.db.execute(
                "INSERT INTO doors (a_key, a_person, b_key, b_person, scope, opened_at,"
                " opened_by) VALUES (?,?,?,?,'session',?,?)",
                (k["from_key"], k["from_person"], by_key, by_pid, now, by_pid))
            scope = "session"
            text = (f"Door open: {by_name} ({who}) said yes to your knock #{knock_id}."
                    f" You can send(to=\"{by_name}\", ...) until either side closes it.")
        else:
            text = f"Knock #{knock_id} declined by {who}."
        knocker = self.db.execute("SELECT * FROM agents WHERE key=?",
                                  (k["from_key"],)).fetchone()
        self.db.execute(
            "INSERT INTO messages (sender_key, sender_name, to_key, to_name, machine, tool,"
            " project, text, created, kind, knock_id) VALUES (?,?,?,?,?,?,?,?,?,'door',?)",
            (by_key, by_name, k["from_key"], knocker["name"] if knocker else "",
             knocker["machine"] if knocker else "", knocker["tool"] if knocker else "",
             knocker["project"] if knocker else "", text, now, knock_id))
        return scope, who

    def door_close(self, by_key: str, to: str) -> None:
        """Close every open door between this session and the one named `to`."""
        with self.lock:
            now = self.clock()
            by = self._session(by_key)
            other = self._online_session(to, now, by["person_id"]) or self.db.execute(
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

    def doors(self, sides: bool = False) -> List[dict]:
        """Every open door, for the admin. With `sides`, each also has `_a` and `_b`: its two
        sessions (see `_side`), for masking."""
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
                if sides:
                    out[-1].update(_a=self._side(d["a_key"]), _b=self._side(d["b_key"]))
            return out

    def _side(self, key) -> Optional[dict]:
        """A session's {person_id, machine, tool, project}, or None (no such session, a page)."""
        r = self.db.execute("SELECT person_id, machine, tool, project FROM agents WHERE key=?",
                            (key,)).fetchone()
        return dict(r) if r else None

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

    def knocks(self, principal: Principal, sides: bool = False) -> List[dict]:
        """Unanswered knocks: all for the admin, else the ones to or from the principal. With
        `sides`, each also has `_from`: the knocking session (see `_side`), for masking."""
        with self.lock:
            rows = self.db.execute(
                "SELECT k.*, s.name AS from_session FROM knocks k LEFT JOIN agents s"
                " ON s.key=k.from_key WHERE k.answered_at IS NULL ORDER BY k.id").fetchall()
            return [{"id": k["id"], "from": self._person_name(k["from_person"]),
                     "from_session": k["from_session"] or "",
                     "to": self._person_name(k["to_person"]), "about": k["about"],
                     "created": k["created"],
                     **({"_from": self._side(k["from_key"])} if sides else {})}
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
        check_secrets(text)
        with self.lock:
            row = self.db.execute("SELECT name, person_id FROM agents WHERE key=?", (key,)).fetchone()
            if row is None:
                raise KaptyorkaError("unknown session")
            if project is not None and row["person_id"] is None:  # nobody to check access for
                raise KaptyorkaError(f"no access to project {project}; ask with request_access")
            author = row["name"]  # a session of no person signs with its session name
            if row["person_id"] is not None:
                who = self._person_principal(row["person_id"])
                self._check_project(project, who, write=True)
                author = who.person_name if who else author
            self._note(topic, text, author, project)

    def notes_write_from_web(self, name: str, topic: str, text: str,
                             project: Optional[str] = None,
                             principal: Optional[Principal] = None) -> None:
        """Write a note from the page; with `principal`, under its project write rules."""
        name, topic = _person(name), topic.strip()
        if not topic:
            raise KaptyorkaError("topic is empty")
        check_secrets(text)
        with self.lock:
            self._check_project(project or None, principal, write=True)
            self._note(topic, text, name, project or None)

    def project_members(self) -> Dict[str, List[dict]]:
        """Each registered project's roles by person, for the admin's page."""
        with self.lock:
            out: Dict[str, List[dict]] = {}
            for r in self.db.execute("SELECT a.project, p.name, a.role FROM project_access a"
                                     " JOIN people p ON p.id=a.person_id ORDER BY a.project, p.name"):
                out.setdefault(r["project"], []).append({"person": r["name"], "role": r["role"]})
            return out

    def machine_grants(self) -> Dict[str, List[str]]:
        """People granted use of each machine, for the admin's page."""
        with self.lock:
            out: Dict[str, List[str]] = {}
            for r in self.db.execute("SELECT a.host, p.name FROM machine_access a"
                                     " JOIN people p ON p.id=a.person_id ORDER BY a.host, p.name"):
                out.setdefault(r["host"], []).append(r["name"])
            return out

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
