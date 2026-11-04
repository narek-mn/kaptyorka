"""Two real Claude Code sessions talk through a scratch hub. Run with: pytest -m slow.

Writes to the real ~/.claude/settings.json (a symlink into ~/claude-setup) and the user MCP
list for the length of the test; both are put back in `finally`. While it runs, every Claude
Code session on this machine runs the kaptyorka hooks on its next prompt; they stay silent,
since the real ~/.kaptyorka has no config.

Optional: KAPTYORKA_E2E_SCREENSHOT=<png> saves the page's Messages view taken mid-run.
"""
import contextlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

import pytest

PORT = 18765
HUB = f"http://127.0.0.1:{PORT}"
KAPTYORKA = str(Path(sys.executable).parent / "kaptyorka")
SETUP = Path.home() / "claude-setup"
PROMPT = ("Use the kaptyorka send tool to ask e2e/claude-code/backend to write PONG into "
          "reply.txt in its folder.")


def _git_status() -> str:
    if not (SETUP / ".git").exists():
        return ""
    return subprocess.run(["git", "-C", str(SETUP), "status", "--short"],
                          capture_output=True, text=True, check=True).stdout


def _feed(token: str) -> dict:
    req = urllib.request.Request(HUB + "/api/feed", headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read())


def _wait(what: str, seconds: float, check) -> None:
    deadline = time.monotonic() + seconds
    while True:
        with contextlib.suppress(Exception):
            if check():
                return
        assert time.monotonic() < deadline, f"{what} not within {seconds:.0f} s"
        time.sleep(1)


def _start_session(cwd: Path, name: str, prompt: str) -> str:
    r = subprocess.run(["claude", "--bg", "-n", name, "--model", "haiku",
                        "--permission-mode", "acceptEdits", "--allowedTools=mcp__kaptyorka",
                        prompt], cwd=cwd, capture_output=True, text=True, timeout=60)
    if "Workspace not trusted" in r.stdout + r.stderr:
        pytest.skip(f"Claude Code does not trust {cwd.parent.parent}; trust it once to run this")
    ids = re.findall(r"backgrounded · ([0-9a-f]+)", r.stdout)
    assert r.returncode == 0 and ids, r.stdout + r.stderr
    return ids[0]


def _stop(proc) -> None:
    if proc is not None and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


@pytest.mark.slow
@pytest.mark.skipif(shutil.which("claude") is None, reason="claude is not on PATH")
def test_two_sessions_talk():
    mcp = subprocess.run(["claude", "mcp", "get", "kaptyorka"], capture_output=True, text=True)
    if mcp.returncode == 0:
        pytest.skip("a kaptyorka MCP server is already registered; this test would remove it")
    settings = (Path.home() / ".claude" / "settings.json").resolve()
    saved = settings.read_bytes()
    git_before = _git_status()
    real_home = Path.home() / ".kaptyorka"
    real_home_existed = real_home.exists()
    # Claude Code refuses --bg in an untrusted folder, and /tmp is not trusted; a hidden folder
    # beside the repo inherits the trust of its parent. The session names come from the folders.
    root = Path(tempfile.mkdtemp(prefix=".kaptyorka-e2e-", dir=Path(__file__).resolve().parents[2]))
    murmur, backend = root / "murmur", root / "backend"
    murmur.mkdir()
    backend.mkdir()
    hub_env = {**os.environ, "KAPTYORKA_HOME": str(root / "hub")}
    pc_home = root / "pc"
    pc_env = {**os.environ, "KAPTYORKA_HOME": str(pc_home), "KAPTYORKA_SKIP_SERVICE": "1"}
    hub = bridge = None
    ids = []
    try:
        hub = subprocess.Popen([KAPTYORKA, "serve", "--foreground", "--port", str(PORT)],
                               env=hub_env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        token_file = root / "hub" / "hub-token"
        _wait("hub token", 20, token_file.exists)
        token = token_file.read_text().strip()
        _wait("hub answering", 20, lambda: _feed(token) is not None)

        r = subprocess.run([KAPTYORKA, "join", "--hub", HUB, "--token", token, "--name", "e2e",
                            "--only", "claude-code"], env=pc_env, capture_output=True, text=True,
                           timeout=60)
        assert r.returncode == 0, r.stdout + r.stderr
        # Background sessions are started by Claude Code's daemon, whose environment is its own,
        # so the shim learns the scratch home from its MCP entry, not from this process.
        subprocess.run(["claude", "mcp", "remove", "--scope", "user", "kaptyorka"],
                       capture_output=True, check=True)
        subprocess.run(["claude", "mcp", "add", "--scope", "user", "kaptyorka", "-e",
                        f"KAPTYORKA_HOME={pc_home}", "--", KAPTYORKA, "mcp"],
                       capture_output=True, check=True)
        bridge = subprocess.Popen([KAPTYORKA, "bridge"], env=pc_env,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        ids.append(_start_session(backend, "kaptyorka-e2e-backend",
                                  "Wait. When a kaptyorka message arrives, do what it says."))
        _wait("backend online", 60, lambda: any(
            a["name"] == "e2e/claude-code/backend" for a in _feed(token)["agents"]))
        ids.append(_start_session(murmur, "kaptyorka-e2e-murmur", PROMPT))

        reply = backend / "reply.txt"
        shot = os.environ.get("KAPTYORKA_E2E_SCREENSHOT")
        _wait("PONG in backend/reply.txt", 240,
              lambda: reply.exists() and "PONG" in reply.read_text())

        feed = _feed(token)
        names = [a["name"] for a in feed["agents"]]
        assert "e2e/claude-code/backend" in names, names
        assert any(n.startswith("e2e/claude-code/murmur") for n in names), names
        assert any(m["to"] == "e2e/claude-code/backend"
                   and m["sender"].startswith("e2e/claude-code/murmur")
                   for m in feed["messages"]), feed["messages"]
        if shot:
            subprocess.run(["google-chrome", "--headless=new", "--disable-gpu",
                            "--window-size=1440,900", f"--screenshot={shot}",
                            f"{HUB}/#t={token}&view=messages"], capture_output=True, timeout=60)
        log = (pc_home / "bridge.log")
        lines = log.read_text().splitlines() if log.exists() else []
        assert not [line for line in lines if "failed" in line], lines
    finally:
        for target in ids:
            for step in ("stop", "rm"):
                with contextlib.suppress(subprocess.TimeoutExpired):
                    subprocess.run(["claude", step, target], capture_output=True, timeout=60)
        _stop(bridge)
        _stop(hub)
        settings.write_bytes(saved)
        subprocess.run(["claude", "mcp", "remove", "--scope", "user", "kaptyorka"],
                       capture_output=True)
        # Other sessions' hooks may have made an empty ~/.kaptyorka while looking for a config.
        if not real_home_existed:
            with contextlib.suppress(OSError):
                real_home.rmdir()
        shutil.rmtree(root, ignore_errors=True)
    assert _git_status() == git_before
