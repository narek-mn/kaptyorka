"""A real Claude Code worker on a team machine, end to end: session A asks, the hub starts
worker B through the team machine's bridge, B does its task, reports to A and closes itself.
Run with: pytest -m slow tests/test_team_worker_slow.py

Sandboxed: B runs under its own HOME (a copy of this user's Claude Code login, its own
background daemon, so the environment reaches it) whose only MCP server is this worktree's
kaptyorka shim with its own KAPTYORKA_HOME, pointed at an in-process hub on a free port. It
never touches the real hub, ~/.kaptyorka or ~/.claude. Everything started here is stopped in
`finally`, and the sandbox's processes are killed by their HOME."""
import contextlib
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import kaptyorka
from helpers import call, person, run
from kaptyorka import admin_ops
from kaptyorka import bridge as bridge_mod
from kaptyorka import identity
from kaptyorka.adapters.claude import ClaudeAdapter
from kaptyorka.bridge import Bridge
from kaptyorka.launch import Launcher
from kaptyorka.workers import WorkerStore

SHIM = Path(sys.executable).with_name("kaptyorka")  # this worktree's own, never the installed one
BOSS = {"key": "claude:boss-a", "machine": "laptop", "tool": "claude-code", "project": "proj-a"}
TASK = ("Write the text KAPTYORKA-TEAM-OK into done.txt in this folder with the Write tool."
        " Stay in this folder: no worktree, no git commands, no shell.")


def _wait(cond, timeout, what):
    deadline = time.monotonic() + timeout
    while True:
        got = cond()
        if got:
            return got
        assert time.monotonic() < deadline, f"{what} within {timeout} s"
        time.sleep(2)


def _sandbox_processes(home: Path) -> list:
    """Pids of the processes running with HOME=home: the sandbox's daemon and sessions."""
    mark = f"HOME={home}".encode()
    out = []
    for p in Path("/proc").iterdir():
        if not p.name.isdigit() or int(p.name) == os.getpid():
            continue
        with contextlib.suppress(OSError):
            if mark in (p / "environ").read_bytes().split(b"\0"):
                out.append(int(p.name))
    return out


@pytest.mark.slow
@pytest.mark.team_only  # the real team-project gate: the shim is a process of its own
@pytest.mark.skipif(shutil.which("claude") is None or not SHIM.exists(),
                    reason="needs claude on PATH and this worktree's kaptyorka")
@pytest.mark.skipif(not (Path.home() / ".claude" / ".credentials.json").exists(),
                    reason="no Claude Code login to copy")
def test_real_worker_on_a_team_machine_reports_and_closes_itself(hub, monkeypatch):
    real_claude = Path.home() / ".claude"
    root = Path(tempfile.mkdtemp(prefix="kaptyorka-team-slow-"))
    home, khome = root / "home", root / "home" / ".kaptyorka"
    folder = root / "proj-a"
    for d in (home / ".claude", khome, folder):
        d.mkdir(parents=True)
    shutil.copy(real_claude / ".credentials.json", home / ".claude" / ".credentials.json")
    os.chmod(home / ".claude" / ".credentials.json", 0o600)
    (home / ".claude" / "settings.json").write_text(json.dumps({"permissions": {
        "defaultMode": "acceptEdits", "allow": ["mcp__kaptyorka", "Write", "Edit", "Read"]}}))
    # no worktree settings here: the launcher's own --settings must keep the worker in proj-a
    (home / ".claude.json").write_text(json.dumps({
        "hasCompletedOnboarding": True,
        "projects": {str(folder): {"hasTrustDialogAccepted": True}},
        "mcpServers": {"kaptyorka": {"type": "stdio", "command": str(SHIM), "args": ["mcp"],
                                     "env": {"KAPTYORKA_HOME": str(khome)}}}}))
    for argv in (["init", "-q", "-b", "main"], ["commit", "-q", "--allow-empty", "-m", "start"]):
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-C", str(folder),
                        *argv], check=True)
    (khome / "folders.json").write_text(json.dumps({"proj-a": str(folder)}))

    monkeypatch.setenv("HOME", str(home))  # the launcher passes its environment on
    monkeypatch.setenv("KAPTYORKA_HOME", str(khome))
    monkeypatch.setenv("ANTHROPIC_MODEL", "haiku")
    for k in ("CLAUDECODE", "CLAUDE_CONFIG_DIR", "TMUX"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(bridge_mod, "WAIT_S", 2)
    monkeypatch.setattr(bridge_mod, "HEARTBEAT_S", 2)
    monkeypatch.setattr(identity, "facts", lambda: {"host": "jetson-1", "os_user": "u",
                                                    "version": kaptyorka.__version__})

    store = hub.store
    store.project_register("proj-a", by="test")
    _, root_token = person(store, "root", "admin")
    alice_id, alice_token = person(store, "alice")
    store.grant_project("proj-a", alice_id, "write", "test")
    jet_id = store.person_create("jetson-1")  # joined as if it were a person
    token, secret = store.invite_redeem(store.invite_create(jet_id), "u@jetson-1",
                                        host="jetson-1", os_user="u")
    admin_ops.make_team_machine(store, "jetson-1", store.authenticate(root_token))
    store.grant_project("proj-a", jet_id, "read", "test")
    cfg = {"hub": hub.url, "token": token, "secret": secret, "machine": "u@jetson-1"}
    (khome / "config.json").write_text(json.dumps(cfg))
    os.chmod(khome / "config.json", 0o600)

    alice = SimpleNamespace(url=hub.url, token=alice_token)
    lines = []
    adapter = ClaudeAdapter(home=home, log=lines.append)
    bridge = Bridge(cfg, [adapter], lines.append, launcher=Launcher(cfg, home, log=lines.append))
    workers = WorkerStore(store)
    name, thread = "proj-a-w1", None
    try:
        bridge.heartbeat()
        bridge.heartbeat()
        run(call(alice, BOSS, "inbox"))  # session A is online
        answer = run(call(alice, BOSS, "work", project="proj-a", task=TASK, machine="jetson-1"))
        assert answer.startswith(f"starting `{name}` on jetson-1"), answer
        thread = threading.Thread(target=bridge.run, daemon=True)
        thread.start()

        def settled():
            w = workers.by_name(name)
            return w if w["state"] in ("up", "failed", "closed") else None

        w = _wait(settled, 180, "worker not up")
        log = khome / "workers" / f"{name}.log"
        assert w["state"] in ("up", "closed"), (w, lines, log.read_text() if log.exists() else "")

        def inbox():
            return [m["text"] for m in run(call(alice, BOSS, "inbox"))]

        texts = []

        def closed_line():
            texts.extend(inbox())
            return any(t.startswith(f"worker `{name}` closed: task done") for t in texts)

        _wait(closed_line, 300, f"no closed line (worker {workers.by_name(name)}, lines {lines})")
        assert (folder / "done.txt").read_text().strip() == "KAPTYORKA-TEAM-OK"  # files stay
        assert any("KAPTYORKA-TEAM-OK" in t or "done.txt" in t for t in texts
                   if not t.startswith("worker `")), texts  # B's own report reached A
        line = next(t for t in texts if t.startswith(f"worker `{name}` closed"))
        assert "it left branch main with" in line and str(folder) in line, line
        w = workers.by_name(name)
        assert (w["state"], w["error"]) == ("closed", "task done")
        _wait(lambda: not any(s.key == w["key"] for s in adapter.sessions()), 60,
              "session not gone")
    except AssertionError as e:  # what the worker saw and did, before the sandbox goes
        env = {**os.environ, "HOME": str(home)}
        r = subprocess.run(["claude", "agents", "--json"], env=env, capture_output=True,
                           text=True, timeout=60)
        logs = []
        with contextlib.suppress(ValueError, TypeError):
            for a in json.loads(r.stdout):
                logs.append(a)
        done = (folder / "done.txt").exists()
        for f in (home / ".claude" / "projects").glob("*/*.jsonl"):
            logs.append(f.read_text()[-6000:])
        with store.lock:
            logs.append([dict(r) for r in store.db.execute("SELECT key, activity, project FROM agents")])
        with store.lock:
            msgs = [dict(m) for m in store.db.execute("SELECT sender_name, to_name, text FROM messages")]
        raise AssertionError(f"{e}\ndone.txt: {done}\nmessages: {msgs}\nlogs: {logs}") from None
    finally:
        bridge.stop.set()
        if thread is not None:
            thread.join(30)
        for pid in _sandbox_processes(home):  # its daemon and anything it left running
            with contextlib.suppress(OSError):
                os.kill(pid, signal.SIGTERM)
        time.sleep(2)
        for pid in _sandbox_processes(home):
            with contextlib.suppress(OSError):
                os.kill(pid, signal.SIGKILL)
        shutil.rmtree(root, ignore_errors=True)  # the login copy goes with it
