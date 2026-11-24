"""What left.py reads from Claude Code, which documents none of it: a child's `derived-from`
line (via "subprocess" for `claude -p`, "launch" for `claude --bg`), the `sdk-cli` entrypoint,
the child notes a session keeps of what it started, and their folder in its Stop hook's
environment. A Claude Code update that drops one fails here, not silently in the product.
Run with: pytest -m slow."""
import contextlib
import json
import os
import re
import shutil
import subprocess
import time

import pytest

from helpers import remove_transcripts, scratch_dir, transcript_dir
from kaptyorka.adapters import claude as claude_mod

pytestmark = [pytest.mark.slow,
              pytest.mark.skipif(shutil.which("claude") is None, reason="claude is not on PATH")]

CHILDREN = "claude -p 'say ok' --model haiku --max-turns 1 ; claude --bg 'say ok' --model haiku"


def top_level() -> dict:
    """This test's own environment without the session it may run in: a session of its own."""
    return {k: v for k, v in os.environ.items()
            if not k.startswith("CLAUDE_CODE_") and k not in ("CLAUDECODE", "CLAUDE_PID")}


def test_children_name_their_parent_and_its_stop_hook_has_the_notes_dir():
    project = scratch_dir(".kaptyorka-left-")
    dump = project / "stop_env.txt"
    settings = project / "settings.json"
    settings.write_text(json.dumps({"hooks": {"Stop": [{"hooks": [{"type": "command", "command":
        f"env | grep -E '^CLAUDE_CODE_(CHILD_NOTES_DIR|SESSION_ID)=' > {dump}"}]}]}}))
    bg = []
    try:
        r = subprocess.run(["claude", "-p", f"Run exactly this one Bash command, then say done: {CHILDREN}",
                            "--model", "haiku", "--allowedTools", "Bash(claude *)",
                            "--settings", str(settings)], cwd=project, env=top_level(),
                           capture_output=True, text=True, timeout=300)
        if "Workspace not trusted" in r.stdout + r.stderr:
            pytest.skip(f"Claude Code does not trust {project.parent}; trust it once to run this")
        assert r.returncode == 0, r.stdout + r.stderr
        seen = dict(line.split("=", 1) for line in dump.read_text().split())
        parent, notes = seen["CLAUDE_CODE_SESSION_ID"], seen["CLAUDE_CODE_CHILD_NOTES_DIR"]
        assert notes.endswith(f"/{parent}/started")
        deadline = time.monotonic() + 90
        while True:  # the --bg child's note and transcript come once it has started
            kids = {n.split(".", 1)[0] for n in os.listdir(notes)}
            got = {k: claude_mod.lineage(transcript_dir(project) / f"{k}.jsonl", k) for k in kids}
            if len(kids) == 2 and all(g[0] for g in got.values()):
                break
            assert time.monotonic() < deadline, f"no derived-from in 90 s: {got}"
            time.sleep(2)
        # a --bg child says entrypoint sdk-cli too: the adapter tells them apart by kind "bg"
        assert sorted((p, via) for p, _, via in got.values()) == [(parent, "launch"), (parent, "subprocess")]
        assert [o for _, o, via in got.values() if via == "subprocess"] == [True]
        bg = [k for k, g in got.items() if g[2] == "launch"]
        assert claude_mod.notes_parent(bg[0]) == parent
    finally:
        for f in transcript_dir(project).glob("*.jsonl"):  # whatever --bg it started
            with contextlib.suppress(OSError):
                bg += re.findall(r"backgrounded \u00b7 ([0-9a-f]{8})", f.read_text())
        for target in set(b[:8] for b in bg):
            for step in ("stop", "rm"):
                with contextlib.suppress(subprocess.TimeoutExpired):
                    subprocess.run(["claude", step, target], capture_output=True, timeout=60)
        remove_transcripts(project)
        shutil.rmtree(project, ignore_errors=True)
        with contextlib.suppress(Exception):  # the parent's temp folder, then its project's if empty
            session_tmp = re.sub(r"/started/?$", "", seen["CLAUDE_CODE_CHILD_NOTES_DIR"])
            shutil.rmtree(session_tmp)
            os.rmdir(os.path.dirname(session_tmp))
