import json
import os
import socket

import pytest

from fakes.opencode_server import FakeOpenCode
from kaptyorka import identity
from kaptyorka.adapters import opencode
from kaptyorka.adapters.base import DeliveryError, Session
from kaptyorka.adapters.opencode import OpenCodeAdapter

CWD = os.getcwd()
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
    assert adapter(fake_opencode).sessions() == [
        Session(key, "opencode", os.path.basename(CWD), "")]


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
    assert len(first) == 3
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
