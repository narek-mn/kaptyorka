import json
import os
import shutil
import socket
import subprocess
import tempfile
import threading
from pathlib import Path

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

    def __init__(self, path, reply=b""):
        self.reply = reply
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.bind(str(path))
        self.sock.listen(1)
        self.data = b""
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        conn, _ = self.sock.accept()
        with conn:
            while chunk := conn.recv(65536):
                self.data += chunk
            conn.sendall(self.reply)
        self.sock.close()

    def lines(self):
        self.thread.join(5)
        return self.data.decode().splitlines()


@pytest.fixture
def fake_claude_cli(tmp_path, monkeypatch):
    """A `claude` on PATH (so installed() is true) and a fake run that records the calls."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "claude"
    script.write_text("#!/bin/sh\nexit 1\n")
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")

    class Fake:
        calls = []

        def run(self, cmd, **kw):
            self.calls.append(list(cmd))
            return subprocess.CompletedProcess(cmd, 0, "", "")

    fake = Fake()
    fake.calls = []
    return fake


@pytest.fixture
def sock_dir():
    """Short folder: macOS caps Unix socket paths at 104 bytes and its tmp_path is longer."""
    d = Path(tempfile.mkdtemp(prefix="kp", dir="/tmp"))
    yield d
    shutil.rmtree(d, ignore_errors=True)


def live_sock(tmp_path):
    p = tmp_path / "live.sock"
    p.touch()
    return p


def test_sessions_from_folder(home, tmp_path):
    write_session_file(home, pid=os.getpid(), cwd="/w/murmur", status="idle", sock=live_sock(tmp_path))
    write_session_file(home, pid=dead_pid(), cwd="/w/gone", sock=live_sock(tmp_path))
    assert ClaudeAdapter(home).sessions() == [
        Session(f"claude:{os.getpid()}", "claude-code", "murmur", "idle")]


def test_socket_push_multiline_text(home, sock_dir):
    srv = UnixServer(sock_dir / "s.sock")
    write_session_file(home, pid=os.getpid(), sock=sock_dir / "s.sock", peer_token="P")
    ClaudeAdapter(home).deliver(f"claude:{os.getpid()}", 'line1\nline2 "q" ✓')
    a, u = [json.loads(line) for line in srv.lines()]
    assert a == {"type": "auth", "token": "P"}
    assert u == {"type": "user", "message": {"role": "user", "content": 'line1\nline2 "q" ✓'}}


def test_sessions_skip_missing_socket(home, tmp_path):
    write_session_file(home, pid=os.getpid(), sock=tmp_path / "gone.sock")  # pid reused
    write_session_file(home, pid=os.getppid())  # no socket path at all
    assert ClaudeAdapter(home).sessions() == []


def test_socket_push_uses_newest_key_file(home, sock_dir):
    srv = UnixServer(sock_dir / "s.sock")
    write_session_file(home, pid=os.getpid(), sock=sock_dir / "s.sock")
    d = home / ".claude" / "sessions"
    (d / f"{os.getpid()}.ffff.key").write_text(json.dumps({"peerToken": "OLD"}))
    os.utime(d / f"{os.getpid()}.ffff.key", (1, 1))
    (d / f"{os.getpid()}.0000.key").write_text(json.dumps({"peerToken": "NEW"}))
    ClaudeAdapter(home).deliver(f"claude:{os.getpid()}", "hi")
    assert json.loads(srv.lines()[0]) == {"type": "auth", "token": "NEW"}


def test_socket_push_error_reply_raises(home, sock_dir):
    srv = UnixServer(sock_dir / "s.sock", reply=b'{"type":"error","message":"bad token"}\n')
    write_session_file(home, pid=os.getpid(), sock=sock_dir / "s.sock", peer_token="P")
    with pytest.raises(DeliveryError, match="bad token"):
        ClaudeAdapter(home).deliver(f"claude:{os.getpid()}", "hi")
    srv.lines()


def test_socket_push_without_key_file_skips_auth(home, sock_dir):
    srv = UnixServer(sock_dir / "s.sock")
    write_session_file(home, pid=os.getpid(), sock=sock_dir / "s.sock")
    ClaudeAdapter(home).deliver(f"claude:{os.getpid()}", "hi")
    assert [json.loads(line)["type"] for line in srv.lines()] == ["user"]


def test_push_to_missing_socket_raises(home, tmp_path):
    write_session_file(home, pid=os.getpid(), sock=tmp_path / "none.sock", peer_token="P")
    with pytest.raises(DeliveryError):
        ClaudeAdapter(home).deliver(f"claude:{os.getpid()}", "hi")
    with pytest.raises(DeliveryError):
        ClaudeAdapter(home).deliver("claude:999999999", "no session file")


def test_newer_version_warns_once(home, tmp_path):
    write_session_file(home, pid=os.getpid(), version="2.2.0", sock=live_sock(tmp_path))
    write_session_file(home, pid=os.getppid(), version="2.10.1", sock=live_sock(tmp_path))
    lines = []
    a = ClaudeAdapter(home, log=lines.append)
    a.sessions()
    a.sessions()
    assert len(lines) == 1
    assert lines[0] in ("claude-code 2.2.0 is newer than tested 2.1.318; push may break, pull still works",
                        "claude-code 2.10.1 is newer than tested 2.1.318; push may break, pull still works")


def test_tested_version_does_not_warn(home, tmp_path):
    write_session_file(home, pid=os.getpid(), version="2.1.318", sock=live_sock(tmp_path))
    lines = []
    ClaudeAdapter(home, log=lines.append).sessions()
    assert lines == []


def test_configure_keeps_existing_hooks_and_is_idempotent(home, fake_claude_cli):
    settings = home / ".claude/settings.json"
    settings.parent.mkdir()
    settings.write_text('{"hooks":{"SessionStart":[{"hooks":[{"type":"command","command":"mine"}]}]},'
                        '"mcpServers":{"other":{}}}')
    a = ClaudeAdapter(home, run=fake_claude_cli.run)
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


def test_configure_replaces_hook_from_moved_install(home, fake_claude_cli):
    settings = home / ".claude/settings.json"
    settings.parent.mkdir()
    settings.write_text(json.dumps({"hooks": {"UserPromptSubmit": [
        {"hooks": [{"type": "command", "command": "/old/kaptyorka hook claude-code prompt"}]},
        {"hooks": [{"type": "command", "command": "mine"}]}]}}))
    ClaudeAdapter(home, run=fake_claude_cli.run).configure("/k")
    ups = json.loads(settings.read_text())["hooks"]["UserPromptSubmit"]
    assert [h["hooks"][0]["command"] for h in ups] == ["/k hook claude-code prompt", "mine"]


def test_configure_writes_through_symlink(home, tmp_path, fake_claude_cli):
    real = tmp_path / "setup" / "settings.json"
    real.parent.mkdir()
    real.write_text("{}")
    (home / ".claude").mkdir()
    (home / ".claude/settings.json").symlink_to(real)
    ClaudeAdapter(home, run=fake_claude_cli.run).configure("/k")
    assert (home / ".claude/settings.json").is_symlink()
    assert "UserPromptSubmit" in json.loads(real.read_text())["hooks"]


def test_configure_leaves_broken_settings_alone(home, fake_claude_cli):
    settings = home / ".claude/settings.json"
    settings.parent.mkdir()
    settings.write_text("{not json")
    lines = []
    ClaudeAdapter(home, log=lines.append, run=fake_claude_cli.run).configure("/k")
    assert settings.read_text() == "{not json"
    assert f"left alone, not valid JSON: {settings}" in lines


def test_claude_key_matches_shim_and_bridge(home, monkeypatch, tmp_path):
    write_session_file(home, pid=os.getpid(), cwd="/w/murmur", sock=live_sock(tmp_path))
    monkeypatch.setattr(identity, "ancestors", lambda pid: [(os.getpid(), "claude")])
    ident = identity.identify("pc", pid=1)
    s = ClaudeAdapter(home).sessions()[0]
    assert (ident.key, ident.tool, ident.project) == (s.key, s.tool, s.project)


def test_configure_quotes_exe_and_replaces_old_unquoted_hook(home, fake_claude_cli):
    settings = home / ".claude/settings.json"
    settings.parent.mkdir()
    settings.write_text(json.dumps({"hooks": {"UserPromptSubmit": [
        {"hooks": [{"type": "command", "command": "/my dir/k hook claude-code prompt"}]}]}}))
    ClaudeAdapter(home, run=fake_claude_cli.run).configure("/my dir/k")
    ups = json.loads(settings.read_text())["hooks"]["UserPromptSubmit"]
    assert [h["hooks"][0]["command"] for h in ups] == ["'/my dir/k' hook claude-code prompt"]
    assert ["claude", "mcp", "add", "--scope", "user", "kaptyorka", "--", "/my dir/k", "mcp"] \
        in fake_claude_cli.calls


def test_label_is_session_name_user_or_derived(home, tmp_path):
    sock = live_sock(tmp_path)
    write_session_file(home, pid=os.getpid(), sock=sock, name="kaptyorka-narek", name_source="user")
    assert [s.label for s in ClaudeAdapter(home).sessions()] == ["kaptyorka-narek"]
    write_session_file(home, pid=os.getpid(), sock=sock, name="fix-the-bug", name_source="derived")
    assert [s.label for s in ClaudeAdapter(home).sessions()] == ["fix-the-bug"]
    write_session_file(home, pid=os.getpid(), sock=sock)  # no name at all
    assert [s.label for s in ClaudeAdapter(home).sessions()] == [""]
