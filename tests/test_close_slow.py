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


class Terminal:
    """A person's terminal running an interactive `claude` in `project` (a pseudo-terminal)."""

    def __init__(self, project, *args):
        self.project, self.env, self.screen = project, top_level(), bytearray()
        self.pid, self.fd = pty.fork()
        if self.pid == 0:  # the child
            os.chdir(project)
            os.execvpe("claude", ["claude", "--model", "haiku", *args], self.env)
        self.adapter = ClaudeAdapter()
        self.lines = []
        self.bridge = bridge_mod.Bridge({"hub": "http://127.0.0.1:9", "token": "t",
                                         "machine": "box-1"}, [self.adapter], self.lines.append)

    def pump(self, seconds):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if select.select([self.fd], [], [], 0.2)[0]:
                with contextlib.suppress(OSError):
                    self.screen.extend(os.read(self.fd, 65536))

    def tail(self):
        return bytes(self.screen[-500:])

    def mine(self):
        return next((s for s in self.adapter.sessions() if s.cwd == str(self.project)), None)

    def say(self, text):
        deadline = time.monotonic() + 60
        while self.mine() is None:
            assert time.monotonic() < deadline, f"no session file in 60 s: {self.tail()!r}"
            self.pump(1)
        os.write(self.fd, text.encode())
        self.pump(1)
        os.write(self.fd, b"\r")

    def until(self, cond, seconds, what):
        deadline = time.monotonic() + seconds
        while True:
            self.pump(2)
            s = self.mine()
            if s is not None and cond(s):
                return s
            if time.monotonic() > deadline and b"trust" in bytes(self.screen).lower():
                pytest.skip(f"Claude Code does not trust {self.project.parent}; trust it once")
            assert time.monotonic() < deadline, f"{what} not within {seconds} s: {self.tail()!r}"

    def close(self, key, confirmed):
        self.bridge._close_done = {}
        self.bridge.hub = {"close_asked": [{"key": key, "confirmed": confirmed}]}
        self.bridge._close_asked(self.adapter.sessions())
        return self.bridge._close_done

    def ended(self, seconds=30) -> bool:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self.pump(0.5)
            if os.waitpid(self.pid, os.WNOHANG)[0] == self.pid:
                self.pid = 0
                return True
        return False

    def stop(self):
        if self.pid:
            with contextlib.suppress(OSError):
                os.kill(self.pid, signal.SIGKILL)
                os.waitpid(self.pid, 0)
        with contextlib.suppress(OSError):
            os.close(self.fd)


def test_a_terminal_session_closed_from_the_page_ends_and_resumes(tmp_path, monkeypatch):
    monkeypatch.setenv("KAPTYORKA_HOME", str(tmp_path / "kh"))
    (tmp_path / "kh").mkdir()
    project = scratch_dir(".kaptyorka-close-")
    t = Terminal(project, "-n", "kaptyorka-close-slow")
    try:
        t.say("Say the word ok and nothing else.")

        def answered(s):
            f = transcript_dir(project) / f"{s.key.split(':', 1)[1]}.jsonl"
            return f.exists() and '"assistant"' in f.read_text() and s.activity == "idle"

        s = t.until(answered, 120, "its answer")  # and it waits at the prompt again
        sid = s.key.split(":", 1)[1]
        done = transcript_dir(project) / f"{sid}.jsonl"
        assert not s.background
        # closed as idle, but it worked a moment ago: the bridge refuses, it keeps running
        assert t.close(s.key, False) == {s.key: "in use again, not closed"}
        assert os.waitpid(t.pid, os.WNOHANG) == (0, 0)
        # confirmed on the page: it ends
        assert t.close(s.key, True) == {s.key: ""}, t.lines
        assert t.ended(), "the session did not end within 30 s of SIGTERM"
        assert t.mine() is None  # its session file is gone: the bridge reports it no more
        assert done.exists()  # the conversation is kept
        r = subprocess.run(["claude", "-p", "--resume", sid, "--model", "haiku",
                            "What word did I ask you to say? Answer with that one word."],
                           cwd=project, env=t.env, capture_output=True, text=True, timeout=180)
        assert r.returncode == 0, r.stdout + r.stderr
        assert "ok" in r.stdout.lower(), r.stdout
    finally:
        t.stop()
        remove_transcripts(project)
        shutil.rmtree(project, ignore_errors=True)


def commands(argv):
    """Pids of the live processes whose command line is exactly argv."""
    want, out = [a.encode() for a in argv], []
    for p in os.listdir("/proc"):
        with contextlib.suppress(OSError):
            if p.isdigit() and open(f"/proc/{p}/cmdline", "rb").read().split(b"\0")[:-1] == want:
                out.append(int(p))
    return out


@pytest.mark.skipif(not os.path.isdir("/proc"), reason="finds the tool's command through /proc")
def test_a_session_closed_in_the_middle_of_a_bash_call_ends_and_so_does_the_command(tmp_path, monkeypatch):
    """Closing a working session (confirmed on the page) ends it and the command its Bash tool
    runs: checked on 2.1.332, that command runs in its own process session (not claude's
    group), and Claude Code ends it itself when SIGTERM ends claude."""
    monkeypatch.setenv("KAPTYORKA_HOME", str(tmp_path / "kh"))
    (tmp_path / "kh").mkdir()
    project = scratch_dir(".kaptyorka-close-")
    cmd = ["tail", "-f", "/dev/null"]  # a standalone sleep may be refused by a person's hooks
    t = Terminal(project, "--allowedTools", "Bash(timeout *)")
    try:
        t.say("Call your Bash tool now, in the foreground, with exactly this command: "
              "timeout 117 tail -f /dev/null")
        deadline = time.monotonic() + 120
        while not commands(cmd):
            assert time.monotonic() < deadline, f"its Bash call never started: {t.tail()!r}"
            t.pump(1)
        [child] = commands(cmd)
        assert os.getsid(child) != os.getsid(t.pid)  # not in claude's group: killpg would miss it
        s = t.mine()
        assert s.activity != "idle"  # working on that call
        assert t.close(s.key, False) == {s.key: "in use again, not closed"}
        assert t.close(s.key, True) == {s.key: ""}, t.lines
        assert t.ended(), "the session did not end within 30 s of SIGTERM"
        deadline = time.monotonic() + 10
        while child in commands(cmd):
            assert time.monotonic() < deadline, "its Bash command was left running"
            time.sleep(0.5)
    finally:
        t.stop()
        for p in commands(cmd):
            with contextlib.suppress(OSError):
                os.kill(p, signal.SIGKILL)
        remove_transcripts(project)
        shutil.rmtree(project, ignore_errors=True)
