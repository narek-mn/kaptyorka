"""Cursor: pull only. Its hooks fetch the brief; there is no way to push into a chat."""
import shlex
import shutil
from pathlib import Path
from typing import Callable, List, Optional

from kaptyorka.adapters.base import DeliveryError, Session, drop_hooks, is_our_hook, merge_json, write_atomic

# (Cursor hook event, our command name, extra fields); beforeSubmitPrompt answers /hub.
# sessionStart's `additional_context` goes into a new chat's system context, from the user's
# own ~/.cursor/hooks.json too (cursor.com/docs/agent/hooks, read 2026-11-20): the hub guide.
HOOKS = (("stop", "stop", {"loop_limit": 5}), ("postToolUse", "tool", {}),
         ("beforeSubmitPrompt", "prompt", {}), ("sessionStart", "start", {}))
SKILL_MARK = "<!-- written by kaptyorka; it rewrites this file -->"
# Cursor runs a skill as /hub. The prompt hook answers /hub (on, off, team) before the
# agent sees them; the text below is only for when it did not.
SKILL = f"""\
---
name: hub
description: Show or switch whether this folder's Cursor chats are on the kaptyorka hub (/hub, /hub on, /hub off, /hub team [project|off]).
disable-model-invocation: true
---
{SKILL_MARK}
kaptyorka's prompt hook answers `/hub`, `/hub on`, `/hub off` and `/hub team` before you see them. You see
this because it did not run. Tell the user, word for word: "kaptyorka: /hub did not run; restart
Cursor so it loads the kaptyorka hooks, then try again." Run nothing yourself.
"""


class CursorAdapter:
    tool = "cursor"
    prefix = "cursor"
    restart_note = "Cursor: restart the app"

    def __init__(self, home: Optional[Path] = None, log: Callable[[str], None] = print):
        self.home = home or Path.home()
        self.dir = self.home / ".cursor"
        self.log = log

    def installed(self) -> bool:
        return self.dir.exists() or shutil.which("cursor") is not None

    def logged_in(self, run: Callable) -> Optional[bool]:
        return None  # no login status command is known

    def sessions(self) -> List[Session]:
        return []  # presence comes from the shim

    def deliver(self, key: str, text: str) -> None:
        raise DeliveryError("cursor: pull only")

    def configure(self, kaptyorka: str) -> List[str]:
        return self._wire(kaptyorka, mcp=True)

    def install_hub(self, kaptyorka: str) -> List[str]:
        """/hub: the skill and the prompt hook that answers it. Also run at bridge start, so an
        update brings them."""
        return self._wire(kaptyorka, mcp=False)

    def _wire(self, kaptyorka: str, mcp: bool) -> List[str]:
        lines = []
        entry = {"type": "stdio", "command": kaptyorka, "args": ["mcp"]}

        def add_mcp(data: dict) -> bool:
            servers = data.setdefault("mcpServers", {})
            if servers.get("kaptyorka") == entry:
                return False
            servers["kaptyorka"] = entry
            return True

        def add_hooks(data: dict) -> bool:
            changed = data.get("version") != 1
            data["version"] = 1
            hooks = data.setdefault("hooks", {})
            for event, name, extra in HOOKS:
                entry = {"command": f"{shlex.quote(kaptyorka)} hook cursor {name}", **extra}
                entries = hooks.setdefault(event, [])
                ours = [i for i, e in enumerate(entries) if str(e.get("command", ""))
                        .endswith(f" hook cursor {name}")]
                if [entries[i] for i in ours] == [entry]:
                    continue
                for i in reversed(ours):  # an older install path: replace, never add a second
                    del entries[i]
                entries.insert(ours[0] if ours else len(entries), entry)
                changed = True
            return changed

        steps = ((("mcp.json", add_mcp, "MCP server"),) if mcp else ()) + (
            ("hooks.json", add_hooks, "hooks"),)
        for name, update, what in steps:
            path = self.dir / name
            result = merge_json(path, update)
            if result == "broken":
                self.log(f"left alone, not valid JSON: {path}")
            elif result == "changed":
                lines.append(f"Cursor: kaptyorka {what} added to {path}")
        return lines + self._write_skill()

    def _write_skill(self) -> List[str]:
        f = self.dir / "skills" / "hub" / "SKILL.md"
        try:
            old = f.read_text() if f.exists() else None
            if old == SKILL:
                return []
            if old is not None and SKILL_MARK not in old:
                self.log(f"left alone, not written by kaptyorka: {f}")
                return []
            f.parent.mkdir(parents=True, exist_ok=True)
            write_atomic(f, SKILL)
        except OSError as e:
            self.log(f"could not write {f}: {e}")
            return []
        return [f"Cursor: /hub skill written to {f}"]

    def unconfigure(self) -> List[str]:
        lines = []
        skill = self.dir / "skills" / "hub" / "SKILL.md"
        try:
            if SKILL_MARK in skill.read_text():
                skill.unlink()
                skill.parent.rmdir()
                lines.append(f"Cursor: /hub skill removed ({skill})")
        except OSError:
            pass

        def drop_mcp(data: dict) -> bool:
            servers = data.get("mcpServers")
            if not isinstance(servers, dict) or "kaptyorka" not in servers:
                return False
            del servers["kaptyorka"]
            if not servers:
                del data["mcpServers"]
            return True

        def drop_cmds(data: dict) -> bool:
            hooks = data.get("hooks")
            if not isinstance(hooks, dict):
                return False
            changed = drop_hooks(hooks, lambda e: is_our_hook(e.get("command", ""), "cursor",
                                                              ("stop", "tool", "prompt", "start")),
                                    nested=False)
            if changed and not hooks:
                del data["hooks"]
            return changed

        for name, update, what in (("mcp.json", drop_mcp, "MCP server"),
                                   ("hooks.json", drop_cmds, "hooks")):
            path = self.dir / name
            result = merge_json(path, update)
            if result == "broken":
                self.log(f"left alone, not valid JSON: {path}")
            elif result == "changed":
                lines.append(f"Cursor: kaptyorka {what} removed from {path}")
        return lines
