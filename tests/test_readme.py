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


def test_readme_done_check_and_brief_cache_wording():
    r = (ROOT / "README.md").read_text()
    done = r.split("## The done check", 1)[1].split("\n## ", 1)[0]
    assert "is never asked" not in done
    assert "Git cannot tell who made the commit" in done and 'status("done: no state change")' in done
    assert "read the folder's git themselves" in done
    brief = r.split("## What a session knows on start", 1)[1].split("\n## ", 1)[0]
    assert "keeps each session's brief for 10 seconds" in brief and "never keeps a brief that delivered messages" in brief
    assert "count of unread messages has changed" in brief
    assert "makes `backup-pass` right away" in r


def test_readme_manager_section():
    r = (ROOT / "README.md").read_text()
    m = r.split("## The manager", 1)[1].split("\n## ", 1)[0]
    for s in ["claude attach manager", "--no-manager", "work_decide", "~/.kaptyorka/manager/", "manager-config.json",
              "cannot answer other people's knocks", "It messages, knocks and files proposals",
              "signs in as the manager, so exit it", "`kaptyorka start --manager`",
              "`--settings ~/.kaptyorka/manager/settings.json`",
              "`--strict-mcp-config --mcp-config ~/.kaptyorka/manager/mcp.json`", "`--restricted`",
              "`--tools=ToolSearch`", "`--append-system-prompt-file ~/.kaptyorka/manager/CLAUDE.md`",
              "do not skip this one-time trust step",
              "closes a manager session still running",
              "an offline bridge and a day-old proposal or request are sent once a day",
              "the failed-twice note at most once an hour", "the review once a week"]:
        assert s in m, s
    assert "Each item is sent once" not in m and "It can only knock" not in m
    assert "The hub offers 25 tools" in r
    assert "add `team-*/` to that repo's `.gitignore`." in r


def test_readme_people_and_access_in_page_order_and_commands_listed():
    r = (ROOT / "README.md").read_text()
    people = r.split("## People and access", 1)[1].split("\n## ", 1)[0]
    steps = ["### 1. Add a person", "### 2. They join", "### 3. Give access", "### 4. They ask for more",
             "### 5. Change a role", "### 6. Remove a person", "### 7. Projects"]
    at = [people.index(s) for s in steps]
    assert at == sorted(at)
    for word in ["My access", "Ask for", "request_access", "Register again", "Archived", "7 days", "(removed)"]:
        assert word in people
    commands = r.split("## Commands", 1)[1].split("\n## ", 1)[0]
    for c in ["kaptyorka invites", "kaptyorka people role", "kaptyorka people remove",
              "kaptyorka project edit", "kaptyorka project remove"]:
        assert c in commands and c in people
    assert "![" not in people


def test_readme_says_logins_and_why_the_manager_is_not_running():
    r = (ROOT / "README.md").read_text()
    assert ("The Machines tab shows which agent tools are installed and logged in on each machine,"
            " as the tools report it; a tool that is not logged in cannot run workers there.") in r
    mgr = r.split("## The manager", 1)[1].split("\n## ", 1)[0]
    assert "When it is not running, the Settings tab says why" in mgr
