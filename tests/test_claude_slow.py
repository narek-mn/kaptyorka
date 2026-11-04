"""Pushes into a real background Claude Code session. Run with: pytest -m slow."""
import json
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import pytest

from kaptyorka.adapters.claude import ClaudeAdapter

NAME = "kaptyorka-slow"


def _session_for(cwd: Path):
    for s in ClaudeAdapter().sessions():
        f = Path.home() / ".claude" / "sessions" / (s.key.split(":", 1)[1] + ".json")
        try:
            if json.loads(f.read_text())["cwd"] == str(cwd):
                return s
        except (OSError, ValueError, KeyError):
            pass
    return None


@pytest.mark.slow
@pytest.mark.skipif(shutil.which("claude") is None, reason="claude is not on PATH")
def test_real_session_receives_push():
    # Claude Code refuses --bg in an untrusted folder, and /tmp is not trusted; a throwaway
    # folder beside the repo inherits the trust of its parent (never the repo itself).
    project = Path(tempfile.mkdtemp(prefix=".kaptyorka-slow-",
                                    dir=Path(__file__).resolve().parents[2]))
    r = subprocess.run(["claude", "--bg", "-n", NAME, "--model", "haiku",
                        "--permission-mode", "acceptEdits",
                        "Wait. When a message arrives, do what it says."],
                       cwd=project, capture_output=True, text=True, timeout=60)
    ids = re.findall(r"backgrounded · ([0-9a-f]+)", r.stdout)
    try:
        if "Workspace not trusted" in r.stdout + r.stderr:
            pytest.skip(f"Claude Code does not trust {project.parent}; trust it once to run this")
        assert r.returncode == 0, r.stdout + r.stderr
        deadline = time.monotonic() + 60
        while (session := _session_for(project)) is None:
            assert time.monotonic() < deadline, "no session file within 60 s"
            time.sleep(1)
        ClaudeAdapter().deliver(session.key, "Kaptyorka message from test/x/y:\n\n"
                                "Write the text KAPTYORKA-OK into received.txt in this folder.")
        received = project / "received.txt"
        deadline = time.monotonic() + 180
        while not (received.exists() and "KAPTYORKA-OK" in received.read_text()):
            assert time.monotonic() < deadline, "received.txt not written within 180 s"
            time.sleep(2)
    finally:
        for target in ids[:1] or [NAME]:
            subprocess.run(["claude", "stop", target], capture_output=True, timeout=60)
            subprocess.run(["claude", "rm", target], capture_output=True, timeout=60)
        shutil.rmtree(project)
