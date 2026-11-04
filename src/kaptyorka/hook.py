"""`kaptyorka hook <tool> <event>`: print the hub's brief for this session. Silent on any failure."""
import json
import os
from typing import Callable, Dict

from kaptyorka import http, paths
from kaptyorka.identity import Identity, identify

TIMEOUT_S = 3

# (event, brief) -> what the tool expects on stdout
FORMATTERS: Dict[str, Callable[[str, str], str]] = {
    "claude-code": lambda event, brief: brief,
    "opencode": lambda event, brief: brief,  # the plugin pushes it as a system line
    "cursor": lambda event, brief: json.dumps(
        {} if not brief else {"followup_message" if event == "stop" else "additional_context": brief}),
    "codex": lambda event, brief: json.dumps({"hookSpecificOutput": {
        "hookEventName": "SessionStart" if event == "start" else "UserPromptSubmit",
        "additionalContext": brief}}),
}


def main(tool: str, event: str, stdin: str = "") -> int:
    out = _run(tool, event, stdin)
    if not out and tool == "cursor":
        out = "{}"  # Cursor may treat an empty stdout as a failed hook
    if out:
        print(out)
    return 0


def _run(tool: str, event: str, stdin: str) -> str:
    try:
        cfg = paths.load_config()
        fmt = FORMATTERS.get(tool)
        if not fmt or not cfg.get("hub") or not cfg.get("token"):
            return ""
        workspace = None
        if tool == "cursor":
            workspace = (json.loads(stdin or "{}").get("workspace_roots") or [None])[0]
        data = json.loads(stdin or "{}") if tool == "codex" else {}
        if data.get("session_id"):  # Codex names the thread itself: no socket call needed
            cwd = data.get("cwd") or os.getcwd()
            ident = Identity(f"codex:{data['session_id']}", cfg.get("machine", ""), "codex",
                             os.path.basename(cwd.rstrip("/")))
        else:
            ident = identify(cfg.get("machine", ""), workspace=workspace)
        if ident is None:
            return ""
        status, brief = http.request("GET", f"{cfg['hub']}/api/brief?event={event}", cfg["token"],
                                     headers=ident.headers(), timeout=TIMEOUT_S)
        if status == 200 and isinstance(brief, str) and brief:
            return fmt(event, brief)
    except Exception:
        pass  # a hook must never disturb the tool
    return ""
