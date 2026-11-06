"""SQLite storage for the hub: agents, messages and notes."""
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence

ONLINE_S = 30
LEASE_S = 60
MAX_TEXT = 32_000
RATE = (30, 60)

SCHEMA = """
CREATE TABLE IF NOT EXISTS agents (
    key TEXT PRIMARY KEY, name TEXT NOT NULL, machine TEXT NOT NULL,
    tool TEXT NOT NULL, project TEXT NOT NULL, status TEXT NOT NULL DEFAULT '',
    activity TEXT NOT NULL DEFAULT '', last_seen REAL NOT NULL,
    label TEXT NOT NULL DEFAULT '');
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


class Store:
    def __init__(self, path: Path, clock: Callable[[], float] = time.time):
        self.clock = clock
        self.lock = threading.Lock()
        self.db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        if "label" not in {r["name"] for r in self.db.execute("PRAGMA table_info(agents)")}:
            self.db.execute("ALTER TABLE agents ADD COLUMN label TEXT NOT NULL DEFAULT ''")
        self.sends: Dict[str, List[float]] = {}

    def close(self) -> None:
        with self.lock:
            self.db.close()

    # agents

    def _agent(self, row, now) -> Agent:
        return Agent(row["key"], row["name"], row["machine"], row["tool"], row["project"],
                     row["status"], row["activity"], row["last_seen"],
                     now - row["last_seen"] < ONLINE_S, row["label"])

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
                "INSERT INTO agents (key, name, machine, tool, project, activity, last_seen, label)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (key, name, machine, tool, project, activity or "", now, label or ""))
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

    def seen(self, key: str, machine: str, tool: str, project: str) -> Agent:
        with self.lock:
            now = self.clock()
            self._touch(key, machine, tool, project, now)
            row = self.db.execute("SELECT * FROM agents WHERE key=?", (key,)).fetchone()
            return self._agent(row, now)

    def heartbeat(self, machine: str, sessions: List[dict], tools: Sequence[str] = ()) -> None:
        """`tools` are the tools whose sessions this list is complete for: the rest of their agents
        on this machine are gone, so their names are free at once."""
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

    def notes_write(self, key: str, topic: str, text: str) -> None:
        with self.lock:
            row = self.db.execute("SELECT name FROM agents WHERE key=?", (key,)).fetchone()
            if row is None:
                raise KaptyorkaError("unknown session")
            self._note(topic, text, row["name"])

    def notes_write_from_web(self, name: str, topic: str, text: str) -> None:
        name, topic = _person(name), topic.strip()
        if not topic:
            raise KaptyorkaError("topic is empty")
        with self.lock:
            self._note(topic, text, name)

    def _note(self, topic, text, author):
        self.db.execute("INSERT INTO notes (topic, text, author, time) VALUES (?,?,?,?)",
                        (topic, text, author, self.clock()))

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
