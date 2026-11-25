"""Closing a session from the page ends a real Claude Code terminal session and keeps it
resumable: an interactive `claude` in a pseudo-terminal, closed through the bridge's own path
(bridge.py `_close_asked`, ClaudeAdapter.end). `claude stop` knows only background jobs, so
a terminal session gets SIGTERM; it must exit, drop its session file, keep its transcript, and
`claude --resume` must still know what was said. Run with: pytest -m slow
tests/test_close_slow.py (one at a time, like the other slow tests)."""
import contextlib
import os
import pty
import select
import shutil
import signal
import subprocess
import time

import pytest

from helpers import remove_transcripts, scratch_dir, transcript_dir
from kaptyorka import bridge as bridge_mod
from kaptyorka.adapters.claude import ClaudeAdapter

pytestmark = [pytest.mark.slow,
              pytest.mark.skipif(shutil.which("claude") is None, reason="claude is not on PATH")]


def top_level() -> dict:
    """This test's own environment without the session it may run in: a session of its own."""
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("CLAUDE_CODE_") and k not in ("CLAUDECODE", "CLAUDE_PID")}
    env["TERM"] = "xterm-256color"
    return env


def test_a_terminal_session_closed_from_the_page_ends_and_resumes(tmp_path, monkeypatch):
    monkeypatch.setenv("KAPTYORKA_HOME", str(tmp_path / "kh"))
    (tmp_path / "kh").mkdir()
    project = scratch_dir(".kaptyorka-close-")
    env = top_level()
    pid, fd = pty.fork()
    if pid == 0:  # the child: a person's terminal running claude
        os.chdir(project)
        os.execvpe("claude", ["claude", "--model", "haiku", "-n", "kaptyorka-close-slow"], env)
    screen = bytearray()

    def pump(seconds):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if select.select([fd], [], [], 0.2)[0]:
                with contextlib.suppress(OSError):
                    screen.extend(os.read(fd, 65536))

    adapter = ClaudeAdapter()
    lines = []
    b = bridge_mod.Bridge({"hub": "http://127.0.0.1:9", "token": "t", "machine": "box-1"},
                          [adapter], lines.append)

    def mine():
        return next((s for s in adapter.sessions() if s.cwd == str(project)), None)

    try:
        deadline = time.monotonic() + 60
        while mine() is None:
            assert time.monotonic() < deadline, f"no session file in 60 s: {bytes(screen[-500:])!r}"
            pump(1)
        os.write(fd, b"Say the word ok and nothing else.")
        pump(1)
        os.write(fd, b"\r")
        deadline = time.monotonic() + 120
        while True:  # its answer is in, and it is waiting at the prompt again
            pump(2)
            s = mine()
            sid = s.key.split(":", 1)[1]
            done = transcript_dir(project) / f"{sid}.jsonl"
            if done.exists() and '"assistant"' in done.read_text() and s.activity == "idle":
                break
            if time.monotonic() > deadline and b"trust" in bytes(screen).lower():
                pytest.skip(f"Claude Code does not trust {project.parent}; trust it once to run this")
            assert time.monotonic() < deadline, f"no answer in 120 s: {bytes(screen[-500:])!r}"
        assert not s.background
        # closed as idle, but it worked a moment ago: the bridge refuses, it keeps running
        b.hub = {"close_asked": [{"key": s.key, "confirmed": False}]}
        b._close_asked(adapter.sessions())
        assert b._close_done == {s.key: "in use again, not closed"}
        assert os.waitpid(pid, os.WNOHANG) == (0, 0)
        # confirmed on the page: it ends
        b._close_done = {}
        b.hub = {"close_asked": [{"key": s.key, "confirmed": True}]}
        b._close_asked(adapter.sessions())
        assert b._close_done == {s.key: ""}, lines
        deadline = time.monotonic() + 30
        while True:
            pump(0.5)
            got, status = os.waitpid(pid, os.WNOHANG)
            if got == pid:
                break
            assert time.monotonic() < deadline, "the session did not end within 30 s of SIGTERM"
        pid = 0
        assert mine() is None  # its session file is gone: the bridge reports it no more
        assert done.exists()  # the conversation is kept
        r = subprocess.run(["claude", "-p", "--resume", sid, "--model", "haiku",
                            "What word did I ask you to say? Answer with that one word."],
                           cwd=project, env=env, capture_output=True, text=True, timeout=180)
        assert r.returncode == 0, r.stdout + r.stderr
        assert "ok" in r.stdout.lower(), r.stdout
    finally:
        if pid:
            with contextlib.suppress(OSError):
                os.kill(pid, signal.SIGKILL)
                os.waitpid(pid, 0)
        with contextlib.suppress(OSError):
            os.close(fd)
        remove_transcripts(project)
        shutil.rmtree(project, ignore_errors=True)
