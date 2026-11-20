"""The hub guide a session hears at start, so the agent can suggest /hub team when a request
needs the team. Built on this machine from the session's own state alone (off, joined, the
folder's project, the person's team projects as the bridge last saved them): no hub call, and
nothing about the folder or the session goes up. CAP bounds every text."""
from typing import List, Optional

from kaptyorka import paths

CAP = 500  # characters per session start, on top of nothing (personal, off) or the hub's brief
HEAD = "kaptyorka hub: "


def _command(key: str) -> str:
    from kaptyorka import optin

    return optin.command(key)


def personal(key: str, mine: Optional[List[str]]) -> str:
    """A folder that is no team project: when to suggest /hub team, and that only the person
    switches. mine: their team projects (None when unknown)."""
    hub = _command(key)
    what = "folder" if key.startswith("cursor:") else "session"

    def text(have: str) -> str:
        return (f"{HEAD}this folder is personal; nothing from it goes to the hub. Only when the"
                f" user's request touches a team project, a teammate, a team machine or the hub,"
                f" suggest once (not every turn) {hub} team <project> for one project{have}, or"
                f" {hub} team to coordinate across projects. Never switch this {what} yourself;"
                f" call join_team only after they say yes. Say nothing of this for personal work.")

    if mine is None:
        return text("")
    if not mine:
        return text(" (they have no team project role yet; the admin gives roles)")
    names = list(mine)
    while names:
        more = ", ..." if len(names) < len(mine) else ""
        out = text(f" (theirs: {', '.join(names)}{more})")
        if len(out) <= CAP:
            return out
        names.pop()
    return text("")


def off(key: str) -> str:
    what = "folder" if key.startswith("cursor:") else "session"
    return f"{HEAD}this {what} is off the hub; {_command(key)} on brings it back."


def on_hub(key: str, project: str, joined: bool) -> str:
    """The one local line added to the hub's start brief."""
    hub = _command(key)
    if project == paths.COORDINATOR:
        return (f"{HEAD}this session is a coordinator (no project): name a project for notes"
                f" and work. {hub} team off leaves the team.")
    if joined:
        return f"{HEAD}for private work, suggest {hub} team off: it leaves the team."
    what = "folder" if key.startswith("cursor:") else "session"
    return (f"{HEAD}for private work here, suggest {hub} off: then nothing from this {what}"
            f" goes to the hub ({hub} on brings it back).")
