"""The done check on a real Claude Code session. Run with: pytest -m slow.

A `claude --bg` session commits a file in a throwaway folder beside the repo; a real bridge
(real ClaudeAdapter, real git facts) tells an in-process hub; the session's Stop hook is
blocked once and it writes the project's state note. The hook and the MCP server come from
`--settings` and `--mcp-config` with a scratch KAPTYORKA_HOME, so neither the real
~/.claude/settings.json nor the real ~/.kaptyorka is touched. Everything stops in `finally`.

Claude Code refuses `--bg` in a folder with a `.git` of its own (a new, untrusted project root),
so the repository's git folder sits beside the work folder: the session names it with
`--git-dir`, and the bridge here finds it through GIT_DIR and GIT_WORK_TREE.
"""
import json
import re
import shutil
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from helpers import person
from kaptyorka import bridge as bridge_mod
from kaptyorka import facts
from kaptyorka import identity
from kaptyorka.adapters.claude import ClaudeAdapter
from kaptyorka.bridge import Bridge
from kaptyorka.memory import MemoryStore
from test_worker_slow import _jobs, _quiet, _scratch_folder, _wait

KAPTYORKA = str(Path(sys.executable).parent / "kaptyorka")
TASK = ("In this folder, create notes.txt containing the line hello. Then run exactly"
        " `{git} add notes.txt && {git} commit -m 'add notes'`, then `{git} log --oneline -1`."
        " Do not send any message. Then stop.")


def _git(folder: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(folder), *args], check=True, capture_output=True)


@pytest.mark.slow
@pytest.mark.skipif(shutil.which("claude") is None, reason="claude is not on PATH")
@pytest.mark.skipif(shutil.which("git") is None, reason="git is not on PATH")
def test_done_check_blocks_a_real_session_once(hub, tmp_path, monkeypatch):
    real_home = Path.home()  # the session shows up in the real ~/.claude/sessions
    khome = tmp_path / "kaptyorka"
    monkeypatch.setenv("KAPTYORKA_HOME", str(khome))  # never the real one
    monkeypatch.setattr(bridge_mod, "WAIT_S", 1)
    monkeypatch.setattr(bridge_mod, "HEARTBEAT_S", 1)
    monkeypatch.setattr(bridge_mod, "FACTS_S", 0)  # git facts on every heartbeat
    monkeypatch.setattr(facts, "GIT_TTL_S", 0)
    monkeypatch.setattr(identity, "facts",
                        lambda: {"host": "box-1", "os_user": "u", "version": "0.4.0"})
    scratch = _scratch_folder()
    repo = scratch / "proj-a"  # the session's project is its folder's name
    repo.mkdir()
    git_dir = scratch / "proj-a.git"
    monkeypatch.setenv("GIT_DIR", str(git_dir))  # for _git and the bridge's facts.git
    monkeypatch.setenv("GIT_WORK_TREE", str(repo))
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "alice@example.com")
    _git(repo, "config", "user.name", "alice")
    (repo / "README").write_text("proj-a\n")
    _git(repo, "add", "README")
    _git(repo, "commit", "-q", "-m", "start")

    hub.store.project_register("proj-a", by="test")
    pid, _ = person(hub.store, "alice")
    hub.store.grant_project("proj-a", pid, "write", "test")
    token, secret = hub.store.invite_redeem(hub.store.invite_create(pid), "box-1",
                                            host="box-1", os_user="u")
    cfg = {"hub": hub.url, "token": token, "secret": secret, "machine": "box-1"}
    khome.mkdir()
    (khome / "config.json").write_text(json.dumps(cfg))
    (khome / "folders.json").write_text(json.dumps({"proj-a": str(repo)}))
    # Background sessions run under Claude Code's daemon, whose environment is its own: the
    # scratch home goes into the hook command and the MCP entry themselves.
    settings = {"hooks": {"Stop": [{"hooks": [{
        "type": "command", "timeout": 10,
        "command": f"KAPTYORKA_HOME={khome} {KAPTYORKA} hook claude-code stop"}]}]}}
    mcp = {"mcpServers": {"kaptyorka": {"command": KAPTYORKA, "args": ["mcp"],
                                        "env": {"KAPTYORKA_HOME": str(khome)}}}}
    (tmp_path / "settings.json").write_text(json.dumps(settings))
    (tmp_path / "mcp.json").write_text(json.dumps(mcp))

    lines = []
    adapter = ClaudeAdapter(home=real_home, log=lines.append)
    bridge = Bridge(cfg, [adapter], lines.append)
    ms = MemoryStore(hub.store)
    principal = hub.store.authenticate(token)
    thread, job = None, None
    try:
        bridge.heartbeat()  # learns its projects
        thread = threading.Thread(target=bridge.run, daemon=True)
        thread.start()
        r = subprocess.run(
            ["claude", "--bg", "-n", "kaptyorka-memory-slow", "--model", "haiku",
             "--settings", str(tmp_path / "settings.json"),
             "--mcp-config", str(tmp_path / "mcp.json"), "--strict-mcp-config",
             "--permission-mode", "acceptEdits",
             "--allowedTools=Bash(git *),Write,mcp__kaptyorka",
             TASK.format(git=f"git --git-dir={git_dir} --work-tree=.")],
            cwd=repo, capture_output=True, text=True, timeout=60)
        if "not trusted" in (r.stdout + r.stderr):
            pytest.skip(f"Claude Code does not trust {repo}; trust {scratch.parent} once to run this")
        ids = re.findall(r"backgrounded · ([0-9a-f]+)", r.stdout)
        assert r.returncode == 0 and ids, r.stdout + r.stderr
        job = ids[0]

        session = _wait(lambda: next((s for s in adapter.sessions() if s.cwd == str(repo)), None),
                        60, "session not in ~/.claude/sessions")
        key = session.key

        def row():
            with hub.store.lock:
                return hub.store.db.execute("SELECT * FROM done_checks WHERE key=?",
                                            (key,)).fetchone()

        _wait(lambda: (row() or {"signal": None})["signal"] in ("commit", "clean"), 180,
              f"no commit seen for the session ({lines})")
        _wait(lambda: row()["blocked_at"] is not None, 120, "stop not blocked")
        note = _wait(lambda: hub.store.notes_read("state", principal, "proj-a"), 180,
                     "no state note written after the block")
        assert "Where it stands" in note.text, note.text
        _wait(lambda: next((s.activity != "busy" for s in adapter.sessions() if s.key == key), True),
              120, "session did not stop after its state write")
        # blocked once: the stored row says so, and a fresh look answers ok
        assert ms.done_check(key, principal, hub.store.clock(), commit=False) == {"decision": "ok"}
        assert "add notes" in subprocess.run(["git", "-C", str(repo), "log", "--oneline"],
                                             capture_output=True, text=True).stdout
        assert not (repo / ".git").exists()
        assert not [line for line in lines if "failed" in line], lines
    finally:
        bridge.stop.set()
        if thread is not None:
            thread.join(30)
        for target in {*([job] if job else []), *_jobs(repo)}:
            for step in ("stop", "rm"):
                _quiet(["claude", step, target])
        shutil.rmtree(scratch, ignore_errors=True)
        # the transcript folder Claude Code made for it: ~/.claude/projects/<path, / and . as ->
        tag = scratch.name.lstrip(".")
        for p in (real_home / ".claude" / "projects").glob(f"*{tag}*"):
            shutil.rmtree(p, ignore_errors=True)
