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
        try:
            data = json.loads(stdin or "{}")
        except ValueError:
            data = {}
        if not isinstance(data, dict):
            data = {}
        stop = event == "stop"
        if stop and data.get("stop_hook_active") is True and tool in ("claude-code", "codex"):
            return "{}" if tool == "codex" else ""  # never block twice in one stop sequence
        workspace = None
        if tool == "cursor":
            workspace = (data.get("workspace_roots") or [None])[0]
        prefix = {"codex": "codex", "claude-code": "claude"}.get(tool)
        if data.get("session_id") and prefix and (tool == "codex" or stop):
            # the tool names the session itself: no session-file lookup needed
            cwd = data.get("cwd") or os.getcwd()
            ident = Identity(f"{prefix}:{data['session_id']}", cfg.get("machine", ""), tool,
                             os.path.basename(cwd.rstrip("/")))
        else:
            ident = identify(cfg.get("machine", ""), workspace=workspace)
        if ident is None:
            return ""

        def get(path: str):
            return http.request("GET", f"{cfg['hub']}{path}", cfg["token"],
                                headers=ident.headers(), timeout=TIMEOUT_S)

        reason = ""
        if stop:
            status, check = get("/api/done-check")
            if status == 200 and isinstance(check, dict) and check.get("decision") == "block":
                reason = str(check.get("reason") or "")
            if tool in ("claude-code", "codex"):
                if reason:
                    return json.dumps({"decision": "block", "reason": reason})
                return "{}" if tool == "codex" else ""
        status, brief = get(f"/api/brief?event={event}")
        if status != 200 or not isinstance(brief, str):
            brief = ""
        brief = "\n\n".join(p for p in (reason, brief) if p)
        if brief:
            return fmt(event, brief)
    except Exception:
        pass  # a hook must never disturb the tool
    return ""
