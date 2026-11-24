"""A real stuck session. Run with: pytest -m slow tests/test_stuck_slow.py

A `claude --bg` session starts a background task that prints nothing and ends its turn. A real
bridge (real ClaudeAdapter) reports it to an in-process hub: status "shell", one live task with
its command and a growing silence. With WAKE_S cut to 25 s, the hub's tick asks the session to
check; the bridge delivers it, and the session answers in a new turn while the task still
runs (a finished turn takes a message at once). Scratch KAPTYORKA_HOME; everything is stopped
in `finally`. Linux only: task detection reads /proc."""
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from conftest import start_hub
from helpers import allowed_tools, person, remove_transcripts, scratch_dir
from kaptyorka import bridge as bridge_mod
from kaptyorka import identity, stuck
from kaptyorka.adapters.base import DeliveryError
from kaptyorka.adapters.claude import ClaudeAdapter
from kaptyorka.bridge import Bridge
from kaptyorka.manager import ManagerStore
from kaptyorka.store import Store
from kaptyorka.workers import HUB_KEY
from test_worker_slow import _jobs, _quiet, _wait

TASK = ("Use the Bash tool with run_in_background set to true to run exactly"
        " `python3 -c 'import time; time.sleep(900)'`. Do not wait for it and do not check on"
        " it. Reply with the single word started and end your turn.")


def _scratch() -> Path:
    return scratch_dir(".kaptyorka-worker-")


@pytest.mark.slow
@pytest.mark.team_only  # the real gate: the developer's own sessions (this one) stay unreported
@pytest.mark.skipif(shutil.which("claude") is None, reason="claude is not on PATH")
@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="task detection reads /proc")
def test_a_silent_background_task_is_found_and_the_session_woken(tmp_path, monkeypatch):
    real_home = Path.home()
    khome = tmp_path / "kaptyorka"
    monkeypatch.setenv("KAPTYORKA_HOME", str(khome))
    monkeypatch.setattr(bridge_mod, "WAIT_S", 1)
    monkeypatch.setattr(bridge_mod, "HEARTBEAT_S", 1)
    monkeypatch.setattr(stuck, "WAKE_S", 25)
    monkeypatch.setattr(stuck, "TELL_S", 10_000)  # this test is about the first stage only
    monkeypatch.setattr(identity, "facts",
                        lambda: {"host": "box-1", "os_user": "u", "version": "0.4.0"})
    store = Store(tmp_path / "hub.db")
    ManagerStore(store).set_enabled(False, "test")
    stop = start_hub(store)
    scratch = _scratch()
    repo = scratch / "proj-a"
    repo.mkdir()
    store.project_register("proj-a", by="test")
    pid, _ = person(store, "alice")
    store.grant_project("proj-a", pid, "write", "test")
    token, secret = store.invite_redeem(store.invite_create(pid), "box-1", host="box-1",
                                        os_user="u")
    cfg = {"hub": f"http://127.0.0.1:{stop.port}", "token": token, "secret": secret,
           "machine": "box-1"}
    khome.mkdir(exist_ok=True)
    (khome / "config.json").write_text(json.dumps(cfg))
    (khome / "folders.json").write_text(json.dumps({"proj-a": str(repo)}))
    lines = []
    adapter = ClaudeAdapter(home=real_home, log=lines.append)
    bridge = Bridge(cfg, [adapter], lines.append)
    thread, job = None, None
    try:
        bridge.heartbeat()
        thread = threading.Thread(target=bridge.run, daemon=True)
        thread.start()
        r = subprocess.run(["claude", "--bg", "-n", "kaptyorka-stuck-slow", "--model", "haiku",
                            "--strict-mcp-config", "--permission-mode", "acceptEdits",
                            allowed_tools("Bash"), TASK],
                           cwd=repo, capture_output=True, text=True, timeout=60)
        if "not trusted" in (r.stdout + r.stderr):
            pytest.skip(f"Claude Code does not trust {repo}; trust {scratch.parent} once")
        job = next(iter(_jobs(repo)), None)
        assert r.returncode == 0, r.stdout + r.stderr
        session = _wait(lambda: next((s for s in adapter.sessions() if s.cwd == str(repo)), None),
                        60, "session not in ~/.claude/sessions")
        key = session.key

        def seen():
            a = store.agent(key)
            return a if a is not None and a.activity == "shell" and a.tasks else None
        a = _wait(seen, 120, f"no shell status with a task on the hub ({lines})")
        assert a.tasks[0]["command"] == "python3 -c 'import time; time.sleep(900)'"
        # only its own session reached the test's hub: never the developer's (this one)
        assert [x.key for x in store.online()] == [key]
        _wait(lambda: (store.agent(key).tasks or [{}])[0].get("out", 1e18) < time.time() - 26,
              60, "the task's silence did not grow")
        stop.app.state.tick()
        with store.lock:
            sent = [r["text"] for r in store.db.execute(
                "SELECT text FROM messages WHERE sender_key=? AND to_key=?", (HUB_KEY, key))]
        assert len(sent) == 1 and sent[0].startswith("kaptyorka: your background task"), sent
        transcript = next((real_home / ".claude" / "projects").glob(
            f"*/{key.split(':', 1)[1]}.jsonl"))

        def answered():
            text = transcript.read_text()
            at = text.find("your background task")
            return at >= 0 and '"type":"assistant"' in text[at:].replace(" ", "")
        _wait(answered, 120, "the session did not take the message in a new turn")
        stop.app.state.tick()
        with store.lock:  # once per episode
            assert store.db.execute("SELECT COUNT(*) FROM messages WHERE sender_key=? AND"
                                    " to_key=?", (HUB_KEY, key)).fetchone()[0] == 1
    finally:
        bridge.stop.set()
        if thread is not None:
            thread.join(30)
        for target in {*([job] if job else []), *_jobs(repo)}:
            for step in ("stop", "rm"):
                _quiet(["claude", step, target])
        stop()
        store.close()
        remove_transcripts(scratch, real_home)
        shutil.rmtree(scratch, ignore_errors=True)


@pytest.mark.slow
def test_a_slow_test_never_messages_the_developer_s_own_sessions(tmp_path):
    """conftest's guard: a session outside a test's scratch folder is refused before any
    socket is touched."""
    home = tmp_path / "home"
    (home / ".claude" / "sessions").mkdir(parents=True)
    (home / ".claude" / "sessions" / f"{os.getpid()}.json").write_text(json.dumps(
        {"pid": os.getpid(), "sessionId": "s1", "cwd": "/home/u/projects", "status": "busy"}))
    with pytest.raises(DeliveryError, match="outside its scratch folder"):
        ClaudeAdapter(home=home, log=lambda *_: None).deliver("claude:s1", "hello")
