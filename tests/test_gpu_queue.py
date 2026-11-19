"""The GPU share queue: /proc/locks parsing (fixtures and the real kernel), the Machines line
for admins and members, the config route and command, and the manager holding hub work."""
import fcntl
import getpass
import json
import os
import subprocess
import sys
import time

import pytest

from helpers import person
from kaptyorka import admin_ops, usage
from kaptyorka.store import Store
from kaptyorka.workers import WorkerStore, clean_facts, clean_queue

# L is inode 100, its shares 501..510, L.queue 200
LOCKS = """\
1: FLOCK  ADVISORY  WRITE 1001 fd:01:501 0 EOF
2: FLOCK  ADVISORY  WRITE 1001 fd:01:502 0 EOF
3: FLOCK  ADVISORY  READ  1001 fd:01:100 0 EOF
4: FLOCK  ADVISORY  WRITE 2002 fd:01:503 0 EOF
5: FLOCK  ADVISORY  READ  2002 fd:01:100 0 EOF
6: FLOCK  ADVISORY  WRITE 3003 fd:01:200 0 EOF
6: -> FLOCK  ADVISORY  WRITE 4004 fd:01:200 0 EOF
6: -> FLOCK  ADVISORY  WRITE 4005 fd:01:200 0 EOF
7: POSIX  ADVISORY  WRITE 5005 fd:01:504 0 EOF
8: FLOCK  ADVISORY  WRITE 6006 fd:01:999 0 EOF
9: OFDLCK ADVISORY  READ  -1 00:06:1234 0 EOF
not a lock line
"""
USERS = {1001: "bob", 2002: "carol", 3003: "alice", 4004: "alice", 4005: "bob", 7007: "bob"}
SHARES = {500 + i: i for i in range(1, 11)}


def state(text, shares=10):
    return usage.queue_state(usage.parse_locks(text), 100, SHARES, 200,
                             lambda pid: USERS.get(pid, "unknown"), shares)


def test_parse_locks_flock_lines_only():
    got = usage.parse_locks(LOCKS)
    assert len(got) == 9  # the POSIX, OFDLCK and junk lines are skipped
    assert got[0] == {"pid": 1001, "ino": 501, "write": True, "waiting": False}
    assert got[6] == {"pid": 4004, "ino": 200, "write": True, "waiting": True}


def test_holders_and_waiters():
    q = state(LOCKS)
    assert q["holders"] == {"bob": 2, "carol": 1}  # the unknown inode 999 is nobody's share
    assert q["whole"] == ""
    # the job holding L.queue looks for room; the two blocked on it wait too
    assert q["waiting"] == 3 and q["waiters"] == {"alice": 2, "bob": 1}
    assert usage.queue_line(q, True) == "GPU queue: bob 2/10 · carol 1/10 · 3 waiting"
    assert usage.queue_line(q, False) == "GPU queue: 3/10 in use · 3 waiting"
    assert usage.queue_full(q)  # someone waits


def test_the_whole_gpu_and_a_waiter_on_the_lock():
    text = ("1: FLOCK  ADVISORY  WRITE 7007 fd:01:100 0 EOF\n"
            "1: -> FLOCK  ADVISORY  READ  2002 fd:01:100 0 EOF\n")
    q = state(text)
    assert q["whole"] == "bob" and q["waiting"] == 1 and q["holders"] == {}
    assert usage.queue_line(q, True) == "GPU queue: taken whole by bob · 1 waiting"
    assert usage.queue_line(q, False) == "GPU queue: taken whole · 1 waiting"
    assert usage.queue_full(state("1: FLOCK  ADVISORY  WRITE 7007 fd:01:100 0 EOF\n"))


def test_free_full_and_unknown():
    q = state("")
    assert usage.queue_line(q, True) == "GPU queue: free" and not usage.queue_full(q)
    full = "".join(f"{i}: FLOCK  ADVISORY  WRITE 1001 fd:01:{500 + i} 0 EOF\n" for i in range(1, 11))
    q = state(full)
    assert q["holders"] == {"bob": 10} and usage.queue_full(q)
    assert usage.queue_line(q, False) == "GPU queue: 10/10 in use"
    assert usage.queue_line(state(full, 4), False) == "GPU queue: 4/4 in use"  # never over N
    assert usage.queue_line({"unknown": True}, True) == "GPU queue: unknown"
    assert not usage.queue_full({"unknown": True}) and usage.queue_line(None, True) == ""


def test_read_queue_missing_files_are_unknown(tmp_path):
    assert usage.read_queue(str(tmp_path / "nope.lock"), 10) == {"unknown": True, "shares": 10}
    (tmp_path / "gpu.lock").write_text("")
    assert usage.read_queue(str(tmp_path / "gpu.lock"), 10, proc=tmp_path / "noproc") == \
        {"unknown": True, "shares": 10}


def test_pid_user_from_status(tmp_path):
    (tmp_path / "42").mkdir()
    (tmp_path / "42" / "status").write_text("Name:\tx\nUid:\t1000\t1000\t1000\t1000\n")
    assert usage.pid_user(tmp_path, 42, lambda uid: f"u{uid}") == "u1000"
    assert usage.pid_user(tmp_path, 43, lambda uid: "x") == "unknown"


@pytest.mark.skipif(not os.path.exists("/proc/locks"), reason="needs Linux /proc/locks")
def test_read_queue_on_the_real_kernel(tmp_path):
    """Real flocks: this process holds two shares and L shared; a child blocks wanting the
    whole GPU. The stale "<user> <pid>" text in the files is never read."""
    lock = tmp_path / "gpu.lock"
    for p in [lock] + [tmp_path / f"gpu.lock.share{i}" for i in range(1, 11)] + [tmp_path / "gpu.lock.queue"]:
        p.write_text("carol 1\n")
    held = [open(lock), open(f"{lock}.share1"), open(f"{lock}.share2")]
    fcntl.flock(held[0], fcntl.LOCK_SH)
    fcntl.flock(held[1], fcntl.LOCK_EX)
    fcntl.flock(held[2], fcntl.LOCK_EX)
    child = subprocess.Popen([sys.executable, "-c", "import fcntl,sys; fcntl.flock(open(sys.argv[1]),"
                              " fcntl.LOCK_EX)", str(lock)])
    try:
        me = getpass.getuser()
        for _ in range(100):
            q = usage.read_queue(str(lock), 10)
            if q.get("waiting"):
                break
            time.sleep(0.05)
        assert q == {"shares": 10, "holders": {me: 2}, "whole": "", "waiting": 1, "waiters": {me: 1}}
    finally:
        child.kill()
        child.wait()
        for f in held:
            f.close()
    assert usage.read_queue(str(lock), 10)["holders"] == {}


def test_hub_keeps_only_names_and_counts():
    raw = {"age": 3, "shares": 10, "holders": {"bob": 4, "-oX": 1, "carol": "x"}, "whole": "-rf",
           "waiting": 2, "waiters": {"alice": 2}, "pids": [1]}
    assert clean_queue(raw) == {"age": 3.0, "shares": 10, "holders": {"bob": 4}, "whole": "",
                                "waiting": 2, "waiters": {"alice": 2}}
    assert clean_queue({"unknown": True, "shares": 10}) == {"age": 0.0, "shares": 10, "unknown": True}
    assert "gpu_queue" in clean_facts({"gpu_queue": {"shares": 10}})


# the hub: config, one reporting bridge per computer, the Machines lines

@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "hub.db")
    yield s
    s.close()


def account(store, name, os_user, host="box-1", version="0.9.23"):
    pid = store.person_by_name(name)["id"] if store.person_by_name(name) else person(store, name)[0]
    p = store.authenticate(store.device_add(pid, f"{os_user}-{host}"))
    store.heartbeat(host, [], principal=p, facts={"host": host, "os_user": os_user})
    WorkerStore(store).facts_seen(p.device_id, {"os_user": os_user, "version": version}, {})
    return p


def report(store, p, q, version="0.9.23"):
    WorkerStore(store).facts_seen(p.device_id, {"os_user": "x", "version": version,
                                                "gpu_queue": {"age": 1, **q}}, {})


QUEUE = {"shares": 10, "holders": {"bob": 4, "carol": 6}, "whole": "", "waiting": 1,
         "waiters": {"alice": 1}}


def test_one_bridge_per_computer_reads_it(store):
    person(store, "alice", "admin")
    old = account(store, "alice", "alice", version="0.9.22")  # an older bridge cannot read it
    a = account(store, "alice", "alice2")
    b = account(store, "bob", "bob")
    ws = WorkerStore(store)
    assert ws.gpu_queue_ask(a.device_id, store.clock()) is None  # not set up
    ws.set_gpu_queue("box-1", "/srv/gpu.lock", 10, "alice")
    asks = [ws.gpu_queue_ask(d.device_id, store.clock()) for d in (old, a, b)]
    assert asks == [None, {"lock": "/srv/gpu.lock", "shares": 10}, None]
    ws.set_gpu_queue("box-1", None, by="alice")
    assert ws.gpu_queue_ask(a.device_id, store.clock()) is None
    with pytest.raises(Exception, match="full path"):
        ws.set_gpu_queue("box-1", "gpu.lock")
    with pytest.raises(Exception, match="no such machine: nope"):
        ws.set_gpu_queue("nope", "/srv/gpu.lock")


def test_admins_see_names_members_see_counts(store):
    person(store, "alice", "admin")
    a = account(store, "alice", "alice")
    b = account(store, "bob", "bob")
    account(store, "carol", "carol")
    ws = WorkerStore(store)
    ws.set_gpu_queue("box-1", "/srv/gpu.lock", 10, "alice")
    report(store, a, QUEUE)
    alice = store.authenticate(store.device_add(store.person_by_name("alice")["id"], "web"))
    mine = admin_ops.account(store, store.authenticate(
        store.device_add(store.person_by_name("bob")["id"], "web")), ws)["machines"]
    assert mine[0]["gpu_queue"] == "GPU queue: 10/10 in use · 1 waiting"
    assert "carol" not in json.dumps(mine) and "alice" not in json.dumps(mine)
    assert admin_ops.account(store, alice, ws)["machines"][0]["gpu_queue"] == \
        "GPU queue: bob 4/10 · carol 6/10 · 1 waiting"
    [m] = ws.overview()
    assert m["gpu_queue"] == "GPU queue: bob 4/10 · carol 6/10 · 1 waiting"
    assert b.device_id  # bob's own account is on the same computer
    # stale, or switched off: no line
    with store.lock:
        store.db.execute("UPDATE device_facts SET updated=updated-1000")
    assert ws.queue_text("box-1", store.clock(), True) == ""
    report(store, a, QUEUE)
    ws.set_gpu_queue("box-1", None)
    assert ws.overview()[0]["gpu_queue"] == ""


def test_queue_busy_for_the_manager(store):
    person(store, "alice", "admin")
    a = account(store, "alice", "alice")
    account(store, "bob", "bob")
    ws = WorkerStore(store)
    ws.set_gpu_queue("box-1", "/srv/gpu.lock", 10)
    report(store, a, {"shares": 10, "holders": {"bob": 3}, "whole": "", "waiting": 0, "waiters": {}})
    assert ws.queue_busy("box-1", store.clock(), "alice") is None  # room left, nobody waits
    report(store, a, {"shares": 10, "holders": {"bob": 10}, "whole": "", "waiting": 0, "waiters": {}})
    assert ws.queue_busy("box-1", store.clock(), "alice") == "GPU queue: bob 10/10"
    assert ws.queue_busy("box-1", store.clock(), "bob") is None  # only bob's own jobs
    assert ws._heavy_by_others("box-1", store.person_by_name("alice")["id"], store.clock())


def test_route_and_cli(tmp_path, monkeypatch, capsys):
    """POST /api/machines/<host>/gpu-queue (admin only) and `kaptyorka machine gpu-queue`."""
    from conftest import start_hub
    from kaptyorka import cli, http, paths
    from kaptyorka.hub import machine_route
    from kaptyorka.manager import ManagerStore
    monkeypatch.setenv("KAPTYORKA_HOME", str(tmp_path / "kh"))
    s = Store(tmp_path / "h.db")
    ManagerStore(s).set_enabled(False, "test")
    stop = start_hub(s)
    url = f"http://127.0.0.1:{stop.port}"
    assert machine_route("POST", "/api/machines/box-1/gpu-queue")
    try:
        _, admin = person(s, "alice", "admin")
        _, member = person(s, "bob")
        s.heartbeat("box-1", [], principal=s.authenticate(member), facts={"host": "box-1", "os_user": "bob"})
        path = url + "/api/machines/box-1/gpu-queue"
        assert http.request("POST", path, member, {"lock": "/srv/gpu.lock"}) == (403, "admin only")
        assert http.request("POST", path, admin, {"lock": "/srv/gpu.lock", "shares": "x"})[0] == 400
        assert http.request("POST", path, admin, {"lock": "/srv/gpu.lock", "shares": 0}) == \
            (400, "shares must be from 1 to 100")
        assert http.request("POST", url + "/api/machines/nope/gpu-queue", admin,
                            {"lock": "/srv/gpu.lock"}) == (404, "no such machine: nope")
        paths.save_config({"hub": url, "token": admin, "machine": "pc"})

        def cmd(*args):
            monkeypatch.setattr(cli.sys, "argv", ["kaptyorka", "machine", "gpu-queue", *args])
            with pytest.raises(SystemExit) as e:
                cli.entry()
            return e.value.code
        assert cmd("box-1", "/srv/gpu.lock", "--shares", "8") == 0
        assert capsys.readouterr().out == "box-1: GPU queue on /srv/gpu.lock (8 shares)\n"
        ws = WorkerStore(s)
        with s.lock:
            assert ws._gpu_config("box-1") == {"lock": "/srv/gpu.lock", "shares": 8}
        # the bridge on box-1 is asked for it in its heartbeat answer
        body = {"machine": "box-1", "sessions": [], "tools": [],
                "facts": {"host": "box-1", "os_user": "bob", "version": "0.9.23"}}
        assert http.request("POST", url + "/bridge/heartbeat", member, body)[1]["gpu_queue"] == \
            {"lock": "/srv/gpu.lock", "shares": 8}
        assert cmd("box-1") == 1 and "or --off" in capsys.readouterr().err
        assert cmd("box-1", "--off") == 0 and capsys.readouterr().out == "box-1: GPU queue off\n"
        with s.lock:
            assert ws._gpu_config("box-1") is None
        assert http.request("POST", url + "/bridge/heartbeat", member, body)[1]["gpu_queue"] is None
    finally:
        stop()
        s.close()


def test_bridge_reads_it_when_asked(monkeypatch, tmp_path):
    from kaptyorka import bridge as bridge_mod
    monkeypatch.setenv("KAPTYORKA_HOME", str(tmp_path))
    b = bridge_mod.Bridge({"hub": "http://h", "token": "t", "machine": "box-1"}, [], lambda s: None)
    monkeypatch.setattr(usage, "read_queue", lambda lock, n: {"shares": n, "holders": {}, "whole": "",
                                                              "waiting": 0, "waiters": {}, "lock": lock})
    b._checks()
    assert "gpu_queue" not in b._facts  # not asked
    b._gpu_queue_ask = {"lock": "/srv/gpu.lock", "shares": 4}
    b._checks()
    assert b._facts["gpu_queue"]["lock"] == "/srv/gpu.lock" and b._facts["gpu_queue"]["shares"] == 4
    sent = bridge_mod.Bridge._sendable({**b._facts, "git": {}}, set())
    assert "read_at" not in sent["gpu_queue"] and sent["gpu_queue"]["age"] < 5
