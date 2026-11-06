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


def _member(s, name, role="member"):
    from helpers import person
    return person(s, name, role)


def _facts(host, os_user="u", version="0.2.0"):
    return {"host": host, "os_user": os_user, "version": version}


def test_heartbeat_without_facts_is_fine(tmp_path):
    s = Store(tmp_path / "hub.db")
    s.heartbeat("m", [{"key": "k:1", "tool": "codex", "project": "p"}])
    assert s.agent("k:1").host == ""
    s.close()


def test_heartbeat_records_host_and_owner(tmp_path):
    s = Store(tmp_path / "hub.db")
    pid, token = _member(s, "u1")
    p = s.authenticate(token)
    s.heartbeat("u1@h1", [{"key": "k:1", "tool": "codex", "project": "p"}], principal=p,
                facts=_facts("h1", "osu", "0.2.0"))
    a = s.agent("k:1")
    assert (a.host, a.os_user) == ("h1", "osu")
    [m] = s.machines(p)
    assert m["host"] == "h1" and m["owner_person_id"] == pid and m["owner"] == "u1"
    assert m["online"] == 1
    d = m["devices"][0]
    assert d["name"] == "u1-laptop" and d["os_user"] == "osu" and d["version"] == "0.2.0"
    s.close()


def test_two_devices_one_host(tmp_path):
    s = Store(tmp_path / "hub.db")
    p1, t1 = _member(s, "u1")
    p2, t2 = _member(s, "u2")
    for t in (t1, t2):
        s.heartbeat("m", [], principal=s.authenticate(t), facts=_facts("h1"))
    admin, ta = _member(s, "root", "admin")
    [m] = s.machines(s.authenticate(ta))
    assert m["owner_person_id"] == p1  # first sight owns it
    assert sorted(d["person"] for d in m["devices"]) == ["u1", "u2"]
    s.close()


def test_may_use_owner_admin_grant(tmp_path):
    s = Store(tmp_path / "hub.db")
    o, to = _member(s, "owner")
    other, _ = _member(s, "other")
    friend, _ = _member(s, "friend")
    root, _ = _member(s, "root", "admin")
    s.heartbeat("m", [], principal=s.authenticate(to), facts=_facts("h1"))
    assert s.may_use("h1", o) and s.may_use("h1", root)
    assert not s.may_use("h1", other)
    s.grant_machine("h1", friend, "root")
    assert s.may_use("h1", friend)
    s.revoke_machine("h1", friend, "root")
    assert not s.may_use("h1", friend)
    rows = [(r["person"], r["detail"]) for r in
            s.db.execute("SELECT person, detail FROM log WHERE kind IN ('grant','revoke')")]
    assert rows == [("root", "machine:h1 -> friend")] * 2
    s.close()


def test_device_on_host_does_not_grant_use(tmp_path):
    s = Store(tmp_path / "hub.db")
    _, ta = _member(s, "u1")
    b, tb = _member(s, "u2")
    s.heartbeat("m", [], principal=s.authenticate(ta), facts=_facts("h1"))
    pb = s.authenticate(tb)
    s.heartbeat("m", [], principal=pb, facts=_facts("h1"))  # self-reported host
    assert not s.may_use("h1", b)
    assert [m["host"] for m in s.machines(pb)] == ["h1"]  # still visible
    s.grant_machine("h1", b, by="root")
    assert s.may_use("h1", b)
    s.close()
