"""Write tests/fakes/hub-0.1.9.db: a small hub database with the 0.1.9 schema, used to test the
migration to schema 2. Run it from the repo root: python3 tests/fakes/make_019_db.py"""
import sqlite3
from pathlib import Path

OUT = Path(__file__).with_name("hub-0.1.9.db")

SCHEMA_019 = """
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


def main() -> None:
    OUT.unlink(missing_ok=True)
    db = sqlite3.connect(str(OUT))
    db.execute("PRAGMA journal_mode=DELETE")
    db.executescript(SCHEMA_019)
    name = "alice@box-1/claude/proj-a"
    db.execute("INSERT INTO agents (key, name, machine, tool, project, last_seen)"
               " VALUES ('claude:abc', ?, 'alice@box-1', 'claude', 'proj-a', 1000.0)", (name,))
    db.execute("INSERT INTO notes (topic, text, author, time) VALUES"
               " ('proj-a', 'how proj-a runs', ?, 1001.0)", (name,))
    db.execute("INSERT INTO notes (topic, text, author, time) VALUES"
               " ('team-howto', 'how the team works', ?, 1002.0)", (name,))
    db.execute("INSERT INTO messages (sender_key, sender_name, to_key, to_name, machine, tool,"
               " project, text, created) VALUES ('web:bob', 'bob', 'claude:abc', ?,"
               " 'alice@box-1', 'claude', 'proj-a', 'hello', 1003.0)", (name,))
    db.commit()
    db.execute("VACUUM")
    db.close()


if __name__ == "__main__":
    main()
