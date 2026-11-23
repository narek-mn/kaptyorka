"""One real manager session: the hub starts it through the admin's own bridge with a real
interactive `claude` in tmux, and it signs in as person `manager`. Run with: pytest -m slow.
Everything started here is stopped in `finally`."""
import contextlib
import json
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import pytest

import kaptyorka
from conftest import start_hub
from helpers import call, person, run
from kaptyorka import bridge as bridge_mod
from kaptyorka import identity, manager, rules
from kaptyorka.adapters.claude import ClaudeAdapter
from kaptyorka.bridge import Bridge
from kaptyorka.launch import Launcher
from kaptyorka.manager import ManagerStore
from kaptyorka.rules import RulesStore
from kaptyorka.store import Store
from kaptyorka.workers import WorkerStore

REPO = Path(__file__).resolve().parents[1]
RULES = "Be kind to each other."


def _quiet(argv, timeout=60):
    with contextlib.suppress(OSError, subprocess.TimeoutExpired):
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _jobs(folder: Path) -> list:
    r = _quiet(["claude", "agents", "--json"])
    try:
        return [a["id"] for a in json.loads(r.stdout) if a.get("cwd") == str(folder) and a.get("id")]
    except (AttributeError, ValueError, TypeError, KeyError):
        return []


def _brief(url, headers):
    req = urllib.request.Request(url + "/api/brief?event=start", headers=headers)
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.status, r.read().decode()


@pytest.mark.slow
@pytest.mark.skipif(shutil.which("claude") is None, reason="claude is not on PATH")
def test_real_manager_session(monkeypatch):
    real_home = Path.home()
    # An interactive session (the manager runs in tmux) inside a git repo asks for the repo
    # root's trust, which a parent folder's trust does not give; a throwaway folder beside
    # the repo, outside any repo, inherits the trust of its parent, as the hub machine's
    # ~/.kaptyorka/manager does.
    khome = Path(tempfile.mkdtemp(prefix=".kaptyorka-manager-slow-", dir=REPO.parent))
    monkeypatch.setenv("KAPTYORKA_HOME", str(khome))
    monkeypatch.setenv("ANTHROPIC_MODEL", "haiku")
    monkeypatch.setattr(bridge_mod, "WAIT_S", 2)
    monkeypatch.setattr(bridge_mod, "HEARTBEAT_S", 2)
    monkeypatch.setattr(identity, "facts", lambda: {"host": "box-1", "os_user": "u",
                                                    "version": kaptyorka.__version__})
    folder = khome / manager.DIR
    config = khome / manager.CONFIG
    store = Store(khome / "hub.db", legacy_token="T")
    mm = ManagerStore(store)
    port = _free_port()
    stop = start_hub(store, port=port, manager_url=f"http://127.0.0.1:{port}")
    url = f"http://127.0.0.1:{port}"
    bridge, thread = None, None
    try:
        RulesStore(store).set_rules(RULES, "test")
        aid, _ = person(store, "root", "admin")
        # like the real hub: the manager runs through the bridge of the hub key's person, a
        # team machine, not an admin (0.9.28 dropped that bridge's reports of the manager)
        sid, _ = person(store, "svc", "member")
        token, secret = store.invite_redeem(store.invite_create(sid), "box-1", host="box-1",
                                            os_user="u")
        with store.lock:
            store.db.execute("UPDATE people SET team=1 WHERE id=?", (sid,))
        (khome / "admin-token").write_text(token + "\n")  # the hub key
        cfg = {"hub": url, "token": token, "secret": secret, "machine": "box-1"}
        lines = []
        adapter = ClaudeAdapter(home=real_home, log=lines.append)
        bridge = Bridge(cfg, [adapter], lines.append,
                        launcher=Launcher(cfg, real_home, log=lines.append))
        bridge.heartbeat()  # the admin's bridge is online; the hub's tick launches the manager
        thread = threading.Thread(target=bridge.run, daemon=True)
        thread.start()

        deadline = time.monotonic() + 90
        agent, w = None, None
        while time.monotonic() < deadline:
            mpid = mm.person_id()
            agent = next((a for a in store.online() if mpid and a.person_id == mpid), None)
            w = WorkerStore(store).by_name("manager")
            if w and w["state"] == "failed":
                break
            if agent is not None and w and w["state"] == "up":
                break
            if bridge.launcher.trust_prompt({"worker": "manager", "manager": True}):
                pytest.xfail(f"Claude Code asks to trust {folder}: trust {REPO.parent} first")
            time.sleep(1)
        log = khome / "workers" / "manager.log"
        err = (w or {}).get("error") or ""
        if "trust" in err.lower():
            print("LAUNCH ERROR:", err)
            pytest.xfail(f"Workspace not trusted: {err}")
        assert w is not None and w["state"] == "up", (w, lines, log.read_text() if log.exists() else "")
        assert agent is not None, (lines, log.read_text() if log.exists() else "")

        pid = mm.person_id()
        assert any(p["name"] == "manager" and p["role"] == "manager" for p in store.people())
        assert agent.person_id == pid
        assert (folder / "CLAUDE.md").exists()
        assert RULES in (folder / "CLAUDE.md").read_text()
        assert config.exists() and (config.stat().st_mode & 0o777) == 0o600
        mcfg = json.loads(config.read_text())
        assert mcfg["token"] and mcfg["person"] == "manager"

        ident = {"key": agent.key, "machine": agent.machine, "tool": agent.tool,
                 "project": agent.project}
        headers = {"Authorization": f"Bearer {mcfg['token']}"}
        headers.update({f"X-Kaptyorka-{k.capitalize()}": v for k, v in ident.items()})
        status, body = _brief(url, headers)
        assert status == 200 and rules.HEADING in body, (status, body)

        rid = mm.request_add(aid, "k:boss", "proj-a", "do it", "claude-code", None, "box-1",
                             "alice", "/w/proj-a")
        answer = run(call(SimpleNamespace(url=url, token=mcfg["token"]), ident,
                          "work_decide", request_id=rid, machine="wait", reason="slow test"))
        assert "wait" in answer, answer
        assert mm.request(rid)["decision"] == "wait"

        # past the hub's gone window (about 90 s), with heartbeats every 2 s: still up, still
        # reported, never judged crashed
        start = time.monotonic()
        while time.monotonic() - start < 120:
            time.sleep(5)
            w = WorkerStore(store).by_name("manager")
            a = store.agent(agent.key)
            assert w["state"] == "up" and w["key"] == agent.key, (w, lines)
            assert store.clock() - a.last_seen < 30, (a, lines)
        assert mm.crashes() == []
    finally:
        if bridge is not None:
            bridge.stop.set()
        if thread is not None:
            thread.join(30)
        for job in _jobs(folder):
            for step in ("stop", "rm"):
                _quiet(["claude", step, job])
        # never stop the scope: it can hold Claude Code's shared supervisor or the tmux server
        _quiet(["tmux", "kill-session", "-t", "=kaptyorka-manager"], 30)
        stop()
        store.close()
        shutil.rmtree(khome, ignore_errors=True)
        for p in (real_home / ".claude" / "projects").glob(f"*{khome.name}*"):
            shutil.rmtree(p, ignore_errors=True)
