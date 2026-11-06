"""Real workers: a Claude Code worker end to end, a scope that outlives its launcher, a tmux
worker. Run with: pytest -m slow. Everything started here is stopped in `finally`."""
import contextlib
import json
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from helpers import call, person, run
from kaptyorka import bridge as bridge_mod
from kaptyorka import identity
from kaptyorka.adapters.claude import ClaudeAdapter
from kaptyorka.bridge import Bridge
from kaptyorka.launch import Launcher
from kaptyorka.workers import WorkerStore

BOSS = {"key": "k:boss", "machine": "laptop", "tool": "codex", "project": "proj-a"}
TASK = ("Write the text KAPTYORKA-WORKER-OK into done.txt in this folder, then stop."
        " Do not send any message.")


def _quiet(argv, timeout=60):
    with contextlib.suppress(OSError, subprocess.TimeoutExpired):
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout)


def _wait(cond, timeout, what):
    deadline = time.monotonic() + timeout
    while True:
        got = cond()
        if got:
            return got
        assert time.monotonic() < deadline, f"{what} within {timeout} s"
        time.sleep(1)


def _scratch_folder() -> Path:
    # Claude Code refuses --bg in an untrusted folder, and /tmp is not trusted; a throwaway
    # folder beside the repo inherits the trust of its parent (never the repo itself). Not a
    # git repo: `git init` makes it a project root of its own, which is untrusted again.
    return Path(tempfile.mkdtemp(prefix=".kaptyorka-worker-",
                                 dir=Path(__file__).resolve().parents[2]))


def _jobs(folder: Path) -> list:
    """The short ids of the background Claude Code sessions working in folder."""
    r = _quiet(["claude", "agents", "--json"])
    try:
        return [a["id"] for a in json.loads(r.stdout) if a.get("cwd") == str(folder) and a.get("id")]
    except (AttributeError, ValueError, TypeError, KeyError):
        return []


def _scope_active(name: str) -> bool:
    r = _quiet(["systemctl", "--user", "is-active", f"kaptyorka-worker-{name}.scope"], 10)
    return r is not None and r.stdout.strip() == "active"


@pytest.mark.slow
@pytest.mark.skipif(shutil.which("claude") is None, reason="claude is not on PATH")
def test_claude_worker_round_trip(hub, tmp_path, monkeypatch):
    real_home = Path.home()  # the worker shows up in the real ~/.claude/sessions
    monkeypatch.setenv("KAPTYORKA_HOME", str(tmp_path / "kaptyorka"))  # never the real one
    monkeypatch.setenv("ANTHROPIC_MODEL", "haiku")  # the worker's model; detach passes env on
    monkeypatch.setattr(bridge_mod, "WAIT_S", 2)
    monkeypatch.setattr(bridge_mod, "HEARTBEAT_S", 2)
    monkeypatch.setattr(identity, "facts",
                        lambda: {"host": "box-1", "os_user": "u", "version": "0.3.0"})
    folder = _scratch_folder()
    (tmp_path / "kaptyorka").mkdir()
    (tmp_path / "kaptyorka" / "folders.json").write_text(json.dumps({"proj-a": str(folder)}))

    hub.store.project_register("proj-a", by="test")
    pid, ptoken = person(hub.store, "alice")
    hub.store.grant_project("proj-a", pid, "write", "test")
    token, secret = hub.store.invite_redeem(hub.store.invite_create(pid), "box-1",
                                            host="box-1", os_user="u")
    client = SimpleNamespace(url=hub.url, token=ptoken)
    cfg = {"hub": hub.url, "token": token, "secret": secret, "machine": "box-1"}
    lines = []
    adapter = ClaudeAdapter(home=real_home, log=lines.append)
    bridge = Bridge(cfg, [adapter], lines.append,
                    launcher=Launcher(cfg, real_home, log=lines.append))
    workers = WorkerStore(hub.store)
    name, key, thread = "proj-a-w1", None, None
    try:
        bridge.heartbeat()  # learns its projects
        bridge.heartbeat()  # the hub learns the folder
        answer = run(call(client, BOSS, "work", project="proj-a", task=TASK))
        assert answer.startswith(f"starting `{name}` on box-1"), answer
        thread = threading.Thread(target=bridge.run, daemon=True)
        thread.start()

        def settled():
            w = workers.by_name(name)
            return w if w["state"] in ("up", "failed") else None

        w = _wait(settled, 120, "worker not up")
        if "does not trust" in (w.get("error") or ""):
            pytest.skip(f"Claude Code does not trust {folder.parent}; trust it once to run this")
        log = Path(tmp_path / "kaptyorka" / "workers" / f"{name}.log")
        assert w["state"] == "up", (w, lines, log.read_text() if log.exists() else "")
        key = w["key"]
        assert key in {s.key for s in adapter.sessions()}

        texts = [m["text"] for m in run(call(client, BOSS, "inbox"))]
        assert any(f"worker `{name}` is up on box-1" in t for t in texts), texts

        assert run(call(client, BOSS, "close_worker", name=name)).startswith(f"closing `{name}`")
        _wait(lambda: workers.by_name(name)["state"] == "closed", 60, "worker not closed")
        _wait(lambda: key not in {s.key for s in adapter.sessions()}, 60, "session not gone")
    finally:
        bridge.stop.set()
        if thread is not None:
            thread.join(30)
        for job in _jobs(folder):  # `claude stop` takes the short id, never the name
            for step in ("stop", "rm"):
                _quiet(["claude", step, job])
        _quiet(["systemctl", "--user", "stop", f"kaptyorka-worker-{name}.scope"], 30)
        shutil.rmtree(folder, ignore_errors=True)
        # the transcript folder Claude Code made for it: ~/.claude/projects/<path with - for / .>
        tag = folder.name.lstrip(".")
        for p in (real_home / ".claude" / "projects").glob(f"*{tag}"):
            if p.name.endswith(f"--{tag}"):
                shutil.rmtree(p, ignore_errors=True)


@pytest.mark.slow
@pytest.mark.skipif(not sys.platform.startswith("linux") or shutil.which("systemd-run") is None,
                    reason="needs Linux with systemd-run")
def test_worker_outlives_bridge_restart(tmp_path):
    """The launching process runs in a unit of its own, like the bridge service; stopping
    that unit kills it, and the worker's scope stays."""
    name = f"slow-{uuid.uuid4().hex[:8]}"
    launcher_unit = f"kaptyorka-slow-bridge-{name}"
    script = ("import sys\nfrom pathlib import Path\nfrom kaptyorka.launch import Launcher\n"
              "Launcher({}, Path(sys.argv[1])).detach(['sleep', '300'], sys.argv[1],"
              " sys.argv[2])\n")
    env = {"KAPTYORKA_HOME": str(tmp_path / "kaptyorka")}
    proc = subprocess.Popen(
        ["systemd-run", "--user", "--scope", "--quiet", "--unit", launcher_unit,
         *[f"--setenv={k}={v}" for k, v in env.items()],
         sys.executable, "-c", script, str(tmp_path), name],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        # detach waits for sleep, which never returns in time: the launcher is still in it
        _wait(lambda: _scope_active(name), 8, "worker scope not active")
        assert proc.poll() is None, proc.stdout.read()
        _quiet(["systemctl", "--user", "kill", "--signal=KILL", f"{launcher_unit}.scope"], 10)
        proc.wait(timeout=10)
        time.sleep(1)
        assert _scope_active(name)
    finally:
        _quiet(["systemctl", "--user", "stop", f"{launcher_unit}.scope"], 30)
        _quiet(["systemctl", "--user", "stop", f"kaptyorka-worker-{name}.scope"], 30)
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.kill()
            proc.wait(timeout=10)
        proc.stdout.close()
    assert not _scope_active(name)


@pytest.mark.slow
@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is not on PATH")
def test_tmux_worker_starts_and_closes(tmp_path, monkeypatch):
    monkeypatch.setenv("KAPTYORKA_HOME", str(tmp_path / "kaptyorka"))
    name = f"slow-{uuid.uuid4().hex[:8]}"
    order = {"worker": name, "tool": "codex", "first_message": "hi"}
    launcher = Launcher({}, tmp_path, log=lambda line: None)
    cmd = launcher.command(order, str(tmp_path))
    argv = cmd[:cmd.index("codex")] + ["bash", "-c", "sleep 300"]  # the launcher's tmux line
    assert argv[:5] == ["tmux", "new-session", "-d", "-s", name]
    try:
        launcher.detach(argv, str(tmp_path), name)
        assert _quiet(["tmux", "has-session", "-t", name], 10).returncode == 0
        launcher.close(order)
        assert _quiet(["tmux", "has-session", "-t", name], 10).returncode != 0
        launcher.close(order)  # closing a gone session is done, not an error
    finally:
        _quiet(["tmux", "kill-session", "-t", name], 10)
        _quiet(["systemctl", "--user", "stop", f"kaptyorka-worker-{name}.scope"], 30)
