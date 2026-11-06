import getpass
import hashlib
import shutil
import sqlite3
from pathlib import Path

from kaptyorka.store import Store

FAKES = Path(__file__).parent / "fakes"


def test_fresh_store_is_schema_2(tmp_path):
    s = Store(tmp_path / "hub.db")
    assert s.schema_version() == 2
    assert s.people() == []


def test_migrates_019_db(tmp_path):
    shutil.copy(FAKES / "hub-0.1.9.db", tmp_path / "hub.db")
    s = Store(tmp_path / "hub.db", legacy_token="old-token")
    assert s.schema_version() == 2
    admin = s.people()[0]
    assert admin["role"] == "admin" and admin["name"] == getpass.getuser()
    dev = s.devices(admin["id"])[0]
    assert dev["legacy"] and dev["name"] == "legacy"
    assert "token_hash" not in dev and "secret_hash" not in dev
    assert s.agent("claude:abc").person_id == admin["id"]
    assert s.notes_read("proj-a").project == "proj-a"  # topic == a seen folder
    assert s.notes_read("team-howto").project is None
    assert [m.text for m in s.messages()] == ["hello"]
    s.close()
    db = sqlite3.connect(str(tmp_path / "hub.db"))
    stored = db.execute("SELECT token_hash FROM devices").fetchone()[0]
    assert stored == hashlib.sha256(b"old-token").hexdigest()
    db.close()


def test_init_twice_is_idempotent(tmp_path):
    shutil.copy(FAKES / "hub-0.1.9.db", tmp_path / "hub.db")
    Store(tmp_path / "hub.db", legacy_token="old-token").close()
    s = Store(tmp_path / "hub.db", legacy_token="old-token")
    assert s.schema_version() == 2
    people = s.people()
    assert [p["role"] for p in people] == ["admin"]
    assert len(s.devices(people[0]["id"])) == 1


def test_notes_filter_by_project(tmp_path):
    shutil.copy(FAKES / "hub-0.1.9.db", tmp_path / "hub.db")
    s = Store(tmp_path / "hub.db", legacy_token="old-token")
    assert [n["topic"] for n in s.notes_list(project="proj-a")] == ["proj-a"]
    assert s.notes_read("team-howto", project="proj-a") is None
    assert [n.project for n in s.note_history("proj-a", project="proj-a")] == ["proj-a"]


def test_log_add(tmp_path):
    s = Store(tmp_path / "hub.db", clock=lambda: 1234.0)
    s.log_add("alice", "invite", "bob")
    row = s.db.execute("SELECT time, person, kind, detail FROM log").fetchone()
    assert tuple(row) == (1234.0, "alice", "invite", "bob")
