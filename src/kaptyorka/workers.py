"""Workers: sessions a person asks the hub to start on one of their machines. Kept out of
store.py; a WorkerStore shares the Store's database, lock and clock."""
import json
import math
import re
import shlex
import time
from typing import List, Optional, Tuple

from kaptyorka.adapters.base import LOGIN_COMMANDS, TOOL_NAMES
from kaptyorka.orders import sign
from kaptyorka.secrets_check import check_secrets
from kaptyorka.store import ONLINE_S, PROJECT_NAME, KaptyorkaError, Principal, Store, _check_length, _person

MAX_FACT = 200
MAX_ERROR = 500  # a no-show error names the folder and the log
UNATTENDED_S = 120  # boss offline this long: the worker is unattended
ABANDONED_S = 86400  # unattended this long: abandoned
GONE_S = 600  # the worker's own session offline this long: closed
BRIDGE_GONE_S = 3600  # bridge silent this long: its workers are closed
STARTUP_S = 120  # after the hub starts, bridges get this long to report before any is gone
OFFLINE_S = 600  # bridge silent this long: warn
REOFFER_S = 60
WAITING_S = 1800  # how often a boss hears that a worker waits for a permission
WAITING = ("permission_prompt", "needs_input")
LIVE = ("starting", "up", "unattended", "abandoned")
HUB_KEY, HUB_NAME = "hub:workers", "hub"  # the sender of worker notices; not a session

SCHEMA = """
CREATE TABLE IF NOT EXISTS workers (
    name TEXT PRIMARY KEY, project TEXT NOT NULL, task TEXT NOT NULL, boss_key TEXT NOT NULL,
    boss_person INTEGER NOT NULL, device_id INTEGER NOT NULL, host TEXT NOT NULL,
    tool TEXT NOT NULL, folder TEXT NOT NULL DEFAULT '', worktree INTEGER NOT NULL DEFAULT 0,
    key TEXT,
    state TEXT NOT NULL CHECK(state IN ('starting','up','unattended','abandoned','closed','failed')),
    error TEXT NOT NULL DEFAULT '', created REAL NOT NULL, up_at REAL, unattended_since REAL,
    closed_at REAL, waiting_told REAL, told REAL, needs_brief INTEGER NOT NULL DEFAULT 0,
    manager INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS launches (
    id INTEGER PRIMARY KEY AUTOINCREMENT, device_id INTEGER NOT NULL, worker TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('launch','close')), body TEXT NOT NULL,
    created REAL NOT NULL, offered_at REAL, done_at REAL, ok INTEGER,
    error TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS device_facts (
    device_id INTEGER PRIMARY KEY, facts TEXT NOT NULL, folders TEXT NOT NULL DEFAULT '{}',
    updated REAL NOT NULL);
CREATE TABLE IF NOT EXISTS device_settings (device_id INTEGER PRIMARY KEY, auto_update INTEGER);
"""


def ensure_schema(db) -> None:
    db.executescript(SCHEMA)
    cols = [r[1] for r in db.execute("PRAGMA table_info(workers)")]
    if "told" not in cols:
        db.execute("ALTER TABLE workers ADD COLUMN told REAL")  # a Task 2 database
    if "needs_brief" not in cols:
        db.execute("ALTER TABLE workers ADD COLUMN needs_brief INTEGER NOT NULL DEFAULT 0")
    if "manager" not in cols:
        db.execute("ALTER TABLE workers ADD COLUMN manager INTEGER NOT NULL DEFAULT 0")


SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def safe_name(value) -> str:
    """A host or os user name fit for an ssh line (never an option); "" otherwise."""
    return value if isinstance(value, str) and SAFE_NAME.match(value) else ""


def _cut(value) -> Optional[str]:
    return value[:MAX_FACT] if isinstance(value, str) else None


def _str_map(value, kind) -> dict:
    """A {string: value} map with string keys cut, keeping only values `kind` accepts."""
    if not isinstance(value, dict):
        return {}
    out = {}
    for k, v in value.items():
        v = kind(v)
        if isinstance(k, str) and v is not None:
            out[k[:MAX_FACT]] = v
    return out


def _bool(value) -> Optional[bool]:
    return value if isinstance(value, bool) else None


def _git(value) -> Optional[dict]:
    if not isinstance(value, dict):
        return None
    out = {}
    for k in ("branch", "subject"):
        if _cut(value.get(k)) is not None:
            out[k] = _cut(value[k])
    if _bool(value.get("dirty")) is not None:
        out["dirty"] = value["dirty"]
    age = value.get("age")  # seconds since the bridge read git (0.6+): no clock compared
    if isinstance(age, (int, float)) and not isinstance(age, bool) \
            and math.isfinite(age) and age >= 0:
        out["age"] = float(min(age, 86400))
    read_at = value.get("read_at")  # 0.3-0.5 bridges: their own clock; missing: the hub's now
    if isinstance(read_at, (int, float)) and not isinstance(read_at, bool) \
            and math.isfinite(read_at):
        out["read_at"] = float(read_at)
    return out


def _harness(raw: dict) -> dict:
    out = {}
    for tool in ("claude-code", "opencode", "codex", "cursor"):
        h = raw.get(tool)
        if isinstance(h, dict) and isinstance(h.get("rules"), bool) and isinstance(h.get("hooks"), bool) \
                and (h.get("skills") is None or isinstance(h["skills"], bool)):
            out[tool] = {"rules": h["rules"], "hooks": h["hooks"], "skills": h.get("skills")}
    return out


def clean_facts(facts: dict) -> dict:
    """Only the known keys, with the right types; strings cut to 200."""
    facts = facts if isinstance(facts, dict) else {}
    out = {}
    if _cut(facts.get("os_user")) is not None:
        out["os_user"] = safe_name(_cut(facts["os_user"]))
    if _cut(facts.get("version")) is not None:
        out["version"] = _cut(facts["version"])
    if _bool(facts.get("tmux")) is not None:
        out["tmux"] = facts["tmux"]
    load = facts.get("load")
    if isinstance(load, (int, float)) and not isinstance(load, bool) and math.isfinite(load):
        out["load"] = float(load)
    cpus = facts.get("cpus")
    if isinstance(cpus, int) and not isinstance(cpus, bool):
        out["cpus"] = cpus
    for key, kind in (("tools", _cut), ("hooks_ok", _bool), ("git", _git)):
        if key in facts:
            out[key] = _str_map(facts[key], kind)
    if isinstance(facts.get("harness"), dict):
        out["harness"] = _harness(facts["harness"])
    if isinstance(facts.get("logins"), dict):
        out["logins"] = {t: v for t, v in facts["logins"].items()
                         if t in TOOL_NAMES and (v is None or isinstance(v, bool))}
    return out


def _version_tuple(v: str) -> Tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", v))


class WorkerStore:
    def __init__(self, store: Store, started: Optional[float] = None):
        self.store = store
        self.db = store.db
        self.lock = store.lock
        self.clock = store.clock
        self.started = store.clock() if started is None else started  # the hub's start

    # device facts

    def facts_seen(self, device_id: int, facts: dict, folders: dict) -> None:
        facts, folders = clean_facts(facts), _str_map(folders, _cut)
        with self.lock:
            self.db.execute(
                "INSERT INTO device_facts (device_id, facts, folders, updated) VALUES (?,?,?,?)"
                " ON CONFLICT(device_id) DO UPDATE SET facts=excluded.facts,"
                " folders=excluded.folders, updated=excluded.updated",
                (device_id, json.dumps(facts), json.dumps(folders), self.clock()))

    def _facts_row(self, device_id) -> Tuple[dict, dict]:
        row = self.db.execute("SELECT facts, folders FROM device_facts WHERE device_id=?",
                              (device_id,)).fetchone()
        return (json.loads(row["facts"]), json.loads(row["folders"])) if row else ({}, {})

    def facts(self, device_id: int) -> dict:
        with self.lock:
            return self._facts_row(device_id)[0]

    def folders(self, device_id: int) -> dict:
        with self.lock:
            return self._facts_row(device_id)[1]

    def set_auto_update(self, device_id: int, on: Optional[bool]) -> None:
        with self.lock:
            self.db.execute(
                "INSERT INTO device_settings (device_id, auto_update) VALUES (?,?)"
                " ON CONFLICT(device_id) DO UPDATE SET auto_update=excluded.auto_update",
                (device_id, None if on is None else int(on)))

    def machine_rows(self, principal: Principal) -> List[dict]:
        """store.machines with each device's facts, folders, bridge online and auto_update."""
        rows = self.store.machines(principal)
        with self.lock:
            now = self.clock()
            for m in rows:
                for d in m["devices"]:
                    d["facts"], d["folders"] = self._facts_row(d["id"])
                    d["online"] = d["last_used"] is not None and d["last_used"] > now - ONLINE_S
                    s = self.db.execute(
                        "SELECT s.auto_update, p.role, d.person_id FROM devices d JOIN people p"
                        " ON p.id=d.person_id LEFT JOIN device_settings s ON s.device_id=d.id"
                        " WHERE d.id=?", (d["id"],)).fetchone()
                    if s["person_id"] != principal.person_id and not (
                            principal.is_admin() and not principal.legacy):
                        # someone else's account: their project names and commits are theirs
                        d["folders"] = {}
                        d["facts"].pop("git", None)
                    d["auto_update"] = (bool(s["auto_update"]) if s["auto_update"] is not None
                                        else s["role"] != "admin")
        return rows

    # where a worker can run

    def candidates(self, principal: Principal, project: str,
                   machine: Optional[str]) -> Tuple[List[dict], List[str]]:
        return self.candidates_for(principal.person_id, project, machine)

    def candidates_for(self, person_id: int, project: str,
                       machine: Optional[str]) -> Tuple[List[dict], List[str]]:
        """Devices this person may start a worker on, and one sentence per device skipped.
        Devices they may not use at all are not mentioned."""
        usable, reasons = [], []
        with self.lock:
            now = self.clock()
            for d in self.db.execute(
                    "SELECT d.*, p.name AS person FROM devices d JOIN people p ON p.id=d.person_id"
                    " WHERE d.revoked_at IS NULL AND d.host != '' ORDER BY d.host, d.id").fetchall():
                if machine is not None and d["host"] != machine:
                    continue
                if not (d["person_id"] == person_id
                        or (d["shared"] and self.store._may_use(d["host"], person_id))):
                    continue
                who = f"{d['host']} ({d['person']})"
                if d["last_used"] is None or d["last_used"] <= now - ONLINE_S:
                    reasons.append(f"{who}: bridge offline")
                    continue
                if not d["secret_hash"]:
                    reasons.append(f"{who}: still on the shared token; invite it or assign it so"
                                   " it gets its own token")
                    continue
                facts, folders = self._facts_row(d["id"])
                usable.append({"device_id": d["id"], "host": d["host"], "os_user": d["os_user"],
                               "person_id": d["person_id"], "tmux": bool(facts.get("tmux")),
                               "load": facts.get("load"), "cpus": facts.get("cpus"),
                               "tools": facts.get("tools", {}), "folder": folders.get(project)})
        return usable, reasons

    def choose(self, usable: List[dict], project: str, tool: str, now: float,
               person_id: int) -> Optional[dict]:
        """The best device: it has a folder for the project (and tmux unless the tool is
        Claude Code); the person's own account first, then a folder with no online session,
        then the lowest load per cpu. A shared account on a host where the person has an own
        online one is never chosen."""
        own_hosts = {c["host"] for c in usable if c.get("person_id") == person_id}
        fits = [c for c in usable if c.get("folder")
                and (tool == "claude-code" or c.get("tmux"))
                and (c.get("person_id") == person_id or c["host"] not in own_hosts)]
        if not fits:
            return None
        with self.lock:
            busy = [self.db.execute(
                "SELECT 1 FROM agents WHERE host=? AND os_user=? AND cwd=? AND last_seen>? LIMIT 1",
                (c["host"], c["os_user"], c["folder"], now - ONLINE_S)).fetchone() is not None
                for c in fits]

        def rank(i):
            c = fits[i]
            return (c.get("person_id") != person_id, busy[i],
                    (c.get("load") or 0.0) / max(c.get("cpus") or 1, 1))

        best = min(range(len(fits)), key=rank)
        return {**fits[best], "occupied": busy[best]}

    # launches and the worker's life

    def _person_name(self, person_id) -> str:
        row = self.db.execute("SELECT name FROM people WHERE id=?", (person_id,)).fetchone()
        return row["name"] if row else ""

    def _signing(self, device_id) -> str:
        row = self.db.execute("SELECT secret_hash FROM devices WHERE id=?",
                              (device_id,)).fetchone()
        if row is None or not row["secret_hash"]:
            raise KaptyorkaError("that device has no signing secret")
        return row["secret_hash"]

    def _queue(self, device_id, worker, kind, order) -> int:
        """Insert a launches row whose body is `order` signed, with the row's id in it."""
        lid = self.db.execute(
            "INSERT INTO launches (device_id, worker, kind, body, created) VALUES (?,?,?,'',?)",
            (device_id, worker, kind, order["issued"])).lastrowid
        order = {"kind": kind, "id": lid, **order}
        self.db.execute("UPDATE launches SET body=? WHERE id=?",
                        (json.dumps(sign(order, self._signing(device_id))), lid))
        return lid

    def _dict(self, row) -> dict:
        dev = self.db.execute("SELECT os_user FROM devices WHERE id=?",
                              (row["device_id"],)).fetchone()
        boss = self.db.execute("SELECT name FROM agents WHERE key=?",
                               (row["boss_key"],)).fetchone()
        os_user = dev["os_user"] if dev else ""
        # bridge-reported values: quoted; `=` makes tmux match the whole name, not a prefix
        dest, name = shlex.quote(f"{os_user}@{row['host']}"), shlex.quote(row["name"])
        attach = f"ssh -t -- {dest} " + (f"claude attach {name}" if row["tool"] == "claude-code"
                                         else f"tmux attach -t ={name}")
        return {"name": row["name"], "project": row["project"], "task": row["task"],
                "host": row["host"], "os_user": os_user, "tool": row["tool"],
                "folder": row["folder"], "worktree": bool(row["worktree"]), "manager": bool(row["manager"]),
                "state": row["state"], "error": row["error"],
                "boss": boss["name"] if boss else row["boss_key"],
                "person": self._person_name(row["boss_person"]), "key": row["key"],
                "attach": attach}

    def _row(self, name):
        return self.db.execute("SELECT * FROM workers WHERE name=?", (name,)).fetchone()

    def create(self, principal: Principal, project: str, task: str, device: dict, tool: str,
               boss_key: str, resume: Optional[str], occupied: bool) -> dict:
        task = (task or "").strip()
        if not task:
            raise KaptyorkaError("a worker needs a task")
        if not PROJECT_NAME.match(project or ""):
            raise KaptyorkaError(f"bad project name: {project!r}")
        folder = device["folder"]
        with self.lock:
            now = self.clock()
            boss = self.db.execute("SELECT name FROM agents WHERE key=?", (boss_key,)).fetchone()
            if boss is None:
                raise KaptyorkaError("unknown boss session")
            self._signing(device["device_id"])  # before anything is written
            base = project.replace(".", "-")  # tmux turns dots in session names into _
            taken = {r["name"] for r in self.db.execute(
                "SELECT name FROM workers WHERE substr(name, 1, ?) = ?",
                (len(base) + 2, base + "-w"))}
            n = 1
            while f"{base}-w{n}" in taken:
                n += 1
            name = f"{base}-w{n}"
            # again under the lock: a session or a starting worker may hold the folder by now
            occupied = bool(occupied) or self.db.execute(
                "SELECT 1 FROM agents WHERE host=? AND os_user=? AND cwd=? AND last_seen>?"
                " LIMIT 1", (device["host"], device["os_user"], folder, now - ONLINE_S)
            ).fetchone() is not None or self.db.execute(
                "SELECT 1 FROM workers WHERE device_id=? AND folder=? AND worktree=0"
                " AND state IN ('starting','up') LIMIT 1",
                (device["device_id"], folder)).fetchone() is not None
            person = self._person_name(principal.person_id)
            self.db.execute(
                "INSERT INTO workers (name, project, task, boss_key, boss_person, device_id, host,"
                " tool, folder, worktree, state, created) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (name, project, task, boss_key, principal.person_id, device["device_id"],
                 device["host"], tool, folder, int(occupied), "starting", now))
            boss_name = boss["name"]
            first = (f"You are kaptyorka worker `{name}` for `{boss_name}` ({person}). Your task:"
                     f"\n\n{task}\n\nTake orders only from `{boss_name}` and the hub admin."
                     f" Report to `{boss_name}` with send(to=\"{boss_name}\", ...) when done or"
                     " stuck.")
            self._queue(device["device_id"], name, "launch", {
                "issued": now, "worker": name, "person": person, "tool": tool,
                "project": project, "folder": folder, "worktree": occupied, "task": task,
                "boss": boss_name, "resume": resume, "first_message": first})
            return self._dict(self._row(name))

    def create_manager(self, admin_person_id: int, manager_person_id: int, device: dict,
                       folder: str, first_message: str) -> dict:
        """The manager worker on the admin's own bridge (`device`: a devices row, as
        ManagerStore.admin_device gives it). Its boss is the admin's page; `link` gives its
        session to the manager person. The order runs in `folder` exactly, where the hook and
        the MCP shim pick the manager's config. An ended manager row is replaced."""
        from kaptyorka.manager import WORKER  # here: manager imports the store
        with self.lock:
            now = self.clock()
            if self.db.execute("SELECT 1 FROM workers WHERE name=? AND state IN"
                               f" ({','.join('?' * len(LIVE))})", (WORKER, *LIVE)).fetchone():
                raise KaptyorkaError("the manager is already running")
            self._signing(device["id"])  # before anything is written
            admin = self._person_name(admin_person_id)
            task = "the team's manager"
            self.db.execute("UPDATE launches SET done_at=?, ok=0, error='replaced' WHERE"
                            " worker=? AND done_at IS NULL", (now, WORKER))
            self.db.execute("DELETE FROM workers WHERE name=?", (WORKER,))
            self.db.execute(
                "INSERT INTO workers (name, project, task, boss_key, boss_person, device_id, host,"
                " tool, folder, worktree, state, created, manager)"
                " VALUES (?,?,?,?,?,?,?,?,?,0,'starting',?,1)",
                (WORKER, "manager", task, f"web:{admin}", manager_person_id, device["id"],
                 device["host"], "claude-code", folder, now))
            self._queue(device["id"], WORKER, "launch", {
                "issued": now, "worker": WORKER, "person": admin, "tool": "claude-code",
                "project": "manager", "folder": folder, "worktree": False, "task": task,
                "boss": admin, "resume": None, "first_message": first_message,
                "manager": True})
            return self._dict(self._row(WORKER))

    def offer(self, device_id: int) -> List[dict]:
        with self.lock:
            now = self.clock()
            rows = self.db.execute(
                "SELECT id, body FROM launches WHERE device_id=? AND done_at IS NULL"
                " AND (offered_at IS NULL OR offered_at < ?) ORDER BY id",
                (device_id, now - REOFFER_S)).fetchall()
            for r in rows:
                self.db.execute("UPDATE launches SET offered_at=? WHERE id=?", (now, r["id"]))
            return [{**json.loads(r["body"]), "launch": True} for r in rows]

    def ack(self, launch_id: int, ok: bool, error: str, device_id: int) -> bool:
        error = (error or "")[:MAX_FACT]
        with self.lock:
            now = self.clock()
            row = self.db.execute("SELECT * FROM launches WHERE id=?", (launch_id,)).fetchone()
            if row is None or row["device_id"] != device_id:
                return False
            if row["done_at"] is not None:
                return True
            self.db.execute("UPDATE launches SET done_at=?, ok=?, error=? WHERE id=?",
                            (now, int(bool(ok)), error, launch_id))
            if row["kind"] == "launch" and not ok:
                self.db.execute("UPDATE workers SET state='failed', error=?, closed_at=?"
                                " WHERE name=? AND state='starting'",
                                (error or "launch failed", now, row["worker"]))
            elif row["kind"] == "close" and ok:
                self.db.execute("UPDATE workers SET state='closed', closed_at=? WHERE name=?",
                                (now, row["worker"]))
            return True

    def link(self, worker: str, key: str, device_id: int, needs_brief: bool = False) -> bool:
        """`needs_brief`: the tool could not take the first message on its command line, so
        the session's start brief carries it. Linking again with the same key is a success:
        the bridge retries when the hub's answer was lost."""
        with self.lock:
            w = self._row(worker)
            if w is not None and w["state"] == "up" and w["key"] == key \
                    and w["device_id"] == device_id:
                return True
            if w is None or w["state"] != "starting" or w["device_id"] != device_id:
                return False
            row = self.db.execute("SELECT person_id, device_id FROM agents WHERE key=?",
                                  (key,)).fetchone()
            owner = self.db.execute("SELECT person_id FROM devices WHERE id=?",
                                    (device_id,)).fetchone()
            # the manager's own session may have called the hub (its token) before this link
            own = w["manager"] and row is not None and row["person_id"] == w["boss_person"] \
                and row["device_id"] is not None and self.db.execute(
                    "SELECT 1 FROM devices WHERE id=? AND person_id=? AND revoked_at IS NULL",
                    (row["device_id"], w["boss_person"])).fetchone() is not None
            if row is not None and not own and (
                    row["person_id"] not in (None, owner["person_id"], w["boss_person"])
                    or row["device_id"] not in (None, device_id)):
                return False  # someone else's session
            if self.db.execute("SELECT 1 FROM workers WHERE key=? AND state IN"
                               " ('starting','up','unattended')", (key,)).fetchone():
                return False  # another worker's session
            self.db.execute("UPDATE workers SET key=?, state='up', up_at=?, needs_brief=?"
                            " WHERE name=?", (key, self.clock(), int(bool(needs_brief)), worker))
            if self.db.execute("SELECT 1 FROM agents WHERE key=?", (key,)).fetchone() is None:
                dev = self.db.execute("SELECT name FROM devices WHERE id=?",
                                      (device_id,)).fetchone()
                self.store._touch(key, dev["name"], w["tool"], w["project"], self.clock())
            # the session belongs to the boss's group, whoever's bridge reports it; the
            # manager's session keeps its own device
            if not own:
                self.db.execute("UPDATE agents SET person_id=?, device_id=? WHERE key=?",
                                (w["boss_person"], device_id, key))
            return True

    def state(self, worker: str, device_id: int) -> Optional[str]:
        """The worker's state when it runs on that device; None otherwise."""
        with self.lock:
            w = self._row(worker)
            return w["state"] if w is not None and w["device_id"] == device_id else None

    def fail(self, worker: str, error: str, device_id: int) -> bool:
        """The bridge could not start a worker it took: it fails with the bridge's error."""
        error = (error or "")[:MAX_ERROR] or "launch failed"
        with self.lock:
            w = self._row(worker)
            if w is None or w["state"] != "starting" or w["device_id"] != device_id:
                return False
            now = self.clock()
            self.db.execute("UPDATE workers SET state='failed', error=?, closed_at=? WHERE name=?",
                            (error, now, worker))
            self.db.execute("UPDATE launches SET done_at=?, ok=0, error=? WHERE worker=?"
                            " AND kind='launch' AND done_at IS NULL", (now, error, worker))
            return True

    def notify(self, to_key: str, text: str) -> bool:
        """A notice from the hub to a boss: a session, or `web:<person>` (the person's page).
        Written straight in: the hub belongs to no person, so send()'s door rule would refuse
        it, and a page cannot be named as a target."""
        with self.lock:
            now = self.clock()
            if to_key.startswith("web:"):
                t = {"name": to_key[4:], "machine": "", "tool": "", "project": ""}
                read_at = now  # a person reads it on the page
            else:
                t = self.db.execute("SELECT * FROM agents WHERE key=?", (to_key,)).fetchone()
                if t is None:
                    return False
                read_at = None
            self.db.execute(
                "INSERT INTO messages (sender_key, sender_name, to_key, to_name, machine, tool,"
                " project, text, created, read_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (HUB_KEY, HUB_NAME, to_key, t["name"], t["machine"], t["tool"],
                 t["project"], text, now, read_at))
            return True

    def boss_key(self, name: str) -> Optional[str]:
        with self.lock:
            row = self._row(name)
            return row["boss_key"] if row else None

    def session_online(self, key: str, now: float) -> bool:
        with self.lock:
            return self.db.execute("SELECT 1 FROM agents WHERE key=? AND last_seen>?",
                                   (key, now - ONLINE_S)).fetchone() is not None

    def session_person(self, key: str) -> Optional[int]:
        """The person an agents row belongs to (None: unknown key or no person yet)."""
        with self.lock:
            row = self.db.execute("SELECT person_id FROM agents WHERE key=?", (key,)).fetchone()
            return row["person_id"] if row else None

    def live_keys(self) -> dict:
        """{session key: worker name} for the sessions of live workers."""
        with self.lock:
            return {r["key"]: r["name"] for r in self.db.execute(
                "SELECT key, name FROM workers WHERE key IS NOT NULL AND state IN"
                f" ({','.join('?' * len(LIVE))})", LIVE)}

    def first_message(self, name: str) -> str:
        """The worker's first message, when its start brief must carry it; "" otherwise."""
        with self.lock:
            w = self._row(name)
            if w is None or not w["needs_brief"]:
                return ""
            row = self.db.execute("SELECT body FROM launches WHERE worker=? AND kind='launch'"
                                  " ORDER BY id DESC", (name,)).fetchone()
            return json.loads(row["body"]).get("first_message", "") if row else ""

    def folder_people(self, host: str, os_user: str, folder: str, now: float) -> List[str]:
        """Names of the people with an online session in that folder ("" for none known)."""
        with self.lock:
            return [r["name"] or "" for r in self.db.execute(
                "SELECT p.name FROM agents a LEFT JOIN people p ON p.id=a.person_id"
                " WHERE a.host=? AND a.os_user=? AND a.cwd=? AND a.last_seen>?",
                (host, os_user, folder, now - ONLINE_S))]

    def invite_person(self, code: str) -> str:
        """The name of the person an invite is for ("" when there is no such invite)."""
        with self.lock:
            row = self.db.execute("SELECT p.name FROM invites i JOIN people p ON p.id=i.person_id"
                                  " WHERE i.code=?", (code,)).fetchone()
            return row["name"] if row else ""

    def registered(self, project: str) -> bool:
        """True for a registered project, and for a removed (archived) one: its name is never
        someone's own unregistered folder."""
        with self.lock:
            return self.store._registered(project) or self.store._archived(project)

    def archived(self, project: str) -> bool:
        with self.lock:
            return self.store._archived(project)

    def device_exists(self, device_id: int) -> bool:
        with self.lock:
            return self.db.execute("SELECT 1 FROM devices WHERE id=?",
                                   (device_id,)).fetchone() is not None

    def auto_update(self, device_id: int) -> bool:
        """The device's setting; unset, on for a member's device and off for the admin's."""
        with self.lock:
            s = self.db.execute(
                "SELECT s.auto_update, p.role FROM devices d JOIN people p ON p.id=d.person_id"
                " LEFT JOIN device_settings s ON s.device_id=d.id WHERE d.id=?",
                (device_id,)).fetchone()
            if s is None:
                return False
            return bool(s["auto_update"]) if s["auto_update"] is not None else s["role"] != "admin"

    def by_key(self, key: str) -> Optional[dict]:
        with self.lock:
            row = self.db.execute("SELECT * FROM workers WHERE key=? ORDER BY created DESC",
                                  (key,)).fetchone()
            return self._dict(row) if row else None

    def by_name(self, name: str) -> Optional[dict]:
        with self.lock:
            row = self._row(name)
            return self._dict(row) if row else None

    def list(self, principal: Principal) -> List[dict]:
        with self.lock:
            if principal.is_admin():
                rows = self.db.execute("SELECT * FROM workers ORDER BY created, name").fetchall()
            else:
                rows = self.db.execute("SELECT * FROM workers WHERE boss_person=?"
                                       " ORDER BY created, name",
                                       (principal.person_id,)).fetchall()
            return [self._dict(r) for r in rows]

    def unattended_for(self, person_id: int) -> List[dict]:
        with self.lock:
            return [self._dict(r) for r in self.db.execute(
                "SELECT * FROM workers WHERE boss_person=? AND state IN ('unattended','abandoned')"
                " ORDER BY created, name", (person_id,)).fetchall()]

    def tick(self, now: float) -> List[Tuple[str, str]]:
        """State changes; notices (to_key, text) for online bosses."""
        notices = []
        with self.lock:
            # gone only when its bridge reports and still misses it; never just after a restart
            judging = now >= self.started + STARTUP_S
            # a bridge that never comes back: nobody could stop or report its workers
            for w in self.db.execute(
                    "SELECT w.name, w.boss_key, d.last_used, d.revoked_at FROM workers w"
                    " LEFT JOIN devices d ON d.id=w.device_id WHERE w.state IN"
                    f" ({','.join('?' * len(LIVE))})", LIVE).fetchall():
                if w["revoked_at"] is not None:
                    why = "device revoked"
                elif judging and w["last_used"] is not None \
                        and w["last_used"] < now - BRIDGE_GONE_S:
                    why = "bridge offline for over an hour"
                else:
                    continue
                self._end(w["name"], why, why, now)
                notices.append((w["boss_key"], f"worker `{w['name']}` is closed: {why}"))
            rows = self.db.execute(
                "SELECT w.*, a.last_seen AS own_seen, a.activity, b.last_seen AS boss_seen,"
                " d.last_used AS bridge_seen FROM workers w LEFT JOIN agents a ON a.key=w.key"
                " LEFT JOIN agents b ON b.key=w.boss_key LEFT JOIN devices d ON d.id=w.device_id"
                " WHERE w.state IN ('up','unattended')").fetchall()
            for w in rows:
                # the page is always there; the manager is never unattended or abandoned
                boss_online = bool(w["manager"]) or w["boss_key"].startswith("web:") or (
                    w["boss_seen"] is not None and w["boss_seen"] > now - ONLINE_S)
                own = w["own_seen"] if w["own_seen"] is not None else w["up_at"]
                bridge_online = w["bridge_seen"] is not None and \
                    w["bridge_seen"] > now - ONLINE_S
                if judging and bridge_online and own + ONLINE_S < now - GONE_S:
                    self.db.execute("UPDATE workers SET state='closed', error='session gone',"
                                    " closed_at=? WHERE name=?", (now, w["name"]))
                    continue
                if w["state"] == "up" and not boss_online and (
                        w["boss_seen"] is None or w["boss_seen"] + ONLINE_S < now - UNATTENDED_S):
                    self.db.execute("UPDATE workers SET state='unattended', unattended_since=?"
                                    " WHERE name=?", (now, w["name"]))
                    continue
                if w["state"] == "unattended" and boss_online:
                    self.db.execute("UPDATE workers SET state='up', unattended_since=NULL"
                                    " WHERE name=?", (w["name"],))
                    continue
                if w["state"] == "unattended" and w["unattended_since"] is None:
                    self.db.execute("UPDATE workers SET unattended_since=? WHERE name=?",
                                    (now, w["name"]))  # never NULL here: start its clock
                    continue
                if w["state"] == "unattended" and w["unattended_since"] < now - ABANDONED_S:
                    self.db.execute("UPDATE workers SET state='abandoned' WHERE name=?",
                                    (w["name"],))
                    continue
                if boss_online and w["activity"] in WAITING and (
                        w["waiting_told"] is None or w["waiting_told"] < now - WAITING_S):
                    self.db.execute("UPDATE workers SET waiting_told=? WHERE name=?",
                                    (now, w["name"]))
                    notices.append((w["boss_key"],
                                    f"worker `{w['name']}` is waiting for a permission; attach"
                                    f" with `{self._dict(w)['attach']}`"))
            for w in self.db.execute(
                    "SELECT w.name, w.key, w.boss_key FROM workers w JOIN agents a ON a.key=w.key"
                    " WHERE w.state='closed' AND w.error='session gone' AND a.last_seen > ?",
                    (now - ONLINE_S,)).fetchall():
                if self.db.execute("SELECT 1 FROM workers WHERE key=? AND name!=? AND state IN"
                                   f" ({','.join('?' * len(LIVE))})",
                                   (w["key"], w["name"], *LIVE)).fetchone():
                    continue  # the session is another worker's now
                self.db.execute("UPDATE workers SET state='up', error='', closed_at=NULL,"
                                " unattended_since=NULL WHERE name=?", (w["name"],))
                notices.append((w["boss_key"], f"worker `{w['name']}` is back"))
            for w in self.db.execute(
                    "SELECT w.name, w.boss_key, w.error FROM workers w LEFT JOIN agents b"
                    " ON b.key=w.boss_key WHERE w.state='failed' AND w.told IS NULL"
                    " AND (w.boss_key LIKE 'web:%' OR b.last_seen > ?)"
                    # the hub sends its own line for this one (hub.MANAGER_UNTRUSTED)
                    " AND NOT (w.manager=1 AND w.error LIKE '%does not trust%')",
                    (now - ONLINE_S,)).fetchall():
                self.db.execute("UPDATE workers SET told=? WHERE name=?", (now, w["name"]))
                notices.append((w["boss_key"],
                                f"worker `{w['name']}` failed to start: {w['error']}"))
        return notices

    def adopt(self, principal: Principal, name: str, new_boss_key: str) -> dict:
        with self.lock:
            w = self._row(name)
            if w is None or w["boss_person"] != principal.person_id:
                raise KaptyorkaError(f"no worker {name} of yours")
            if w["state"] not in ("unattended", "abandoned"):
                raise KaptyorkaError(f"worker {name} is {w['state']}, not unattended")
            boss = self.db.execute("SELECT person_id FROM agents WHERE key=?",
                                   (new_boss_key,)).fetchone()
            web = new_boss_key == f"web:{self._person_name(principal.person_id)}"
            if not web and (boss is None or boss["person_id"] != principal.person_id):
                raise KaptyorkaError("the new boss must be a session of yours")
            self.db.execute("UPDATE workers SET boss_key=?, state='up', unattended_since=NULL"
                            " WHERE name=?", (new_boss_key, name))
            moved = 0
            if w["key"] is not None:
                moved = self.db.execute(
                    "UPDATE messages SET to_key=?, push_state='new' WHERE to_key=?"
                    " AND sender_key=? AND read_at IS NULL",
                    (new_boss_key, w["boss_key"], w["key"])).rowcount
            return {**self._dict(self._row(name)), "delivered": moved}

    def close(self, principal: Principal, name: str) -> dict:
        with self.lock:
            w = self._row(name)
            if w is None or not (w["boss_person"] == principal.person_id
                                 or principal.is_admin()):
                raise KaptyorkaError(f"no worker {name} of yours")
            if w["state"] in ("closed", "failed"):
                raise KaptyorkaError(f"worker {name} is already {w['state']}")
            return self._close(w, name)

    def close_manager(self) -> Optional[dict]:
        """The hub closes a live manager worker (switched off): the same order a person's
        close sends. None when no manager worker is live."""
        from kaptyorka.manager import WORKER  # here: manager imports the store
        with self.lock:
            w = self._row(WORKER)
            if w is None or not w["manager"] or w["state"] not in LIVE:
                return None
            return self._close(w, WORKER)

    def _close(self, w, name: str) -> dict:
        """Close a live worker row; under the lock."""
        now = self.clock()
        dev = self.db.execute("SELECT last_used, revoked_at FROM devices WHERE id=?",
                              (w["device_id"],)).fetchone()
        offline = dev is None or dev["revoked_at"] is not None or \
            dev["last_used"] is None or dev["last_used"] <= now - ONLINE_S
        notes = []
        if w["state"] == "starting":  # nothing to kill yet: the launch is never sent
            self._end(name, "", "closed before start", now)
        elif offline:  # nobody would take a close order: closed here and now
            self._end(name, "", "bridge offline", now)
            notes.append(f"the bridge is offline; the session may still run on {w['host']}")
        else:
            self._queue(w["device_id"], name, "close", {
                "issued": now, "worker": name, "tool": w["tool"], "key": w["key"]})
        out = self._dict(self._row(name))
        if w["worktree"]:
            notes.append(f"its worktree {w['folder']} stays in place")
        if notes:
            out["note"] = "; ".join(notes)
        return out

    def _end(self, name: str, error: str, order_error: str, now: float) -> None:
        """Closed by the hub alone: the worker and its orders still waiting. Under the lock."""
        self.db.execute("UPDATE launches SET done_at=?, ok=0, error=? WHERE worker=?"
                        " AND done_at IS NULL", (now, order_error, name))
        self.db.execute("UPDATE workers SET state='closed', error=?, closed_at=? WHERE name=?",
                        (error, now, name))

    def abandoned(self) -> List[dict]:
        """Workers left unattended for a day (the manager's own never counts)."""
        with self.lock:
            rows = self.db.execute(
                "SELECT w.name, w.host, p.name AS person FROM workers w"
                " LEFT JOIN people p ON p.id=w.boss_person"
                " WHERE w.state='abandoned' AND w.manager=0 ORDER BY w.created, w.name").fetchall()
        return [dict(r) for r in rows]

    def shared_offline(self, now: float) -> List[dict]:
        """Shared-machine bridges silent for more than OFFLINE_S."""
        with self.lock:
            rows = self.db.execute(
                "SELECT id, host, os_user, last_used FROM devices WHERE shared=1 AND legacy=0"
                " AND revoked_at IS NULL AND last_used IS NOT NULL AND last_used < ?",
                (now - OFFLINE_S,)).fetchall()
        return [{"id": r["id"], "host": r["host"], "os_user": r["os_user"],
                 "minutes": int((now - r["last_used"]) // 60)} for r in rows]

    def failed_twice(self, now: float) -> List[dict]:
        """People with two or more failed launches in the last hour, with the latest error."""
        with self.lock:
            rows = self.db.execute(
                "SELECT w.boss_person, p.name AS person, w.error FROM workers w"
                " JOIN people p ON p.id=w.boss_person WHERE w.state='failed' AND w.manager=0"
                " AND w.created > ? ORDER BY w.created, w.name", (now - 3600,)).fetchall()
        by: dict = {}
        for r in rows:
            n = by.get(r["boss_person"], (0,))[0]
            by[r["boss_person"]] = (n + 1, r["error"], r["person"])
        return [{"person_id": k, "person": v[2], "error": v[1]} for k, v in by.items() if v[0] >= 2]

    def send_via_manager(self, sender_key: str, to: str, text: str,
                         manager_pid: Optional[int]) -> Optional[int]:
        """The manager needs no door: a send from its sessions, or to one of them, goes in
        without the door check. None when neither side is the manager (use store.send)."""
        if manager_pid is None:
            return None
        st = self.store
        check_secrets(text)
        with st.lock:
            now = st.clock()
            sender = st.db.execute("SELECT * FROM agents WHERE key=?", (sender_key,)).fetchone()
            if sender is None:
                raise KaptyorkaError("unknown sender session")
            _check_length(text)
            if to == "all":
                if sender["person_id"] != manager_pid:
                    return None
                st._rate_check(sender_key, now)
                targets = st._targets(sender_key, to, now, sender["person_id"])
            else:
                targets = st._targets(sender_key, to, now, sender["person_id"])
                pids = {t["person_id"] if "person_id" in t.keys() else None for t in targets}
                if sender["person_id"] != manager_pid and manager_pid not in pids:
                    return None
                st._rate_check(sender_key, now)
            n = st._insert(sender_key, sender["name"], targets, text, now)
            st._rate_hit(sender_key, now)
            return n

    def send_web_via_manager(self, name: str, to: str, text: str, person_id: Optional[int],
                             manager_pid: Optional[int],
                             file_id: Optional[int] = None) -> Optional[int]:
        """A person's page writing to the manager's session needs no door. None when the
        target is anyone else (use store.send_from_web)."""
        if manager_pid is None or person_id is None or to == "all":
            return None
        st = self.store
        name = _person(name)
        check_secrets(text)
        with st.lock:
            _check_length(text)
            now = st.clock()
            key = f"web:{name}"
            targets = st._targets(key, to, now, person_id)
            if not all(not t["key"].startswith("web:") and t["person_id"] == manager_pid
                       for t in targets):
                return None
            return st._insert(key, name, targets, text, now, file_id)

    def warnings(self, now: float, hub_version: str) -> List[dict]:
        """Things a person should look at: failed launches, offline bridges, old versions,
        missing hooks or tmux, unattended or abandoned workers."""
        failed, offline, rest = [], [], []
        with self.lock:
            devices = self.db.execute(
                "SELECT d.id, d.host, d.os_user, d.shared, d.version, d.last_used, p.name AS person,"
                " f.facts FROM devices d LEFT JOIN people p ON p.id=d.person_id"
                " LEFT JOIN device_facts f ON f.device_id=d.id"
                " WHERE d.revoked_at IS NULL AND d.legacy=0 ORDER BY d.id").fetchall()
            workers = self.db.execute(
                "SELECT w.*, p.name AS person, d.os_user FROM workers w"
                " LEFT JOIN people p ON p.id=w.boss_person"
                " LEFT JOIN devices d ON d.id=w.device_id"
                " WHERE (w.state IN ('unattended','abandoned') AND w.manager=0)"
                " OR (w.state='failed' AND w.created > ?) ORDER BY w.created",
                (now - 86400,)).fetchall()
        for d in devices:
            host, user = d["host"], d["os_user"]
            who = f"{host} ({user})"

            def add(out, text):
                out.append({"host": host, "os_user": user, "person": d["person"] or "",
                            "text": text})
            if d["last_used"] is not None and d["last_used"] < now - OFFLINE_S:
                add(offline, f"bridge on {who} has been offline for"
                    f" {int((now - d['last_used']) // 60)} minutes")
            facts = json.loads(d["facts"]) if d["facts"] else {}
            v = facts.get("version") or d["version"] or ""
            if _version_tuple(v) and _version_tuple(v) < _version_tuple(hub_version):
                add(rest, f"{who} runs kaptyorka {v}; the hub runs {hub_version}")
            for tool, ok in (facts.get("hooks_ok") or {}).items():
                if ok is False:
                    add(rest, f"{tool} on {who} is set up but its kaptyorka hooks are missing")
            for tool, ok in (facts.get("logins") or {}).items():
                if ok is False and tool in LOGIN_COMMANDS:
                    add(rest, f"{who}: {TOOL_NAMES[tool]} is not logged in."
                        f" Run `{LOGIN_COMMANDS[tool]}` there.")
            if d["shared"] and facts.get("tmux") is False:
                add(rest, f"{who} has no tmux: only Claude Code workers can run there")
        for w in workers:
            item = {"host": w["host"], "os_user": w["os_user"] or "", "person": w["person"] or ""}
            if w["state"] == "failed":
                failed.append({**item, "text": f"worker {w['name']} on {w['host']} failed to"
                               f" start: {w['error']}"})
            elif w["state"] == "abandoned":
                rest.append({**item, "text": f"worker {w['name']} on {w['host']} was abandoned"})
            else:
                hhmm = time.strftime("%H:%M", time.localtime(w["unattended_since"] or now))
                rest.append({**item, "text": f"worker {w['name']} on {w['host']} is unattended"
                             f" since {hhmm}"})
        rest.sort(key=lambda x: x["host"])
        return failed + offline + rest
