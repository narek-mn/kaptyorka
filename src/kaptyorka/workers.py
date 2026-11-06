"""Workers: sessions a person asks the hub to start on one of their machines. Kept out of
store.py; a WorkerStore shares the Store's database, lock and clock."""
import json
import math
import re
import time
from typing import List, Optional, Tuple

from kaptyorka.orders import sign
from kaptyorka.store import ONLINE_S, PROJECT_NAME, KaptyorkaError, Principal, Store

MAX_FACT = 200
UNATTENDED_S = 120  # boss offline this long: the worker is unattended
ABANDONED_S = 86400  # unattended this long: abandoned
GONE_S = 600  # the worker's own session offline this long: closed
OFFLINE_S = 600  # bridge silent this long: warn
REOFFER_S = 60
WAITING_S = 1800  # how often a boss hears that a worker waits for a permission
WAITING = ("permission_prompt", "needs_input")
LIVE = ("starting", "up", "unattended", "abandoned")

SCHEMA = """
CREATE TABLE IF NOT EXISTS workers (
    name TEXT PRIMARY KEY, project TEXT NOT NULL, task TEXT NOT NULL, boss_key TEXT NOT NULL,
    boss_person INTEGER NOT NULL, device_id INTEGER NOT NULL, host TEXT NOT NULL,
    tool TEXT NOT NULL, folder TEXT NOT NULL DEFAULT '', worktree INTEGER NOT NULL DEFAULT 0,
    key TEXT,
    state TEXT NOT NULL CHECK(state IN ('starting','up','unattended','abandoned','closed','failed')),
    error TEXT NOT NULL DEFAULT '', created REAL NOT NULL, up_at REAL, unattended_since REAL,
    closed_at REAL, waiting_told REAL, told REAL);
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
    if "told" not in [r[1] for r in db.execute("PRAGMA table_info(workers)")]:
        db.execute("ALTER TABLE workers ADD COLUMN told REAL")  # a Task 2 database


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
    return out


def clean_facts(facts: dict) -> dict:
    """Only the known keys, with the right types; strings cut to 200."""
    facts = facts if isinstance(facts, dict) else {}
    out = {}
    if _cut(facts.get("os_user")) is not None:
        out["os_user"] = _cut(facts["os_user"])
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
    return out


def _version_tuple(v: str) -> Tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", v))


class WorkerStore:
    def __init__(self, store: Store):
        self.store = store
        self.db = store.db
        self.lock = store.lock
        self.clock = store.clock

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
                        "SELECT s.auto_update, p.role FROM devices d JOIN people p"
                        " ON p.id=d.person_id LEFT JOIN device_settings s ON s.device_id=d.id"
                        " WHERE d.id=?", (d["id"],)).fetchone()
                    d["auto_update"] = (bool(s["auto_update"]) if s["auto_update"] is not None
                                        else s["role"] != "admin")
        return rows

    # where a worker can run

    def candidates(self, principal: Principal, project: str,
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
                if not (d["person_id"] == principal.person_id
                        or (d["shared"] and self.store._may_use(d["host"], principal.person_id))):
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

    def choose(self, usable: List[dict], project: str, tool: str,
               now: float) -> Optional[dict]:
        """The best device: it has a folder for the project (and tmux unless the tool is
        Claude Code); a folder with no online session first, then the lowest load per cpu."""
        fits = [c for c in usable if c.get("folder")
                and (tool == "claude-code" or c.get("tmux"))]
        if not fits:
            return None
        with self.lock:
            busy = [self.db.execute(
                "SELECT 1 FROM agents WHERE host=? AND os_user=? AND cwd=? AND last_seen>? LIMIT 1",
                (c["host"], c["os_user"], c["folder"], now - ONLINE_S)).fetchone() is not None
                for c in fits]

        def rank(i):
            c = fits[i]
            return (busy[i], (c.get("load") or 0.0) / max(c.get("cpus") or 1, 1))

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
        attach = (f"claude attach {row['name']}" if row["tool"] == "claude-code"
                  else f"ssh {os_user}@{row['host']} -t tmux attach -t {row['name']}")
        return {"name": row["name"], "project": row["project"], "task": row["task"],
                "host": row["host"], "os_user": os_user, "tool": row["tool"],
                "folder": row["folder"], "worktree": bool(row["worktree"]),
                "state": row["state"], "error": row["error"],
                "boss": boss["name"] if boss else row["boss_key"], "key": row["key"],
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
            taken = {r["name"] for r in self.db.execute(
                "SELECT name FROM workers WHERE project=?", (project,))}
            n = 1
            while f"{project}-w{n}" in taken:
                n += 1
            name = f"{project}-w{n}"
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

    def link(self, worker: str, key: str, device_id: int) -> bool:
        with self.lock:
            w = self._row(worker)
            if w is None or w["state"] != "starting" or w["device_id"] != device_id:
                return False
            row = self.db.execute("SELECT person_id, device_id FROM agents WHERE key=?",
                                  (key,)).fetchone()
            owner = self.db.execute("SELECT person_id FROM devices WHERE id=?",
                                    (device_id,)).fetchone()
            if row is not None and (
                    row["person_id"] not in (None, owner["person_id"], w["boss_person"])
                    or row["device_id"] not in (None, device_id)):
                return False  # someone else's session
            if self.db.execute("SELECT 1 FROM workers WHERE key=? AND state IN"
                               " ('starting','up','unattended')", (key,)).fetchone():
                return False  # another worker's session
            self.db.execute("UPDATE workers SET key=?, state='up', up_at=? WHERE name=?",
                            (key, self.clock(), worker))
            if self.db.execute("SELECT 1 FROM agents WHERE key=?", (key,)).fetchone() is None:
                dev = self.db.execute("SELECT name FROM devices WHERE id=?",
                                      (device_id,)).fetchone()
                self.store._touch(key, dev["name"], w["tool"], w["project"], self.clock())
            # the session belongs to the boss's group, whoever's bridge reports it
            self.db.execute("UPDATE agents SET person_id=?, device_id=? WHERE key=?",
                            (w["boss_person"], device_id, key))
            return True

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
            rows = self.db.execute(
                "SELECT w.*, a.last_seen AS own_seen, a.activity, b.last_seen AS boss_seen"
                " FROM workers w LEFT JOIN agents a ON a.key=w.key"
                " LEFT JOIN agents b ON b.key=w.boss_key"
                " WHERE w.state IN ('up','unattended')").fetchall()
            for w in rows:
                boss_online = w["boss_key"].startswith("web:") or (  # the page: always there
                    w["boss_seen"] is not None and w["boss_seen"] > now - ONLINE_S)
                own = w["own_seen"] if w["own_seen"] is not None else w["up_at"]
                if own + ONLINE_S < now - GONE_S:
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
                    "SELECT w.name, w.boss_key, w.error FROM workers w LEFT JOIN agents b"
                    " ON b.key=w.boss_key WHERE w.state='failed' AND w.told IS NULL"
                    " AND (w.boss_key LIKE 'web:%' OR b.last_seen > ?)",
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
            if w["key"] is not None:
                self.db.execute("UPDATE messages SET to_key=?, push_state='new' WHERE to_key=?"
                                " AND sender_key=? AND read_at IS NULL",
                                (new_boss_key, w["boss_key"], w["key"]))
            return self._dict(self._row(name))

    def close(self, principal: Principal, name: str) -> dict:
        with self.lock:
            w = self._row(name)
            if w is None or not (w["boss_person"] == principal.person_id
                                 or principal.is_admin()):
                raise KaptyorkaError(f"no worker {name} of yours")
            if w["state"] in ("closed", "failed"):
                raise KaptyorkaError(f"worker {name} is already {w['state']}")
            now = self.clock()
            if w["state"] == "starting":  # nothing to kill yet: the launch is never sent
                self.db.execute("UPDATE launches SET done_at=?, ok=0, error='closed before start'"
                                " WHERE worker=? AND kind='launch' AND done_at IS NULL",
                                (now, name))
                self.db.execute("UPDATE workers SET state='closed', closed_at=? WHERE name=?",
                                (now, name))
            else:
                self._queue(w["device_id"], name, "close", {
                    "issued": now, "worker": name, "tool": w["tool"], "key": w["key"]})
            out = self._dict(self._row(name))
            if w["worktree"]:
                out["note"] = f"its worktree {w['folder']} stays in place"
            return out

    def warnings(self, now: float, hub_version: str) -> List[dict]:
        """Things a person should look at: failed launches, offline bridges, old versions,
        missing hooks or tmux, unattended or abandoned workers."""
        failed, offline, rest = [], [], []
        with self.lock:
            devices = self.db.execute(
                "SELECT d.id, d.host, d.os_user, d.shared, d.last_used, p.name AS person,"
                " f.facts FROM devices d LEFT JOIN people p ON p.id=d.person_id"
                " LEFT JOIN device_facts f ON f.device_id=d.id"
                " WHERE d.revoked_at IS NULL AND d.legacy=0 ORDER BY d.id").fetchall()
            workers = self.db.execute(
                "SELECT w.*, p.name AS person, d.os_user FROM workers w"
                " LEFT JOIN people p ON p.id=w.boss_person"
                " LEFT JOIN devices d ON d.id=w.device_id"
                " WHERE w.state IN ('unattended','abandoned')"
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
            v = facts.get("version") or ""
            if _version_tuple(v) and _version_tuple(v) < _version_tuple(hub_version):
                add(rest, f"{who} runs kaptyorka {v}; the hub runs {hub_version}")
            for tool, ok in (facts.get("hooks_ok") or {}).items():
                if ok is False:
                    add(rest, f"{tool} on {who} is set up but its kaptyorka hooks are missing")
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
                hhmm = time.strftime("%H:%M", time.localtime(w["unattended_since"]))
                rest.append({**item, "text": f"worker {w['name']} on {w['host']} is unattended"
                             f" since {hhmm}"})
        rest.sort(key=lambda x: x["host"])
        return failed + offline + rest
