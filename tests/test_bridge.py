import json
import os
import socket
import subprocess
import threading
import time
import types

import pytest

import kaptyorka
from kaptyorka import bridge as bridge_mod
from kaptyorka import launch as launch_mod
from kaptyorka.adapters.base import DeliveryError, Session, merge_json
from kaptyorka.bridge import Bridge, joined_adapters
from kaptyorka.launch import Launcher
from helpers import call, run
from test_launch import FakePopen, FakeRun as LaunchRun

PC = {"key": "claude:1", "machine": "pc", "tool": "claude-code", "project": "proj-a"}


class FakeAdapter:
    restart_note = None

    def __init__(self, tool="codex", prefix="codex", sessions=(), fail=False):
        self.tool, self.prefix, self.fail = tool, prefix, fail
        self._sessions = list(sessions)
        self.delivered = []

    def installed(self):
        return True

    def sessions(self):
        return self._sessions

    def deliver(self, key, text):
        if self.fail:
            raise DeliveryError("door closed")
        self.delivered.append((key, text))

    def configure(self, kaptyorka):
        return []


class FakeRun:
    """A runner for git and --version: every command fails, none is really started."""

    def __init__(self):
        self.calls = []

    def __call__(self, cmd, **kw):
        self.calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 1, "", "")


@pytest.fixture(autouse=True)
def no_real_commands(monkeypatch):
    """No tool is on PATH, so the facts run no `--version`; git is only asked for session folders."""
    from kaptyorka import facts

    facts._cache.clear()
    monkeypatch.setattr(facts.shutil, "which", lambda n: None)
    monkeypatch.setattr(bridge_mod.subprocess, "run", FakeRun())  # Bridge's default runner


def cfg(hub):
    return {"hub": hub.url, "token": hub.token, "machine": "box-2"}


def fake_codex():
    return FakeAdapter(sessions=[Session("codex:5", "codex", "proj")])


def send_hi(hub):
    run(call(hub, PC, "send", to="box-2/codex/proj", text="hi"))


def wait_for(cond, timeout=5):
    deadline = time.monotonic() + timeout
    while not cond():
        assert time.monotonic() < deadline, "condition not met in time"
        time.sleep(0.01)


def test_heartbeat_reports_all_sessions(hub, home):
    a = FakeAdapter(sessions=[Session("codex:1", "codex", "a"), Session("codex:2", "codex", "b")])
    b = FakeAdapter("opencode", "opencode", [Session("opencode:x", "opencode", "c", "editing")])
    Bridge(cfg(hub), [a, b], bridge_mod.log).heartbeat()
    online = {x.key: x for x in hub.store.online()}
    assert set(online) == {"codex:1", "codex:2", "opencode:x"}
    assert online["opencode:x"].activity == "editing"
    assert online["opencode:x"].machine == "box-2"


class BrokenAdapter(FakeAdapter):
    def sessions(self):
        raise OSError("state db locked")


def test_heartbeat_skips_broken_adapter_and_logs_once(hub, home):
    lines = []
    bridge = Bridge(cfg(hub), [BrokenAdapter("opencode", "opencode"), fake_codex()], lines.append)
    for _ in range(3):
        bridge.heartbeat()
    assert [a.key for a in hub.store.online()] == ["codex:5"]
    assert lines == ["opencode sessions failed: state db locked"]


def test_poll_delivers_and_acks(hub, home):
    fake = fake_codex()
    bridge = Bridge(cfg(hub), [fake], bridge_mod.log)
    bridge.heartbeat()
    send_hi(hub)
    assert bridge.poll_once(wait=1) == 1
    assert fake.delivered == [("codex:5", "Kaptyorka message from pc/claude-code/proj-a:\n\nhi")]
    assert hub.store.inbox("codex:5") == []


def test_failed_door_logs_and_leaves_for_pull(hub, home, capsys):
    fake = fake_codex()
    fake.fail = True
    bridge = Bridge(cfg(hub), [fake], bridge_mod.log)
    bridge.heartbeat()
    send_hi(hub)
    assert bridge.poll_once(1) == 0
    line = "push to codex:5 failed: door closed; message 1 left for pull"
    assert line in (home / ".kaptyorka/bridge.log").read_text()
    assert line in capsys.readouterr().err
    assert [m.text for m in hub.store.inbox("codex:5")] == ["hi"]


def test_no_adapter_for_key_is_not_acked_ok(hub, home):
    bridge = Bridge(cfg(hub), [FakeAdapter("cursor", "cursor")], bridge_mod.log)
    Bridge(cfg(hub), [fake_codex()], bridge_mod.log).heartbeat()
    send_hi(hub)
    assert bridge.poll_once(1) == 0
    assert "push to codex:5 failed" in (home / ".kaptyorka/bridge.log").read_text()
    assert [m.text for m in hub.store.inbox("codex:5")] == ["hi"]


def test_empty_poll_is_silent(hub, home):
    lines = []
    assert Bridge(cfg(hub), [fake_codex()], lines.append).poll_once(0) == 0
    assert lines == []


def test_bad_token_is_logged_not_unreachable(hub, home):
    lines = []
    bridge = Bridge({**cfg(hub), "token": "wrong"}, [], lines.append)
    assert bridge._step() == bridge_mod.RETRY_S
    assert lines == ["hub answered 401 to poll"]


def test_run_heartbeats_delivers_and_stops(hub, home, monkeypatch):
    monkeypatch.setattr(bridge_mod, "WAIT_S", 1)
    fake = fake_codex()
    bridge = Bridge(cfg(hub), [fake], bridge_mod.log)
    th = threading.Thread(target=bridge.run, daemon=True)
    th.start()
    try:
        wait_for(lambda: hub.store.agent("codex:5") is not None)
        send_hi(hub)
        wait_for(lambda: fake.delivered)
    finally:
        bridge.stop.set()
        th.join(10)
    assert not th.is_alive()
    assert fake.delivered[0][0] == "codex:5"


def _closed_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_hub_down_logged_once_per_outage(hub, home, monkeypatch):
    monkeypatch.setattr(bridge_mod, "RETRY_S", 0.01)
    lines = []
    url = f"http://127.0.0.1:{_closed_port()}"
    bridge = Bridge({"hub": url, "token": "T", "machine": "box-2"}, [], lines.append)
    calls = []
    real_poll = bridge.poll_once
    bridge.poll_once = lambda wait=25: (calls.append(1), real_poll(wait))[1]
    th = threading.Thread(target=bridge.run, daemon=True)
    th.start()
    try:
        wait_for(lambda: len(calls) >= 3)
    finally:
        bridge.stop.set()
        th.join(10)
    assert not th.is_alive()
    down = f"kaptyorka hub unreachable at {url}"
    assert lines == [down]
    # recovery, then a second outage is logged again
    monkeypatch.setattr(bridge_mod, "WAIT_S", 0)
    bridge.cfg = {**bridge.cfg, "hub": hub.url}
    assert bridge._step() == 0
    bridge.cfg = {**bridge.cfg, "hub": url}
    bridge._step()
    assert lines == [down, f"kaptyorka hub unreachable at {url}"]


def test_cut_off_reply_counts_as_unreachable():
    from kaptyorka import http

    with socket.socket() as srv:
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)

        def answer():
            conn, _ = srv.accept()
            with conn:
                conn.recv(4096)
                conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                             b"Content-Length: 100\r\n\r\n[1, 2")

        th = threading.Thread(target=answer, daemon=True)
        th.start()
        try:
            url = f"http://127.0.0.1:{srv.getsockname()[1]}/bridge/poll"
            try:
                http.request("GET", url, "T")
                raise AssertionError("expected HubUnreachable")
            except http.HubUnreachable:
                pass
        finally:
            th.join(10)


def test_merge_json_keeps_symlink_and_other_keys(tmp_path):
    target = tmp_path / "real.json"
    target.write_text('{"a": 1}')
    os.chmod(target, 0o640)
    link = tmp_path / "link.json"
    link.symlink_to(target)

    def add_b(d):
        d["b"] = 2
        return True

    assert merge_json(link, add_b) == "changed"
    assert link.is_symlink()
    assert json.loads(target.read_text()) == {"a": 1, "b": 2}
    assert target.stat().st_mode & 0o777 == 0o640
    assert sorted(p.name for p in tmp_path.iterdir()) == ["link.json", "real.json"]
    assert merge_json(link, lambda d: False) == "unchanged"


def test_merge_json_missing_file_starts_empty(tmp_path):
    p = tmp_path / "sub" / "new.json"
    assert merge_json(p, lambda d: d.setdefault("x", 1) == 1) == "changed"
    assert json.loads(p.read_text()) == {"x": 1}


def test_merge_json_broken_file_untouched(tmp_path):
    p = tmp_path / "c.json"
    p.write_text("{not json")
    assert merge_json(p, lambda d: True) == "broken"
    assert p.read_text() == "{not json"


def test_bridge_drives_only_joined_tools():
    a, b = FakeAdapter("claude-code", "claude"), FakeAdapter("codex", "codex")
    assert joined_adapters({"tools": ["claude-code"]}, [a, b]) == [a]
    assert joined_adapters({}, [a, b]) == [a, b]  # joined without --only


def test_heartbeat_frees_closed_claude_session_but_keeps_codex_when_list_is_empty(hub, home):
    cl = FakeAdapter("claude-code", "claude", [Session("claude:S1", "claude-code", "m")])
    codex = fake_codex()
    bridge = Bridge(cfg(hub), [cl, codex], bridge_mod.log)
    bridge.heartbeat()
    cl._sessions.clear()
    codex._sessions.clear()  # daemon down: an empty list proves nothing
    bridge.heartbeat()
    assert [a.key for a in hub.store.online()] == ["codex:5"]


def test_heartbeat_sends_facts(hub, home, monkeypatch):
    import getpass

    import kaptyorka
    sent = []
    real = bridge_mod.http.request

    def spy(method, url, token, body=None, *a, **kw):
        sent.append(body)
        return real(method, url, token, body, *a, **kw)

    monkeypatch.setattr(bridge_mod.http, "request", spy)
    Bridge(cfg(hub), [fake_codex()], bridge_mod.log).heartbeat()
    facts = sent[0]["facts"]
    assert facts["host"] == socket.gethostname().split(".")[0]
    assert facts["os_user"] == getpass.getuser() and facts["version"] == kaptyorka.__version__


def test_bridge_applies_rekey_and_saves_config(hub, home, monkeypatch):
    from helpers import person
    from kaptyorka import identity, paths

    monkeypatch.setattr(identity, "facts",
                        lambda: {"host": "box-2", "os_user": "me", "version": "0.2.0"})
    lines = []
    conf = {**cfg(hub), "tools": ["codex"]}
    paths.save_config(conf)
    bridge = Bridge(conf, [fake_codex()], lines.append)
    bridge.heartbeat()  # with the old shared token: an unassigned legacy machine
    kim, _ = person(hub.store, "kim")
    hub.store.assign_legacy("box-2", kim, "root")
    bridge.heartbeat()  # now reports its version on the assigned device
    assert bridge.poll_once(0) == 0
    saved = paths.load_config()
    assert saved["token"] != "T" and saved["secret"] and saved["tools"] == ["codex"]
    assert bridge.cfg["token"] == saved["token"]
    assert lines == ["rekeyed: this machine now has its own token"]
    with hub.store.lock:
        assert hub.store.db.execute("SELECT COUNT(*) FROM rekeys").fetchone()[0] == 0
    p = hub.store.authenticate(saved["token"])
    assert (p.person_id, p.device_name, p.legacy) == (kim, "box-2", False)
    hub.store.send_from_web("root", "box-2/codex/proj", "hi")
    assert bridge.poll_once(0) == 1  # the new token works for the next requests


def test_lost_rekey_confirm_heals_and_is_retried(hub, home, monkeypatch):
    from helpers import person
    from kaptyorka import identity, paths

    monkeypatch.setattr(identity, "facts",
                        lambda: {"host": "box-2", "os_user": "me", "version": "0.2.0"})
    real, failed = bridge_mod.http.request, []

    def first_confirm_fails(method, url, token, body=None, *a, **kw):
        if url.endswith("/bridge/rekeyed") and not failed:
            failed.append(url)
            return 500, "boom"
        return real(method, url, token, body, *a, **kw)

    monkeypatch.setattr(bridge_mod.http, "request", first_confirm_fails)
    lines = []
    conf = cfg(hub)
    paths.save_config(conf)
    bridge = Bridge(conf, [fake_codex()], lines.append)
    bridge.heartbeat()
    kim, _ = person(hub.store, "kim")
    hub.store.assign_legacy("box-2", kim, "root")
    bridge.heartbeat()
    bridge.poll_once(0)
    assert bridge.pending_confirm and lines == ["hub answered 500 to rekeyed"]
    rekeys = lambda: hub.store.db.execute("SELECT COUNT(*) FROM rekeys").fetchone()[0]  # noqa: E731
    assert rekeys() == 1
    bridge.heartbeat()  # the first call with the new token clears the pending rekey
    assert rekeys() == 0
    assert [r["detail"] for r in hub.store.log() if r["kind"] == "rekeyed"] == ["box-2"]
    bridge.poll_once(0)  # and the bridge retries its confirm
    assert not bridge.pending_confirm
    assert lines[-1] == "rekeyed: this machine now has its own token"
    assert [r["detail"] for r in hub.store.log() if r["kind"] == "rekeyed"] == ["box-2"]


def test_rekey_confirm_hub_down_does_not_escape_poll(hub, home, monkeypatch):
    lines = []
    bridge = Bridge(cfg(hub), [fake_codex()], lines.append)
    bridge.pending_confirm = True
    real = bridge_mod.http.request

    def down(method, url, *a, **kw):
        if url.endswith("/bridge/rekeyed"):
            raise bridge_mod.http.HubUnreachable(url)
        return real(method, url, *a, **kw)

    monkeypatch.setattr(bridge_mod.http, "request", down)
    assert bridge.poll_once(0) == 0
    assert bridge.pending_confirm
    assert lines == ["hub unreachable when confirming the new token"]


def test_heartbeat_sends_facts_and_reads_answer(hub, home, tmp_path):
    from helpers import person
    from kaptyorka.workers import WorkerStore

    proj = home / "proj-a"
    proj.mkdir()
    pid, token = person(hub.store, "alice")
    device = hub.store.authenticate(token).device_id
    a = FakeAdapter(sessions=[Session("codex:1", "codex", "proj-a", cwd=str(proj))])
    fake = FakeRun()
    bridge = Bridge({**cfg(hub), "token": token}, [a], bridge_mod.log, run=fake)
    assert bridge.hub == {}
    bridge.heartbeat()
    assert bridge.hub["you"] == "alice" and "projects" in bridge.hub
    assert any(c[:3] == ["git", "-C", str(proj)] for c in fake.calls)
    bridge.hub["projects"] = ["proj-a", "proj-b"]
    (home / "proj-b").mkdir()  # found under the default home, ~/<project>
    bridge.heartbeat()
    f = WorkerStore(hub.store).facts(device)
    assert f["os_user"] and f["cpus"] >= 1 and f["tools"] == {"codex": ""}
    assert WorkerStore(hub.store).folders(device) == {"proj-a": str(proj), "proj-b": str(home / "proj-b")}
    row = hub.store.db.execute("SELECT cwd FROM agents WHERE key='codex:1'").fetchone()
    assert row["cwd"] == str(proj)


# --- launch and close orders ---------------------------------------------------------------

BOSS = {"key": "k:boss", "machine": "laptop", "tool": "codex", "project": "proj-a"}


class Workers:
    """alice's machine box-2 joined with an invite; her boss session can start workers."""

    def __init__(self, hub, home, monkeypatch, secret=True):
        from types import SimpleNamespace

        from helpers import person

        from kaptyorka import identity

        monkeypatch.setattr(launch_mod.shutil, "which", lambda n: None)  # no systemd-run
        monkeypatch.setattr(identity, "facts",
                            lambda: {"host": "box-2", "os_user": "u", "version": "0.3.0"})
        hub.store.project_register("proj-a", by="test")
        self.pid, ptoken = person(hub.store, "alice")
        hub.store.grant_project("proj-a", self.pid, "write", "test")
        token, sec = hub.store.invite_redeem(hub.store.invite_create(self.pid), "box-2",
                                             host="box-2", os_user="u")
        (home / "proj-a").mkdir()
        self.folder = str(home / "proj-a")
        self.hub, self.client = hub, SimpleNamespace(url=hub.url, token=ptoken)
        self.claude = FakeAdapter("claude-code", "claude")
        self.run, self.popen, self.lines, self.acks = LaunchRun(), FakePopen(), [], []
        conf = {**cfg(hub), "token": token, **({"secret": sec} if secret else {})}
        self.launcher = Launcher(conf, home, run=self.run, popen=self.popen,
                                 log=self.lines.append)
        self.bridge = Bridge(conf, [self.claude], self.lines.append, run=FakeRun(),
                             launcher=self.launcher)
        real_ack = self.bridge._ack

        def spy(msg_id, ok, launch=False, error=""):
            self.acks.append((msg_id, ok, launch, error))
            real_ack(msg_id, ok, launch, error)

        self.bridge._ack = spy
        self.bridge.heartbeat()  # the bridge learns who it is and its projects
        self.bridge.heartbeat()  # the hub learns the folder

    def work(self, task="fix the tests", **kw):
        assert run(call(self.client, BOSS, "work", project="proj-a", task=task, **kw)) \
            .startswith("starting `proj-a-w1` on box-2")

    def order(self):
        with self.hub.store.lock:
            body = self.hub.store.db.execute(
                "SELECT body FROM launches ORDER BY id DESC").fetchone()["body"]
        return {**json.loads(body), "launch": True}

    def worker(self):
        from kaptyorka.workers import WorkerStore

        return WorkerStore(self.hub.store).by_name("proj-a-w1")


@pytest.fixture
def team(hub, home, monkeypatch):
    return Workers(hub, home, monkeypatch)


def test_bridge_launches_a_signed_order_and_acks_it(team, home):
    team.work(task='say "hi"; $(rm -rf /)')
    assert team.bridge.poll_once(0) == 0
    argv, kw = team.popen.calls[0]
    assert argv[:3] == ["claude", "--bg", "-n"] and 'say "hi"; $(rm -rf /)' in argv[-1]
    assert kw["cwd"] == team.folder
    oid = team.order()["id"]
    assert team.acks == [(oid, True, True, "")]
    assert team.worker()["state"] == "starting"
    assert set(team.bridge.pending) == {"proj-a-w1"}
    saved = json.loads((home / ".kaptyorka" / "launched.json").read_text())
    assert saved == {str(oid): {"ok": True, "error": ""}}


def test_bridge_refuses_replay_and_bad_signature(team):
    team.work()
    team.bridge.poll_once(0)
    item = team.order()
    team.bridge._order(item)  # the same signed order again
    team.bridge._order({**item, "task": "rm -rf ~"})  # changed on the way
    assert len(team.popen.calls) == 1
    assert team.acks[1:] == [(item["id"], True, True, ""),  # the stored outcome, again
                             (item["id"], False, True, "bad signature")]
    # a restarted bridge still knows the id and its outcome
    again = Bridge(team.bridge.cfg, [], team.lines.append, run=FakeRun(), launcher=team.launcher)
    assert again.seen_ids == {item["id"]: {"ok": True, "error": ""}}


def test_lost_ack_is_answered_with_the_stored_outcome(team, monkeypatch):
    real = bridge_mod.http.request
    lost = []

    def lose_first_ack(method, url, token, body=None, *a, **kw):
        if url.endswith("/bridge/ack") and len(lost) < 2:  # the ack and its one retry
            lost.append(body)
            raise bridge_mod.http.HubUnreachable(url)
        return real(method, url, token, body, *a, **kw)

    monkeypatch.setattr(bridge_mod.http, "request", lose_first_ack)
    team.work()
    with pytest.raises(bridge_mod.http.HubUnreachable):
        team.bridge.poll_once(0)
    item = team.order()
    with team.hub.store.lock:
        team.hub.store.db.execute("UPDATE launches SET offered_at=NULL")  # offered again
    team.bridge.poll_once(0)
    assert len(team.popen.calls) == 1  # not started twice
    assert team.acks[-1] == (item["id"], True, True, "")
    assert team.worker()["state"] == "starting"  # not failed: it did start


def test_bridge_without_secret_refuses_every_order(hub, home, monkeypatch):
    team = Workers(hub, home, monkeypatch, secret=False)
    team.work()
    team.bridge.poll_once(0)
    assert team.popen.calls == []
    assert team.acks == [(team.order()["id"], False, True,
                          "this machine has no signing secret; re-join with an invite")]
    assert team.worker()["state"] == "failed"


def test_bridge_refuses_other_persons_order_unless_shared(team):
    team.work()
    item = team.order()
    team.bridge.hub = {**team.bridge.hub, "you": "bob"}
    team.bridge._order(item)
    assert team.acks == [(item["id"], False, True, "this account belongs to bob")]
    assert team.popen.calls == []
    team.acks.clear()
    team.bridge.hub["shared"] = True
    team.bridge._order(item)
    assert team.acks == [(item["id"], True, True, "")] and len(team.popen.calls) == 1


def test_crash_while_launching_is_acked_and_does_not_stop_the_poll(team, monkeypatch):
    def boom(order, sessions=()):
        raise ZeroDivisionError("division by zero")

    monkeypatch.setattr(team.launcher, "launch", boom)
    team.work()
    assert team.bridge.poll_once(0) == 0
    assert team.acks == [(team.order()["id"], False, True, "launch failed: division by zero")]
    assert team.worker()["state"] == "failed"
    assert any("proj-a-w1" in line and "division by zero" in line for line in team.lines)


def test_bridge_reports_worker_key_when_session_appears(team):
    team.work()
    team.bridge.poll_once(0)
    key = "claude:" + team.popen.calls[0][0][5]
    team.bridge.heartbeat()
    assert team.worker()["state"] == "starting"  # not seen yet
    team.claude._sessions.append(Session(key, "claude-code", "proj-a", cwd=team.folder))
    team.bridge.heartbeat()
    w = team.worker()
    assert (w["state"], w["key"]) == ("up", key)
    assert team.bridge.pending == {}


def test_bridge_reports_timeout_after_two_minutes(team):
    team.work()
    team.bridge.poll_once(0)
    order, result = team.bridge.pending["proj-a-w1"][:2]
    team.bridge.pending["proj-a-w1"] = (order, result, time.monotonic() - 121)
    team.bridge.heartbeat()
    w = team.worker()
    assert w["state"] == "failed"
    assert w["error"] == ("the session did not show up within 2 minutes;"
                          " see ~/.kaptyorka/workers/proj-a-w1.log")
    assert team.bridge.pending == {}
    assert team.run.calls == [["claude", "stop", "proj-a-w1"]]  # nothing left running
    assert "worker proj-a-w1 did not show up within 2 minutes; stopped its session" in team.lines


def test_worker_closed_while_starting_gets_its_session_closed(team):
    team.work()
    team.bridge.poll_once(0)
    run(call(team.client, BOSS, "close_worker", name="proj-a-w1"))
    key = "claude:" + team.popen.calls[0][0][5]
    team.claude._sessions.append(Session(key, "claude-code", "proj-a", cwd=team.folder))
    team.bridge.heartbeat()
    assert team.run.calls == [["claude", "stop", "proj-a-w1"]]
    assert "worker proj-a-w1 was closed while starting; stopped its session" in team.lines
    assert team.bridge.pending == {}


def test_lost_link_answer_is_retried_without_stopping_the_worker(team, monkeypatch):
    team.work()
    team.bridge.poll_once(0)
    key = "claude:" + team.popen.calls[0][0][5]
    team.claude._sessions.append(Session(key, "claude-code", "proj-a", cwd=team.folder))
    real, lost = bridge_mod.http.request, []

    def lose_first_answer(method, url, token, body=None, *a, **kw):
        answer = real(method, url, token, body, *a, **kw)
        if url.endswith("/bridge/worker") and not lost:
            lost.append(answer)
            raise bridge_mod.http.HubUnreachable(url)
        return answer

    monkeypatch.setattr(bridge_mod.http, "request", lose_first_answer)
    team.bridge.heartbeat()
    assert lost[0][0] == 204 and "proj-a-w1" in team.bridge.pending
    team.bridge.heartbeat()  # the retry is a success too
    assert team.bridge.pending == {} and team.run.calls == []
    assert team.worker()["state"] == "up"


def test_unknown_worker_404_is_dropped_without_stopping(team):
    team.work()
    team.bridge.poll_once(0)
    key = "claude:" + team.popen.calls[0][0][5]
    with team.hub.store.lock:  # the hub lost track of it (say, a restored database)
        team.hub.store.db.execute("DELETE FROM workers")
    team.claude._sessions.append(Session(key, "claude-code", "proj-a", cwd=team.folder))
    team.bridge.heartbeat()
    assert team.bridge.pending == {} and team.run.calls == []
    assert "worker proj-a-w1: the hub does not know it as starting here" in team.lines


def test_close_order_stops_the_session_and_acks(team):
    team.work()
    team.bridge.poll_once(0)
    key = "claude:" + team.popen.calls[0][0][5]
    team.claude._sessions.append(Session(key, "claude-code", "proj-a", cwd=team.folder))
    team.bridge.heartbeat()
    run(call(team.client, BOSS, "close_worker", name="proj-a-w1"))
    team.bridge.poll_once(0)
    assert team.run.calls == [["claude", "stop", "proj-a-w1"]]
    assert team.acks[-1] == (team.order()["id"], True, True, "")
    assert team.worker()["state"] == "closed"


def test_failed_close_is_acked_with_the_error(team):
    team.work()
    team.bridge.poll_once(0)
    key = "claude:" + team.popen.calls[0][0][5]
    team.claude._sessions.append(Session(key, "claude-code", "proj-a", cwd=team.folder))
    team.bridge.heartbeat()
    run(call(team.client, BOSS, "close_worker", name="proj-a-w1"))
    team.run.rc, team.run.stderr = 1, "EACCES: permission denied, open '/run/user/1000/x'"
    team.bridge.poll_once(0)
    assert team.acks[-1] == (team.order()["id"], False, True,
                             "EACCES: permission denied, open '/run/user/1000/x'")
    assert team.worker()["state"] == "up"


def test_close_of_a_gone_session_marks_the_worker_closed(team):
    team.work()
    team.bridge.poll_once(0)
    key = "claude:" + team.popen.calls[0][0][5]
    team.claude._sessions.append(Session(key, "claude-code", "proj-a", cwd=team.folder))
    team.bridge.heartbeat()
    run(call(team.client, BOSS, "close_worker", name="proj-a-w1"))
    team.run.rc, team.run.stderr = 1, "No job matching 'proj-a-w1'."
    team.bridge.poll_once(0)
    assert team.acks[-1] == (team.order()["id"], True, True, "")
    assert team.worker()["state"] == "closed"


def update_bridge(hub, monkeypatch, version="99.0.0", auto=True):
    """A bridge whose heartbeat answer says the hub runs `version`; its runner records argv."""
    from kaptyorka import hub as hub_mod

    monkeypatch.setattr(hub_mod, "__version__", version)
    runner = LaunchRun()
    lines = []
    b = Bridge(cfg(hub), [fake_codex()], lines.append, run=runner)
    b.heartbeat()
    b.hub["auto_update"] = auto  # the test token is a legacy one: the hub answers false
    b.poll_once = lambda wait=0: 0
    return b, runner, lines


def update_calls(runner):
    return [c for c in runner.calls if c[-1] == "update"]


def test_auto_update_runs_once_at_quiet_moment(hub, home, monkeypatch):
    b, runner, lines = update_bridge(hub, monkeypatch)
    b.pending["w"] = ({}, {}, 0.0)
    b._step()
    assert update_calls(runner) == []  # a launch is pending
    b.pending.clear()
    b.poll_once = lambda wait=0: 1
    b._step()
    assert update_calls(runner) == []  # the poll delivered something
    b.poll_once = lambda wait=0: 0
    b._step()
    b._step()
    calls = update_calls(runner)
    assert len(calls) == 1 and calls[0][0].endswith("kaptyorka")
    assert lines.count(f"updating kaptyorka {kaptyorka.__version__} -> 99.0.0") == 1


def test_no_auto_update_when_off_or_same_version(hub, home, monkeypatch):
    b, runner, _ = update_bridge(hub, monkeypatch, auto=False)
    b._step()
    assert update_calls(runner) == []
    b, runner, _ = update_bridge(hub, monkeypatch, version=kaptyorka.__version__)
    b._step()
    assert update_calls(runner) == []
    b, runner, _ = update_bridge(hub, monkeypatch, version="0.0.1")
    b._step()
    assert update_calls(runner) == []


def test_auto_update_failure_logs_once_and_retries_after_an_hour(hub, home, monkeypatch):
    b, runner, lines = update_bridge(hub, monkeypatch)
    runner.rc = 1
    now = [1000.0]
    monkeypatch.setattr(bridge_mod, "time", types.SimpleNamespace(
        monotonic=lambda: now[0], time=time.time, strftime=time.strftime))
    b._step()
    b._step()
    assert len(update_calls(runner)) == 1
    assert len([l for l in lines if l.startswith("auto-update failed")]) == 1
    now[0] += 3601
    b._step()
    assert len(update_calls(runner)) == 2
    assert len([l for l in lines if l.startswith("auto-update failed")]) == 1


def test_auto_update_timeout_counts_as_failure(hub, home, monkeypatch):
    b, runner, lines = update_bridge(hub, monkeypatch)
    seen = []

    def hang(cmd, **kw):
        runner.calls.append(cmd)
        if cmd[-1] == "update":
            seen.append(kw.get("timeout"))
            raise subprocess.TimeoutExpired(cmd, kw.get("timeout"))
        return subprocess.CompletedProcess(cmd, 1, "", "")

    b.runner = hang
    now = [1000.0]
    monkeypatch.setattr(bridge_mod, "time", types.SimpleNamespace(
        monotonic=lambda: now[0], time=time.time, strftime=time.strftime))
    b._step()
    b._step()
    assert seen == [600] and len([l for l in lines if l.startswith("auto-update failed")]) == 1
    now[0] += 3601
    b._step()
    assert len(seen) == 2
