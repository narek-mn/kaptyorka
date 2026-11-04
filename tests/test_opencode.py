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
NEWEST, OLDER, ELSEWHERE = "ses_new", "ses_old", "ses_other"


@pytest.fixture
def fake_opencode(monkeypatch):
    srv = FakeOpenCode([
        {"id": OLDER, "directory": CWD, "time": {"created": 1, "updated": 100}},
        {"id": NEWEST, "directory": CWD, "time": {"created": 2, "updated": 200}},
        {"id": ELSEWHERE, "directory": "/elsewhere", "time": {"created": 3, "updated": 300}},
    ])
    monkeypatch.setattr(opencode, "_listen_ports",
                        lambda pid: [srv.port] if pid == os.getpid() else [])
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
    fake_opencode.sessions = []
    assert identity.RESOLVERS["opencode"](os.getpid(), CWD) is None
    assert adapter(fake_opencode).sessions() == []


def test_identify_honours_opencode_pid_env(fake_opencode, monkeypatch):
    monkeypatch.setattr(identity, "ancestors", lambda pid: [(1, "bun")])
    monkeypatch.setenv("OPENCODE_PID", str(os.getpid()))
    ident = identity.identify("pc", pid=1)
    assert (ident.key, ident.tool) == (f"opencode:{NEWEST}", "opencode")


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


def test_configure_leaves_broken_config_alone(home, monkeypatch):
    monkeypatch.setenv("SHELL", "/bin/bash")
    cfg = home / ".config/opencode/opencode.json"
    cfg.parent.mkdir(parents=True)
    cfg.write_text("{not json")
    lines = []
    OpenCodeAdapter(home, log=lines.append).configure("/k")
    assert cfg.read_text() == "{not json"
    assert f"left alone, not valid JSON: {cfg}" in lines
