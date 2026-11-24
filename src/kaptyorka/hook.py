"""`kaptyorka hook <tool> <event>`: print the hub's brief for this session. Silent on any failure."""
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from typing import Callable, Dict, List, Optional
from urllib.parse import urlencode

from kaptyorka import facts, guide, http, offhub, optin, overnight, paths, session_end, suggest
from kaptyorka.identity import Identity, identify, project_of

TIMEOUT_S = 3
GIT_S = 1  # the stop hook's own git read, inside TIMEOUT_S
# tools that show no hub indicator of their own: the start text says the state first (Codex's
# status line takes built-in items only, Cursor has none for hooks, OpenCode 1 only toasts, and
# OpenCode 2's footer needs its Bun build)
NO_INDICATOR = ("codex", "cursor", "opencode")
# prompt hooks that get the prompt's text (suggest.py); OpenCode's from kaptyorka's plugin
PROMPT_TEXT = ("claude-code", "codex", "opencode")
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


def _local(fmt, event: str, text: str, shown: str) -> str:
    """What a hook prints for a text built on this machine, with Codex's state line."""
    out = fmt(event, text) if text else ""
    if shown:
        return json.dumps({**(json.loads(out) if out else {}), "systemMessage": shown})
    return out


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


NIGHT_EVENTS = ("pretool", "permission", "failure", "end")


def _night_team(sid: str, cwd: str, cfg: dict) -> "tuple[bool, str, str]":
    """(team session, key, project) for overnight mode: team only when on the hub."""
    key = f"claude:{sid}"
    ident = optin.apply(Identity(key, cfg.get("machine", ""), "claude-code", project_of(cwd), cwd))
    team = bool(cfg.get("hub") and cfg.get("token")) and not offhub.personal(
        ident.project, cwd, key) and not offhub.off(key)
    return team, key, ident.project


def _night_prompt(data: dict, cfg: dict) -> "tuple[str, str]":
    """(context for Claude, line for the person) from overnight mode for this prompt."""
    sid = str(data.get("session_id") or "")
    prompt = data.get("prompt") if isinstance(data.get("prompt"), str) else ""
    cwd = str(data.get("cwd") or os.getcwd())
    st = overnight.state(sid)
    m = overnight.HUB_OVERNIGHT.fullmatch(prompt)
    if st is not None:
        if m and (m.group(1) or "").lower() == "off":
            return "", ""  # the /hub command turns it off and shows the line
        if m or not overnight.from_person(sid, data):
            return "", ""
        if st.get("setup") and overnight.is_reply(prompt):
            lines = overnight.apply_words(sid, st, prompt)
            if overnight.YES.fullmatch(prompt.strip()):
                st = overnight.state(sid) or st
                overnight.save(sid, {**st, "setup": False})
                lines.append("Overnight rules confirmed. Good night.")
            text = "\n".join(lines)
            return (f"Repeat this to the user in one line each, then go on: {text}"
                    if lines else ""), text
        report = overnight.finish(sid, "you are back")
        return ("kaptyorka: the person is back, so overnight mode is off. Here is the morning"
                " report; show it to them if they ask:\n" + report,
                "kaptyorka: overnight mode is off: you are back. Morning report: "
                + str(overnight.save_report(sid, report)))
    if paths.in_manager_folder(cwd) or not overnight.from_person(sid, data):
        return "", ""
    if not (m and (m.group(1) or "on").lower() == "on") and not overnight.triggered(prompt):
        return "", ""
    team, key, project = _night_team(sid, cwd, cfg)
    st = overnight.turn_on(sid, project, cwd, team, key, cfg.get("machine", ""),
                           "" if m else prompt, str(data.get("permission_mode") or ""))
    overnight.apply_words(sid, st, prompt)
    st = overnight.state(sid) or st
    return (overnight.instructions(st, shlex.quote(_exe())),
            "kaptyorka: overnight mode is on for this task. To stop it: /hub overnight off")


def _report_end(event: str, sid: str, data: dict, cfg: dict) -> None:
    """A team session's usage limit, API error or end, to the hub (session_end.py). Never
    from a personal folder, never waits long (SessionEnd hooks get about 1.5 s)."""
    try:
        body = session_end.event(event, data)
        cwd = str(data.get("cwd") or os.getcwd())
        if body is None or paths.in_manager_folder(cwd):
            return
        team, key, project = _night_team(sid, cwd, cfg)
        if not team:
            return
        ident = Identity(key, cfg.get("machine", ""), "claude-code", project, cwd)
        http.request("POST", f"{cfg['hub']}/api/session-event", cfg["token"], body,
                     headers=ident.headers(), timeout=1)
    except Exception:
        pass


def _night(event: str, data: dict, cfg: dict) -> Optional[str]:
    """Claude Code's overnight events; None when overnight mode has nothing to say."""
    sid = str(data.get("session_id") or "")
    if not sid:
        return None
    if event == "pretool":
        return overnight.on_pretool(sid, data, overnight.finish)
    if event == "permission":
        return overnight.on_permission(sid, data, overnight.finish)
    if event == "failure":
        overnight.on_failure(sid, data)
        _report_end(event, sid, data, cfg)
        return ""
    if event == "end":
        overnight.on_end(sid, data, overnight.finish)
        _report_end(event, sid, data, cfg)
        return ""
    if event == "stop":
        return overnight.on_stop(sid, data, overnight.finish)
    return None


def _run(tool: str, event: str, stdin: str, run=subprocess.run) -> str:
    try:
        try:
            data = json.loads(stdin or "{}")
        except ValueError:
            data = {}
        if not isinstance(data, dict):
            data = {}
    except Exception:
        return ""
    if tool == "claude-code" and event in NIGHT_EVENTS + ("stop", "prompt"):
        return _with_night(event, data, run)
    if tool == "opencode" and event == "failure":
        _report_limit(tool, data)
        return ""
    return _hub_run(tool, event, data, run)


def _report_limit(tool: str, data: dict) -> None:
    """OpenCode's plugin saw a usage or rate limit stop a session (its `session.error` on
    OpenCode 1, its `retry` hook on 2): told to the hub like Claude Code's StopFailure
    (session_end.py). Team sessions only, never one kept off the hub."""
    try:
        sid = str(data.get("session_id") or os.environ.get("KAPTYORKA_OPENCODE_SESSION") or "")
        cwd = str(data.get("cwd") or os.getcwd())
        cfg = paths.load_config(None)
        body = session_end.event("failure", data)
        if not sid or body is None or not cfg.get("hub") or not cfg.get("token"):
            return
        key = f"{tool}:{sid}"
        ident = optin.apply(Identity(key, cfg.get("machine", ""), tool, project_of(cwd), cwd))
        if offhub.personal(ident.project, cwd, key) or offhub.off(key):
            return
        http.request("POST", f"{cfg['hub']}/api/session-event", cfg["token"], body,
                     headers=ident.headers(), timeout=2)
    except Exception:
        pass


def _config(data: dict) -> dict:
    here = data.get("cwd") if isinstance(data.get("cwd"), str) else None
    # the manager's folder (the input's or the hook's own): its own config, never the admin's
    return paths.load_config(next((c for c in (here, os.getcwd())
                                   if paths.in_manager_folder(c)), None))


def _with_night(event: str, data: dict, run) -> str:
    """Overnight mode first (Claude Code only), then the hub's own hook. A failing overnight
    check while the mode is on denies the call, never lets it through."""
    sid = str(data.get("session_id") or "")
    try:
        cfg = _config(data)
        if event == "prompt":
            context, line = _night_prompt(data, cfg)
        else:
            night = _night(event, data, cfg)
    except Exception:
        on = False
        try:
            on = bool(sid) and overnight.state(sid) is not None
        except Exception:
            pass
        if on and event == "pretool":
            return overnight._deny_pre("kaptyorka's overnight check failed on this call; skip it"
                                       " and note it.")
        if on and event == "permission":
            return overnight._decide("deny", "kaptyorka's overnight check failed; skip and note.")
        context, line, night = "", "", None
    if event in NIGHT_EVENTS:
        return night or ""
    if event == "stop" and night is not None:
        return night
    out = _hub_run("claude-code", event, data, run)
    if event == "stop" and not out:  # the hub's done check first: one block per stop
        return _children_check(data)
    if event != "prompt" or not (context or line):
        return out
    body = {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext":
                                   "\n\n".join(p for p in (context, out) if p)}}
    if line:
        body["systemMessage"] = line
    return json.dumps(body)


LEFT_STOP = "left_stop"  # per session: the children it was last asked about


def _children_check(data: dict) -> str:
    """A session that stops while `claude --bg` sessions it started still run is asked once
    to stop them or say why they keep running (left.py): once per set of children, never
    twice in one stop, never for the manager, a hub worker or one in overnight mode. The hub
    closes its own workers. Local files only, well inside the hook's time."""
    try:
        sid = str(data.get("session_id") or "")
        cwd = data.get("cwd") if isinstance(data.get("cwd"), str) else os.getcwd()
        if not sid or data.get("stop_hook_active") is True or paths.in_manager_folder(cwd) \
                or overnight.state(sid) is not None:
            return ""
        workers = offhub.load_team()[1]
        if f"claude:{sid}" in workers:
            return ""
        from kaptyorka.adapters.claude import live_children
        kids = [k for k in live_children(sid, os.environ.get("CLAUDE_CODE_CHILD_NOTES_DIR"))
                if f"claude:{k[0]}" not in workers]
        memo = paths.home() / LEFT_STOP / sid
        seen = " ".join(sorted(k[0] for k in kids))
        try:
            if memo.read_text() == seen:
                return ""
        except OSError:
            pass
        if not kids:
            memo.unlink(missing_ok=True)
            return ""
        memo.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        memo.write_text(seen)
        names = ", ".join(f"{name} (`claude stop {k[:8]}`)" for k, name in kids)
        return json.dumps({"decision": "block", "reason":
                           f"You started {names}, still running: stop them or say in one line"
                           " why they must keep running."})
    except Exception:
        return ""  # a hook must never disturb the tool


def _hub_run(tool: str, event: str, data: dict, run=subprocess.run) -> str:
    try:
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
                line = _hub_line(ident, words, run) or f"kaptyorka: {optin.command(ident.key)} did not run"
                if tool == "codex":
                    return json.dumps({"decision": "block", "reason": line, "systemMessage": line})
                return json.dumps({"continue": False, "user_message": line})
            if tool == "cursor":
                return json.dumps({"continue": True})
        shown = ""
        if tool == "codex" and event == "start":
            shown = _hub_line(ident, [], run)  # Codex shows no status line: say it at start
        # The hub guide (guide.py), built here alone, through each tool's own user-level path:
        # Claude Code adds a SessionStart hook's plain stdout to the context
        # (code.claude.com/docs/en/hooks, read 2026-11-20); Codex hands SessionStart's
        # hookSpecificOutput.additionalContext to the model (openai/codex rust-v0.166.0,
        # codex-rs/hooks/src/events/session_start.rs:264, engine/output_parser.rs:93); the
        # OpenCode plugin pushes this text into the system prompt (v1.19.6
        # experimental.chat.system.transform, packages/plugin/src/index.ts:291; v2.3.2
        # session.hook("context") event.system, packages/plugin/src/promise/session.ts:35),
        # rebuilt for every request, so there it comes with each prompt; Cursor's sessionStart
        # additional_context (adapters/cursor.py).
        guided = event == "start" or (tool == "opencode" and event == "prompt")
        prompt = data.get("prompt") if event == "prompt" and tool in PROMPT_TEXT \
            and isinstance(data.get("prompt"), str) else ""
        why = offhub.personal(ident.project, ident.cwd, ident.key)

        def head(text: str, off: bool) -> str:
            """The state in plain words first, where the tool shows no hub indicator."""
            if not guided or tool not in NO_INDICATOR:
                return text
            return "\n".join(p for p in (guide.state(ident.key, ident.project, why, off), text) if p)
        if why:  # not a team project's folder: nothing is sent, only the guide is shown
            text, off = "", offhub.off(ident.key)
            if guided and off:
                text = guide.off(ident.key)
            elif guided and why == paths.NOT_TEAM:
                text = guide.personal(ident.key, offhub.load_mine())
            elif prompt and why == paths.NOT_TEAM and not off:  # never after /hub off
                text = suggest.off_line(ident.key, prompt)
            return _local(fmt, event, head(text, off), shown)
        if tool == "claude-code":
            sid = ident.key.split(":", 1)[1]
            found = offhub.session(sid) if event == "start" else None
            if found is not None:
                offhub.started(sid, *found, os.environ)
        if offhub.off(ident.key):  # kept off the hub: nothing is sent, only its one line
            return _local(fmt, event, head(guide.off(ident.key) if guided else "", True), shown)
        # joined with /hub team, or a team machine's coordinator: nothing of its folder is sent
        joined = optin.get(ident.key) is not None or ident.project == paths.COORDINATOR
        if tool == "cursor" and event == "start":  # its brief comes with tool calls, not here
            return _local(fmt, event, head(guide.on_hub(ident.key, ident.project, joined), False),
                          shown)

        deadline = time.monotonic() + TIMEOUT_S  # one budget for all calls of this hook

        def get(path: str):
            left = max(deadline - time.monotonic(), 0.1)
            return http.request("GET", f"{cfg['hub']}{path}", cfg["token"],
                                headers=ident.headers(), timeout=left)

        reason = ""
        if stop:
            query = ""
            if tool in ("claude-code", "codex") and not joined:
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
        if brief and event == "start":
            brief += "\n\n" + guide.on_hub(ident.key, ident.project, joined)
        hints = suggest.on_lines(ident.key, prompt) if prompt else ""
        brief = "\n\n".join(p for p in (reason, head(brief, False) if brief else "", hints) if p)
        if shown:
            return json.dumps({**(json.loads(fmt(event, brief)) if brief else {}),
                               "systemMessage": shown})
        if brief:
            return fmt(event, brief)
    except Exception:
        pass  # a hook must never disturb the tool
    return ""
