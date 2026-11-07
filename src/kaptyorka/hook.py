"""`kaptyorka hook <tool> <event>`: print the hub's brief for this session. Silent on any failure."""
import json
import os
import subprocess
import time
from typing import Callable, Dict
from urllib.parse import urlencode

from kaptyorka import facts, http, paths
from kaptyorka.identity import Identity, identify

TIMEOUT_S = 3
GIT_S = 1  # the stop hook's own git read, inside TIMEOUT_S

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


def main(tool: str, event: str, stdin: str = "", run=subprocess.run) -> int:
    out = _run(tool, event, stdin, run)
    if not out and (tool == "cursor" or (tool == "codex" and event == "stop")):
        out = "{}"  # Cursor may treat an empty stdout as a failed hook; Codex needs JSON on exit 0
    if out:
        print(out)
    return 0


def _git_query(cwd: str, run, deadline: float) -> str:
    """The folder's git state read now, as done-check query parameters ("" when none): the
    bridge reports git once a minute, so a commit made just before the stop would be missed."""
    def quiet(args, **kw):  # never take git's optional locks: a hook must not slow the repo
        return run(args, env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"}, **kw)

    budget = min(GIT_S, deadline - time.monotonic() - 0.5)  # leave time for the request
    g = facts.git(cwd, quiet, cached=False, budget=budget) if budget > 0 else None
    if not g:
        return ""
    q = {"cwd": cwd, "branch": g["branch"], "subject": g["subject"]}
    if g.get("dirty") is not None:
        q["dirty"] = "1" if g["dirty"] else "0"
    return "?" + urlencode(q)


def _run(tool: str, event: str, stdin: str, run=subprocess.run) -> str:
    try:
        try:
            data = json.loads(stdin or "{}")
        except ValueError:
            data = {}
        if not isinstance(data, dict):
            data = {}
        here = data.get("cwd") if isinstance(data.get("cwd"), str) else None
        cfg = paths.load_config(here or os.getcwd())  # the manager's folder: its own config
        fmt = FORMATTERS.get(tool)
        if not fmt or not cfg.get("hub") or not cfg.get("token"):
            return ""
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

        deadline = time.monotonic() + TIMEOUT_S  # one budget for all calls of this hook

        def get(path: str):
            left = max(deadline - time.monotonic(), 0.1)
            return http.request("GET", f"{cfg['hub']}{path}", cfg["token"],
                                headers=ident.headers(), timeout=left)

        reason = ""
        if stop:
            query = ""
            if tool in ("claude-code", "codex"):
                query = _git_query(data.get("cwd") or os.getcwd(), run, deadline)
            status, check = get("/api/done-check" + query)
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
