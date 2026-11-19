"""`kaptyorka hook <tool> <event>`: print the hub's brief for this session. Silent on any failure."""
import json
import os
import re
import shutil
import subprocess
import sys
import time
from typing import Callable, Dict, List, Optional
from urllib.parse import urlencode

from kaptyorka import facts, http, offhub, optin, paths
from kaptyorka.identity import Identity, identify, project_of

TIMEOUT_S = 3
GIT_S = 1  # the stop hook's own git read, inside TIMEOUT_S
HUB_TEAM_S = 8  # `hub team ...` asks the hub to check the role (optin.JOIN_S) before it saves

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


HUB_PROMPT = re.compile(r"\s*[/$]hub(?:\s+(.*?))?\s*")


def hub_words(prompt) -> Optional[List[str]]:
    """The words after /hub (or $hub, as Codex runs skills) when the prompt is that command
    alone; None for any other prompt."""
    m = HUB_PROMPT.fullmatch(prompt) if isinstance(prompt, str) else None
    return None if m is None else (m.group(1) or "").split()


def _exe() -> str:
    me = sys.argv[0] if sys.argv else ""
    return me if os.path.basename(me) == "kaptyorka" else shutil.which("kaptyorka") or "kaptyorka"


def hub_timeout(words: List[str]) -> float:
    """How long `kaptyorka hub <words>` may take: a join waits for the hub, on/off/status not."""
    return HUB_TEAM_S if words and words[0].lower() == "team" else TIMEOUT_S


def _hub_line(ident: Identity, words: List[str], run) -> str:
    """What `kaptyorka hub <words>` prints for this session: the same command /hub runs in
    Claude Code and OpenCode, so every tool answers alike. "" when it did not run."""
    env = {k: v for k, v in os.environ.items()
           if k not in ("CLAUDE_CODE_SESSION_ID", offhub.OPENCODE_SESSION)}  # this key decides
    try:
        r = run([_exe(), "hub", *words], capture_output=True, text=True, timeout=hub_timeout(words),
                cwd=ident.cwd or None, env={**env, offhub.SESSION_KEY: ident.key})
    except Exception:
        return ""
    return (r.stdout or "").strip()


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
        # the manager's folder (the input's or the hook's own): its own config, never the admin's
        cfg = paths.load_config(next((c for c in (here, os.getcwd())
                                      if paths.in_manager_folder(c)), None))
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
                             project_of(cwd), cwd)
            ident = optin.apply(ident)
        elif tool == "cursor" and event == "prompt" and isinstance(workspace, str) and workspace:
            # the same key identify() gives Cursor: its folder names the session
            ident = optin.apply(Identity(f"cursor:{cfg.get('machine', '')}:{workspace}",
                                         cfg.get("machine", ""), tool, project_of(workspace),
                                         workspace))
        else:
            ident = identify(cfg.get("machine", ""), workspace=workspace)
        if ident is None:
            return ""
        if event == "prompt" and tool in ("codex", "cursor"):
            words = hub_words(data.get("prompt"))
            if words is not None:  # /hub typed into the chat: answered here, never sent on
                line = _hub_line(ident, words, run) or "kaptyorka: /hub did not run"
                if tool == "codex":
                    return json.dumps({"decision": "block", "reason": line, "systemMessage": line})
                return json.dumps({"continue": False, "user_message": line})
            if tool == "cursor":
                return json.dumps({"continue": True})
        shown = ""
        if tool == "codex" and event == "start":
            shown = _hub_line(ident, [], run)  # Codex shows no status line: say it at start
            if offhub.personal(ident.project, ident.cwd, ident.key) or offhub.off(ident.key):
                return json.dumps({"systemMessage": shown}) if shown else ""
        if offhub.personal(ident.project, ident.cwd, ident.key):
            return ""  # not a team project's folder: nothing is sent, nothing is shown
        if tool == "claude-code":
            sid = ident.key.split(":", 1)[1]
            found = offhub.session(sid) if event == "start" else None
            if found is not None:
                offhub.started(sid, *found, os.environ)
        if offhub.off(ident.key):
            return ""  # kept off the hub: nothing is sent, nothing is shown

        deadline = time.monotonic() + TIMEOUT_S  # one budget for all calls of this hook

        def get(path: str):
            left = max(deadline - time.monotonic(), 0.1)
            return http.request("GET", f"{cfg['hub']}{path}", cfg["token"],
                                headers=ident.headers(), timeout=left)

        reason = ""
        if stop:
            query = ""
            if tool in ("claude-code", "codex") and optin.get(ident.key) is None:
                # a session that joined the team sends nothing of its folder
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
        if shown:
            return json.dumps({**(json.loads(fmt(event, brief)) if brief else {}),
                               "systemMessage": shown})
        if brief:
            return fmt(event, brief)
    except Exception:
        pass  # a hook must never disturb the tool
    return ""
