"""Manager: account, files, keep-alive decisions, requests, deduped pushes."""
import json
import os

import pytest

from helpers import person
from kaptyorka import manager, paths
from kaptyorka.manager import ManagerStore
from kaptyorka.store import ONLINE_S, Store

HOST = "box-1"


@pytest.fixture
def clock():
    return [1_000_000.0]


@pytest.fixture
def store(tmp_path, clock):
    s = Store(tmp_path / "hub.db", clock=lambda: clock[0])
    yield s
    s.close()


@pytest.fixture
def ms(store):
    return ManagerStore(store)


def admin_with_bridge(store, online=True):
    pid, _ = person(store, "root", "admin")
    with store.lock:
        store.db.execute("UPDATE devices SET host=?, os_user='u', secret_hash='x',"
                         " last_used=? WHERE person_id=?",
                         (HOST, store.clock() if online else None, pid))
    return pid


def test_schema_and_enable_flag(store, ms):
    tables = {r[0] for r in store.db.execute("SELECT name FROM sqlite_master")}
    assert {"work_requests", "manager_pushes"} <= tables
    assert ms.enabled()
    ms.set_enabled(False, "root")
    assert not ms.enabled()
    ms.set_enabled(True, "root")
    assert ms.enabled()
    assert [e["detail"] for e in store.log(10) if e["kind"] == "manager"] == ["on", "off"]
    assert ms.person_id() is None


def test_ensure_account_once_and_rotates_after_revoke(store, ms):
    admin_with_bridge(store)
    t1 = ms.ensure_account(HOST, "root")
    assert t1
    assert ms.ensure_account(HOST, "root") is None
    pid = ms.person_id()
    assert store.person_by_name("manager")["role"] == "manager"
    dev = store.devices(pid)[0]
    assert dev["name"] == "manager" and dev["host"] == HOST and dev["os_user"] == "u"
    assert store.authenticate(t1).person_name == "manager"
    store.device_revoke(dev["id"], "root")
    t2 = ms.ensure_account(HOST, "root")
    assert t2 and t2 != t1
    assert ms.ensure_account(HOST, "root") is None


def test_write_files_never_drops_token(tmp_path, ms):
    d = tmp_path / "m"
    ms.write_files(d, "http://hub", "tok1", "be kind")
    cfg = json.loads((d / "config.json").read_text())
    assert cfg["hub"] == "http://hub" and cfg["token"] == "tok1" and cfg["person"] == "manager"
    assert cfg["machine"].startswith("manager@")
    assert os.stat(d / "config.json").st_mode & 0o777 == 0o600
    ms.write_files(d, "http://hub", None, "be kind v2")
    assert json.loads((d / "config.json").read_text())["token"] == "tok1"
    assert "be kind v2" in (d / "CLAUDE.md").read_text()
    d2 = tmp_path / "n"
    ms.write_files(d2, "http://hub", None, "")
    assert not (d2 / "config.json").exists() and (d2 / "CLAUDE.md").exists()
    assert ms.rules_written_version() is None
    ms.mark_rules_written(3)
    assert ms.rules_written_version() == 3


def test_may_launch_rules(store, ms, clock):
    assert ms.may_launch(clock[0]) == (False, "the admin's bridge on the hub machine is offline")
    admin_with_bridge(store)
    ms.ensure_account(HOST, "root")
    assert ms.may_launch(clock[0]) == (True, "")
    ms.set_enabled(False, "root")
    ok, why = ms.may_launch(clock[0])
    assert not ok and "off" in why
    ms.set_enabled(True, "root")
    with store.lock:
        store.db.execute("UPDATE devices SET last_used=? WHERE name!='manager'",
                         (clock[0] - ONLINE_S - 1,))
    assert not ms.may_launch(clock[0])[0]
    with store.lock:
        store.db.execute("UPDATE devices SET last_used=? WHERE name!='manager'", (clock[0],))
    ms.record_launch(clock[0])
    ok, why = ms.may_launch(clock[0] + 60)
    assert not ok and "2 minutes" in why
    with store.lock:
        store.db.execute("UPDATE devices SET last_used=? WHERE name!='manager'",
                         (clock[0] + 200,))
    assert ms.may_launch(clock[0] + 200) == (True, "")
    ms.record_launch(clock[0] + 200)
    with store.lock:
        store.db.execute("UPDATE devices SET last_used=? WHERE name!='manager'",
                         (clock[0] + 400,))
    ok, why = ms.may_launch(clock[0] + 400)
    assert not ok and "twice" in why
    assert ms.launches_last_hour(clock[0] + 400) == 2
    assert ms.launches_last_hour(clock[0] + 4000) == 0
    with store.lock:
        store.db.execute("UPDATE devices SET last_used=? WHERE name!='manager'",
                         (clock[0] + 4000,))
        store.db.execute(
            "INSERT INTO workers (name, project, task, boss_key, boss_person, device_id, host,"
            " tool, state, created) VALUES ('manager','p','t','k',1,1,'box-1','claude','up',1)")
    ok, why = ms.may_launch(clock[0] + 4000)
    assert not ok and "running" in why


def test_requests_add_decide_expire(ms, clock):
    a = ms.request_add(1, "k:1", "proj-a", "fix", "claude", None, "box-1", "bob", "/x")
    b = ms.request_add(1, "k:1", "proj-a", "fix", "claude", "r1", "box-1", "bob", "/x")
    assert ms.request(a)["decision"] == "" and ms.request(b)["resume"] == "r1"
    ms.request_decide(a, "box-2", "free", "w1")
    r = ms.request(a)
    assert (r["decision"], r["reason"], r["worker"]) == ("box-2", "free", "w1")
    assert r["decided_at"] == clock[0]
    assert ms.expire_requests(clock[0] + 10) == []
    gone = ms.expire_requests(clock[0] + manager.REQUEST_TTL_S + 1)
    assert [g["id"] for g in gone] == [b]
    assert ms.request(b)["decision"] == "expired"
    assert ms.request(a)["decision"] == "box-2"
    assert ms.expire_requests(clock[0] + 99999) == []
    assert ms.request(999) is None


def test_push_once_and_ttl(store, ms, clock):
    assert ms.push_once("k", clock[0])
    assert not ms.push_once("k", clock[0] + 5)
    assert not ms.push_once("k", clock[0] + 50, ttl_s=100)
    assert ms.push_once("k", clock[0] + 100, ttl_s=100)
    assert not ms.push_once("k", clock[0] + 150, ttl_s=100)
    assert manager.prune(store, clock[0] + 100 + 30 * 86400 + 1) == 1
    assert ms.push_once("k", clock[0] + 100 + 30 * 86400 + 2)


def test_claude_md_has_rules_and_section():
    text = manager.claude_md("always test")
    assert "always test" in text and "\n\n# Manager\n\n" in text
    assert "work_decide" in text and "propose_rule" in text
    assert manager.claude_md("").startswith("No team rules yet.")


def test_text_busy():
    t = manager.text_busy("alice", "proj-a", "box-1", "bob", "/p", 7)
    assert t == ("alice wants proj-a on box-1, occupied by bob in /p. Ask them, or pick another"
                 " machine. Request 7: work_decide(7, \"box-1\" or another machine or"
                 " \"wait\", reason).")
    assert manager.request_line(7, "box-1") in t


def test_text_busy_keeps_braces():
    t = manager.text_busy("a{id}", "proj-a", "b{0}x", "{bob}", "/p/{x}", 3)
    assert t.startswith("a{id} wants proj-a on b{0}x, occupied by {bob} in /p/{x}.")
    assert t.endswith("Request 3: work_decide(3, \"b{0}x\" or another machine or \"wait\","
                      " reason).")


def test_session_key(store, ms, clock):
    admin_with_bridge(store)
    t = ms.ensure_account(HOST, "root")
    assert ms.session_key(clock[0]) is None
    store.seen("k:m", "manager@box-1", "claude", "manager", principal=store.authenticate(t))
    assert ms.session_key(clock[0]) == "k:m"
    assert ms.session_key(clock[0] + ONLINE_S + 1) is None


def test_config_path_override(tmp_path, monkeypatch):
    monkeypatch.setenv("KAPTYORKA_HOME", str(tmp_path / "h"))
    assert paths.config_path() == tmp_path / "h" / "config.json"
    alt = tmp_path / "other" / "c.json"
    alt.parent.mkdir()
    monkeypatch.setenv("KAPTYORKA_CONFIG", str(alt))
    assert paths.load_config() == {}
    paths.save_config({"a": 1})
    assert json.loads(alt.read_text()) == {"a": 1}
    assert paths.load_config() == {"a": 1}


@pytest.mark.parametrize("value", ["", "rel/c.json", "c.json"])
def test_config_path_ignores_relative_or_empty_override(tmp_path, monkeypatch, value):
    monkeypatch.setenv("KAPTYORKA_HOME", str(tmp_path / "h"))
    monkeypatch.setenv("KAPTYORKA_CONFIG", value)
    assert paths.config_path() == tmp_path / "h" / "config.json"


def test_config_written_0600_and_survives_failure(tmp_path, ms, monkeypatch):
    d = tmp_path / "m"
    os.umask(0o022)
    ms.write_files(d, "http://hub", "tok1", "r")
    assert os.stat(d / "config.json").st_mode & 0o777 == 0o600
    assert not (d / "config.json.tmp").exists()

    real = os.replace

    def boom(src, dst):
        if str(dst).endswith("config.json"):
            raise OSError("disk full")
        real(src, dst)
    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        ms.write_files(d, "http://hub", "tok2", "r")
    assert json.loads((d / "config.json").read_text())["token"] == "tok1"
    assert not (d / "config.json.tmp").exists()
