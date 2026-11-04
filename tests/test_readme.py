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
