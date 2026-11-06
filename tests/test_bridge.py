import json
import os
import socket
import threading
import time

import pytest

from kaptyorka import bridge as bridge_mod
from kaptyorka.adapters.base import DeliveryError, Session, merge_json
from kaptyorka.bridge import Bridge, joined_adapters
from helpers import call, run

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


@pytest.fixture(autouse=True)
def no_real_commands(monkeypatch):
    """No tool is on PATH, so the facts run no `--version`; git is only asked for session folders."""
    from kaptyorka import facts

    facts._cache.clear()
    monkeypatch.setattr(facts.shutil, "which", lambda n: None)


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


def test_heartbeat_sends_facts_and_reads_answer(hub, home, tmp_path, monkeypatch):
    from helpers import person
    from kaptyorka import facts as facts_mod
    from kaptyorka.workers import WorkerStore

    facts_mod._cache.clear()
    pid, token = person(hub.store, "alice")
    device = hub.store.authenticate(token).device_id
    a = FakeAdapter(sessions=[Session("codex:1", "codex", "proj-a", cwd="/w/proj-a")])
    bridge = Bridge({**cfg(hub), "token": token}, [a], bridge_mod.log)
    assert bridge.hub == {}
    bridge.heartbeat()
    assert bridge.hub["you"] == "alice" and "projects" in bridge.hub
    bridge.hub["projects"] = ["proj-a"]
    bridge.heartbeat()
    f = WorkerStore(hub.store).facts(device)
    assert f["os_user"] and f["cpus"] >= 1 and f["tools"] == {"codex": ""}
    assert WorkerStore(hub.store).folders(device) == {"proj-a": "/w/proj-a"}
    row = hub.store.db.execute("SELECT cwd FROM agents WHERE key='codex:1'").fetchone()
    assert row["cwd"] == "/w/proj-a"
