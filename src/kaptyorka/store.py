"""SQLite storage for the hub: agents, messages and notes."""
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional

ONLINE_S = 30
LEASE_S = 60
MAX_TEXT = 32_000
RATE = (30, 60)

SCHEMA = """
CREATE TABLE IF NOT EXISTS agents (
    key TEXT PRIMARY KEY, name TEXT NOT NULL, machine TEXT NOT NULL,
    tool TEXT NOT NULL, project TEXT NOT NULL, status TEXT NOT NULL DEFAULT '',
    activity TEXT NOT NULL DEFAULT '', last_seen REAL NOT NULL);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT, sender_key TEXT NOT NULL,
    sender_name TEXT NOT NULL, to_key TEXT, to_name TEXT NOT NULL,
    machine TEXT NOT NULL, tool TEXT NOT NULL, project TEXT NOT NULL,
    text TEXT NOT NULL, created REAL NOT NULL, read_at REAL,
    push_state TEXT NOT NULL DEFAULT 'new', offered_at REAL);
CREATE TABLE IF NOT EXISTS notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT, topic TEXT NOT NULL,
    text TEXT NOT NULL, author TEXT NOT NULL, time REAL NOT NULL);
"""


class KaptyorkaError(Exception):
    """Error whose message is shown to the agent."""


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


@dataclass
class Message:
    id: int
    sender: str
    to: str
    text: str
    created: float
    read: bool


@dataclass
class Note:
    topic: str
    text: str
    author: str
    time: float


class Store:
    def __init__(self, path: Path, clock: Callable[[], float] = time.time):
        self.clock = clock
        self.lock = threading.Lock()
        self.db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        self.sends: Dict[str, List[float]] = {}

    def close(self) -> None:
        with self.lock:
            self.db.close()

    # agents

    def _agent(self, row, now) -> Agent:
        return Agent(row["key"], row["name"], row["machine"], row["tool"], row["project"],
                     row["status"], row["activity"], row["last_seen"],
                     now - row["last_seen"] < ONLINE_S)

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

    def _touch(self, key, machine, tool, project, now, activity=None):
        row = self.db.execute("SELECT * FROM agents WHERE key=?", (key,)).fetchone()
        if row is None:
            name = self._free_name(key, machine, tool, project, now)
            self.db.execute(
                "INSERT INTO agents (key, name, machine, tool, project, activity, last_seen)"
                " VALUES (?,?,?,?,?,?,?)",
                (key, name, machine, tool, project, activity or "", now))
            return
        name = row["name"]
        if now - row["last_seen"] >= ONLINE_S:  # was offline: its name may have been taken
            name = self._free_name(key, row["machine"], row["tool"], row["project"], now)
        if activity is None:
            self.db.execute("UPDATE agents SET name=?, last_seen=? WHERE key=?", (name, now, key))
        else:
            self.db.execute("UPDATE agents SET name=?, last_seen=?, activity=? WHERE key=?",
                            (name, now, activity, key))

    def seen(self, key: str, machine: str, tool: str, project: str) -> Agent:
        with self.lock:
            now = self.clock()
            self._touch(key, machine, tool, project, now)
            row = self.db.execute("SELECT * FROM agents WHERE key=?", (key,)).fetchone()
            return self._agent(row, now)

    def heartbeat(self, machine: str, sessions: List[dict]) -> None:
        with self.lock:
            now = self.clock()
            for s in sessions:
                self._touch(s["key"], machine, s["tool"], s["project"], now, s.get("activity"))

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
            if len(text) > MAX_TEXT:
                raise KaptyorkaError(f"message too long: {len(text)} characters, limit {MAX_TEXT}")
            count, window = RATE
            recent = [t for t in self.sends.get(sender_key, []) if now - t < window]
            if len(recent) >= count:
                raise KaptyorkaError(f"rate limit: at most {count} sends per {window} s")
            online = self._online_rows(now)
            if to == "all":
                targets = [r for r in online if r["key"] != sender_key]
            else:
                match = [r for r in online if r["name"] == to] or self.db.execute(
                    "SELECT * FROM agents WHERE name=? ORDER BY last_seen DESC LIMIT 1",
                    (to,)).fetchall()
                if not match:
                    names = ", ".join(r["name"] for r in online) or "none"
                    raise KaptyorkaError(f"no session named {to}; online: {names}")
                targets = match[:1]
            for t in targets:
                self.db.execute(
                    "INSERT INTO messages (sender_key, sender_name, to_key, to_name, machine,"
                    " tool, project, text, created) VALUES (?,?,?,?,?,?,?,?,?)",
                    (sender_key, sender["name"], t["key"], t["name"], t["machine"],
                     t["tool"], t["project"], text, now))
            recent.append(now)
            self.sends[sender_key] = recent
            return len(targets)

    def _deliverable(self, now):
        """Unread messages paired with the online agent they go to now."""
        online = self._online_rows(now)
        by_key = {r["key"]: r for r in online}
        out = []
        for m in self.db.execute("SELECT * FROM messages WHERE read_at IS NULL ORDER BY id"):
            target = by_key.get(m["to_key"]) or next(
                (r for r in online if (r["machine"], r["tool"], r["project"])
                 == (m["machine"], m["tool"], m["project"])), None)
            if target is not None:
                out.append((m, target))
        return out

    def inbox(self, key: str) -> List[Message]:
        with self.lock:
            now = self.clock()
            rows = [m for m, t in self._deliverable(now) if t["key"] == key]
            for m in rows:
                self.db.execute("UPDATE messages SET read_at=? WHERE id=?", (now, m["id"]))
            return [Message(m["id"], m["sender_name"], m["to_name"], m["text"], m["created"], True)
                    for m in rows]

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
                                "text": f"Kaptyorka message from {m['sender_name']}:\n\n{m['text']}"})
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
                            m["read_at"] is not None) for m in reversed(rows)]

    # notes

    def notes_write(self, key: str, topic: str, text: str) -> None:
        with self.lock:
            row = self.db.execute("SELECT name FROM agents WHERE key=?", (key,)).fetchone()
            if row is None:
                raise KaptyorkaError("unknown session")
            self.db.execute("INSERT INTO notes (topic, text, author, time) VALUES (?,?,?,?)",
                            (topic, text, row["name"], self.clock()))

    def notes_list(self) -> List[dict]:
        with self.lock:
            rows = self.db.execute(
                "SELECT topic, author, time FROM notes WHERE id IN"
                " (SELECT MAX(id) FROM notes GROUP BY topic) ORDER BY id DESC").fetchall()
            return [dict(r) for r in rows]

    def notes_read(self, topic: str) -> Optional[Note]:
        history = self.note_history(topic)
        return history[0] if history else None

    def note_history(self, topic: str) -> List[Note]:
        with self.lock:
            rows = self.db.execute("SELECT * FROM notes WHERE topic=? ORDER BY id DESC",
                                   (topic,)).fetchall()
            return [Note(r["topic"], r["text"], r["author"], r["time"]) for r in rows]
