import json
import os
import socket
import threading

import pytest

from helpers import write_session_file
from kaptyorka import identity
from kaptyorka.adapters.base import DeliveryError, Session
from kaptyorka.adapters.claude import ClaudeAdapter


def dead_pid() -> int:
    pid = 4_000_000
    while True:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return pid
        except PermissionError:
            pass
        pid += 1


class UnixServer:
    """Accepts one connection and records the lines it receives until EOF."""

    def __init__(self, path):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.bind(str(path))
        self.sock.listen(1)
        self.data = b""
        self.thread = threading.Thread(target=self._serve)
        self.thread.start()

    def _serve(self):
        conn, _ = self.sock.accept()
        with conn:
            while chunk := conn.recv(65536):
                self.data += chunk
        self.sock.close()

    def lines(self):
        self.thread.join(5)
        return self.data.decode().splitlines()


@pytest.fixture
def fake_claude_cli(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    record = tmp_path / "claude-calls"
    script = bin_dir / "claude"
    script.write_text("#!/usr/bin/env python3\nimport json, sys\n"
                      f"open({str(record)!r}, 'a').write(json.dumps(sys.argv[1:]) + '\\n')\n")
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")

    class Fake:
        @property
        def calls(self):
            return [["claude"] + json.loads(line) for line in record.read_text().splitlines()]

    return Fake()


def test_sessions_from_folder(home):
    write_session_file(home, pid=os.getpid(), cwd="/w/murmur", status="idle")
    write_session_file(home, pid=dead_pid(), cwd="/w/gone")
    assert ClaudeAdapter(home).sessions() == [
        Session(f"claude:{os.getpid()}", "claude-code", "murmur", "idle")]


def test_socket_push_multiline_text(home, tmp_path):
    srv = UnixServer(tmp_path / "s.sock")
    write_session_file(home, pid=os.getpid(), sock=tmp_path / "s.sock", peer_token="P")
    ClaudeAdapter(home).deliver(f"claude:{os.getpid()}", 'line1\nline2 "q" ✓')
    a, u = [json.loads(line) for line in srv.lines()]
    assert a == {"type": "auth", "token": "P"}
    assert u == {"type": "user", "message": {"role": "user", "content": 'line1\nline2 "q" ✓'}}


def test_socket_push_without_key_file_skips_auth(home, tmp_path):
    srv = UnixServer(tmp_path / "s.sock")
    write_session_file(home, pid=os.getpid(), sock=tmp_path / "s.sock")
    ClaudeAdapter(home).deliver(f"claude:{os.getpid()}", "hi")
    assert [json.loads(line)["type"] for line in srv.lines()] == ["user"]


def test_push_to_missing_socket_raises(home, tmp_path):
    write_session_file(home, pid=os.getpid(), sock=tmp_path / "none.sock", peer_token="P")
    with pytest.raises(DeliveryError):
        ClaudeAdapter(home).deliver(f"claude:{os.getpid()}", "hi")
    with pytest.raises(DeliveryError):
        ClaudeAdapter(home).deliver("claude:999999999", "no session file")


def test_newer_version_warns_once(home):
    write_session_file(home, pid=os.getpid(), version="2.2.0")
    write_session_file(home, pid=os.getppid(), version="2.10.1")
    lines = []
    a = ClaudeAdapter(home, log=lines.append)
    a.sessions()
    a.sessions()
    assert len(lines) == 1
    assert lines[0] in ("claude-code 2.2.0 is newer than tested 2.1.318; push may break, pull still works",
                        "claude-code 2.10.1 is newer than tested 2.1.318; push may break, pull still works")


def test_tested_version_does_not_warn(home):
    write_session_file(home, pid=os.getpid(), version="2.1.318")
    lines = []
    ClaudeAdapter(home, log=lines.append).sessions()
    assert lines == []


def test_configure_keeps_existing_hooks_and_is_idempotent(home, fake_claude_cli):
    settings = home / ".claude/settings.json"
    settings.parent.mkdir()
    settings.write_text('{"hooks":{"SessionStart":[{"hooks":[{"type":"command","command":"mine"}]}]},'
                        '"mcpServers":{"other":{}}}')
    a = ClaudeAdapter(home)
    a.configure("/k")
    a.configure("/k")
    data = json.loads(settings.read_text())
    ss = data["hooks"]["SessionStart"]
    assert [h["hooks"][0]["command"] for h in ss] == ["mine", "/k hook claude-code start"]
    assert ss[1] == {"hooks": [{"type": "command", "command": "/k hook claude-code start", "timeout": 5}]}
    assert [h["hooks"][0]["command"] for h in data["hooks"]["UserPromptSubmit"]] == \
        ["/k hook claude-code prompt"]
    assert data["mcpServers"] == {"other": {}}
    assert ["claude", "mcp", "remove", "--scope", "user", "kaptyorka"] in fake_claude_cli.calls
    assert ["claude", "mcp", "add", "--scope", "user", "kaptyorka", "--", "/k", "mcp"] in fake_claude_cli.calls


def test_configure_writes_through_symlink(home, tmp_path, fake_claude_cli):
    real = tmp_path / "setup" / "settings.json"
    real.parent.mkdir()
    real.write_text("{}")
    (home / ".claude").mkdir()
    (home / ".claude/settings.json").symlink_to(real)
    ClaudeAdapter(home).configure("/k")
    assert (home / ".claude/settings.json").is_symlink()
    assert "UserPromptSubmit" in json.loads(real.read_text())["hooks"]


def test_configure_leaves_broken_settings_alone(home, fake_claude_cli):
    settings = home / ".claude/settings.json"
    settings.parent.mkdir()
    settings.write_text("{not json")
    lines = []
    ClaudeAdapter(home, log=lines.append).configure("/k")
    assert settings.read_text() == "{not json"
    assert f"left alone, not valid JSON: {settings}" in lines


def test_claude_key_matches_shim_and_bridge(home, monkeypatch):
    write_session_file(home, pid=os.getpid(), cwd="/w/murmur")
    monkeypatch.setattr(identity, "ancestors", lambda pid: [(os.getpid(), "claude")])
    ident = identity.identify("pc", pid=1)
    s = ClaudeAdapter(home).sessions()[0]
    assert (ident.key, ident.tool, ident.project) == (s.key, s.tool, s.project)
