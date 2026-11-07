"""Cursor: pull only. Its hooks fetch the brief; there is no way to push into a chat."""
import shlex
import shutil
from pathlib import Path
from typing import Callable, List, Optional

from kaptyorka.adapters.base import DeliveryError, Session, drop_hooks, is_our_hook, merge_json


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
            for event, name, extra in (("stop", "stop", {"loop_limit": 5}),
                                       ("postToolUse", "tool", {})):
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

        for name, update, what in (("mcp.json", add_mcp, "MCP server"),
                                   ("hooks.json", add_hooks, "hooks")):
            path = self.dir / name
            result = merge_json(path, update)
            if result == "broken":
                self.log(f"left alone, not valid JSON: {path}")
            elif result == "changed":
                lines.append(f"Cursor: kaptyorka {what} added to {path}")
        return lines

    def unconfigure(self) -> List[str]:
        lines = []

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
                                                              ("stop", "tool")), nested=False)
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
