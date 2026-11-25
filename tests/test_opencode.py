import json
import os
import socket
import subprocess
import sys
import time

import pytest

from fakes.opencode_server import FakeOpenCode
from kaptyorka import identity
from kaptyorka.adapters import opencode
from kaptyorka.adapters.base import DeliveryError, Session
from kaptyorka.adapters.opencode import OpenCodeAdapter

CWD = os.getcwd()
REAL_MAJOR, REAL_SERVICE = opencode.major, opencode.service  # conftest fakes both
NEWEST, OLDER, ELSEWHERE, STALE = "ses_new", "ses_old", "ses_other", "ses_stale"
STARTED = 50  # the fake TUI's start time; STALE is from an earlier run in the same folder


@pytest.fixture
def fake_opencode(monkeypatch):
    srv = FakeOpenCode([
        {"id": STALE, "directory": CWD, "time": {"created": 0, "updated": 10}},
        {"id": OLDER, "directory": CWD, "time": {"created": 1, "updated": 100}},
        {"id": NEWEST, "directory": CWD, "time": {"created": 2, "updated": 200}},
        {"id": ELSEWHERE, "directory": "/elsewhere", "time": {"created": 3, "updated": 300}},
    ])
    monkeypatch.setattr(opencode, "_listen_ports",
                        lambda pid: [srv.port] if pid == os.getpid() else [])
    monkeypatch.setattr(opencode, "_started_ms", lambda pid: STARTED)
    yield srv
    srv.stop()


def adapter(srv, **kw):
    return OpenCodeAdapter(find_ports=lambda: {os.getpid(): srv.port}, **kw)


def test_deliver_posts_to_session(fake_opencode):
    adapter(fake_opencode).deliver(f"opencode:{NEWEST}", 'hi "team" ✓\nline2')
    method, path, body = fake_opencode.requests[-1]
    assert (method, path) == ("POST", f"/session/{NEWEST}/prompt_async")
    assert body == {"parts": [{"type": "text", "text": 'hi "team" ✓\nline2'}]}


def test_deliver_unknown_session_raises(fake_opencode):
    with pytest.raises(DeliveryError):
        adapter(fake_opencode).deliver("opencode:ses_gone", "hi")
    assert [r[0] for r in fake_opencode.requests] == ["GET"]


def test_opencode_key_matches_shim_and_bridge(fake_opencode):
    key = identity.RESOLVERS["opencode"](os.getpid(), CWD)
    assert key == f"opencode:{NEWEST}"
    s, = adapter(fake_opencode).sessions()
    assert (s.key, s.tool, s.project, s.cwd, s.activity) == (key, "opencode", os.path.basename(CWD),
                                                              CWD, "idle")  # none busy


def test_v1_session_carries_its_title(fake_opencode):
    """The title OpenCode gives a session, or the one its person renamed it to: one field."""
    fake_opencode.sessions[2]["title"] = "  fix the\nlogin page "
    s, = adapter(fake_opencode).sessions()
    assert s.title == "fix the login page" and s.label == ""
    fake_opencode.sessions[2]["title"] = "New session - 2026-11-25T10:00:00.000Z"
    s, = adapter(fake_opencode).sessions()
    assert s.title == ""  # the placeholder before the first prompt is no name


def test_resolver_none_before_first_prompt(fake_opencode):
    fake_opencode.sessions = fake_opencode.sessions[:1]  # only a session from an earlier run
    assert identity.RESOLVERS["opencode"](os.getpid(), CWD) is None
    assert adapter(fake_opencode).sessions() == []


def test_identify_honours_opencode_pid_env(fake_opencode, monkeypatch):
    monkeypatch.setattr(identity, "ancestors", lambda pid: [(1, "bun")])
    monkeypatch.setenv("OPENCODE_PID", str(os.getpid()))
    ident = identity.identify("pc", pid=1)
    assert (ident.key, ident.tool) == (f"opencode:{NEWEST}", "opencode")


def test_identify_takes_session_from_plugin(monkeypatch):
    monkeypatch.setenv("KAPTYORKA_OPENCODE_SESSION", "ses_p")
    monkeypatch.setattr(identity, "ancestors", lambda pid: pytest.fail("no walk needed"))
    ident = identity.identify("pc", pid=1)
    assert (ident.key, ident.tool, ident.project) == ("opencode:ses_p", "opencode",
                                                      os.path.basename(CWD))


def test_password_server_logged_once_and_skipped(fake_opencode):
    fake_opencode.status = 401
    lines = []
    a = adapter(fake_opencode, log=lines.append)
    assert a.sessions() == [] and a.sessions() == []
    with pytest.raises(DeliveryError):
        a.deliver(f"opencode:{NEWEST}", "hi")
    assert lines == [f"opencode server on port {fake_opencode.port} needs a password "
                     "(not readable on this OS)"]
    assert identity.RESOLVERS["opencode"](os.getpid(), CWD) is None


def test_deliver_bad_key_raises():
    with pytest.raises(DeliveryError):
        OpenCodeAdapter(find_ports=lambda: {}).deliver("opencode", "hi")


def test_started_ms_of_real_process():
    import time
    assert time.time() * 1000 - 3600_000 * 24 * 365 < opencode._started_ms(os.getpid()) <= time.time() * 1000


def test_deliver_server_gone_raises():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    with pytest.raises(DeliveryError):
        OpenCodeAdapter(find_ports=lambda: {9: port}).deliver("opencode:ses_x", "hi")


def test_listen_ports_of_real_process():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        assert s.getsockname()[1] in opencode._listen_ports(os.getpid())


def test_configure_merges_mcp_and_plugin(home, monkeypatch):
    monkeypatch.setenv("SHELL", "/bin/bash")
    monkeypatch.setattr(opencode.sys, "platform", "linux")  # macOS writes .bash_profile instead
    cfg = home / ".config/opencode/opencode.json"
    cfg.parent.mkdir(parents=True)
    cfg.write_text(json.dumps({"mcp": {"other": {"type": "local", "command": ["x"]}}}))
    a = OpenCodeAdapter(home)
    first = a.configure("/k")
    data = json.loads(cfg.read_text())
    assert data["mcp"] == {"other": {"type": "local", "command": ["x"]},
                           "kaptyorka": {"type": "local", "command": ["/k", "mcp"], "enabled": True}}
    plugin = (home / ".config/opencode/plugins/kaptyorka.js").read_text()
    assert '"/k"' in plugin and "experimental.chat.system.transform" in plugin
    assert "alias opencode='opencode --port 0'" in (home / ".bashrc").read_text()
    assert any("--port 0" in line for line in first)
    assert len(first) == 4  # MCP, plugin, /hub command, alias
    assert a.configure("/k") == []
    assert (home / ".bashrc").read_text().count("alias opencode=") == 1


@pytest.mark.parametrize("shell,platform,rc", [("/bin/zsh", "linux", ".zshrc"),
                                               ("/bin/zsh", "darwin", ".zshrc"),
                                               ("/bin/bash", "darwin", ".bash_profile")])
def test_configure_alias_file(home, monkeypatch, shell, platform, rc):
    monkeypatch.setenv("SHELL", shell)
    monkeypatch.setattr(opencode.sys, "platform", platform)
    OpenCodeAdapter(home).configure("/k")
    assert (home / rc).read_text() == "alias opencode='opencode --port 0'\n"
    assert not (home / ".bashrc").exists()


def test_configure_leaves_broken_config_alone(home, monkeypatch):
    monkeypatch.setenv("SHELL", "/bin/bash")
    cfg = home / ".config/opencode/opencode.json"
    cfg.parent.mkdir(parents=True)
    cfg.write_text("{not json")
    lines = []
    OpenCodeAdapter(home, log=lines.append).configure("/k")
    assert cfg.read_text() == "{not json"
    assert f"left alone, not valid JSON: {cfg}" in lines


def test_identify_takes_session_from_opencode_v2_env(monkeypatch):
    monkeypatch.delenv("KAPTYORKA_OPENCODE_SESSION", raising=False)
    monkeypatch.setenv("OPENCODE_SESSION_ID", "ses_v2")
    monkeypatch.setattr(identity, "ancestors", lambda pid: pytest.fail("no walk needed"))
    ident = identity.identify("pc", pid=1)
    assert (ident.key, ident.tool) == ("opencode:ses_v2", "opencode")


def test_configure_v2_writes_default_export_plugin_and_drops_alias(home, monkeypatch):
    monkeypatch.setenv("SHELL", "/bin/bash")
    monkeypatch.setattr(opencode.sys, "platform", "linux")
    (home / ".bashrc").write_text("export X=1\nalias opencode='opencode --port 0'\n")
    monkeypatch.setattr(opencode, "major", lambda: 2)
    lines = OpenCodeAdapter(home).configure("/k")
    plugin = (home / ".config/opencode/plugins/kaptyorka.js").read_text()
    assert "export default {" in plugin and 'id: "kaptyorka"' in plugin and '"/k"' in plugin
    assert 'ctx.session.hook("prompt"' in plugin and 'ctx.session.hook("context"' in plugin
    assert "export const" not in plugin
    assert (home / ".bashrc").read_text() == "export X=1\n"
    assert any("removed" in line for line in lines)


@pytest.mark.parametrize("version", [1, 2])
def test_plugin_is_valid_javascript(home, monkeypatch, tmp_path, version):
    import shutil

    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    monkeypatch.setattr(opencode, "major", lambda: version)
    OpenCodeAdapter(home).configure("/k")
    js = tmp_path / "plugin.mjs"
    js.write_text((home / ".config/opencode/plugins/kaptyorka.js").read_text())
    r = subprocess.run([node, "--check", str(js)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_major_reads_version(monkeypatch):

    out = {"stdout": "2.2.5\n"}
    monkeypatch.setattr(opencode, "major", REAL_MAJOR)
    monkeypatch.setattr(opencode.subprocess, "run",
                        lambda *a, **k: subprocess.CompletedProcess(a, 0, out["stdout"], ""))
    assert opencode.major() == 2
    out["stdout"] = "opencode 1.19.5"
    assert opencode.major() == 1
    out["stdout"] = "garbage"
    assert opencode.major() == 1


def test_service_reads_service_json(tmp_path, monkeypatch):

    monkeypatch.setattr(opencode, "service", REAL_SERVICE)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    assert opencode.service() is None
    (tmp_path / "opencode").mkdir()
    (tmp_path / "opencode/service.json").write_text(json.dumps(
        {"id": "x", "version": "2.2.5", "url": "http://127.0.0.1:49374/", "pid": os.getpid(),
         "password": "pw"}))
    svc = opencode.service()
    assert svc["url"] == "http://127.0.0.1:49374"
    assert svc["Authorization"] == "Basic b3BlbmNvZGU6cHc="  # opencode:pw
    gone = subprocess.Popen(["true"])
    gone.wait()
    info = json.loads((tmp_path / "opencode/service.json").read_text())
    (tmp_path / "opencode/service.json").write_text(json.dumps({**info, "pid": gone.pid}))
    assert opencode.service() is None  # left by a service that has stopped


@pytest.fixture
def fake_v2(monkeypatch):
    srv = FakeOpenCode({"data": [
        {"id": "ses_child", "parentID": "ses_v2new", "location": {"directory": CWD},
         "time": {"created": 3, "updated": 300}},
        {"id": "ses_v2new", "location": {"directory": CWD}, "time": {"created": 2, "updated": 200}},
        {"id": "ses_v2old", "location": {"directory": CWD}, "time": {"created": 1, "updated": 100}},
        {"id": "ses_v2stale", "location": {"directory": CWD}, "time": {"created": 0, "updated": 10}},
    ]})
    monkeypatch.setattr(opencode, "service", lambda: {"url": f"http://127.0.0.1:{srv.port}",
                                                      "Authorization": "Basic x"})
    monkeypatch.setattr(opencode, "_tuis", lambda: [os.getpid()])
    monkeypatch.setattr(opencode, "_started_ms", lambda pid: STARTED)
    yield srv
    srv.stop()


def test_v2_sessions_from_service(fake_v2):
    a = OpenCodeAdapter(find_ports=lambda: pytest.fail("no V1 port scan on V2"))
    assert a.sessions() == [Session("opencode:ses_v2new", "opencode", os.path.basename(CWD), "", cwd=CWD)]
    assert any(m == "GET" and p.startswith("/api/session?") and "directory=" in p
               for m, p, _ in fake_v2.requests)


@pytest.fixture
def windows(fake_v2, monkeypatch):
    """Running TUIs (pids) and the notes kaptyorka's TUI plugin writes for them."""

    kids = [subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
            for _ in range(2)]
    pids = [os.getpid()] + [k.pid for k in kids]
    monkeypatch.setattr(opencode, "_tuis", lambda: pids)

    def note(pid, sid, directory=CWD):
        opencode.open_dir().mkdir(parents=True, exist_ok=True)
        (opencode.open_dir() / f"{pid}.json").write_text(
            json.dumps({"session": sid, "directory": directory}))

    yield pids, note
    for k in kids:
        k.kill()
        k.wait()


def keys(a):
    return [s.key for s in a.sessions()]


def test_v2_two_windows_in_one_folder_are_two_sessions(windows):
    (me, w1, w2), note = windows
    note(w1, "ses_v2new")
    note(w2, "ses_v2old")  # older than the other window's: the folder guess could never see it
    a = OpenCodeAdapter()
    # me has no note: guessed from the folder, never as a session another window has open
    assert keys(a) == ["opencode:ses_v2new", "opencode:ses_v2old"]
    note(me, "ses_v2stale")
    assert sorted(keys(a)) == ["opencode:ses_v2new", "opencode:ses_v2old", "opencode:ses_v2stale"]


def test_v2_resumed_old_session_is_listed_and_stays_while_idle(windows, tmp_path):
    (me, w1, w2), note = windows
    other = str(tmp_path / "proj-b")  # `opencode proj-b` from another folder
    note(me, "ses_v2stale", other)  # last updated long before this window started
    a = OpenCodeAdapter()
    for _ in range(3):  # nothing happens in it: still listed, every time
        got = [s for s in a.sessions() if s.key == "opencode:ses_v2stale"]
        assert got == [Session("opencode:ses_v2stale", "opencode", "proj-b", "", cwd=other,
                               closable=True)]  # its window is proven: the page may close it


def test_v2_window_on_its_home_screen_lists_nothing_and_is_not_guessed(windows):
    (me, w1, w2), note = windows
    for pid in (me, w1, w2):
        note(pid, "")
    assert OpenCodeAdapter().sessions() == []


def test_v2_note_of_a_closed_window_is_dropped(windows):

    (me, w1, w2), note = windows
    gone = subprocess.Popen([sys.executable, "-c", "pass"])
    gone.wait()
    note(gone.pid, "ses_v2old")
    note(me, "ses_v2new")
    note(w1, "")
    note(w2, "")
    assert keys(OpenCodeAdapter()) == ["opencode:ses_v2new"]
    assert not (opencode.open_dir() / f"{gone.pid}.json").exists()


@pytest.fixture
def recent(fake_v2):
    """The service's sessions as just used: inside the 30 minutes an app's session stays."""
    now = time.time() * 1000
    for row in fake_v2.sessions["data"]:
        row["time"]["updated"] = now - 60_000
    return fake_v2


def test_v2_a_closed_windows_chat_leaves_at_once_and_comes_back_when_used(windows, recent):
    """Closing a window used to leave its chat on the hub for 30 minutes as an app's."""
    (me, w1, w2), note = windows
    for pid in (me, w1, w2):
        note(pid, "")
    gone = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    note(gone.pid, "ses_v2new")
    a = OpenCodeAdapter()
    assert "opencode:ses_v2new" in keys(a)
    gone.kill()
    gone.wait()
    assert "opencode:ses_v2new" not in keys(a)  # the window closed: not an app's session
    assert "opencode:ses_v2new" not in keys(a)  # and it stays gone
    row = next(r for r in recent.sessions["data"] if r["id"] == "ses_v2new")
    row["time"]["updated"] = time.time() * 1000 + 1000  # used again (the desktop app)
    assert "opencode:ses_v2new" in keys(a)


def test_v2_a_window_that_moves_to_another_chat_lets_the_old_one_go(windows, recent):
    (me, w1, w2), note = windows
    note(me, "")
    note(w2, "")
    note(w1, "ses_v2old")
    a = OpenCodeAdapter()
    assert "opencode:ses_v2old" in keys(a)
    note(w1, "ses_v2stale")  # /sessions in the same window
    got = keys(a)
    assert "opencode:ses_v2stale" in got and "opencode:ses_v2old" not in got
    note(w1, "ses_v2old")  # and back: open in a window again
    assert "opencode:ses_v2old" in keys(a)


def test_v2_a_note_that_cannot_be_read_now_does_not_close_its_chat(windows, recent):
    (me, w1, w2), note = windows
    note(me, "")
    note(w2, "")
    note(w1, "ses_v2old")
    a = OpenCodeAdapter()
    keys(a)
    (opencode.open_dir() / f"{w1}.json").write_text("{half")  # mid-write
    keys(a)
    assert "ses_v2old" not in opencode._closed_since()  # unread is not closed
    note(w1, "")  # the window went to its home screen: now it let the chat go
    keys(a)
    assert opencode._closed_since().keys() == {"ses_v2old"}
    note(w1, "ses_v2old")
    assert "opencode:ses_v2old" in keys(a) and opencode._closed_since() == {}


def test_v2_note_older_than_its_process_is_a_reused_pid(windows, monkeypatch):
    (me, w1, w2), note = windows
    note(w1, "ses_v2old")
    note(w2, "")
    note(me, "")
    monkeypatch.setattr(opencode, "_started_ms", lambda pid: time.time() * 1000 + 60000)
    assert OpenCodeAdapter().sessions() == []
    assert not (opencode.open_dir() / f"{w1}.json").exists()


def closable(a):
    return {s.key: s.closable for s in a.sessions()}


def test_v2_end_terminates_only_the_window_proven_to_show_it(windows):
    """The page's close: SIGTERM to the window whose note names the session, nothing else."""
    (me, w1, w2), note = windows
    note(me, "")
    note(w1, "ses_v2old")
    note(w2, "ses_v2stale")
    a = OpenCodeAdapter()
    assert closable(a) == {"opencode:ses_v2old": True, "opencode:ses_v2stale": True}
    a.end("opencode:ses_v2old")
    assert opencode._ended(w1) and not opencode._ended(w2) and not opencode._ended(me)
    assert "opencode:ses_v2old" not in keys(a)  # gone from the list at once


def test_v2_end_refuses_what_no_window_here_shows(windows, recent, monkeypatch):
    """An app's session (no process here), a note of a process that is not a TUI (the service),
    and a made-up key: refused, nothing signalled, never reported closed."""
    (me, w1, w2), note = windows
    note(me, "")
    note(w1, "")
    note(w2, "ses_v2old")
    monkeypatch.setattr(opencode, "_tuis", lambda: [me, w1])  # w2 is the service, say
    a = OpenCodeAdapter()
    got = closable(a)
    assert got["opencode:ses_v2new"] is False and got["opencode:ses_v2old"] is False
    for key in ("opencode:ses_v2new", "opencode:ses_v2old", "opencode:../x", "opencode:"):
        with pytest.raises(ValueError):
            a.end(key)
    assert not any(opencode._ended(p) for p in (me, w1, w2))


def test_v2_end_says_so_when_the_window_does_not_exit(windows, monkeypatch):
    (me, w1, w2), note = windows
    stubborn = subprocess.Popen([sys.executable, "-c", "import signal, time; signal.signal("
                                 "signal.SIGTERM, signal.SIG_IGN); print('ok', flush=True);"
                                 " time.sleep(60)"], stdout=subprocess.PIPE)
    try:
        stubborn.stdout.readline()  # its handler is set
        monkeypatch.setattr(opencode, "_tuis", lambda: [me, w1, w2, stubborn.pid])
        monkeypatch.setattr(opencode, "EXIT_WAIT_S", 0.3)
        note(stubborn.pid, "ses_v2old")
        with pytest.raises(ValueError, match="did not exit"):
            OpenCodeAdapter().end("opencode:ses_v2old")
    finally:
        stubborn.kill()
        stubborn.wait()


def test_v1_end_terminates_the_tui_whose_server_shows_it(fake_opencode, monkeypatch):
    """V1: the TUI's own server's newest session since it started; never an `opencode serve`."""
    monkeypatch.setattr(opencode, "service", lambda: None)
    tui = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    serve = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", "serve"])
    try:
        a = OpenCodeAdapter(find_ports=lambda: {serve.pid: fake_opencode.port,
                                                tui.pid: fake_opencode.port})
        assert [(s.key, s.closable) for s in a.sessions()] == [
            (f"opencode:{NEWEST}", False), (f"opencode:{NEWEST}", True)]
        with pytest.raises(ValueError):
            a.end(f"opencode:{OLDER}")  # not what any window shows now
        a.end(f"opencode:{NEWEST}")
        assert tui.wait(timeout=5) == -15 and serve.poll() is None
    finally:
        for p in (tui, serve):
            p.kill()
            p.wait()


def test_v2_2_session_list_shape(fake_v2, monkeypatch):
    """OpenCode v2.2.5's GET /api/session, as a real 2.2.5 service answered it."""
    fake_v2.sessions = {"data": [{
        "id": "ses_v22", "projectID": "a743b0e7", "cost": 0,
        "tokens": {"input": 4748, "output": 3, "reasoning": 13, "cache": {"read": 512, "write": 0}},
        "outcome": "succeeded", "time": {"created": 60, "updated": 70, "busy": 61, "idle": 71},
        "location": {"directory": CWD}}], "cursor": {"previous": "x", "next": "y"}}
    assert keys(OpenCodeAdapter()) == ["opencode:ses_v22"]


def test_v2_deliver_posts_prompt(fake_v2):
    OpenCodeAdapter(find_ports=lambda: pytest.fail("no V1 port scan on V2")).deliver(
        "opencode:ses_v2new", "hi team")
    assert fake_v2.requests[-1] == ("POST", "/api/session/ses_v2new/prompt",
                                    {"text": "hi team", "delivery": "queue", "resume": True})


def test_v2_deliver_refused_raises(fake_v2, monkeypatch):
    fake_v2.server.RequestHandlerClass.do_POST = lambda self: (
        self.rfile.read(int(self.headers["Content-Length"])), self._send(404, b""))
    with pytest.raises(DeliveryError, match="HTTP 404"):
        OpenCodeAdapter().deliver("opencode:ses_x", "hi")


def test_activity_from_time_updated_and_a_running_turn(monkeypatch):
    """Checked on OpenCode 1.19.5 and 2.3.2: time.updated moves with each step of a turn and
    stays still while idle or while one tool runs long."""
    monkeypatch.setattr(opencode.time, "time", lambda: 10_000.0)
    assert opencode.activity(9_000_000, False) == (1000.0, "idle", None)
    assert opencode.activity(9_400_000, True) == (600.0, "busy", 600.0)
    assert opencode.activity(None, True) == (None, "busy", None)
    assert opencode.activity(True, False) == (None, "", None)


def test_v2_activity_reads_busy_and_idle_times(monkeypatch):
    monkeypatch.setattr(opencode.time, "time", lambda: 10_000.0)
    rows = {"busy": {"updated": 9_900_000, "busy": 9_800_000, "idle": 9_700_000},
            "done": {"updated": 9_900_000, "busy": 9_800_000, "idle": 9_900_000},
            "new": {"updated": 9_900_000}}
    monkeypatch.setattr(opencode, "_v2_call", lambda svc, m, path: (
        200, {"data": {"time": rows[path.rsplit("/", 1)[1]]}}))
    assert opencode._v2_activity({}, "busy") == (100.0, "busy", 100.0)
    assert opencode._v2_activity({}, "done") == (100.0, "idle", None)
    assert opencode._v2_activity({}, "new") == (100.0, "idle", None)
    monkeypatch.setattr(opencode, "_v2_call", lambda *a: (_ for _ in ()).throw(OSError("down")))
    assert opencode._v2_activity({}, "busy") == (None, "", None)


def test_v2_session_carries_its_title(monkeypatch):
    """A window's session: its title comes with the same call that reads its activity."""
    monkeypatch.setattr(opencode.time, "time", lambda: 10_000.0)
    calls = []
    monkeypatch.setattr(opencode, "_v2_call", lambda svc, m, path: calls.append(path) or (
        200, {"data": {"title": "proj-a-stream", "time": {"updated": 9_900_000}}}))
    s = OpenCodeAdapter()._v2_session({}, "opencode:ses_a", CWD)
    assert (s.key, s.title, s.label, s.activity, s.idle_s) == (
        "opencode:ses_a", "proj-a-stream", "", "idle", 100.0)
    assert calls == ["/api/session/ses_a"]  # one call, not one more for the title
    monkeypatch.setattr(opencode, "_v2_call", lambda *a: (_ for _ in ()).throw(OSError("down")))
    assert OpenCodeAdapter()._v2_session({}, "opencode:ses_a", CWD).title == ""


@pytest.mark.parametrize("raw, title", [
    ("Fix the login page", "Fix the login page"), ("x" * 200, "x" * 80), ("a\tb\n c", "a b c"),
    ("New session - 2026-11-25T10:00:00.000Z", ""), ("Child session - 2026-11-25T10:00:00.000Z", ""),
    ("New session ideas", "New session ideas"), ("", ""), (None, ""), (7, ""), ({"a": 1}, "")])
def test_session_title_is_one_clean_line_and_never_the_placeholder(raw, title):
    assert opencode.session_title(raw) == title


def test_plugin_tells_the_hub_of_a_usage_limit_once(tmp_path):
    """The plugins' limit(): a usage or rate limit runs `kaptyorka hook opencode failure` with
    its text on stdin, once per session in 10 minutes; an abort (Esc) says nothing."""
    import shutil

    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    got = tmp_path / "got.jsonl"
    exe = tmp_path / "kaptyorka"
    exe.write_text(f"#!/bin/sh\necho \"$KAPTYORKA_OPENCODE_SESSION $*\" >> {got}.args\n"
                   f"cat >> {got}\necho >> {got}\n")
    exe.chmod(0o755)
    js = tmp_path / "t.mjs"
    js.write_text('import { execFile } from "node:child_process";\n'
                  f"const KAPTYORKA = {json.dumps(str(exe))};\n" + opencode.LIMIT_JS + """
limit("ses_1", { name: "APIError", data: { message: "You have hit your usage limit. Resets at 7pm" } }, process.cwd());
limit("ses_1", { name: "APIError", data: { message: "Rate limit exceeded" } }, process.cwd());
limit("ses_2", { name: "MessageAbortedError", data: { message: "aborted" } }, process.cwd());
setTimeout(() => {}, 1500);
""")
    r = subprocess.run([node, str(js)], capture_output=True, text=True, timeout=20)
    assert r.returncode == 0, r.stderr
    sent = [json.loads(line) for line in got.read_text().splitlines() if line.strip()]
    assert len(sent) == 1 and sent[0]["error"] == "rate_limit" and sent[0]["session_id"] == "ses_1"
    assert "Resets at 7pm" in sent[0]["error_details"]
    assert (tmp_path / "got.jsonl.args").read_text() == "ses_1 hook opencode failure\n"
