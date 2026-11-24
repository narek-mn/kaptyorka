"""Every agent-tool internal kaptyorka depends on (compat.DEPS), probed on the real install.
Run with: pytest -m slow tests/test_claude_compat_slow.py (about 2 minutes, real tokens).

A `claude --bg` session started the way a worker starts, in a scratch folder beside the repo:
it starts a silent background task, gets a push, is stopped twice and removed. Every Claude
Code probe runs against it, the cheap OpenCode and Codex probes against what runs here. A
failure names each broken dependency exactly. When it passes on a Claude Code newer than
CLAUDE_CODE_TESTED, that constant may move to the version it prints (CLAUDE.md)."""
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from helpers import allowed_tools, remove_transcripts, scratch_dir
from kaptyorka import compat
from kaptyorka.adapters.claude import CLAUDE_CODE_TESTED


@pytest.mark.slow
@pytest.mark.skipif(shutil.which("claude") is None, reason="claude is not on PATH")
@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="task detection reads /proc")
def test_every_claude_code_internal_still_works(capsys):
    home = Path.home()
    scratch = scratch_dir(".kaptyorka-compat-")
    try:
        res = compat.full(scratch, home, subprocess.run,
                          extra=["--model", "haiku", "--strict-mcp-config", "--permission-mode",
                                 "acceptEdits", allowed_tools("Bash")], log=lambda *_: None)
        if "not trusted" in res["claude.bg_launch"][1]:
            pytest.skip(f"Claude Code does not trust {scratch}; trust {scratch.parent} once: {res['claude.bg_launch'][1]}")
        others = [t for t, exe in (("opencode", "opencode"), ("codex", "codex")) if shutil.which(exe)]
        res.update(compat.cheap(others, compat.Ctx(home, subprocess.run)))
        version = subprocess.run(["claude", "--version"], capture_output=True, text=True).stdout.strip()
        with capsys.disabled():
            print(f"\n{version}: " + ", ".join(f"{i} {s}" for i, (s, _) in sorted(res.items())))
        broken = compat.report(res)
        assert not broken, f"on {version}:\n" + "\n".join(broken)
        # what must be checked for real here, never left unknown
        must = [d.id for d in compat.DEPS if d.tool == "claude-code" and d.probed  # no evidence without a
                and d.id not in ("claude.title", "claude.derived_from", "claude.child_notes")]  # titled or parent session
        unknown = [i for i in must if res[i][0] != compat.OK]
        assert not unknown, f"not checked on {version}: " + ", ".join(f"{i} ({res[i][1]})" for i in unknown)
        m = re.match(r"\d+\.\d+\.\d+", version)
        with capsys.disabled():
            print(f"passed on Claude Code {m.group(0) if m else version}; CLAUDE_CODE_TESTED is {CLAUDE_CODE_TESTED}")
    finally:
        remove_transcripts(scratch, home)
        shutil.rmtree(scratch, ignore_errors=True)
