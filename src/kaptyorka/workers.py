"""Workers: sessions a person asks the hub to start on one of their machines. Kept out of
store.py; a WorkerStore shares the Store's database, lock and clock."""
import json
import math
from typing import List, Optional, Tuple

from kaptyorka.store import ONLINE_S, Principal, Store

MAX_FACT = 200

SCHEMA = """
CREATE TABLE IF NOT EXISTS workers (
    name TEXT PRIMARY KEY, project TEXT NOT NULL, task TEXT NOT NULL, boss_key TEXT NOT NULL,
    boss_person INTEGER NOT NULL, device_id INTEGER NOT NULL, host TEXT NOT NULL,
    tool TEXT NOT NULL, folder TEXT NOT NULL DEFAULT '', worktree INTEGER NOT NULL DEFAULT 0,
    key TEXT,
    state TEXT NOT NULL CHECK(state IN ('starting','up','unattended','abandoned','closed','failed')),
    error TEXT NOT NULL DEFAULT '', created REAL NOT NULL, up_at REAL, unattended_since REAL,
    closed_at REAL, waiting_told REAL);
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
