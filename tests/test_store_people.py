import getpass
import hashlib
import shutil
import sqlite3
from pathlib import Path

import pytest

from kaptyorka.store import KaptyorkaError, Store

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
    assert s.notes_read("proj-a", project="proj-a").project == "proj-a"  # topic == a seen folder
    assert s.notes_read("proj-a") is None  # a note is (project, topic): not a team note
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
    row = s.log()[0]
    assert (row["time"], row["person"], row["kind"], row["detail"]) == (1234.0, "alice", "invite", "bob")


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


def test_heartbeat_records_host_without_owner(tmp_path):
    s = Store(tmp_path / "hub.db")
    pid, token = _member(s, "u1")
    p = s.authenticate(token)
    s.heartbeat("u1@h1", [{"key": "k:1", "tool": "codex", "project": "p"}], principal=p,
                facts=_facts("h1", "osu", "0.2.0"))
    a = s.agent("k:1")
    assert (a.host, a.os_user) == ("h1", "osu")
    root, tr = _member(s, "root", "admin")
    assert s.machines(p) != []  # u1 has a device there
    [m] = s.machines(s.authenticate(tr))
    assert m["host"] == "h1" and m["owner_person_id"] is None and m["owner"] is None
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
    assert m["owner_person_id"] is None  # first sight owns nothing; the admin names an owner
    s.set_machine_owner("h1", p2, "root")
    [m] = s.machines(s.authenticate(ta))
    assert (m["owner_person_id"], m["owner"]) == (p2, "u2")
    assert s.may_use("h1", p2) and not s.may_use("h1", p1)
    assert [(r["person"], r["detail"]) for r in s.log() if r["kind"] == "machine_owner"] == [
        ("root", "h1 -> u2")]
    s.set_machine_owner("h1", None, "root")
    assert not s.may_use("h1", p2)
    assert sorted(d["person"] for d in m["devices"]) == ["u1", "u2"]
    s.close()


def test_may_use_owner_admin_grant(tmp_path):
    s = Store(tmp_path / "hub.db")
    o, to = _member(s, "owner")
    other, _ = _member(s, "other")
    friend, _ = _member(s, "friend")
    root, _ = _member(s, "root", "admin")
    s.heartbeat("m", [], principal=s.authenticate(to), facts=_facts("h1"))
    assert not s.may_use("h1", o) and s.may_use("h1", root)  # reporting a host is not owning it
    s.set_machine_owner("h1", o, "root")
    assert s.may_use("h1", o)
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


def test_project_register_roles_and_log(tmp_path):
    import pytest
    from kaptyorka.store import KaptyorkaError

    s = Store(tmp_path / "hub.db")
    ann, ta = _member(s, "ann")
    ben, _ = _member(s, "ben")
    root, tr = _member(s, "root", "admin")
    s.project_register("shop", description="d", repo="r", by="ann", by_person_id=ann)
    assert s.project_role("shop", ann) == "manage"
    assert s.project_role("shop", ben) is None
    assert s.project_role("shop", root) == "manage" and s.project_role("lab", root) == "manage"
    with pytest.raises(KaptyorkaError, match="a project named shop already exists"):
        s.project_register("shop", by="root")
    for bad in ("", "a b", "x" * 65, "a/b", "p:q"):
        with pytest.raises(KaptyorkaError):
            s.project_register(bad, by="root")
    s.grant_project("shop", ben, "read", by="root")
    assert s.project_role("shop", ben) == "read"
    s.grant_project("shop", ben, "write", by="root")  # a new grant replaces the role
    assert s.project_role("shop", ben) == "write"
    with pytest.raises(KaptyorkaError):
        s.grant_project("shop", ben, "owner", by="root")
    with pytest.raises(KaptyorkaError, match="no project named lab"):
        s.grant_project("lab", ben, "read", by="root")
    with pytest.raises(KaptyorkaError, match="no such person"):
        s.grant_project("shop", 999, "read", by="root")
    s.revoke_project("shop", ben, by="root")
    assert s.project_role("shop", ben) is None
    rows = [(r["person"], r["kind"], r["detail"]) for r in
            s.db.execute("SELECT person, kind, detail FROM log WHERE kind IN"
                         " ('project_register','grant','revoke') ORDER BY id")]
    assert rows == [("ann", "project_register", "shop"),
                    ("root", "grant", "project:shop:read -> ben"),
                    ("root", "grant", "project:shop:write -> ben"),
                    ("root", "revoke", "project:shop -> ben")]
    s.close()


def test_may_read_write_project(tmp_path):
    import pytest
    from kaptyorka.store import KaptyorkaError

    s = Store(tmp_path / "hub.db")
    ann, ta = _member(s, "ann")
    ben, tb = _member(s, "ben")
    _, tr = _member(s, "root", "admin")
    pa, pb, pr = s.authenticate(ta), s.authenticate(tb), s.authenticate(tr)
    s.seen("k:a", "pa", "codex", "lab", principal=pa)
    assert s.may_read_project("lab", pa) and s.may_write_project("lab", pa)
    assert not s.may_read_project("lab", pb) and not s.may_write_project("lab", pb)
    assert s.may_read_project("lab", pr) and s.may_write_project("lab", pr)
    s.project_register("lab", by="root")
    assert not s.may_read_project("lab", pa)  # registered: only roles count
    s.grant_project("lab", ann, "read", by="root")
    assert s.may_read_project("lab", pa) and not s.may_write_project("lab", pa)
    s.grant_project("lab", ann, "manage", by="root")
    assert s.may_write_project("lab", pa)
    s.seen("k:nobody", "pz", "codex", "lab")  # a session with no person
    with pytest.raises(KaptyorkaError, match="no access to project lab"):
        s.notes_write("k:nobody", "t", "x", project="lab")
    s.notes_write("k:nobody", "t", "team")  # team notes stay open
    s.close()


SECRETS = ["-----BEGIN RSA PRIVATE KEY-----", "sk-" + "a" * 20, "ghp_" + "b" * 30,
           "glpat-" + "c" * 20, "AKIA" + "A" * 16, "xoxb-" + "d" * 20]


def test_secret_patterns_blocked_and_plain_text_ok(tmp_path):
    import secrets as std

    s = Store(tmp_path / "hub.db")
    _, ta = _member(s, "u1", "admin")
    s.heartbeat("m", [{"key": "k1", "tool": "codex", "project": "p", "activity": "x"}],
                principal=s.authenticate(ta), facts=_facts("h1"))
    keys = [r["key"] for r in s.db.execute("SELECT key FROM agents")]
    assert keys
    for bad in SECRETS:
        with pytest.raises(KaptyorkaError, match="looks like a secret"):
            s.send(keys[0], "all", f"here: {bad} ok")
        with pytest.raises(KaptyorkaError, match="looks like a secret"):
            s.notes_write(keys[0], "t", bad)
        with pytest.raises(KaptyorkaError, match="looks like a secret"):
            s.send_from_web("u1", "all", bad)
        with pytest.raises(KaptyorkaError, match="looks like a secret"):
            s.notes_write_from_web("u1", "t", bad)
    with pytest.raises(KaptyorkaError) as e:
        s.notes_write_from_web("u1", "t", "x ghp_" + "b" * 30)
    assert str(e.value) == "looks like a secret: GitHub token"
    assert s.db.execute("SELECT COUNT(*) FROM notes").fetchone()[0] == 0
    s.notes_write_from_web("u1", "t", "A normal sentence, with sk-short and " + std.token_urlsafe(32))
    s.close()


def test_grant_writes_log(tmp_path):
    s = Store(tmp_path / "hub.db")
    o, to = _member(s, "owner")
    f, _ = _member(s, "friend")
    s.heartbeat("m", [], principal=s.authenticate(to), facts=_facts("h1"))
    s.grant_machine("h1", f, "root")
    rows = [r for r in s.log() if r["kind"] == "grant"]
    assert rows and rows[0]["person"] == "root"
    assert s.log(person="root")[0]["kind"] == "grant"
    assert s.log(person="nobody") == []
    assert len(s.log(limit=1)) == 1
    s.revoke_machine("h1", f, "root")
    assert [r["kind"] for r in s.log()[:2]] == ["revoke", "grant"]  # newest first
    s.close()


def test_prune_deletes_old_messages_and_files_keeps_notes(tmp_path, home):
    from kaptyorka import paths

    clock = [1_000_000.0]
    s = Store(tmp_path / "hub.db", clock=lambda: clock[0])
    pid, ta = _member(s, "u1", "admin")
    _, tb = _member(s, "u2")
    s.heartbeat("m", [{"key": "k2", "tool": "codex", "project": "p", "activity": "x"}],
                principal=s.authenticate(tb), facts=_facts("h1"))
    s.notes_write_from_web("u1", "n", "keep me")
    fid = s.file_add("a.png", b"abc", pid)
    s.send_from_web("u1", "all", "old one")
    with s.lock:
        s.db.execute("UPDATE files SET message_id=(SELECT MAX(id) FROM messages) WHERE id=?", (fid,))
        s.db.execute("UPDATE messages SET file_id=?", (fid,))
    orphan_old = s.file_add("b.png", b"zz", pid)
    clock[0] += 90_000  # a day and a bit later
    orphan_new = s.file_add("c.png", b"yy", pid)
    s.heartbeat("m", [{"key": "k2", "tool": "codex", "project": "p", "activity": "x"}],
                principal=s.authenticate(tb), facts=_facts("h1"))
    s.send_from_web("u1", "all", "new one")
    assert s.prune(clock[0]) == {"messages": 0, "files": 1}  # only the stale unsent upload
    assert not (paths.files_dir() / str(orphan_old)).exists()
    assert (paths.files_dir() / str(orphan_new)).exists()
    clock[0] += 180 * 86400 - 90_000 + 10  # the first message passes 180 days, the second not
    assert s.retention_s() == 180 * 86400
    (paths.files_dir() / str(fid)).unlink()  # a missing file on disk is fine
    s.log_add("x", "keep", "d")
    out = s.prune(clock[0])
    assert out["messages"] >= 1 and out["files"] >= 1
    texts = [r["text"] for r in s.db.execute("SELECT text FROM messages")]
    assert "old one" not in texts and "new one" in texts
    assert s.db.execute("SELECT COUNT(*) FROM files WHERE id=?", (fid,)).fetchone()[0] == 0
    assert s.notes_read("n").text == "keep me"
    assert [r["kind"] for r in s.log()] == ["keep"]
    s.close()


def test_retention_setting_store(tmp_path):
    s = Store(tmp_path / "hub.db")
    assert s.retention_days() == 180
    s.set_retention_days(30, "root")
    assert s.retention_days() == 30 and s.retention_s() == 30 * 86400
    assert s.log()[0]["kind"] == "settings" and s.log()[0]["person"] == "root"
    for bad in (0, 3651):
        with pytest.raises(KaptyorkaError):
            s.set_retention_days(bad, "root")
    s.close()


def test_prune_loop_runs(tmp_path, monkeypatch):
    import threading

    from kaptyorka import memory

    monkeypatch.setattr(memory, "prune", lambda store, now: 0)  # Fake has no database

    from kaptyorka.retention import prune_loop

    calls, lines, stop = [], [], threading.Event()

    class Fake:
        def prune(self, now):
            calls.append(now)
            if len(calls) == 3:
                stop.set()
            return {"messages": 1 if len(calls) == 1 else 0, "files": 0}

    prune_loop(Fake(), stop, interval_s=0.01, report=lines.append)
    assert len(calls) == 3
    assert len(lines) == 1  # reported once, when something was deleted


def test_prune_loop_one_failure_does_not_skip_the_other(monkeypatch):
    import threading

    from kaptyorka import memory
    from kaptyorka.retention import prune_loop

    for store_fails in (True, False):
        ran, lines, stop = [], [], threading.Event()

        class Fake:
            def prune(self, now):
                ran.append("store")
                if store_fails:
                    raise RuntimeError("store boom")
                return {"messages": 0, "files": 0}

        def mem(store, now):
            ran.append("memory")
            stop.set()
            if not store_fails:
                raise RuntimeError("memory boom")
            return 2

        monkeypatch.setattr(memory, "prune", mem)
        prune_loop(Fake(), stop, interval_s=0.01, report=lines.append)
        assert ran == ["store", "memory"]
        if store_fails:
            assert len(lines) == 2 and "store boom" in lines[0] and "2 git facts" in lines[1]
        else:
            assert len(lines) == 1 and "memory boom" in lines[0]
