from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_readme_agent_steps():
    r = (ROOT / "README.md").read_text()
    assert "## For agents" in r
    assert "pipx install kaptyorka" in r and "kaptyorka join --hub" in r


def test_readme_names_every_tool_and_door():
    r = (ROOT / "README.md").read_text()
    for t in ["Claude Code", "OpenCode", "Codex", "Cursor"]:
        assert t in r
    assert "pull only" in r
    for tool in ["who", "send", "inbox", "status", "notes_list", "notes_read", "notes_write"]:
        assert f"`{tool}`" in r


def test_readme_tool_table_matches_the_hub(hub):
    import re

    from helpers import tool_names
    from kaptyorka.shim import FALLBACK_TOOLS

    r = (ROOT / "README.md").read_text()
    table = r.split("## What agents can do", 1)[1].split("\n\n", 2)[1]
    listed = re.findall(r"^\| `(\w+)` \|", table, re.M)
    assert len(listed) == len(set(listed))
    assert set(listed) == tool_names(hub) == set(FALLBACK_TOOLS)
