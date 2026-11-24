"""The hub app: MCP tools and plain routes over HTTP behind per-device tokens."""
import asyncio
import contextlib
import functools
import html
import importlib.resources
import inspect
import json
import math
import mimetypes
import re
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from dataclasses import asdict, replace
from typing import Dict, List, Mapping, Optional, Union
from urllib.parse import unquote, urlparse

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response

from kaptyorka import __version__, admin_ops, brief, identity, knowledge, machine_events, manager, paths, session_end, signin, stuck, usage
from kaptyorka.gitlab import GitLabError, GitLabSource
from kaptyorka import overnight as overnight_mod
from kaptyorka import rules as rules_mod
from kaptyorka.secrets_check import PATTERNS as SECRET_PATTERNS
from kaptyorka.auth import Auth, NoWebSocket, RateLimiter, bearer, quiet_upgrade_warnings
from kaptyorka.brief import message_text
from kaptyorka.connects import ConnectStore
from kaptyorka import compat, hub_groups, left, limits, rollout, tls
from kaptyorka.hub_feed import feed, session_row
from kaptyorka.hub_feed import knocks as hub_knocks
from kaptyorka.manager import ManagerStore
from kaptyorka.memory import MemoryStore, check_state
from kaptyorka.secrets_check import check_secrets, labelled_values
from kaptyorka.store import (MAX_FILE, MAX_IDENT, ONLINE_S, RECENT_S, UNREACHABLE, Agent,
                              KaptyorkaError, Principal, Store, attach_line, clean, machine_name,
                              session_id)
from kaptyorka.workers import LIVE, MAX_FACT, STARTUP_S, WorkerStore, _git as clean_git, clean_facts, safe_name

MAX_WAIT_S = 25
TICK_S = 15
ARRIVAL_GAP_S = 600  # no own session on a host this long: the next one is an arrival
OLD_VERSION_S = 1800  # a machine behind the hub this long after the hub started: the manager tells
OWN_TOKEN = ("use your own device token: run kaptyorka join with an invite, or ask the admin to"
             " assign this machine")
REMOVED = "{project} was removed; ask the admin to register it again"
ASK_MANAGER = ("to reach another person's session, message the manager: say whom and why;"
               " only the admin opens a door")
ON_SHARED = "a worker on a shared account cannot start, adopt or close workers; ask your boss"
SECRET_DAYS = 30  # values sent to the manager as a password or token, refused in its writes
SECRET_RULE = ("Never store or repeat a secret: leave the value out, and tell the sender in one"
               " line to rotate it, since it has been sent.")
MANAGER_NO_WORK = ("the manager never starts workers itself: ask the person's own session to call"
                   " work, so that session is the worker's boss")
CSP = ("default-src 'none'; connect-src 'self'; script-src 'unsafe-inline'; "
       "style-src 'unsafe-inline'; img-src 'self' data:; media-src 'self'; font-src 'self'")
FONTS = ("archivo-latin.woff2",)  # the page's font files (web/fonts/, SIL OFL 1.1)
NO_STORE = {"Cache-Control": "no-store"}
CLEAR_COOKIE = "kaptyorka_signin=; Path=/auth/gitlab; HttpOnly; SameSite=Lax; Max-Age=0"

# The /api/ routes machines and the command line call with a device token: the bridge, the MCP
# shim (files), the hooks (brief, done-check), doctor and join (me, feed), hub-state (feed),
# Connect GitLab from a device link (me/gitlab) and the admin commands. Every other /api/ route
# is the page's: with sign-in with GitLab on, only a GitLab sign-in or the admin token reaches
# it. Routes outside /api/ (/mcp, /bridge/...) are all machines'.
MACHINE_ROUTES = [(m, re.compile(p)) for m, p in (
    ("GET", r"/api/(brief|done-check|me|feed|settings|people|invites|rules|machines|team/machines|gitlab|rollout)"),
    ("GET", r"/api/files/\d+"),
    ("POST", r"/api/(files|me/gitlab|people|invites|rules|projects|gitlab|team-machines|overnight|rollout)"),
    ("POST", r"/api/people/[^/]+/(gitlab|role|team)"),
    ("DELETE", r"/api/people/[^/]+/team"),
    ("POST", r"/api/people/[^/]+/team-machines/[^/]+"),
    ("POST", r"/api/machines/[^/]+/(grant|owner|revoke|gpu-queue)"),
    ("POST", r"/api/projects/[^/]+(/grant|/revoke)?"),
    ("POST", r"/api/devices/\d+/(revoke|move)"),
    ("POST", r"/api/legacy/(on|off)"),
    ("PUT", r"/api/skills"),
    ("DELETE", r"/api/(people|invites|projects)/[^/]+"),
    ("DELETE", r"/api/gitlab"),
)]
SIGN_IN_FIRST = "the page needs a GitLab sign-in: open the hub in a browser and sign in with GitLab"


def _ago(s: int) -> str:
    """34 s, 3 min: how long a held send's target has been gone."""
    return f"{s} s" if s < 120 else f"{s // 60} min"


def old_version_text(w: dict, admins: List[str]) -> str:
    """The manager's notice for one machine behind the hub (a WorkerStore.warnings row): the
    machine, its person and the short fixed reason, never the update's own output."""
    who = machine_name("", w["os_user"], w["host"], w["host"])
    head = (f"old kaptyorka: {who} runs {w['version']}, the hub runs {__version__}"
            f" ({w['reason']}).")
    if w.get("team"):
        return (head + f" It is a team machine: tell each admin ({', '.join(admins)}) in one"
                " line to run `kaptyorka update` there. Once; never repeat it.")
    return (head + f" Send {w['person']} one short message (their online session, else their"
            f" page web:{w['person']}) naming {who}, the reason, and `kaptyorka update` to run"
            " there. Once; never repeat it.")


def old_versions_text(due: List[dict], admins: List[str]) -> str:
    """One notice for every machine found behind the hub at once: one alone as
    old_version_text, several as one list, so a burst is one message."""
    if len(due) == 1:
        return old_version_text(due[0], admins)
    names = [machine_name("", w["os_user"], w["host"], w["host"]) for w in due]
    listed = "; ".join(f"{n} runs {w['version']} ({w['reason']})" for n, w in zip(names, due))
    team = [n for n, w in zip(names, due) if w.get("team")]
    own: Dict[str, List[str]] = {}
    for n, w in zip(names, due):
        if not w.get("team"):
            own.setdefault(w["person"], []).append(n)
    do = []
    if team:
        do.append(f"tell each admin ({', '.join(admins)}) in one line to run `kaptyorka update`"
                  f" on the team machines ({', '.join(team)})")
    for person, machines in own.items():
        do.append(f"send {person} one short message (their online session, else their page"
                  f" web:{person}) naming {', '.join(machines)}, the reason and `kaptyorka"
                  " update` to run there")
    todo = "; ".join(do)
    return (f"{len(due)} machines run an old kaptyorka, the hub runs {__version__}: {listed}."
            f" {todo[0].upper() + todo[1:]}. Once; never repeat it.")


def machine_route(method: str, path: str) -> bool:
    if not path.startswith("/api/"):
        return True
    method = "GET" if method == "HEAD" else method
    return any(m == method and p.fullmatch(path) for m, p in MACHINE_ROUTES)

# the invite and sign-in pages: the main page's tokens, font, button and mark (web/index.html)
_INVITE_STYLE = (
    '@font-face { font-family: "Archivo"; font-style: normal; font-weight: 100 900; font-display: swap;'
    ' src: url(/fonts/archivo-latin.woff2) format("woff2"); }'
    ":root { --bg: #1b1a16; --surface: #211f1a; --surface2: #2a2822; --ink: #e7e4dc; --muted: #aaa498; --faint: #878377;"
    " --line: rgba(231, 228, 220, 0.18); --accent: #da6947; --accent-hover: #e27a5a; --accent-press: #c45a3a;"
    " --dur: 160ms; --ease-out: cubic-bezier(.2, .7, .3, 1);"
    ' --sans: "Archivo", system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;'
    ' --mono: "JetBrains Mono", "SF Mono", ui-monospace, Menlo, Consolas, monospace; }'
    "* { box-sizing: border-box; }"
    "html { background: var(--bg); }"
    "body { background: var(--bg); color: var(--ink); font: 15px/1.55 var(--sans); -webkit-font-smoothing: antialiased;"
    " max-width: 40rem; margin: 4rem auto; padding: 0 16px; animation: fade-in 220ms var(--ease-out); }"
    "@keyframes fade-in { from { opacity: 0; transform: translateY(4px); } to { opacity: 1; transform: none; } }"
    ".brand { display: flex; align-items: center; gap: 10px; font-weight: 650; font-size: 18px; margin-bottom: 32px; }"
    "h1 { font-size: 26px; font-weight: 650; letter-spacing: -.01em; margin: 0 0 20px; }"
    "p { margin: 0 0 16px; color: var(--muted); }"
    "pre { margin: 0 0 16px; font: 13px/1.5 var(--mono); color: var(--ink); background: var(--surface);"
    " border: 1px solid var(--line); border-left: 2px solid var(--accent); padding: 14px 16px;"
    " white-space: pre-wrap; word-break: break-all; }"
    "a { color: var(--accent); }"
    "form { margin: 24px 0; }"
    ".btn { height: 40px; padding: 0 22px; display: inline-flex; align-items: center; justify-content: center;"
    " font: 600 14px var(--sans); color: var(--bg); background: var(--accent); border: 1px solid var(--accent);"
    " cursor: pointer; transition: background-color var(--dur) var(--ease-out), border-color var(--dur) var(--ease-out); }"
    ".btn:hover { background: var(--accent-hover); border-color: var(--accent-hover); }"
    ".btn:active { background: var(--accent-press); border-color: var(--accent-press); }"
    ".btn:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }"
    "@media (max-width: 480px) { body { margin-top: 2rem; } .btn { width: 100%; } }"
    "@media (prefers-reduced-motion: reduce) { body { animation: none; } .btn { transition: none; } }")
_MARK = ('<svg width="28" height="28" viewBox="0 0 512 512" aria-hidden="true">'
         '<rect width="512" height="512" rx="112" fill="#111217"/>'
         '<g stroke="#3a3f4a" stroke-width="14" stroke-linecap="round">'
         '<line x1="256" y1="256" x2="256" y2="118"/><line x1="256" y1="256" x2="394" y2="256"/>'
         '<line x1="256" y1="256" x2="256" y2="394"/><line x1="256" y1="256" x2="118" y2="256"/></g>'
         '<g fill="#ccccdc"><circle cx="256" cy="118" r="34"/><circle cx="394" cy="256" r="34"/>'
         '<circle cx="256" cy="394" r="34"/><circle cx="118" cy="256" r="34"/></g>'
         '<circle cx="256" cy="256" r="52" fill="#f5b73d"/></svg>')
INVITE_PAGE = ("<!doctype html><html lang=en><meta charset=utf-8>"
               "<meta name=viewport content='width=device-width, initial-scale=1'>"
               "<meta name=color-scheme content=dark><title>kaptyorka</title><style>"
               + _INVITE_STYLE.replace("{", "{{").replace("}", "}}")
               + "</style><div class=brand>" + _MARK + "<span>kaptyorka</span></div>{body}")

# the invite page's Sign in with GitLab: a form, so another site cannot start it with a link
INVITE_GITLAB = "<form method=post action='{action}'><button class=btn>Sign in with GitLab</button></form>"

# the invite page's second way in, without script: the form's POST redirects to the page
INVITE_WEB = ("<form method=post action='{action}'><button class=btn>Open the page as this person"
              "</button></form>")

INSTRUCTIONS = (
    "kaptyorka connects you with the other coding agents on this team. "
    "who() lists the sessions you may reach; send(to, text) messages one by name or 'all'. "
    "You may message your own person's sessions and the manager. To reach another person's "
    "session, message the manager with whom and why; only the admin opens a door. door_close(to) "
    "ends a door; "
    "inbox() reads your messages; status(text) says what you are working on; "
    "notes_list/notes_read/notes_write share a team board, one text per topic; "
    "pass project to use that project's notes. projects() lists the projects you have a role in. "
    "work(project, task) starts a worker session for you on another machine; workers() lists "
    "yours; adopt(name) takes over an unattended one; close_worker(name) ends one. "
    "state(project) reads the project's state note; state_write(project, text) updates it "
    "(five headings: What it is, Where it stands, Decisions, Open problems, How to run and test); "
    "decide(project, text) records one decision. "
    "rules() reads the team rules; propose_rule(text, evidence) suggests one after your person "
    "corrected you twice; conflict(rule_a, rule_b) reports two rules that clash. "
    "Only sessions in a team project's folder use the hub: in any other folder every tool "
    "refuses, and nothing from it reaches the hub, until the user puts the session on the team "
    "with /hub team [project] (or join_team, only when the user asked)."
)
NAME_A_PROJECT = ("this session coordinates across projects, so name the project:"
                  " work(project, task) with one of your projects")


def opencode_reminder(ms, key: str, principal, now: float, project: Optional[str] = None) -> str:
    """OpenCode has no stop event: the done-check reason rides on the next prompt brief.
    Looks only (blocked_at stays unset), so it repeats until the state note is written."""
    out = ms.done_check(key, principal, now, commit=False, project=project)
    return out["reason"] if out["decision"] == "block" else ""


def _bad_request(fn):
    """Answer 400 instead of 500 when a client sends a malformed request."""
    @functools.wraps(fn)
    async def wrapper(request):
        try:
            return await fn(request)
        except (ValueError, KeyError, TypeError):  # JSONDecodeError is a ValueError
            return PlainTextResponse("bad request", status_code=400)
    return wrapper


def _tool_errors(fn):
    """Show a KaptyorkaError to the agent as a tool error with its message."""
    if inspect.iscoroutinefunction(fn):
        @functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            try:
                return await fn(*args, **kwargs)
            except KaptyorkaError as e:
                raise ToolError(str(e)) from e
    else:
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            try:
                return fn(*args, **kwargs)
            except KaptyorkaError as e:
                raise ToolError(str(e)) from e
    return wrapper


AUTO_DOORS_KEY = "auto_doors_told_id"  # the last door_open log row the admin heard of
MANAGER_FIRST = ("You are the team's manager session; follow the team rules and manager"
                 " instructions in your system prompt and wait for the hub's messages.")
MANAGER_FAILED = ("the manager session failed to start twice; see"
                  " ~/.kaptyorka/workers/manager.log on the hub machine")
MANAGER_UNTRUSTED = ("the manager cannot start: Claude Code does not trust ~/.kaptyorka/manager"
                     " on the hub machine; run claude there once, accept, then"
                     " kaptyorka start --manager")


def build_app(store: Store, manager_url: str = "") -> Starlette:
    server = MCPServer("kaptyorka", instructions=INSTRUCTIONS)
    sent = {"event": asyncio.Event()}  # set and replaced after each send; wakes bridge polls
    ws = WorkerStore(store)
    ms = MemoryStore(store)
    rs = rules_mod.RulesStore(store)
    mm = ManagerStore(store)
    mm.note_version(__version__)  # a new version starts with no crash streak
    cs = ConnectStore(store, lambda key, text: ws.notify(key, text), mm.person_id)
    brief_cache: dict = {}  # per session key; see brief.cached
    gl = GitLabSource(store)  # the GitLab group's project list: names only, never access
    si = signin.SignIn(store)  # sign in to the page with GitLab
    decide_lock = threading.Lock()
    work_decide_lock = threading.Lock()  # one decision per request, even when two race
    started = {"first": True}  # the first tick closes a manager left running while off
    # sessions in overnight mode, per bridge device, as its last heartbeat said: their bridge
    # wakes them when stuck, so the hub does not as well
    night: dict = {"by_device": {}, "keys": set()}
    mev = machine_events.MachineEvents(store, lambda: identity.facts()["host"])

    def principal_of(headers: Mapping) -> Principal:
        """The caller's principal from its bearer token (the Auth middleware already let it in)."""
        machine = unquote(headers.get("x-kaptyorka-machine") or "") or None
        principal = store.authenticate(bearer(headers), machine=machine)
        if principal is None:
            raise KaptyorkaError("unauthorized")
        return principal

    def page_ok(p: Principal) -> bool:
        """May this principal use the page? Always while sign-in with GitLab is off; once on,
        only a GitLab sign-in or the hub key (~/.kaptyorka/admin-token on the hub machine)."""
        return signin.is_session(store, p.device_id) or signin.status(store) is not None \
            or p.hub_key

    def page_gate(scope, p: Principal) -> Optional[str]:
        if machine_route(scope.get("method", ""), scope["path"]) or page_ok(p):
            return None
        return SIGN_IN_FIRST

    def on_hub(project: str, key: str, principal: Optional[Principal] = None) -> bool:
        """Only sessions in a registered project's folder reach the hub, besides the manager
        (its account, or a session row already its own) and the workers the hub started."""
        manager_id = mm.person_id()
        if principal is not None and manager_id is not None and principal.person_id == manager_id:
            return True
        if project == paths.COORDINATOR:  # joined with /hub team: its person's, no project
            return principal is not None and not principal.legacy
        if store.team_project(project) or key in ws.live_keys():
            return True
        return manager_id is not None and ws.session_person(key) == manager_id

    def identify(h, principal: "Principal | None" = None) -> Agent:
        # values are percent-encoded by Identity.headers(), since headers must be ASCII
        ident = [unquote(h.get(f"x-kaptyorka-{k}") or "") for k in ("key", "machine", "tool", "project")]
        ident[1:] = [clean(v, MAX_IDENT) for v in ident[1:]]  # one line: names and push texts
        if not all(ident):
            raise KaptyorkaError("no kaptyorka identity; run kaptyorka join")
        principal = principal or principal_of(h)
        if not on_hub(ident[3], ident[0], principal):  # checked before anything is stored
            raise KaptyorkaError(paths.not_team_help(team_projects_of(principal)))
        return store.seen(*ident, principal=principal)

    def team_projects_of(principal: Principal) -> List[str]:
        """The registered projects the principal has a role in (archived ones left out)."""
        return [p["name"] for p in store.projects_for(principal) if not p.get("archived")]

    def session_project(agent: Agent, principal: Optional[Principal]) -> str:
        """The session's project from its folder, the same lookup the brief uses."""
        folders = ws.folders(principal.device_id) if principal is not None else {}
        return ms.session_project(agent.cwd, agent.project, folders)

    def me(ctx: Context) -> Agent:
        return identify(ctx.headers or {})

    def caller(ctx: Context) -> "tuple[Agent, Principal]":
        principal = principal_of(ctx.headers or {})
        return identify(ctx.headers or {}, principal), principal

    @server.tool()
    @_tool_errors
    def who(ctx: Context) -> List[dict]:
        """Online sessions you may reach: your own, the manager's, and those behind a door
        the admin opened. Name, id, label, person, machine, tool, project, status, activity,
        last seen. A project you may not see shows as (private), in the name too; send to its
        id then. To reach anyone else, message the manager."""
        agent, principal = caller(ctx)
        now = store.clock()
        names = admin_ops.names(store)
        live = ws.live_keys()
        mine = ws.by_key(agent.key) if agent.key in live else None
        boss = ws.boss_key(mine["name"]) if mine else None  # a worker sees its boss marked
        rows, mid = [], mm.person_id()
        fresh = store.fresh_helpers(now)  # most end within seconds (left.py)
        for a in store.online():
            if a.key in fresh or not store.listed(principal.person_id, a.key, a.person_id):
                continue
            row = {**session_row(store, a, principal, names, now, live, ws, mid),
                   "you": a.key == agent.key}
            row.pop("writable", None)  # the page's own question
            if a.key == boss:
                row["boss"] = True
            rows.append(row)
        return rows

    def wake_bridges():
        sent["event"].set()
        sent["event"] = asyncio.Event()

    def tell(to_key: str, text: str) -> None:
        if to_key == mm.last_key():  # the manager: a due task-list reminder rides along
            text += mm.tasks_nudge(store.clock())
        if ws.notify(to_key, text):
            wake_bridges()

    def manager_files(host: str, now: float) -> None:
        """The manager's account, CLAUDE.md and config; written again when the rules changed
        or a file is missing. A lost config means a lost token: a new one, once an hour."""
        folder = paths.home() / manager.DIR
        token = mm.ensure_account(host, "hub")
        if token is None and not (paths.home() / manager.CONFIG).exists() \
                and mm.push_once("manager-rotate", now, 3600):
            print("kaptyorka: the manager's config is missing; issuing a new token", flush=True)
            for d in store.devices(mm.person_id()):
                if d["name"] == manager.DEVICE and d["revoked_at"] is None:
                    store.device_revoke(d["id"], "hub")
            token = mm.ensure_account(host, "hub")
        rules_now = rs.current()
        # its real tools, as this hub lists them: the capability table is written from them
        tools = [(t.name, t.description or "") for t in server._tool_manager.list_tools()]
        md = folder / "CLAUDE.md"  # a hub update with new manager wording rewrites it too
        if token or mm.rules_written_version() != rules_now["version"] \
                or not md.exists() or md.read_text() != manager.claude_md(rules_now["text"], tools):
            folder.mkdir(mode=0o700, parents=True, exist_ok=True)
            mm.write_files(folder, manager_url, token, rules_now["text"], tools=tools)
            mm.mark_rules_written(rules_now["version"])
        # put back at once when changed or removed; the shim is this hub's own executable
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        manager.write_launch_files(folder, str(Path(sys.argv[0]).resolve()))

    def manager_pushes(now: float) -> None:
        """Tell the online manager session what needs the admin; each item once."""
        key = mm.session_key(now) if mm.enabled() else None
        if key is None:
            return
        names = admin_ops.admins(store)  # several admins: the manager tells each one
        them = (f"the admin ({names[0]})" if len(names) == 1
                else f"each admin ({', '.join(names)})")

        def push(push_key: str, ttl: Optional[float], text: str) -> None:
            if mm.push_once(push_key, now, ttl):
                tell(key, text)
        for w in ws.abandoned():
            push(f"abandoned:{w['name']}", None,
                 f"worker `{w['name']}` of {w['person']} is abandoned (unattended a day)"
                 f" on {w['host']}; tell {them} in one line")
        for d in ws.shared_offline(now):
            if mev.tracked_team(d["id"]):
                continue  # a team machine: its machine event says it
            push(f"offline:{d['id']}", 86400,
                 f"bridge on {d['host']} ({d['os_user']}), a shared machine, has been offline"
                 f" {d['minutes']} minutes; tell {them}")
        for f in ws.failed_twice(now):
            push(f"failed:{f['person_id']}", 3600,
                 f"{f['person']}'s workers failed to start twice ({f['error']}); tell {them}")
        old = now - manager.REMIND_AFTER_S
        for p in rs.proposals("open"):
            if p["time"] < old:
                push(f"remind:proposal:{p['id']}", 86400,
                     f"proposal from {p['person']} has waited a day: {p['text'][:80]};"
                     f" remind {them}")
        for r in store.requests(pending_only=True):
            if r["created"] < old:
                what = f"{r['kind']} {r['target']}: {r['why']}"
                push(f"remind:request:{r['id']}", 86400,
                     f"access request from {r['person']} has waited a day: {what[:80]};"
                     f" remind {them}")
        for row in store.log(200):  # overnight rule clashes: the manager tells the person once
            if row["kind"] == overnight_mod.CLASH_KIND and row["time"] > now - 2 * 86400:
                text = overnight_mod.clash_notice(row["person"], row["detail"])
                if text:
                    push(f"clash:{row['id']}", None, text)
        push("weekly", manager.WEEKLY_S,
             "weekly review: read log(500) for repeated conflicts and corrections; file"
             " propose_rule for what repeats; never edit rules")
        knowledge_pushes(key, now)

    def knowledge_pushes(key: str, now: float) -> None:
        """The daily knowledge digest after the quiet hour, and between digests one light
        notice at most every 10 minutes. Only to an awake manager: a sleeping one gets them
        when it wakes, covering everything since (they never wake it)."""
        last, light = mm.stamp(knowledge.DIGEST_KEY), mm.stamp(knowledge.LIGHT_KEY)
        if last is None:  # a new hub, or an upgrade: start counting now
            mm.set_stamp(knowledge.DIGEST_KEY, now)
            mm.set_stamp(knowledge.LIGHT_KEY, now)
            return
        if knowledge.digest_due(last, now):
            text = knowledge.digest(store, last, now)
            if text and not people_first("digest", now):
                return
            if text:
                tell(key, text)
            mm.set_stamp(knowledge.DIGEST_KEY, now)
            mm.set_stamp(knowledge.LIGHT_KEY, now)  # the digest covers them
            return
        since = max(light or last, last)
        text = knowledge.light(store, since, now)
        if text and people_first("light", now) \
                and mm.push_once("knowledge-light", now, knowledge.LIGHT_GAP_S):
            tell(key, text)
            mm.set_stamp(knowledge.LIGHT_KEY, now)

    held_since: Dict[str, float] = {}  # routine notice -> when it was first due and held

    def people_first(what: str, now: float) -> bool:
        """True when a routine notice to the manager may go now: no person's or session's
        message to it waits unanswered (younger than PEOPLE_FIRST_S), or it has been held
        PEOPLE_FIRST_MAX_S. Urgent notices never ask."""
        if mm.person_waiting(now):
            first = held_since.setdefault(what, now)
            if now - first < manager.PEOPLE_FIRST_MAX_S:
                return False
        held_since.pop(what, None)
        return True

    def manager_tick(now: float) -> None:
        if started["first"]:
            started["first"] = False
            close_if_off()
        keep_manager(now)
        manager_pushes(now)
        arrivals(now)
        old_versions(now)

    def old_versions(now: float) -> None:
        """A machine still on an older kaptyorka half an hour after the hub started this
        version: the manager tells its person, once per machine and hub version. It is work,
        so it wakes a sleeping manager; switched off, nothing is sent and nothing is marked
        (the Machines page shows the warning anyway)."""
        if now < ws.started + OLD_VERSION_S:
            return
        # a machine whose auto-update has not run yet while the staged rollout holds it back
        # (or has just let it go) updates by itself: asking its person would be noise
        st = rollout.state(store, __version__, now)
        settled = st["state"] == "passed" and now - (st.get("decided_at") or now) >= OLD_VERSION_S
        old = [w for w in ws.warnings(now, __version__)
               if w.get("reason") and w["person"] and w["person"] != manager.PERSON
               and (w["reason"] != paths.UPDATE_WAITING or settled or w["device_id"] in st["first"])]
        key = manager_to_wake() if old else None
        if key is None:
            return
        due = [w for w in old if not mm.pushed(f"old-version:{w['device_id']}:{__version__}")]
        if not due or not people_first("old-version", now):
            return
        for w in due:
            mm.push_once(f"old-version:{w['device_id']}:{__version__}", now)
        tell(key, old_versions_text(due, admin_ops.admins(store)))

    def arrivals(now: float) -> None:
        """A person's first own session on a host (none there for ARRIVAL_GAP_S) while someone
        else uses it heavily: the manager warns them and tells the admin. Never held.
        Arrivals are recorded with the manager off too, so switching it on warns no one."""
        warn = now >= ws.started + STARTUP_S and mm.enabled()  # just started: bridges have not all reported
        done = set()
        for s in ws.direct_sessions(now):
            pair = (s["host"], s["person_id"])
            if pair in done:
                continue
            done.add(pair)
            if not mm.arrived(f"arrive:{s['host']}:{s['person_id']}", now, ARRIVAL_GAP_S) \
                    or not warn:
                continue
            here = ws.host_use(s["host"], now)
            lines = usage.others(here[1], here[0], s["person"]) if here is not None else []
            key = manager_to_wake() if lines else None
            if key is not None:
                tell(key, manager.text_arrival(s["person"], s["session"], s["host"], lines))
                store.log_add("hub", "machine_in_use",
                              f"{s['person']} on {s['host']}: {'; '.join(lines)}"[:500])

    def close_if_off() -> None:
        """A hub started with the manager switched off closes a manager still running."""
        if mm.enabled() or ws.close_manager() is None:
            return
        store.log_add("hub", "manager", "closed: switched off")
        wake_bridges()

    def keep_manager(now: float) -> None:
        """Keep the manager session alive through the bridge of the hub's own OS user on this machine."""
        for r in mm.expire_requests(now):
            names = {p["id"]: p["name"] for p in store.people()}
            to = r["boss_key"] if ws.session_online(r["boss_key"], now) \
                else "web:" + names.get(r["person_id"], "")
            tell(to, f"no decision on your {r['project']} request within an hour; ask again")
        if not mm.enabled():
            return
        taken = mm.name_taken()
        if taken is not None:
            if mm.push_once("manager-name-taken", now, 86400):
                print(f"kaptyorka: a person named manager already exists with role {taken};"
                      " the manager cannot start", flush=True)
            return
        host = identity.facts()["host"]
        manager_files(host, now)
        admin = mm.admin_device(host)
        failed = mm.trust_failure()
        if failed is not None and mm.push_once(f"manager-untrusted:{failed!r}", now):
            mm.set_blocked("trust")  # relaunching cannot help; start --manager clears it
            if admin is not None:  # every admin's page: any of them may be the one at hand
                for name in admin_ops.admins(store):
                    tell(f"web:{name}", MANAGER_UNTRUSTED)
        try:  # its account at its usage limit: wait for the reset, never a crash (limits.py)
            if limits.manager_gate(store, mm, tell, admin_ops.admins(store), host, now):
                return
        except Exception as e:
            print(f"kaptyorka: manager usage limit check failed: {e}", flush=True)
        if admin is not None and mm.failures_last_hour(now) >= 2 \
                and mm.push_once("manager-failed", now, 3600):
            for name in admin_ops.admins(store):
                tell(f"web:{name}", MANAGER_FAILED)
        ok, _ = mm.may_launch(now)
        if not ok or admin is None:
            return
        boss = mm.boss(admin)
        if boss is None:
            return
        folder = paths.home() / manager.DIR
        ws.create_manager(boss, mm.person_id(), admin, str(folder),
                          manager.first_message(MANAGER_FIRST, mm.tasks()[0], mm.lessons()),
                          mm.model(), mm.effort())
        mm.record_launch(now)
        wake_bridges()

    def stuck_tick(now: float) -> None:
        """Stuck sessions (stuck.py). An episode is one silence (its start a steady hub time);
        its stages run from when the hub first saw it, one per tick, each once, and the state
        is kept in the store so a restart sends nothing again: the session itself is asked
        (not one in overnight mode: its bridge asks); a task session that writes after that
        has seen its task and is never asked about it again; one that stays silent TELL_S
        after the question is reported to its person, a worker's boss and the manager, and
        the manager hears it once more AGAIN_S later."""
        online = store.online()
        live = ws.live_keys()
        names = {p["id"]: p["name"] for p in store.people()}
        mid = mm.person_id()
        for a in online:
            st, acked = a.stuck_ep, set(a.stuck_acked)
            if a.tasks is not None:
                acked &= {t["id"] for t in a.tasks}  # a task that ended needs no answer kept
            # it wrote after the hub asked: it has seen its task, which is meant to run
            if st and st.get("kind") == "task" and a.wrote is not None \
                    and a.wrote > float(st.get("woke") or 0) + 1:
                acked.add(str(st.get("task")))
                st = None
            ep = stuck.of_agent(replace(a, stuck_acked=tuple(acked)), now)
            same = ep is not None and st is not None and st.get("kind") == ep["kind"] \
                and st.get("task") == ep.get("task", {}).get("id", "") \
                and abs(float(st.get("start") or 0) - float(ep["start"] or 0)) < stuck.STEADY_S
            if ep is None:
                st = None
            elif not same:  # first sight, however old the silence: only the question
                st = {"kind": ep["kind"], "task": ep.get("task", {}).get("id", ""),
                      "start": ep["start"], "woke": now, "told": None, "again": None}
                if a.key not in night["keys"]:  # overnight mode: its own bridge asks
                    tell(a.key, stuck.wake_text(ep))
            elif st.get("told") is None:
                if now - float(st["woke"]) >= stuck.TELL_S:
                    st = {**st, "told": now}
                    stuck_report(a, ep, now, float(st["woke"]), online, live, names, mid, False)
            elif st.get("again") is None and now - float(st["told"]) >= stuck.AGAIN_S:
                st = {**st, "again": now}
                stuck_report(a, ep, now, float(st["woke"]), online, live, names, mid, True)
            if st != a.stuck_ep or tuple(sorted(acked)) != tuple(sorted(a.stuck_acked)):
                store.stuck_save(a.key, st, acked)

    def stuck_report(a, ep: dict, now: float, woke: float, online, live, names, mid,
                     again: bool) -> None:
        """A wake the session did not answer: its person's online sessions (else their page)
        and a worker's boss hear it once, the manager twice (`again`: the second time)."""
        person, asked_s = names.get(a.person_id, ""), now - woke
        if not again:
            if a.person_id is not None and a.person_id == mid:
                to = [f"web:{n}" for n in admin_ops.admins(store)]  # the manager's own
            else:
                to = [b.key for b in online if b.person_id == a.person_id and b.key != a.key
                      and b.key not in live and a.person_id is not None] \
                    or ([f"web:{person}"] if person else [])
            boss = ws.boss_key(live[a.key]) if a.key in live else None
            if boss and boss != a.key and boss not in to and ws.session_online(boss, now):
                to.append(boss)
            text = stuck.people_text(a.name, a.machine, ep, a.key, a.background, asked_s)
            for k in to:
                tell(k, text)
            store.log_add("hub", "stuck", f"{a.name}: {ep['kind']} {stuck.span(ep['age'])}")
        if a.person_id is not None and a.person_id == mid:
            return  # the manager is not told about itself
        key = manager_to_wake()
        if key is None or key == a.key:
            return
        git = (ws.facts(a.device_id).get("git") or {}).get(a.cwd) \
            if a.device_id is not None and a.cwd else None
        tell(key, stuck.manager_text(a.name, a.machine, person, ep, a.key, a.background,
                                     asked_s, git, again=again))

    def machine_news(events: List[str], now: float) -> None:
        """Confirmed machine events: one dated line each in the team note `machines`, and one
        message each to the manager (none while it is switched off)."""
        if not events:
            return
        mev.write_note(events, now)
        key = manager_to_wake()
        for text in events:
            store.log_add("hub", "machine", text[:500])
            if key is not None:
                warn = sessions_to_warn(machine_events.event_hosts(text), now)
                tell(key, machine_events.manager_text(text, warn))

    def sessions_to_warn(hosts: List[str], now: float) -> List[str]:
        """The online sessions a machine event concerns: those on the machine, the bosses of
        its live workers, and those that talked with a session there in the last day (an
        offline machine's own sessions are gone, its partners are not). Never the manager."""
        if not hosts:
            return []
        mid = mm.person_id()
        marks = ",".join("?" * len(hosts))
        live = ",".join("?" * len(LIVE))
        with store.lock:
            there = {r["key"] for r in store.db.execute(
                f"SELECT key FROM agents WHERE machine IN ({marks}) OR host IN ({marks})",
                (*hosts, *hosts))}
            keys = set(there)
            keys |= {r["boss_key"] for r in store.db.execute(
                f"SELECT boss_key FROM workers WHERE host IN ({marks}) AND manager=0 AND state"
                f" IN ({live})", (*hosts, *LIVE))}
            if there:
                inner = ",".join("?" * len(there))
                for r in store.db.execute(
                        f"SELECT sender_key, to_key FROM messages WHERE created>? AND kind='message'"
                        f" AND (sender_key IN ({inner}) OR to_key IN ({inner}))",
                        (now - 86400, *there, *there)):
                    keys |= {r["sender_key"], r["to_key"] or ""}
            rows = store.db.execute(
                f"SELECT key, name, person_id FROM agents WHERE last_seen>? AND key IN"
                f" ({','.join('?' * len(keys)) or 'NULL'})", (now - ONLINE_S, *keys)).fetchall()
        return sorted({r["name"] for r in rows if r["person_id"] != mid or mid is None})

    def tick() -> None:
        """Move workers on; deliver what the bosses hear; keep the manager alive."""
        now = store.clock()
        for to_key, text in ws.tick(now):
            tell(to_key, text)
        if ws.ordered:  # a worker whose boss session ended is being closed
            wake_bridges()
        try:
            stuck_tick(now)
        except Exception as e:  # a stuck check must never stop the workers' clock
            print(f"kaptyorka: stuck check failed: {e}", flush=True)
        try:
            machine_news(mev.tick(now), now)
            mev.write_note(mev.tool_news(now), now)  # agent tool updates: the note only
        except Exception as e:  # nor may a machine event
            print(f"kaptyorka: machine events failed: {e}", flush=True)
        try:
            limits.tick(store, now)
        except Exception as e:
            print(f"kaptyorka: usage limits check failed: {e}", flush=True)
        try:
            left.tick(store, mm, ws.live_keys(), tell, now)
        except Exception as e:  # nor may the left-behind check
            print(f"kaptyorka: left sessions check failed: {e}", flush=True)
        try:
            manager_tick(now)
        except Exception as e:  # the manager must never stop the workers' clock
            print(f"kaptyorka: manager tick failed: {e}", flush=True)
        try:
            doors_tick(now)
        except Exception as e:
            print(f"kaptyorka: door requests failed: {e}", flush=True)
        try:
            resumes_tick(now)
        except Exception as e:
            print(f"kaptyorka: session resumes failed: {e}", flush=True)
        store.legacy_auto_off(now)
        try:  # a staged update that failed on the first machines: the admins and the manager
            for text in rollout.tick(store, __version__, now):
                store.log_add("hub", "rollout", text[:500])
                for name in admin_ops.admins(store):
                    tell(f"web:{name}", text)
                key = manager_to_wake()
                if key is not None:
                    tell(key, text)
        except Exception as e:  # nor may the rollout
            print(f"kaptyorka: rollout check failed: {e}", flush=True)
        try:  # a Claude Code newer than the tested one, the https certificate near its end
            for text in compat.version_news(store, now) + tls.expiry_news(store, now):
                store.log_add("hub", "compat", text[:500])
                for name in admin_ops.admins(store):
                    tell(f"web:{name}", text)
        except Exception as e:
            print(f"kaptyorka: compatibility news failed: {e}", flush=True)

    def manager_secret(principal: Principal, *texts: str) -> None:
        """The manager never stores or repeats a secret: a text it writes (a note, its task
        list, a lesson, a decision, a proposal, a message) that holds a known secret pattern, or
        a value someone sent it as a password or token in the last SECRET_DAYS, is refused."""
        if principal.role != "manager":
            return
        text = "\n".join(t for t in texts if t)
        try:
            check_secrets(text)
        except KaptyorkaError as e:
            raise KaptyorkaError(f"refused: {e}. {SECRET_RULE}") from None
        if not text.strip():
            return
        with store.lock:
            rows = store.db.execute(
                "SELECT m.text FROM messages m JOIN agents a ON a.key=m.to_key"
                " WHERE a.person_id=? AND m.created>?",
                (principal.person_id, store.clock() - SECRET_DAYS * 86400)).fetchall()
        for r in rows:
            for v in labelled_values(r["text"]):
                if v in text:
                    raise KaptyorkaError(f"refused: this holds a secret someone sent you"
                                         f" ({v[:3]}…). {SECRET_RULE}")

    def to_admins(text: str) -> None:
        """A line for every admin: their online sessions, else their page."""
        now = store.clock()
        for name in admin_ops.admins(store):
            with store.lock:
                keys = [r["key"] for r in store.db.execute(
                    "SELECT a.key FROM agents a JOIN people p ON p.id=a.person_id WHERE p.name=?"
                    " AND a.last_seen>? ORDER BY a.last_seen DESC", (name, now - ONLINE_S))]
            for key in keys or [f"web:{name}"]:
                tell(key, text)

    def doors_tick(now: float) -> None:
        """An hour-old door request reminds the admin once; the doors that opened by
        themselves (same project) reach the admin's page as one line, 5 minutes after the
        first, like the other routine notices."""
        for line in cs.due_reminders(now):
            to_admins(line)
        since = mm.stamp(AUTO_DOORS_KEY) or 0  # the last log row told (an id)
        rows = [r for r in store.log(200) if r["kind"] == "door_open" and r["id"] > since
                and ("(auto: same project" in r["detail"] or "(by manager for " in r["detail"])]
        if not rows or min(r["time"] for r in rows) > now - knowledge.LIGHT_WAIT_S:
            return
        mm.set_stamp(AUTO_DOORS_KEY, max(r["id"] for r in rows))
        listed = "; ".join(r["detail"] for r in reversed(rows))
        for name in admin_ops.admins(store):
            tell(f"web:{name}", f"Doors opened without you: {listed}. Turn these off with"
                                " kaptyorka auto-doors off or kaptyorka manager-doors off.")

    @server.tool()
    @_tool_errors
    async def send(to: str, text: str, ctx: Context, file: str = "") -> str:
        """Send text to one session by name or label, or to 'all' (the manager: also 'admins',
        every admin's online sessions, else their page). file: the local path of one file to
        attach (png jpg gif webp svg txt md log json csv pdf, up to 5 MB)."""
        file_id = None
        if file:
            m = re.fullmatch(r"id:(\d+)", file)
            if m is None:
                raise KaptyorkaError("attach a file by passing its local path; "
                                     "the kaptyorka tool uploads it")
            file_id = int(m.group(1))
        agent, principal = caller(ctx)
        manager_secret(principal, text)
        key, held = agent.key, []
        if to == "admins" and principal.role == "manager":  # every admin, wherever they are
            to_admins(f"from the manager: {text}")
            wake_bridges()
            return "sent to the admins (their online sessions, else their page)"
        n = ws.send_via_manager(key, to, text, mm.person_id(), file_id, held)
        if n is None:
            n = store.send(key, to, text, file_id, held)
        wake_bridges()
        wait = limits.waiting_line(store, mm, identity.facts()["host"], store.clock()) \
            if limits.to_manager(store, mm, to) else ""
        if held:
            return "; ".join(held_line(name, s) for name, s in held)
        return f"sent to {n} session" + ("" if n == 1 else "s") + (f"; {wait}" if wait else "")

    @server.tool()
    @_tool_errors
    async def knock(to: str, about: str, ctx: Context) -> str:
        """The admin only: ask another person's session (name or label), or the person by
        name, to open a door. Anyone else messages the manager instead."""
        kid = store.knock(me(ctx).key, to, about)
        wake_bridges()
        return f"knocked (#{kid}); waiting for an answer"

    @server.tool()
    @_tool_errors
    async def answer(knock_id: int, answer: str, ctx: Context) -> str:
        """The admin only: answer a knock with "yes" (opens a door) or "no"."""
        if answer not in ("yes", "no"):
            raise KaptyorkaError('answer must be "yes" or "no"')
        store.answer(knock_id, me(ctx).key, answer == "yes")
        wake_bridges()
        return "door open" if answer == "yes" else "knock declined"

    def _door_side(to: str, viewer: Principal, own: bool) -> str:
        """The session key `to` names (name, label or #id, seen in the last RECENT_S). `own`:
        only the viewer's sessions. A member names any other session only exactly, and an
        unknown or hidden one gets the same answer, so nothing is learned from it."""
        with store.lock:
            now = store.clock()
            rows = store.db.execute("SELECT * FROM agents WHERE last_seen>? ORDER BY last_seen"
                                    " DESC", (now - RECENT_S,)).fetchall()
            limited = viewer.role not in ("admin", "manager")
            if own:
                rows = [r for r in rows if r["person_id"] == viewer.person_id]
            elif limited:
                rows = [r for r in rows if r["person_id"] == viewer.person_id
                        or not (store._is_team(r["person_id"])
                                and store._hidden(r["person_id"], viewer.person_id))]
                rows = [r for r in rows if r["name"] == to or session_id(r["key"]) == to
                        or (r["person_id"] == viewer.person_id
                            and r["label"].lower() == to.lower() and r["label"])]
            try:
                match = store._named(rows, to, None if limited else viewer.person_id)
            except KaptyorkaError:
                match = []
        if len(match) != 1:
            raise KaptyorkaError(UNREACHABLE.format(to=to) if limited else
                                 f"no session named {to} seen in the last 10 minutes")
        return match[0]["key"]

    def _last_asker(manager_key: str) -> Optional[str]:
        """The session that last wrote to the manager (not the hub, not a page), within 10
        minutes."""
        with store.lock:
            row = store.db.execute(
                "SELECT sender_key FROM messages WHERE to_key=? AND sender_key NOT LIKE 'hub:%'"
                " AND sender_key NOT LIKE 'web:%' AND created>? ORDER BY id DESC LIMIT 1",
                (manager_key, store.clock() - RECENT_S)).fetchone()
        return row["sender_key"] if row else None

    @server.tool()
    @_tool_errors
    def reach(sender: str, target: str, ctx: Context) -> str:
        """Admin and manager only: may session `sender` message session `target` (names or
        #ids as in who), is the target online, and what became of the sender's last message to
        it. Ask this before you say anything about doors."""
        _, principal = caller(ctx)
        if not (principal.is_admin() or principal.role == "manager"):
            raise KaptyorkaError("reach is for the admin and the manager")
        def side(name: str) -> str:  # one gone longer than 10 minutes: by its exact name
            try:
                return _door_side(name, principal, own=False)
            except KaptyorkaError:
                with store.lock:
                    row = store.db.execute("SELECT key FROM agents WHERE name=? OR key=? ORDER"
                                           " BY last_seen DESC LIMIT 1", (name, name)).fetchone()
                if row is None:
                    raise
                return row["key"]
        s_key, t_key = side(sender), side(target)
        now = store.clock()
        with store.lock:
            s = store.db.execute("SELECT * FROM agents WHERE key=?", (s_key,)).fetchone()
            t = store.db.execute("SELECT * FROM agents WHERE key=?", (t_key,)).fetchone()
            may = store._may_message(s, t)
            last = store.db.execute(
                "SELECT created, push_state, push_error FROM messages WHERE sender_key=? AND"
                " to_key=? AND kind='message' ORDER BY id DESC LIMIT 1", (s_key, t_key)).fetchone()
        out = [f"{s['name']} {'may' if may else 'may not'} message {t['name']}"
               + (": no door needed." if may else ": that takes a door (door_request).")]
        away = now - t["last_seen"]
        out.append(f"{t['name']} is online." if away < ONLINE_S else
                   f"{t['name']} is offline, last seen {_ago(int(away))} ago; a message to it waits"
                   " and is delivered when it reports again.")
        if last is None:
            out.append(f"{s['name']} sent it no message.")
        else:
            state = {"pushed": "delivered", "failed": "not delivered: "
                     + paths.push_reason(last["push_error"] or "")}.get(
                last["push_state"], "waiting for delivery" + (" (the target is offline)"
                                                              if away >= ONLINE_S else ""))
            out.append(f"Its last message to it, {_ago(int(now - last['created']))} ago: {state}.")
        return " ".join(out)

    @server.tool()
    @_tool_errors
    def door_request(a: str, b: str, why: str, ctx: Context, asked_by: str = "",
                     size: str = "sessions") -> str:
        """A door between two sessions of different people (names or #ids as in who). The
        manager: for a teammate who asked it, a session of the person of a or b; it opens at
        once (asked_by: that session's name or #id, default the last session that wrote to
        you; size "sessions" or "people" for 7 days). Anything else waits for the admin. A
        member's session: for one of its own sessions (a: "" for this one); two sessions of one
        shared project get their door at once, the rest waits for the admin."""
        agent, principal = caller(ctx)
        if principal.legacy:
            raise KaptyorkaError(OWN_TOKEN)
        manager_asks = principal.role == "manager"
        a_key = agent.key if (not a and not manager_asks) else \
            _door_side(a, principal, own=not (manager_asks or principal.is_admin()))
        b_key = _door_side(b, principal, own=False)
        asker = None
        if manager_asks:
            asker = _door_side(asked_by, principal, own=False) if asked_by \
                else _last_asker(agent.key)
        out = cs.file(a_key, b_key, why, principal, agent.key, asker, size)
        wake_bridges()
        if out["kind"] == "auto":
            return (f"door open between {out['a']} and {out['b']}: same project"
                    f" {out['project']}; both sessions were told")
        if out["kind"] == "opened":
            return (f"door open between {out['a']} and {out['b']}"
                    + (" for the two people, 7 days" if out["size"] == "people" else "")
                    + f", for {out['asker']}; both sessions were told, the admin hears it later")
        if out["kind"] == "pending":
            return f"door request #{out['id']} for these two already waits for the admin"
        to_admins(out["line"])
        return (f"door request #{out['id']} filed; it waits for the admin, who allows or denies"
                " it. Both sides hear the answer.")

    def held_line(name: str, s: int) -> str:
        """A held send's answer; why the session stopped, when its hook said so."""
        with store.lock:
            r = store.db.execute("SELECT * FROM agents WHERE name=? ORDER BY last_seen DESC"
                                 " LIMIT 1", (name,)).fetchone()
            why = stopped_why(r) if r is not None else ""
        if not why or why.endswith(" is gone"):
            why = f"{name} was last seen {_ago(s)} ago"
        return f"held: {why}; it gets the message when it next reports"

    def stopped_rows(viewer: Principal, asker_pid: Optional[int]) -> List[dict]:
        """Lock held. Sessions gone within the last day that the manager may resume for this
        asker: on a team machine, or the asker's own."""
        now = store.clock()
        return [r for r in store.db.execute(
            "SELECT * FROM agents WHERE last_seen<=? AND last_seen>? AND device_id IS NOT NULL"
            " ORDER BY last_seen DESC", (now - ONLINE_S, now - 86400)).fetchall()
            if r["person_id"] is not None and (store._is_team(r["person_id"])
                                               or r["person_id"] == asker_pid)
            and r["tool"] in ("claude-code", "codex") and r["cwd"]]

    def stopped_why(r) -> str:
        """Lock held. Why it stopped, when its own hook said so at the end of its life."""
        if r["stopped"] and r["stopped_at"] and r["stopped_at"] >= r["last_seen"] - 120:
            error = "" if r["stopped"].startswith("end:") else r["stopped"]
            return session_end.line(r["name"], error, r["stopped"][4:] if not error else "",
                                    r["resets_at"])
        return f"{r['name']} is gone"

    def launch_resume(key: str, boss_key: str) -> str:
        """Resume the gone session `key` in its folder as a worker of `boss_key`."""
        with store.lock:
            r = store.db.execute("SELECT * FROM agents WHERE key=?", (key,)).fetchone()
            d = store.db.execute("SELECT * FROM devices WHERE id=? AND revoked_at IS NULL",
                                 (r["device_id"],)).fetchone() if r else None
            who = store._person_principal(r["person_id"]) if r else None
        if r is None or d is None or who is None:
            raise KaptyorkaError("that session's machine is gone")
        if d["bridge_seen"] is None or d["bridge_seen"] <= store.clock() - ONLINE_S:
            raise KaptyorkaError(f"the bridge on {d['host']} is offline; it cannot resume"
                                 f" {r['name']} now")
        device = {"device_id": d["id"], "host": d["host"], "os_user": d["os_user"],
                  "folder": r["cwd"]}
        w = ws.create(who, r["project"], f"continue where session {r['name']} stopped", device,
                      r["tool"], boss_key, key.split(":", 1)[1], False)
        wake_bridges()
        return f"resuming {r['name']} on {w['host']} as `{w['name']}`"

    @server.tool()
    @_tool_errors
    def resume_session(name: str, ctx: Context, asked_by: str = "", when: str = "now") -> str:
        """Manager only: resume a session that ended (its id, machine and folder from the hub),
        as a worker of the asking session (asked_by: its name or #id, default the last session
        that wrote to you). Only a session on a team machine or on the asker's own machines.
        when: "now", or "reset" to wait for its usage limit to reset."""
        agent, principal = caller(ctx)
        if principal.role != "manager":
            raise KaptyorkaError("only the manager resumes sessions")
        if when not in ("now", "reset"):
            raise KaptyorkaError('when is "now" or "reset"')
        boss = _door_side(asked_by, principal, own=False) if asked_by else _last_asker(agent.key)
        if boss is None:
            raise KaptyorkaError("say who asked, in asked_by (a session that wrote to you)")
        with store.lock:
            asker_pid = store.db.execute("SELECT person_id FROM agents WHERE key=?",
                                         (boss,)).fetchone()["person_id"]
            rows = stopped_rows(principal, asker_pid)
            match = store._named(rows, name, principal.person_id)
            gone = "; ".join(stopped_why(r) for r in rows[:8])
            why = stopped_why(match[0]) if match else ""
        if not match:
            raise KaptyorkaError(f"no ended session named {name} you may resume for this asker"
                                 + (f"; ended in the last day: {gone}" if gone else ""))
        r = match[0]
        if when == "reset":
            if not r["resets_at"]:
                raise KaptyorkaError(f"{why}; no reset time is known, resume it now or later")
            with store.lock:
                store.db.execute("UPDATE agents SET resume_at=?, resume_boss=? WHERE key=?",
                                 (r["resets_at"], boss, r["key"]))
            return f"{why}; it is resumed then, as a worker of the asker"
        return f"{why}; " + launch_resume(r["key"], boss)

    def resumes_tick(now: float) -> None:
        """Sessions the manager asked to resume at their usage limit's reset."""
        with store.lock:
            due = store.db.execute("SELECT key, resume_boss, name, last_seen FROM agents WHERE"
                                   " resume_at IS NOT NULL AND resume_at<=?", (now,)).fetchall()
            for r in due:
                store.db.execute("UPDATE agents SET resume_at=NULL WHERE key=?", (r["key"],))
        mkey = mm.session_key(now) if mm.enabled() else None
        for r in due:
            if r["last_seen"] > now - ONLINE_S:
                continue  # it is back by itself
            try:
                text = launch_resume(r["key"], r["resume_boss"])
            except KaptyorkaError as e:
                text = f"could not resume {r['name']} at its reset: {e}"
            for to in {r["resume_boss"], mkey} - {None}:
                tell(to, text)

    @server.tool()
    @_tool_errors
    def lesson(text: str, ctx: Context) -> str:
        """Manager only: record a correction a person gave you and you accept, as one short
        rule for yourself. Your next runs get the lessons in their first message."""
        _, principal = caller(ctx)
        if principal.role != "manager":
            raise KaptyorkaError("only the manager records lessons; the admin adds one with"
                                 " kaptyorka lesson")
        manager_secret(principal, text)
        n = mm.add_lesson(text, principal.person_name)
        return f"lesson saved ({n} in all); your next run starts with them"

    @server.custom_route("/api/lessons", methods=["GET", "POST"])
    @_bad_request
    async def api_lessons(request: Request) -> Response:
        """The admin reads or adds a manager lesson ({"text"})."""
        denied = admin_only(request)
        if denied:
            return denied
        if request.method == "GET":
            return PlainTextResponse(mm.lessons())
        body = await request.json()
        try:
            n = mm.add_lesson(str(body["text"]), request.scope["state"]["principal"].person_name)
        except KaptyorkaError as e:
            return PlainTextResponse(str(e), status_code=400)
        return JSONResponse({"lessons": n})

    @server.tool()
    @_tool_errors
    def door_decide(request_id: int, how: str, ctx: Context, reason: str = "") -> str:
        """Admin only: answer a door request. how: "sessions" (a door between the two
        sessions), "people" (between the two people, for 7 days) or "deny"."""
        _, principal = caller(ctx)
        if not principal.is_admin():
            raise KaptyorkaError("only the admin decides door requests")
        if how not in ("sessions", "people", "deny"):
            raise KaptyorkaError('how is "sessions", "people" or "deny"')
        out = cs.decide(request_id, principal, how != "deny", reason,
                        how if how != "deny" else "sessions")
        wake_bridges()
        return {"approved": f"door request #{request_id}: allowed ({how})",
                "denied": f"door request #{request_id}: denied",
                "offline": f"door request #{request_id}: a session was gone 10 minutes, so no"
                           " door was opened; the request is closed"}[out]

    @server.tool()
    @_tool_errors
    async def door_close(to: str, ctx: Context) -> str:
        """Close the door between this session and another one."""
        store.door_close(me(ctx).key, to)
        wake_bridges()
        return f"door to {to} closed"

    @server.tool()
    @_tool_errors
    def inbox(ctx: Context) -> List[dict]:
        """Your unread messages; reading marks them read."""
        return [{"from": m.sender, "text": m.text + attach_line(m.file), "time": m.created, "kind": m.kind,
                 "file": m.file, **({"reply_to": m.reply_to} if m.reply_to else {})}
                for m in store.inbox(me(ctx).key)]

    @server.tool()
    @_tool_errors
    def fetch_file(id: int, ctx: Context) -> Dict[str, Union[int, str]]:
        """Download a file attached to a message you sent or received (file id or message
        id, as shown in the message's [attached] line)."""
        _, principal = caller(ctx)
        try:
            info = store.file_meta(id, principal)
        except KaptyorkaError:
            fid = store.file_for_message(id, principal)
            if fid is None:
                raise
            info, id = store.file_meta(fid, principal), fid
        return {**info, "url": f"/api/files/{id}"}

    @server.tool()
    @_tool_errors
    def status(text: str, ctx: Context) -> str:
        """Say what you are working on; others see it in who()."""
        agent, principal = caller(ctx)
        store.set_status(agent.key, text)
        ms.status_signal(agent.key, text, session_project(agent, principal))
        return "status set"

    @server.tool()
    @_tool_errors
    def notes_list(ctx: Context, project: str = "") -> List[dict]:
        """Topics on the team board, newest first; with project, that project's notes."""
        _, principal = caller(ctx)
        return store.notes_list(principal, project or None)

    @server.tool()
    @_tool_errors
    def notes_read(topic: str, ctx: Context, project: str = "") -> Union[dict, str]:
        """Read the latest text of a note (a team note, or with project, that project's)."""
        _, principal = caller(ctx)
        note = store.notes_read(topic, principal, project or None)
        return asdict(note) if note else f"no note named {topic}"

    @server.tool()
    @_tool_errors
    def notes_write(topic: str, text: str, ctx: Context, project: str = "") -> str:
        """Write a new version of a note (older versions are kept); with project, that
        project's note."""
        agent, principal = caller(ctx)
        manager_secret(principal, text)
        project = project or None
        is_state = project is not None and topic.strip() == "state"
        if is_state:
            problem = check_state(text)
            if problem:
                raise KaptyorkaError(problem)
        store.notes_write(agent.key, topic, text, project)
        if is_state:
            ms.state_written(agent.key, project, principal.person_id, store.clock())
        return f"saved note {topic}"

    @server.tool()
    @_tool_errors
    def state(project: str, ctx: Context) -> str:
        """Read a project's state note (what it is, where it stands, decisions, open problems,
        how to run and test)."""
        _, principal = caller(ctx)
        if not project:
            raise KaptyorkaError("project is empty")
        note = store.notes_read("state", principal, project)
        if note is None:
            return (f"no state note yet for {project}; write one with state_write(project, text)"
                    " using the five headings")
        return note.text

    @server.tool()
    @_tool_errors
    def state_write(project: str, text: str, ctx: Context) -> str:
        """Update a project's state note. It needs the headings What it is, Where it stands,
        Decisions, Open problems, How to run and test, in that order. The manager's own task
        list, state_write("manager", text), takes any text."""
        agent, principal = caller(ctx)
        if not project:
            raise KaptyorkaError("project is empty")
        manager_secret(principal, text)
        # the manager's own task list (project `manager`, only it may write it) is free text
        problem = None if project == manager.PERSON and principal.role == "manager" \
            else check_state(text)
        if problem:
            raise KaptyorkaError(problem)
        store.notes_write(agent.key, "state", text, project)
        ms.state_written(agent.key, project, principal.person_id, store.clock())
        return f"state of {project} updated"

    @server.tool()
    @_tool_errors
    def decide(project: str, text: str, ctx: Context) -> str:
        """Record one decision as a dated line in the project's decisions note (project ""
        is the team note)."""
        agent, principal = caller(ctx)
        line = clean(text, 500)
        if not line:
            raise KaptyorkaError("text is empty")
        manager_secret(principal, line)
        day = datetime.fromtimestamp(store.clock(), timezone.utc).strftime("%Y-%m-%d")
        entry = f"- {day} {principal.person_name}: {line}"
        with decide_lock:  # read then write must not interleave
            old = store.notes_read("decisions", principal, project or None)
            body = (old.text.rstrip("\n") + "\n" if old and old.text.strip() else "") + entry + "\n"
            store.notes_write(agent.key, "decisions", body, project or None)
        return "decision recorded"

    @server.tool()
    @_tool_errors
    def projects(ctx: Context) -> List[dict]:
        """Projects you have a role in: name, description, repo, your role, sessions in it now."""
        _, principal = caller(ctx)
        return store.projects_for(principal)

    @server.tool()
    @_tool_errors
    def request_access(kind: str, target: str, ctx: Context, why: str = "") -> str:
        """Ask the admin for read access to a project (kind "project") or for use of a machine
        (kind "machine"), with a short reason. To reach another person's session, message the
        manager instead."""
        agent, principal = caller(ctx)
        if kind == "session":
            raise KaptyorkaError(ASK_MANAGER)
        rid = store.request_access(principal.person_id, kind, target, why)
        return f"requested #{rid}; the admin sees it on the page"

    def foreign(principal: Principal, person_id: Optional[int]) -> Optional[int]:
        """The person behind a session on a shared device that is not the device's owner (a
        worker the bridge there started for someone else); None for the owner's own."""
        if principal.shared and person_id is not None and person_id != principal.person_id:
            return person_id
        return None

    def chained(agent, principal: Principal) -> bool:
        """True for a worker's session on a shared account (a team machine's too): it may not
        start, adopt or close workers; its boss does. Another person's session there, or a
        worker whose boss is the team machine itself."""
        return foreign(principal, agent.person_id) is not None or (
            principal.shared and agent.key in ws.live_keys())

    def page_foreign(request: Request, principal: Principal) -> Optional[int]:
        key = unquote(request.headers.get("x-kaptyorka-key") or "")
        return foreign(principal, ws.session_person(key)) if key else None

    def own_workers(principal: Principal, other: Optional[int] = None) -> List[dict]:
        if other is not None:  # a session of another person on a shared device: theirs only
            return ws.list(replace(principal, person_id=other, role="member", hub_key=False))
        # the shared token is no admin here: anyone may hold it
        return ws.list(replace(principal, role="member") if principal.legacy else principal)

    def no_machine(project: str, machine: Optional[str], tool: str, usable: List[dict],
                   reasons: List[str]) -> str:
        if machine is not None and not usable and not reasons:
            reasons = [f"no machine named {machine} that you may use; check the name on the"
                       " page's Machines list, or ask an admin"]
        for c in usable:
            who = f"{c['host']} ({c['os_user']})"
            if not c.get("folder") and c.get("team") and not c.get("team_role"):
                reasons.append(f"{who}: no folder for {project}; its bridge looks for"
                               f" ~/{project} once the team machine has a role there:"
                               f" `kaptyorka grant {c['team']} {project}:write`, or open a"
                               f" session in the {project} folder there once")
            elif not c.get("folder"):
                reasons.append(f"{who}: no folder for {project}; clone it to ~/{project}"
                               " there, or open a session in its folder there once")
            elif tool != "claude-code" and not c.get("tmux"):
                reasons.append(f"{who}: no tmux (only Claude Code workers can run there)")
        return f"no machine can take {project}: " + ("; ".join(reasons)
                                                     or "no machine you may use is joined")

    def manager_to_wake() -> Optional[str]:
        """The manager's session for a notice; between runs its last one (the new session
        gets it)."""
        if not mm.enabled():
            return None
        row = ws.by_name(manager.WORKER)
        between = mm.retired() or (row is not None and row["state"] == "starting")
        return mm.session_key(store.clock()) or (mm.last_key() if between else None)

    def in_use(host: str, me: str) -> "tuple[List[str], List[str], List[str], bool]":
        """(the other people using host heavily, one line of use each for the manager, the
        other people with a session there, whether the use report is fresh). The manager
        account is nobody; `me`'s own accounts never count."""
        now = store.clock()
        here = ws.host_use(host, now)
        use, by_person = here if here is not None else ({}, {})
        present = [n for n in ws.host_people(host, now) if n != me]
        heavy = [n for n, p in by_person.items()
                 if me not in p["people"] and usage.heavy(p, use)]
        busy = ws.queue_busy(host, now, me)  # every share held, taken whole, or a job waits
        queue = None
        if busy is not None:  # the person hears counts only; the manager's line has names
            counts = usage.queue_line(busy[1], False).replace("GPU queue: ", "")
            heavy.append(f"the GPU queue ({counts})")
            queue = f"{busy[0]} (names for admins only; to anyone else say: {counts})"
        who = present + [n for n in heavy if n not in present]
        if not who:
            return [], [], [], here is not None
        if here is None:
            return heavy, ["no fresh use report from this machine"] + ([queue] if queue else []), \
                present, queue is not None
        named = {n for n in who if n in by_person}
        for n, p in by_person.items():  # a shared account's label holds the person's name
            if n not in named and any(x in p["people"] for x in present):
                named.add(n)
        lines = [usage.describe(n, by_person[n], use) for n in sorted(
            named, key=lambda n: (-len(usage.held(by_person[n])), -by_person[n]["cpu"], n))]
        if queue is not None:
            lines.append(queue)
        return heavy, lines or ["no notable use by them"], present, True

    @server.tool()
    @_tool_errors
    async def work(project: str, task: str, ctx: Context, machine: str = "",
                   tool: str = "claude-code", resume: str = "") -> str:
        """Start a worker session for you on another machine: in project's folder there, with
        task as its first message. machine: one host by name; tool: claude-code (default),
        codex or opencode; resume: the full session id to continue."""
        agent, principal = caller(ctx)
        if principal.legacy:
            raise KaptyorkaError(OWN_TOKEN)
        if chained(agent, principal):
            raise KaptyorkaError(ON_SHARED)
        if principal.role == "manager":
            raise KaptyorkaError(MANAGER_NO_WORK)
        if not project.strip() or project == paths.COORDINATOR:
            raise KaptyorkaError(NAME_A_PROJECT)
        if ws.archived(project):
            raise KaptyorkaError(REMOVED.format(project=project))
        if not (store.may_write_project(project, principal)
                or (agent.project == project and not ws.registered(project))):
            if store.is_team(principal.person_id):  # a session on a team machine asks
                raise KaptyorkaError(
                    f"{principal.person_name} is a team machine without the write role in"
                    f" {project}; an admin runs `kaptyorka grant {principal.person_name}"
                    f" {project}:write`")
            raise KaptyorkaError(f"you may not start workers in {project}: that takes the write"
                                 " role (or your own unregistered folder)")
        tool = clean(tool, MAX_IDENT) or "claude-code"
        usable, reasons = ws.candidates(principal, project, machine or None)
        usable = limits.skip_limited(store, usable, reasons, tool, store.clock())
        chosen = ws.choose(usable, project, tool, store.clock(), principal.person_id)
        if chosen is None:
            return no_machine(project, machine or None, tool, usable, reasons)
        heavy, load, present, fresh = in_use(chosen["host"], principal.person_name)
        if chosen["occupied"]:
            others = [n for n in ws.folder_people(chosen["host"], chosen["os_user"],
                                                  chosen["folder"], store.clock())
                      if n and n != principal.person_name]
            manager_key = manager_to_wake() if others else None
            if manager_key is not None:
                rid = mm.request_add(principal.person_id, agent.key, project, task, tool,
                                     resume or None, chosen["host"], others[0],
                                     chosen["folder"])
                tell(manager_key, manager.text_busy(principal.person_name, project,
                                                    chosen["host"], others[0],
                                                    chosen["folder"], rid, load,
                                                    ws.live_on(chosen["host"])))
                return (f"busy: {chosen['host']} has {others[0]}'s session in that folder; the"
                        f" manager is asking them (request {rid}); you will hear back here")
            if others:  # no manager online: the asker picks again
                return (f"busy: {chosen['host']} has {others[0]}'s session in that folder;"
                        " ask again later or name another machine")
        if heavy:  # someone else uses much of it: the manager decides; light use starts
            manager_key = manager_to_wake()
            if manager_key is not None:  # without a manager it starts, as before
                rid = mm.request_add(principal.person_id, agent.key, project, task, tool,
                                     resume or None, chosen["host"], heavy[0], chosen["folder"])
                tell(manager_key, manager.text_in_use(principal.person_name, project,
                                                      chosen["host"], heavy, load, rid,
                                                      ws.live_on(chosen["host"])))
                return (f"busy: {chosen['host']} is in use by {heavy[0]}; the manager is"
                        f" deciding (request {rid}); you will hear back here")
        w = ws.create(principal, project, task, chosen, tool, agent.key, resume or None,
                      chosen["occupied"])
        wake_bridges()
        if present and not fresh:  # could not check for heavy use: the manager hears of it
            manager_key = manager_to_wake()
            if manager_key is not None:
                tell(manager_key, manager.text_unchecked(principal.person_name, project,
                                                         w["host"], present))
        return (f"starting `{w['name']}` on {w['host']} ({w['os_user']})"
                + (" in a worktree, the folder is busy" if w["worktree"] else ""))

    @server.tool()
    @_tool_errors
    def work_decide(request_id: int, machine: str, reason: str, ctx: Context) -> str:
        """Manager only: decide a work request. machine: a host to start the worker on,
        "wait" or "no". reason: why, 1 to 500 characters. A request decided "wait" may be
        decided again within an hour of it; a start or a no is final."""
        _, principal = caller(ctx)
        if principal.role != "manager":
            raise KaptyorkaError("only the manager decides work requests")
        if not mm.enabled():
            raise KaptyorkaError("the manager is switched off")
        reason = clean(reason, 500)
        if not reason:
            raise KaptyorkaError("a decision needs a reason")
        with work_decide_lock:
            return decide_request(request_id, machine, reason, principal)

    def decide_request(request_id: int, machine: str, reason: str, principal: Principal) -> str:
        """work_decide's body, under work_decide_lock."""
        r = mm.request(request_id)
        if r is None:
            raise KaptyorkaError(f"no request {request_id}")
        now = store.clock()
        if r["decided_at"] is not None and r["decision"] != "wait":
            raise KaptyorkaError(f"request {request_id} is already decided: {r['decision']}")
        if r["decided_at"] is not None and r["created"] <= now - manager.REQUEST_TTL_S:
            raise KaptyorkaError(f"request {request_id} waited over an hour and is closed; the"
                                 " asker files a new one with work")
        live_line = manager.live_text(r["wanted"], ws.live_on(r["wanted"]))
        stale = ws.named_not_live(reason)
        if stale:  # it decides from a worker it remembers, not from the hub's live list
            named = ", ".join(f"`{n}` ({s})" for n, s in stale)
            raise KaptyorkaError(f"not decided: your reason leans on {named}, which is not live"
                                 f" now. {live_line} Decide again from that list; if you meant"
                                 " that it ended, say so in the reason.")
        names = {p["id"]: p["name"] for p in store.people()}
        page = "web:" + names.get(r["person_id"], "")
        online = ws.session_online(r["boss_key"], now)
        asker = r["boss_key"] if online else page
        project = r["project"]
        if machine == "wait":
            mm.request_decide(request_id, "wait", reason)
            tell(asker, f"the manager says wait on {project}: {reason}")
            store.log_add("manager", "work_decide", f"request {request_id}: wait — {reason}")
            # what to watch before deciding it again
            return f"request {request_id}: told them to wait. {live_line}"
        if machine == "no":
            mm.request_decide(request_id, "no", reason)
            tell(asker, f"the manager says no to {project} for now: {reason}")
            store.log_add("manager", "work_decide", f"request {request_id}: no — {reason}")
            return f"request {request_id}: told them no"
        row = next((p for p in store.people() if p["id"] == r["person_id"]), None)
        boss = store.agent(r["boss_key"])
        who = replace(principal, person_id=r["person_id"], person_name=row["name"] if row else "",
                      role=row["role"] if row else "member", hub_key=False)  # the asker's rights, not the manager's
        if ws.archived(project):
            mm.request_decide(request_id, "refused", reason)
            note = REMOVED.format(project=project)
            tell(asker, note)
            store.log_add("manager", "work_decide", f"request {request_id}: refused — {reason}")
            return note
        if not (store.may_write_project(project, who)
                or (boss is not None and boss.project == project and not ws.registered(project))):
            mm.request_decide(request_id, "refused", reason)
            note = f"the manager could not start {project} for you: you no longer have the write role"
            tell(asker, note)
            store.log_add("manager", "work_decide", f"request {request_id}: refused — {reason}")
            return note
        usable, reasons = ws.candidates_for(r["person_id"], project, machine)
        usable = limits.skip_limited(store, usable, reasons, r["tool"], now)
        chosen = ws.choose(usable, project, r["tool"], now, r["person_id"])
        if chosen is None:
            raise KaptyorkaError(no_machine(project, machine, r["tool"], usable, reasons))
        if not online:
            mm.request_decide(request_id, "asker gone", reason)
            tell(page, f"the manager picked {machine} for {project} but your session is gone;"
                        " ask again")
            store.log_add("manager", "work_decide",
                          f"request {request_id}: asker gone — {reason}")
            return f"request {request_id}: the asker's session is gone; told them on the page"
        w = ws.create(who, project, r["task"], chosen,
                      r["tool"], r["boss_key"], r["resume"], chosen["occupied"])
        mm.request_decide(request_id, f"start {w['host']}", reason, w["name"])
        wake_bridges()
        tell(asker, f"the manager started `{w['name']}` on {w['host']} for {project}:"
                            f" {reason}")
        store.log_add("manager", "work_decide",
                      f"request {request_id}: start {w['host']} — {reason}")
        return f"started `{w['name']}` on {w['host']} for request {request_id}"

    @server.tool()
    @_tool_errors
    def workers(ctx: Context) -> List[dict]:
        """Your workers (the admin: everyone's): name, project, host, state, attach line."""
        agent, principal = caller(ctx)
        return own_workers(principal, foreign(principal, agent.person_id))

    @server.tool()
    @_tool_errors
    async def adopt(name: str, ctx: Context) -> str:
        """Become the boss of one of your unattended workers; its held messages come to you."""
        agent, principal = caller(ctx)
        if principal.legacy:
            raise KaptyorkaError(OWN_TOKEN)
        if chained(agent, principal):
            raise KaptyorkaError(ON_SHARED)
        w = ws.adopt(principal, name, agent.key)
        wake_bridges()
        return f"you are now the boss of `{name}`; {w['delivered']} held messages delivered"

    @server.tool()
    @_tool_errors
    async def close_worker(name: str, ctx: Context, reason: str = "") -> str:
        """End one of your workers (its session on that machine is stopped). A worker calls it
        with its own name when its task is done and reported: its boss hears what it left.
        The manager may close a worker started on a request it decided; reason: who asked."""
        agent, principal = caller(ctx)
        if principal.role == "manager" and not (clean(reason, 300)):
            raise KaptyorkaError("say who asked, or why, in reason")
        if principal.legacy:
            raise KaptyorkaError(OWN_TOKEN)
        mine = ws.by_key(agent.key)
        if mine is not None and mine["name"] == name and mine["state"] in LIVE:
            w = ws.close_done(name, agent.key)  # the worker's own done signal
            wake_bridges()
            return (f"closing `{name}`: your task is done; your commits, branch and files stay"
                    + (f"; {w['note']}" if w.get("note") else ""))
        if chained(agent, principal):
            raise KaptyorkaError(ON_SHARED)
        w = ws.close(principal, name)
        if principal.role == "manager":
            store.log_add("manager", "worker_close", f"{name}: {clean(reason, 300)}")
            boss = ws.boss_key(name)  # its boss hears who closed it and why
            if boss:
                tell(boss, f"the manager closed worker `{name}`: {clean(reason, 300)}")
        off = ""
        if principal.is_admin() and w.get("manager"):  # what the admin closed must not be relaunched by the tick
            mm.set_enabled(False, principal.person_name)
            off = "; the manager is switched off until kaptyorka start --manager"
        wake_bridges()
        return f"closing `{name}`" + (f"; {w['note']}" if w.get("note") else "") + off

    @server.tool()
    @_tool_errors
    def rules(ctx: Context) -> str:
        """The team rules, with their version."""
        caller(ctx)
        cur = rs.current()
        if not cur["text"]:
            return "No team rules yet."
        return f"Team rules v{cur['version']}:\n" + rules_mod.rules_text(cur["text"])

    @server.tool()
    @_tool_errors
    def log(ctx: Context, limit: int = 200) -> List[dict]:
        """The team's activity log, newest first (admin and manager only)."""
        _, principal = caller(ctx)
        if not (principal.is_admin() or principal.role == "manager"):
            raise KaptyorkaError("the log is for the admin and the manager")
        return store.log(max(1, min(limit, 1000)))

    @server.tool()
    @_tool_errors
    def machines(ctx: Context) -> List[dict]:
        """Each machine as its bridge last reported it (admin and manager only): host, online,
        size, use, free disk space ("disk: / 212 of 468 GB free, ...")."""
        _, principal = caller(ctx)
        if not (principal.is_admin() or principal.role == "manager"):
            raise KaptyorkaError("the machines list is for the admin and the manager")
        at = limits.by_host(store, store.clock())
        return [{"host": h["host"], "online": h["online"], "team": bool(h["team"]),
                 "kind": h["kind"], "use": h["use"], "disk": h["disk"],
                 **({"limits": at[h["host"]]} if h["host"] in at else {})} for h in ws.overview()]

    @server.tool()
    @_tool_errors
    def propose_rule(text: str, evidence: str, ctx: Context) -> str:
        """Suggest a team rule when your person corrected you the same way twice. Say what
        the rule is and what happened (evidence). The admin decides."""
        agent, principal = caller(ctx)
        if principal.legacy:
            raise KaptyorkaError(OWN_TOKEN)
        manager_secret(principal, text, evidence)
        pid = rs.propose(principal, agent.key, text, evidence)
        return f"Proposal {pid} filed; the admin decides on the Rules tab."

    @server.tool()
    @_tool_errors
    def conflict(rule_a: str, rule_b: str, ctx: Context) -> str:
        """Report two rules that clash, when you had to ask your person which one wins."""
        agent, principal = caller(ctx)
        if principal.legacy:
            raise KaptyorkaError(OWN_TOKEN)
        cid = rs.conflict(principal, agent.key, session_project(agent, principal), rule_a, rule_b)
        return f"Conflict {cid} logged for the admin."

    @server.custom_route("/bridge/heartbeat", methods=["POST"])
    @_bad_request
    async def bridge_heartbeat(request: Request) -> Response:
        body = await request.json()
        facts = body.get("facts")
        if isinstance(facts, dict):  # they go on ssh lines: never one that reads as an option
            facts = {**facts, **{k: safe_name(facts[k]) for k in ("host", "os_user")
                                 if k in facts}}
        principal = request.scope.get("state", {}).get("principal")
        if principal is not None and principal.legacy:
            # the machine is only in the body, which Auth does not read: map it to its person
            principal = store.authenticate(bearer(request.headers),
                                           machine=str(body["machine"])) or principal
        # an older or changed bridge may still report sessions of personal folders: dropped
        body["sessions"] = [s for s in body["sessions"]
                            if on_hub(str(s.get("project") or ""), str(s.get("key") or ""))
                            or (s.get("project") == paths.COORDINATOR and principal is not None
                                and not principal.legacy)]
        for s in body["sessions"]:
            if s.get("project") == paths.COORDINATOR:
                s["cwd"] = ""  # a coordinator has no folder on the hub
        kept = {s.get("cwd") for s in body["sessions"] if s.get("cwd")}
        if isinstance(body.get("folders"), dict):
            body["folders"] = {p: f for p, f in body["folders"].items()
                               if isinstance(p, str) and isinstance(f, str)
                               and store.team_project(p)}
            kept |= set(body["folders"].values())
        if isinstance(facts, dict) and isinstance(facts.get("git"), dict):
            facts = {**facts, "git": {c: g for c, g in facts["git"].items() if c in kept or (
                isinstance(c, str) and store.team_project(c.rstrip("/").rsplit("/", 1)[-1]))}}
        # the bridge sends tools and facts every time; a shim's heartbeat has no tools, and a
        # /hub off or leave's one-off drop no facts: those never make the machine online
        bridge = body.get("bridge") is True or (
            isinstance(body.get("tools"), list) and isinstance(facts, dict))
        store.heartbeat(body["machine"], body["sessions"], body.get("tools", ()),
                        principal=principal, facts=facts if isinstance(facts, dict) else None,
                        gone=body.get("gone", ()), bridge=bridge)
        if principal is None:
            return Response(status_code=204)
        own = not principal.legacy
        if own:  # which boss sessions ended, which workers are in overnight mode
            tools = body.get("tools")
            ws.bridge_seen(principal.device_id, [str(s.get("key")) for s in body["sessions"]],
                           [t for t in tools if isinstance(t, str)] if isinstance(tools, list)
                           else [], body.get("overnight"))
            if isinstance(body.get("overnight"), list):
                night["by_device"][principal.device_id] = {str(k) for k in body["overnight"]}
                night["keys"] = set().union(*night["by_device"].values())
        if own and isinstance(facts, dict):  # this account's usage limits (limits.py)
            limits.from_facts(store, str(facts.get("host") or ""), str(facts.get("os_user") or ""),
                              facts.get("limits"), store.clock())
        if own and body.get("closed"):  # sessions it closed for us (left.py)
            left.closed(store, principal.device_id, body["closed"], principal.person_name)
        if own and (isinstance(facts, dict) or isinstance(body.get("folders"), dict)):
            ws.facts_seen(principal.device_id, facts, body.get("folders"))
            try:  # history is a bonus: a bad git payload must not fail the heartbeat
                ms.git_seen(principal, principal.device_id,
                            clean_facts(facts).get("git") or {},
                            body.get("folders") if isinstance(body.get("folders"), dict) else {})
            except Exception as e:
                print(f"memory: git facts skipped: {e}", file=sys.stderr, flush=True)
        if own and isinstance(facts, dict):
            try:  # machine events are a bonus: they must not fail the heartbeat
                machine_news(mev.observe(principal.device_id, clean_facts(facts), store.clock()),
                             store.clock())
            except Exception as e:
                print(f"kaptyorka: machine events failed: {e}", file=sys.stderr, flush=True)
        # left behind (left.py): its bridge checks each again before it closes it
        close = left.close_keys(store, principal.device_id, ws.live_keys(), mm.person_id(),
                                store.clock()) if own else []
        announce = tls.announcement(device_signing(principal.device_id),
                                    request.headers.get("host") or request.url.netloc, store.clock())
        return JSONResponse({"version": __version__,
                             **({"close_left": close} if close else {}),
                             **({"tls": announce} if announce else {}),  # tls.py: signed
                             "auto_update": own and ws.auto_update(principal.device_id)
                             and rollout.allows(store, principal.device_id, __version__),  # staged
                             "projects": [] if principal.legacy else
                             [p["name"] for p in store.projects_for(principal)],
                             "team": store.team_projects(),  # the bridge reports only these
                             "repos": store.team_repos(),  # and folders whose remote is one
                             "keep": ws.live_keys_on(principal.device_id) if own else [],
                             "you": principal.person_name, "shared": principal.shared,
                             "legacy": principal.legacy,  # a 0.1 token: keep the machine name
                             "skills_version": rs.skills_version(),
                             # its computer's GPU queue, read by one bridge per computer
                             "gpu_queue": ws.gpu_queue_ask(principal.device_id, store.clock())
                             if own else None,
                             # "team" or "hub": send the machine facts its events need
                             "machine_facts": mev.scope(principal.device_id) if own else None})

    @server.custom_route("/bridge/join-team", methods=["POST"])
    @_bad_request
    async def bridge_join_team(request: Request) -> Response:
        """/hub team [project] asks first: a project needs the person's role in it (an admin
        has every one) and the project registered; no project (a coordinator) needs a person."""
        body = await request.json()
        project = str(body.get("project") or "").strip()
        principal = request.scope["state"]["principal"]
        if principal.legacy:
            return PlainTextResponse(OWN_TOKEN, status_code=403)
        mine = team_projects_of(principal)
        if project and project not in mine:
            listed = ", ".join(mine) if mine else "none yet; the admin gives roles"
            why = (f"{project} is not a team project" if not store.team_project(project)
                   or paths.team_name(project) != project
                   else f"you have no role in {project}")
            return PlainTextResponse(f"{why}; your projects: {listed}", status_code=403)
        return JSONResponse({"project": project, "projects": mine})

    @server.custom_route("/bridge/poll", methods=["GET"])
    @_bad_request
    async def bridge_poll(request: Request) -> Response:
        machine = request.query_params["machine"]
        wait = float(request.query_params.get("wait", 0))
        if not math.isfinite(wait):
            raise ValueError("wait must be a finite number")
        wait = min(max(wait, 0), MAX_WAIT_S)
        event = sent["event"]  # taken before offer, so a send after offer still wakes us
        principal = request.scope["state"]["principal"]
        version = store.bridge_version(principal, machine)

        def offer():
            return store.offer(machine, version, principal) + ws.offer(principal.device_id)
        items = offer()
        if not items and wait:
            try:
                await asyncio.wait_for(event.wait(), wait)
                items = offer()
            except asyncio.TimeoutError:
                pass
        return JSONResponse(items)

    @server.custom_route("/bridge/ack", methods=["POST"])
    @_bad_request
    async def bridge_ack(request: Request) -> Response:
        body = await request.json()
        principal = request.scope["state"]["principal"]
        if body.get("launch") is True:
            notices: list = []
            if not ws.ack(int(body["id"]), body["ok"] is True, str(body.get("error") or ""),
                          principal.device_id, body.get("left"), notices):
                return PlainTextResponse("no such launch", status_code=404)
            for to_key, text in notices:  # an auto-closed worker: what it left
                tell(to_key, text)
            return Response(status_code=204)
        if not store.ack(int(body["id"]), body["ok"] is True, principal,
                         str(body.get("error") or "")):
            return PlainTextResponse("no such message", status_code=404)
        return Response(status_code=204)

    @server.custom_route("/bridge/worker", methods=["POST"])
    @_bad_request
    async def bridge_worker(request: Request) -> Response:
        """The bridge started a worker (its session key) or could not (the error)."""
        body = await request.json()
        device_id = request.scope["state"]["principal"].device_id
        name = str(body["worker"])
        def refused() -> Response:  # 410: stop that session; 404: leave it alone
            if ws.state(name, device_id) in ("closed", "failed"):
                return PlainTextResponse("that worker is closed", status_code=410)
            return PlainTextResponse("no such starting worker here", status_code=404)

        if "key" not in body:
            if not ws.fail(name, str(body["error"]), device_id):
                return refused()
            tick()  # the boss hears it now
            return Response(status_code=204)
        was = ws.state(name, device_id)
        if not ws.link(name, str(body["key"]), device_id, body.get("needs_brief") is True):
            return refused()
        if was != "starting":
            return Response(status_code=204)  # a retried link: the boss heard it already
        w = ws.by_name(name)
        tell(ws.boss_key(name), f"worker `{name}` is up on {w['host']}; attach with"
                                f" `{w['attach']}`")
        return Response(status_code=204)

    @server.custom_route("/bridge/rekeyed", methods=["POST"])
    @_bad_request
    async def bridge_rekeyed(request: Request) -> Response:
        body = await request.json()
        if not store.rekeyed(str(body["machine"]), request.scope["state"]["principal"]):
            return PlainTextResponse("not this machine's new device", status_code=403)
        return Response(status_code=204)

    @server.custom_route("/api/brief", methods=["GET"])
    async def api_brief(request: Request) -> Response:
        try:
            agent = identify(request.headers)
        except KaptyorkaError as e:
            return PlainTextResponse(str(e), status_code=400)
        principal = request.scope.get("state", {}).get("principal")
        if request.query_params.get("event") != "start":
            parts = [message_text(m) for m in store.inbox(agent.key)]
            if request.query_params.get("event") == "prompt" and agent.tool == "opencode":
                parts.insert(0, opencode_reminder(ms, agent.key, principal, store.clock(),
                                                  session_project(agent, principal)))
            return PlainTextResponse("\n\n".join(p for p in parts if p))
        return PlainTextResponse(brief.cached(
            agent.key, lambda: brief.compose(
                store, ws, ms, agent, principal,
                rules_text=rules_mod.rules_text(rs.current()["text"])),
            unread=lambda: brief.unread_count(store, agent.key), now=store.clock(),
            cache=brief_cache))

    @server.custom_route("/api/overnight", methods=["POST"])
    @_bad_request
    async def api_overnight(request: Request) -> Response:
        """A team session's overnight mode: the morning report goes to that person's page only;
        a rule clash is a short fixed-text log event the manager passes on to them once."""
        try:
            principal = principal_of(request.headers)
            agent = identify(request.headers, principal)
        except KaptyorkaError as e:
            return PlainTextResponse(str(e), status_code=400)
        if principal.legacy or principal.person_id is None:
            return PlainTextResponse(OWN_TOKEN, status_code=400)
        body = await request.json()
        project = session_project(agent, principal)
        person = principal.person_name
        if body.get("kind") == "report":
            lines = [ln for ln in str(body.get("text", ""))[:overnight_mod.REPORT_CAP].splitlines()
                     if not any(p.search(ln) for _, p in SECRET_PATTERNS)]
            tell(f"web:{person}", "\n".join(lines))
            store.log_add(person, "overnight", f"overnight report in {project}")
            return JSONResponse({"ok": True})
        if body.get("kind") == "clash":
            detail = overnight_mod.clash_detail(str(body.get("scope", "")), str(body.get("rule", "")),
                                                str(body.get("class", "")), project)
            store.log_add(person, overnight_mod.CLASH_KIND, detail)
            return JSONResponse({"ok": True})
        return PlainTextResponse("kind must be report or clash", status_code=400)

    @server.custom_route("/api/session-event", methods=["POST"])
    @_bad_request
    async def api_session_event(request: Request) -> Response:
        """A team session's own hook says why it stopped (session_end.py): a usage limit with
        its reset time, an API error, or its end. Kept on its agents row; never marks it seen.
        A team machine's session that others depend on: the manager hears it in one line."""
        try:
            principal = principal_of(request.headers)
        except KaptyorkaError as e:
            return PlainTextResponse(str(e), status_code=400)
        key = unquote(request.headers.get("x-kaptyorka-key") or "")
        body = await request.json()
        kind = body.get("kind")
        if kind not in ("failure", "end"):
            return PlainTextResponse("kind must be failure or end", status_code=400)
        error = clean(str(body.get("error") or ""), 40) if kind == "failure" else ""
        reason = "" if kind == "failure" else clean(str(body.get("reason") or "other"), 40)
        resets = body.get("resets_at") if error == "rate_limit" else None
        now = store.clock()
        if not isinstance(resets, (int, float)) or not now < resets < now + 7 * 86400:
            resets = None
        with store.lock:
            row = store.db.execute("SELECT * FROM agents WHERE key=?", (key,)).fetchone()
            if row is None or row["person_id"] != principal.person_id:
                return PlainTextResponse("no such session", status_code=400)
            store.db.execute("UPDATE agents SET stopped=?, stopped_at=?, resets_at=? WHERE key=?",
                             (error or f"end:{reason}", now, resets, key))
            team = store._is_team(row["person_id"])
        limits.from_event(store, row, error, resets, now)  # its account shares the limit
        mkey = mm.session_key(now) if mm.enabled() else None
        if team and mkey is not None and (error or reason) and \
                mm.push_once(f"stopped:{key}:{error or reason}", now, 3600):
            tell(mkey, session_end.line(row["name"], error, reason, resets)
                 + f"; resume it with resume_session(\"{row['name']}\") when someone needs it")
        return JSONResponse({"ok": True})

    @server.custom_route("/api/done-check", methods=["GET"])
    async def api_done_check(request: Request) -> Response:
        try:
            principal = principal_of(request.headers)
            agent = identify(request.headers, principal)
        except KaptyorkaError as e:
            return PlainTextResponse(str(e), status_code=400)
        q = request.query_params
        cwd = agent.cwd[:MAX_FACT] if agent.cwd else ""
        if principal is not None and not principal.legacy and cwd and (
                "branch" in q or "subject" in q) and q.get("cwd", "")[:MAX_FACT] == cwd:
            # the stop hook read the session's folder just now: a commit made seconds ago
            # counts. Stamped with the hub's now, so the bridge's older cached read is dropped.
            dirty = {"1": True, "0": False}.get(q.get("dirty", ""))
            g = clean_git({"branch": q.get("branch", ""), "subject": q.get("subject", ""),
                           **({} if dirty is None else {"dirty": dirty})})
            try:
                ms.git_seen(principal, principal.device_id, {cwd: g},
                            ws.folders(principal.device_id))
            except Exception as e:  # history is a bonus: never fail the check over it
                print(f"memory: stop git fact skipped: {e}", file=sys.stderr, flush=True)
        out = ms.done_check(agent.key, principal, store.clock(),
                            project=session_project(agent, principal))
        out["mode"] = "block" if agent.tool in ("claude-code", "codex") else "remind"
        return JSONResponse(out)

    @server.custom_route("/api/me", methods=["GET"])
    async def api_me(request: Request) -> Response:
        p = request.scope["state"]["principal"]
        ok = page_ok(p)  # false: a device token on a hub where the page needs a GitLab sign-in
        if ok:
            store.page_seen(p)  # only the page asks who it is
        return JSONResponse({"person": p.person_name, "role": "admin" if p.is_admin() else p.role,
                             "device": p.device_name,
                             "person_id": p.person_id, "gitlab_connect": signin.can_connect(store, p),
                             "page": ok})

    @server.custom_route("/api/me/account", methods=["GET"])
    async def api_me_account(request: Request) -> Response:
        """The account panel: only the caller's own person, machines and projects."""
        return JSONResponse(admin_ops.account(store, request.scope["state"]["principal"], ws),
                            headers=NO_STORE)

    @server.custom_route("/api/me/machines/{device_id:int}/label", methods=["POST"])
    @_bad_request
    async def api_me_machine_label(request: Request) -> Response:
        body = await request.json()
        try:
            name = admin_ops.machine_label(store, request.scope["state"]["principal"],
                                           request.path_params["device_id"], str(body["label"]))
        except KaptyorkaError as e:
            return admin_error(e)
        return JSONResponse({"name": name})

    @server.custom_route("/api/me/machines/{device_id:int}/remove", methods=["POST"])
    async def api_me_machine_remove(request: Request) -> Response:
        try:
            admin_ops.machine_remove(store, request.scope["state"]["principal"],
                                     request.path_params["device_id"])
        except KaptyorkaError as e:
            return admin_error(e)
        return Response(status_code=204)

    @server.custom_route("/api/me/browsers/others", methods=["DELETE"])
    async def api_me_browsers_others(request: Request) -> Response:
        """Sign out everywhere else: your other GitLab sign-ins end; this one stays."""
        n = admin_ops.browsers_other(store, si, request.scope["state"]["principal"])
        return JSONResponse({"ended": n})

    @server.custom_route("/api/me/invite", methods=["POST"])
    async def api_me_invite(request: Request) -> Response:
        """Add a machine: a fresh invite for yourself, with the command that uses it."""
        p = request.scope["state"]["principal"]
        try:
            code = admin_ops.invite_self(store, p)
        except KaptyorkaError as e:
            return PlainTextResponse(str(e), status_code=429 if "already have" in str(e) else 403)
        url = invite_url(request, code)
        return JSONResponse({"url": url, "command": f"kaptyorka join {url}",
                             "expires": admin_ops.invite_expires(store, code)}, headers=NO_STORE)

    def manager_state():
        """The manager session's state word, why it cannot start, and its worker row."""
        now = store.clock()
        row = ws.by_name(manager.WORKER)
        running = row is not None and row["state"] in LIVE
        reason = "" if running else mm.launch_reason(now)
        lim = limits.manager_limit(store, mm, identity.facts()["host"], now) if mm.enabled() else None
        if lim is not None:  # its account is at its usage limit: it waits for the reset
            return "waiting", limits.manager_words(lim), row
        if running:  # unattended or abandoned: the session is there, just nobody watching
            state = "starting" if row["state"] == "starting" else "up"
        elif not mm.enabled():
            state = "off"
        else:  # nothing in the way: the next tick starts it
            state = "blocked" if reason else "starting"
        return state, reason, row

    @server.custom_route("/api/feed", methods=["GET"])
    async def api_feed(request: Request) -> Response:
        # everyone writes to the manager, so everyone sees its state word on Home
        principal = request.scope["state"]["principal"]
        load = {"manager_load": manager.load_line(mm.load(store.clock()), store.clock())} \
            if principal.role == "admin" else {}  # the manager's load: for the admin only
        state, why, _ = manager_state()
        return JSONResponse({**feed(store, principal, ws, cs), "manager": state,
                             **({"manager_why": why} if state == "waiting" else {}), **load})

    @server.custom_route("/api/groups/{gid}/messages", methods=["GET"])
    async def api_group_messages(request: Request) -> Response:
        """A "working together" group's conversation, as far as this viewer may see it."""
        msgs = hub_groups.conversation(store, request.scope["state"]["principal"],
                                       request.path_params["gid"], mm.person_id())
        if msgs is None:
            return PlainTextResponse("no such group", status_code=404)
        return JSONResponse({"messages": msgs})

    @server.custom_route("/api/files", methods=["POST"])
    async def api_file_upload(request: Request) -> Response:
        principal = request.scope["state"]["principal"]
        if request.headers.get("x-kaptyorka-key"):  # from a session's tool, not the page
            try:
                identify(request.headers, principal)
            except KaptyorkaError as e:
                return PlainTextResponse(str(e), status_code=403)
        name = unquote(request.headers.get("x-file-name") or "") or "file"
        def mb(n):  # as the page writes sizes
            return f"{n / 1_000_000:.1f} MB"

        def too_large(n):
            return PlainTextResponse(f"file too large: {mb(n)}, limit {mb(MAX_FILE)}", status_code=400)

        declared = request.headers.get("content-length") or ""
        if declared.isdigit() and int(declared) > MAX_FILE:
            return too_large(int(declared))
        data = b""
        async for chunk in request.stream():
            data += chunk
            if len(data) > MAX_FILE:
                return too_large(len(data))
        try:
            fid = store.file_add(name, data, principal.person_id)
        except KaptyorkaError as e:
            return PlainTextResponse(str(e), status_code=400)
        return JSONResponse({"id": fid, "name": name[:200], "size": len(data)})

    @server.custom_route("/api/files/{id}", methods=["GET"])
    async def api_file_get(request: Request) -> Response:
        try:
            info, data = store.file_get(int(request.path_params["id"]),
                                        request.scope["state"]["principal"])
        except (KaptyorkaError, ValueError):
            return PlainTextResponse("no such file", status_code=404)
        safe = re.sub(r"[^A-Za-z0-9._-]", "", info["name"].replace("\\", "/").rsplit("/", 1)[-1])
        headers = {"Content-Disposition": f'attachment; filename="{safe or "file"}"',
                   "X-Content-Type-Options": "nosniff", "Accept-Ranges": "bytes"}
        ctype = mimetypes.guess_type(info["name"])[0] or "application/octet-stream"
        # a video player seeks with one Range header: bytes=a-b, bytes=a- or bytes=-n
        m = re.fullmatch(r"bytes=(\d*)-(\d*)", request.headers.get("range", "").strip())
        if m and (m.group(1) or m.group(2)):
            size = len(data)
            if m.group(1):
                start = int(m.group(1))
                end = min(int(m.group(2)), size - 1) if m.group(2) else size - 1
            else:
                start, end = max(size - int(m.group(2)), 0), size - 1
            if start >= size or start > end:
                return Response(status_code=416, headers={**headers, "Content-Range": f"bytes */{size}"})
            return Response(data[start:end + 1], status_code=206, media_type=ctype,
                            headers={**headers, "Content-Range": f"bytes {start}-{end}/{size}"})
        return Response(data, media_type=ctype, headers=headers)

    @server.custom_route("/api/send", methods=["POST"])
    @_bad_request
    async def api_send(request: Request) -> Response:
        body = await request.json()
        p = request.scope["state"]["principal"]  # the body's "from" (0.1 pages) is ignored
        file_id = body.get("file_id")
        try:
            if file_id is not None:
                if not isinstance(file_id, int) or isinstance(file_id, bool):
                    raise KaptyorkaError("file_id must be a number")
                if p.person_id is None:
                    raise KaptyorkaError("use your own device token to send a file")
                with store.lock:
                    row = store.db.execute("SELECT person_id, message_id FROM files WHERE id=?",
                                           (file_id,)).fetchone()
                if row is None:
                    raise KaptyorkaError("no such file")
                if row["person_id"] != p.person_id or row["message_id"] is not None:
                    raise KaptyorkaError("that file is not yours to send")
            n = ws.send_web_via_manager(p.person_name, str(body["to"]), str(body["text"]),
                                        p.person_id, mm.person_id(), file_id=file_id)
            if n is None:
                n = store.send_from_web(p.person_name, str(body["to"]), str(body["text"]),
                                        person_id=p.person_id, file_id=file_id)
        except KaptyorkaError as e:
            return PlainTextResponse(str(e), status_code=400)
        wait = limits.waiting_line(store, mm, identity.facts()["host"], store.clock()) \
            if limits.to_manager(store, mm, str(body["to"])) else ""
        lim = limits.manager_limit(store, mm, identity.facts()["host"], store.clock()) if wait else None
        if lim is not None and mm.push_once(f"limit-told:{p.person_name}:{lim['since']!r}",
                                            store.clock()):
            tell(f"web:{p.person_name}", wait)  # once per person per wait
        wake_bridges()
        return JSONResponse({"sent": n})

    @server.custom_route("/api/notes", methods=["POST"])
    @_bad_request
    async def api_notes(request: Request) -> Response:
        body = await request.json()
        p = request.scope["state"]["principal"]
        topic, text = str(body["topic"]), str(body["text"])
        project = str(body.get("project") or "") or None
        try:
            is_state = project is not None and topic.strip() == "state"
            if is_state:
                problem = check_state(text)
                if problem:
                    raise KaptyorkaError(problem)
            store.notes_write_from_web(p.person_name, topic, text, project, principal=p)
            if is_state:
                ms.state_written(f"web:{p.person_name}", project, p.person_id, store.clock())
        except KaptyorkaError as e:
            return PlainTextResponse(str(e), status_code=400)
        return Response(status_code=204)

    @server.custom_route("/api/notes", methods=["DELETE"])
    @_bad_request
    async def api_note_delete(request: Request) -> Response:
        q = request.query_params
        try:
            if not (q.get("topic") or "").strip():
                raise KaptyorkaError("topic is empty")
            admin_ops.note_delete(store, request.scope["state"]["principal"],
                                  q["topic"], q.get("project") or None)
        except KaptyorkaError as e:
            return PlainTextResponse(str(e), status_code=400)
        return Response(status_code=204)

    @server.custom_route("/api/notes/undo", methods=["POST"])
    @_bad_request
    async def api_note_undo(request: Request) -> Response:
        body = await request.json()
        try:
            admin_ops.note_undo(store, request.scope["state"]["principal"],
                                str(body["topic"]), str(body.get("project") or "") or None)
        except KaptyorkaError as e:
            return PlainTextResponse(str(e), status_code=400)
        return Response(status_code=204)

    limiter = RateLimiter()  # shared by token checks (Auth) and failed invite redeems

    def admin_only(request: Request) -> "Response | None":
        principal = request.scope.get("state", {}).get("principal")
        if principal is None or not principal.is_admin():
            return PlainTextResponse("admin only", status_code=403)
        if principal.legacy and store.has_own_device(principal.person_id):
            # anyone holding the shared token would be the admin: once the admin has a token
            # of their own, admin work takes that one
            return PlainTextResponse("use your own admin token", status_code=403)
        return None

    def hub_url(request: Request) -> str:
        """The hub's address for links: https on its TLS port once it uses https (tls.py)."""
        return tls.base_url(request.url.scheme, request.headers.get("host") or request.url.netloc)

    def own_url(request: Request) -> str:
        """The address this request came to: a machine joined over plain http stays there
        (it has no pin yet) and its bridge follows the hub's signed announcement."""
        return f"{request.url.scheme}://{request.headers.get('host') or request.url.netloc}"

    def invite_url(request: Request, code: str) -> str:
        return tls.invite_url(hub_url(request), code)  # over https with the fingerprint

    def device_signing(device_id) -> Optional[str]:
        """The device's signing key (its secret's hash), None for a legacy token."""
        if device_id is None:
            return None
        with store.lock:
            row = store.db.execute("SELECT secret_hash FROM devices WHERE id=?", (device_id,)).fetchone()
        return row["secret_hash"] if row and row["secret_hash"] else None

    @server.custom_route("/api/log", methods=["GET"])
    @_bad_request
    async def api_log(request: Request) -> Response:
        denied = admin_only(request)
        if denied:
            return denied
        q = request.query_params
        try:
            return JSONResponse(admin_ops.log_page(store, int(q.get("limit") or 200), q.get("person") or None,
                                                   q.get("kind") or None,
                                                   int(q.get("before") or 0) or None))
        except KaptyorkaError as e:
            return PlainTextResponse(str(e), status_code=400)

    @server.custom_route("/api/log/kinds", methods=["GET"])
    async def api_log_kinds(request: Request) -> Response:
        denied = admin_only(request)
        if denied:
            return denied
        return JSONResponse([{"kind": g, "label": g.capitalize()}
                             for g in [g for g, _ in admin_ops.log_kinds()] + ["other"]])

    @server.custom_route("/api/settings", methods=["GET"])
    async def api_settings(request: Request) -> Response:
        denied = admin_only(request)
        if denied:
            return denied
        state, reason, row = manager_state()
        return JSONResponse({
            "retention_days": store.retention_days(), "auto_doors": store.auto_doors(),
            "manager_doors": store.manager_doors(),
            "manager_model": mm.model(), "manager_effort": mm.effort(),
            "manager_enabled": mm.enabled(),
            "manager_state": state, "manager_reason": reason,
            "manager_error": (row["error"] or "") if row and row["state"] == "failed" else "",
            "manager_load": manager.load_line(mm.load(store.clock()), store.clock()),
            "close_left": store.close_left()})

    @server.custom_route("/api/settings", methods=["POST"])
    @_bad_request
    async def api_settings_set(request: Request) -> Response:
        denied = admin_only(request)
        if denied:
            return denied
        body = await request.json()
        by = request.scope["state"]["principal"].person_name
        try:
            for key in ("auto_doors", "manager_doors"):
                if key in body:
                    store.set_auto_doors(body[key], by, key)
            if "close_left" in body:
                store.set_close_left(body["close_left"], by)
            if "manager_model" in body or "manager_effort" in body:
                mm.set_model(body.get("manager_model"), body.get("manager_effort"), by)
            if "retention_days" in body or not ({"auto_doors", "manager_doors", "close_left",
                                                 "manager_model", "manager_effort"} & set(body)):
                store.set_retention_days(body.get("retention_days"), by)
        except KaptyorkaError as e:
            return PlainTextResponse(str(e), status_code=400)
        return JSONResponse({"retention_days": store.retention_days(),
                             "auto_doors": store.auto_doors(),
                             "manager_doors": store.manager_doors(),
                             "close_left": store.close_left(),
                             "manager_model": mm.model(), "manager_effort": mm.effort()})

    @server.custom_route("/api/legacy", methods=["GET"])
    async def api_legacy(request: Request) -> Response:
        denied = admin_only(request)
        if denied:
            return denied
        return JSONResponse({"unassigned": store.legacy_unassigned(),
                             "enabled": store.legacy_enabled(),
                             "ready_to_disable": store.legacy_ready_to_disable()})

    @server.custom_route("/api/legacy/assign", methods=["POST"])
    @_bad_request
    async def api_legacy_assign(request: Request) -> Response:
        denied = admin_only(request)
        if denied:
            return denied
        body = await request.json()
        person = store.person_by_name(str(body["person"]))
        try:
            if person is None:
                raise KaptyorkaError("no such person")
            store.assign_legacy(str(body["machine"]), person["id"],
                                request.scope["state"]["principal"].person_name)
        except KaptyorkaError as e:
            return PlainTextResponse(str(e), status_code=400)
        wake_bridges()  # its bridge may pick up the new token on its next heartbeat and poll
        return Response(status_code=204)

    async def legacy_toggle(request: Request, on: bool) -> Response:
        denied = admin_only(request)
        if denied:
            return denied
        store.set_legacy_enabled(on, request.scope["state"]["principal"].person_name)
        return Response(status_code=204)

    @server.custom_route("/api/legacy/off", methods=["POST"])
    async def api_legacy_off(request: Request) -> Response:
        principal = request.scope.get("state", {}).get("principal")
        if principal is not None and principal.is_admin() and (
                principal.legacy or not store.has_own_device(principal.person_id)):
            # switched off with itself, the shared token would lock the admin out
            return PlainTextResponse(
                "switch the shared token off with your own admin token: run kaptyorka start"
                " on the hub machine and open the page link it prints", status_code=400)
        return await legacy_toggle(request, False)

    @server.custom_route("/api/legacy/on", methods=["POST"])
    async def api_legacy_on(request: Request) -> Response:
        return await legacy_toggle(request, True)

    @server.custom_route("/api/rules", methods=["GET"])
    async def api_rules(request: Request) -> Response:
        cur = rs.current()
        return JSONResponse({**cur, "skills": [
            {"name": k["name"], "author": k["author"], "time": k["time"], "size": len(k["text"])}
            for k in rs.skills()], "skills_version": rs.skills_version()})

    @server.custom_route("/api/rules", methods=["POST"])
    @_bad_request
    async def api_rules_set(request: Request) -> Response:
        denied = admin_only(request)
        if denied:
            return denied
        body = await request.json()
        try:
            n = rs.set_rules(str(body["text"]), request.scope["state"]["principal"].person_name)
        except KaptyorkaError as e:
            return PlainTextResponse(str(e), status_code=400)
        return JSONResponse({"version": n})

    @server.custom_route("/api/rules/history", methods=["GET"])
    async def api_rules_history(request: Request) -> Response:
        return JSONResponse(rs.history(limit=200))

    @server.custom_route("/api/skills", methods=["PUT"])
    @_bad_request
    async def api_skills_set(request: Request) -> Response:
        denied = admin_only(request)
        if denied:
            return denied
        body = await request.json()
        items = body["skills"]
        if not isinstance(items, dict):
            return PlainTextResponse("skills must be an object of name: text", status_code=400)
        try:
            return JSONResponse(rs.set_skills({str(k): str(v) for k, v in items.items()},
                                              request.scope["state"]["principal"].person_name))
        except KaptyorkaError as e:
            return PlainTextResponse(str(e), status_code=400)

    @server.custom_route("/bridge/skills", methods=["GET"])
    async def bridge_skills(request: Request) -> Response:
        return JSONResponse({"version": rs.skills_version(),
                             "skills": [{"name": k["name"], "text": k["text"]}
                                        for k in rs.skills()]})

    @server.custom_route("/api/proposals", methods=["GET"])
    async def api_proposals(request: Request) -> Response:
        principal = request.scope["state"]["principal"]
        rows = rs.proposals(request.query_params.get("status") or None,
                            person_id=None if principal.is_admin() else principal.person_id)
        return JSONResponse([{k: v for k, v in r.items() if k not in ("session_key", "person_id")}
                             for r in rows])

    @server.custom_route("/api/proposals", methods=["POST"])
    @_bad_request
    async def api_proposal_add(request: Request) -> Response:
        body = await request.json()
        p = request.scope["state"]["principal"]
        if p.legacy or p.person_id is None:
            return PlainTextResponse(OWN_TOKEN, status_code=400)
        text, evidence = str(body.get("text") or "").strip(), str(body.get("evidence") or "")
        try:
            if not text:
                raise KaptyorkaError("the rule text is empty")
            check_secrets(text)
            check_secrets(evidence)
            pid = rs.propose(p, f"web:{p.person_name}", text, evidence)
        except KaptyorkaError as e:
            return PlainTextResponse(str(e), status_code=400)
        return JSONResponse({"id": pid})

    @server.custom_route("/api/proposals/{pid:int}", methods=["POST"])
    @_bad_request
    async def api_proposal_decide(request: Request) -> Response:
        denied = admin_only(request)
        if denied:
            return denied
        body = await request.json()
        by = request.scope["state"]["principal"].person_name
        pid = request.path_params["pid"]
        if not isinstance(body.get("accept"), bool):
            return PlainTextResponse("accept must be true or false", status_code=400)
        accept, reason = body["accept"], str(body.get("reason", ""))[:1000]
        try:
            row = rs.decide(pid, accept, reason, by)
        except KaptyorkaError as e:
            return PlainTextResponse(str(e), status_code=409 if "already" in str(e) else 400)
        if accept:
            text = (f"Your rule proposal {pid} was accepted by {by}: team rules are now "
                    f"v{rs.current()['version']}.")
        else:
            text = f"Your rule proposal {pid} was rejected by {by}" + (f": {reason}" if reason else ".")
        tell(row["session_key"], text)
        return JSONResponse({"status": row["status"]})

    @server.custom_route("/api/conflicts", methods=["GET"])
    async def api_conflicts(request: Request) -> Response:
        denied = admin_only(request)
        if denied:
            return denied
        return JSONResponse(rs.conflicts())

    @server.custom_route("/api/people", methods=["GET"])
    async def api_people(request: Request) -> Response:
        denied = admin_only(request)
        if denied:
            return denied
        return JSONResponse([{**p, "devices": store.devices(p["id"])} for p in store.people()])

    @server.custom_route("/api/people", methods=["POST"])
    @_bad_request
    async def api_people_create(request: Request) -> Response:
        denied = admin_only(request)
        if denied:
            return denied
        body = await request.json()
        principal = request.scope["state"]["principal"]
        try:
            username = str(body.get("gitlab") or "").strip()
            if username:  # checked first, so a taken username adds nobody
                signin.check_free(store, username)
            pid = store.person_create(str(body["name"]), str(body.get("role", "member")),
                                      by=principal.person_name)
            if username:
                signin.set_user(store, si, str(body["name"]), username, principal)
        except KaptyorkaError as e:
            status = 409 if "already exists" in str(e) else 400
            return PlainTextResponse(str(e), status_code=status)
        return JSONResponse({"id": pid})

    @server.custom_route("/api/people/{name}/gitlab", methods=["POST"])
    @_bad_request
    async def api_person_gitlab(request: Request) -> Response:
        """Link a person to their GitLab username ("" unlinks); a change ends their GitLab sign-ins."""
        denied = admin_only(request)
        if denied:
            return denied
        body = await request.json()
        try:
            kept = signin.set_user(store, si, request.path_params["name"], str(body.get("username") or ""),
                                   request.scope["state"]["principal"])
        except KaptyorkaError as e:
            return admin_error(e)
        return JSONResponse({"gitlab_user": kept})

    @server.custom_route("/api/invites", methods=["POST"])
    @_bad_request
    async def api_invites(request: Request) -> Response:
        denied = admin_only(request)
        if denied:
            return denied
        body = await request.json()
        person = store.person_by_name(str(body["person"]))
        if person is None:
            return PlainTextResponse("no such person", status_code=404)
        principal = request.scope["state"]["principal"]
        try:  # replace: the Team row's New invite link; the person's older links stop working
            code = (admin_ops.invite_renew(store, person["name"], principal) if body.get("replace")
                    else store.invite_create(person["id"], by=principal.person_name))
        except KaptyorkaError as e:
            return PlainTextResponse(str(e), status_code=400)
        return JSONResponse({"code": code, "url": invite_url(request, code),
                             "expires": admin_ops.invite_expires(store, code)})

    @server.custom_route("/api/team-machines", methods=["POST"])
    @_bad_request
    async def api_team_machines(request: Request) -> Response:
        """Add a team machine: its row and an invite, with the join command to run on the
        shared account."""
        denied = admin_only(request)
        if denied:
            return denied
        body = await request.json()
        try:
            _, code = store.team_machine_add(str(body["name"]),
                                             request.scope["state"]["principal"].person_name)
        except KaptyorkaError as e:
            return PlainTextResponse(str(e), status_code=409 if "taken" in str(e) else 400)
        url = invite_url(request, code)
        return JSONResponse({"name": str(body["name"]).strip(), "url": url,
                             "command": f"kaptyorka join {url}",
                             "expires": admin_ops.invite_expires(store, code)}, headers=NO_STORE)

    def admin_error(e: KaptyorkaError) -> Response:
        return PlainTextResponse(str(e), status_code=404 if str(e).startswith("no ") else 400)

    @server.custom_route("/api/invites", methods=["GET"])
    async def api_invites_open(request: Request) -> Response:
        denied = admin_only(request)
        if denied:
            return denied
        base = hub_url(request)
        return JSONResponse([{**v, "url": tls.invite_url(base, v["code"])}
                             for v in admin_ops.invites_open(store)])

    @server.custom_route("/api/invites/{code}", methods=["DELETE"])
    async def api_invite_cancel(request: Request) -> Response:
        denied = admin_only(request)
        if denied:
            return denied
        try:
            admin_ops.invite_cancel(store, request.path_params["code"],
                                    request.scope["state"]["principal"])
        except KaptyorkaError as e:
            return admin_error(e)
        return Response(status_code=204)

    @server.custom_route("/api/people/{name}", methods=["DELETE"])
    async def api_person_remove(request: Request) -> Response:
        denied = admin_only(request)
        if denied:
            return denied
        try:
            done = admin_ops.remove_person(store, ws, request.path_params["name"],
                                           request.scope["state"]["principal"])
        except KaptyorkaError as e:
            return admin_error(e)
        wake_bridges()  # close orders for their workers
        return JSONResponse(done)

    @server.custom_route("/api/people/{name}/role", methods=["POST"])
    @_bad_request
    async def api_person_role(request: Request) -> Response:
        denied = admin_only(request)
        if denied:
            return denied
        body = await request.json()
        try:
            done = admin_ops.set_role(store, request.path_params["name"], str(body["role"]),
                                      request.scope["state"]["principal"])
        except KaptyorkaError as e:
            return admin_error(e)
        return JSONResponse(done)

    @server.custom_route("/api/people/{name}/team", methods=["POST", "DELETE"])
    async def api_person_team(request: Request) -> Response:
        """Turn a person into a team machine (admin_ops.make_team_machine); DELETE turns it
        back (admin_ops.undo_team_machine)."""
        denied = admin_only(request)
        if denied:
            return denied
        try:
            if request.method == "DELETE":
                done = admin_ops.undo_team_machine(store, request.path_params["name"],
                                                   request.scope["state"]["principal"])
            else:
                done = admin_ops.make_team_machine(store, request.path_params["name"],
                                                   request.scope["state"]["principal"])
        except KaptyorkaError as e:
            return admin_error(e)
        wake_bridges()
        return JSONResponse(done)

    @server.custom_route("/api/people/{name}/team-machines/{machine}", methods=["POST"])
    @_bad_request
    async def api_person_team_machine(request: Request) -> Response:
        """Show or hide a team machine (and its sessions) for one person: {"visible": bool}."""
        denied = admin_only(request)
        if denied:
            return denied
        body = await request.json()
        if not isinstance(body.get("visible"), bool):
            return PlainTextResponse("visible must be true or false", status_code=400)
        try:
            store.team_machine_set_visible(request.path_params["machine"], request.path_params["name"],
                                           body["visible"], request.scope["state"]["principal"].person_name)
        except KaptyorkaError as e:
            return admin_error(e)
        return Response(status_code=204)

    @server.custom_route("/api/me/access", methods=["GET"])
    async def api_my_access(request: Request) -> Response:
        return JSONResponse(admin_ops.my_access(store, ws, request.scope["state"]["principal"]))

    @server.custom_route("/api/devices/{device_id:int}/revoke", methods=["POST"])
    async def api_device_revoke(request: Request) -> Response:
        denied = admin_only(request)
        if denied:
            return denied
        store.device_revoke(request.path_params["device_id"],
                            by=request.scope["state"]["principal"].person_name)
        return Response(status_code=204)

    @server.custom_route("/api/devices/{device_id:int}/shared", methods=["POST"])
    @_bad_request
    async def api_device_shared(request: Request) -> Response:
        denied = admin_only(request)
        if denied:
            return denied
        body = await request.json()
        if not isinstance(body["shared"], bool):
            raise ValueError("shared must be true or false")
        try:
            store.device_set_shared(request.path_params["device_id"], body["shared"],
                                    by=request.scope["state"]["principal"].person_name)
        except KaptyorkaError as e:
            return PlainTextResponse(str(e), status_code=400)
        return Response(status_code=204)

    @server.custom_route("/api/rollout", methods=["GET", "POST"])
    @_bad_request
    async def api_rollout(request: Request) -> Response:
        """The staged update of the hub's version: its state; POST {"go": true|false} releases
        the rest now or holds them (the admin)."""
        denied = admin_only(request)
        if denied:
            return denied
        if request.method == "POST":
            body = await request.json()
            if not isinstance(body.get("go"), bool):
                raise ValueError("go must be true or false")
            who = request.scope["state"]["principal"].person_name
            s = rollout.decide(store, __version__, body["go"], who)
            store.log_add(who, "rollout", ("released " if body["go"] else "held ") + __version__)
        else:
            s = rollout.status(store, __version__)
        return JSONResponse({**s, "line": rollout.line(s)})

    @server.custom_route("/api/devices/{device_id:int}/auto_update", methods=["POST"])
    @_bad_request
    async def api_device_auto_update(request: Request) -> Response:
        denied = admin_only(request)
        if denied:
            return denied
        body = await request.json()
        if body["on"] is not None and not isinstance(body["on"], bool):
            raise ValueError("on must be true, false or null")
        if not ws.device_exists(request.path_params["device_id"]):
            return PlainTextResponse("no such device", status_code=404)
        ws.set_auto_update(request.path_params["device_id"], body["on"])
        with store.lock:
            d = store.db.execute("SELECT d.name, d.host, p.name AS person FROM devices d"
                                 " LEFT JOIN people p ON p.id=d.person_id WHERE d.id=?",
                                 (request.path_params["device_id"],)).fetchone()
        if d is None:  # removed between the switch and this read
            return PlainTextResponse("no such device", status_code=404)
        state = "default" if body["on"] is None else "on" if body["on"] else "off"
        where = d["host"] or d["name"]
        if d["person"]:
            where += f" ({d['person']})"
        store.log_add(request.scope["state"]["principal"].person_name, "auto_update",
                      f"{where} {state}")
        return Response(status_code=204)

    @server.custom_route("/api/workers", methods=["GET"])
    async def api_workers(request: Request) -> Response:
        principal = request.scope.get("state", {}).get("principal")
        if principal is None:
            return PlainTextResponse("unauthorized", status_code=401)
        rows = own_workers(principal, page_foreign(request, principal))
        return JSONResponse(rows)

    async def worker_action(request: Request, close: bool) -> Response:
        principal = request.scope.get("state", {}).get("principal")
        if principal is None:
            return PlainTextResponse("unauthorized", status_code=401)
        if principal.legacy:
            return PlainTextResponse(OWN_TOKEN, status_code=403)
        key = unquote(request.headers.get("x-kaptyorka-key") or "")
        if page_foreign(request, principal) is not None \
                or (principal.shared and key and key in ws.live_keys()):
            return PlainTextResponse(ON_SHARED, status_code=403)
        name = request.path_params["name"]
        try:
            # the page acts as the person: a web boss hears its notices on the page
            w = (ws.close(principal, name) if close
                 else ws.adopt(principal, name, f"web:{principal.person_name}"))
        except KaptyorkaError as e:
            return PlainTextResponse(str(e), status_code=400)
        if close and principal.is_admin() and w.get("manager"):  # as close_worker: the tick must not relaunch it
            mm.set_enabled(False, principal.person_name)
        wake_bridges()
        return JSONResponse(w)

    @server.custom_route("/api/workers/{name}/close", methods=["POST"])
    async def api_worker_close(request: Request) -> Response:
        return await worker_action(request, True)

    @server.custom_route("/api/workers/{name}/adopt", methods=["POST"])
    async def api_worker_adopt(request: Request) -> Response:
        return await worker_action(request, False)

    @server.custom_route("/api/warnings", methods=["GET"])
    async def api_warnings(request: Request) -> Response:
        """The Machines page's warnings: an admin's all, anyone else's only their own accounts,
        their workers and the team machines they may use (WorkerStore.warnings_for)."""
        principal = request.scope.get("state", {}).get("principal")
        if principal is None:
            return PlainTextResponse("unauthorized", status_code=401)
        if admin_only(request) is None:  # the admin (the shared admin token too) sees all
            return JSONResponse(ws.warnings(store.clock(), __version__))
        return JSONResponse(ws.warnings_for(principal, store.clock(), __version__))

    @server.custom_route("/api/machines", methods=["GET"])
    async def api_machines(request: Request) -> Response:
        principal = request.scope.get("state", {}).get("principal")
        if principal is None:
            return PlainTextResponse("unauthorized", status_code=401)
        return JSONResponse(ws.machine_rows(principal))

    async def machine_grant(request: Request, grant: bool) -> Response:
        denied = admin_only(request)
        if denied:
            return denied
        body = await request.json()
        person = store.person_by_name(str(body["person"]))
        try:
            if person is None:
                raise KaptyorkaError("no such person")
            host = request.path_params["host"]
            principal = request.scope["state"]["principal"]
            (store.grant_machine if grant else store.revoke_machine)(
                host, person["id"], principal.person_name)
        except KaptyorkaError as e:
            return PlainTextResponse(str(e), status_code=400)
        return Response(status_code=204)

    @server.custom_route("/api/team/machines", methods=["GET"])
    async def api_team_machines_list(request: Request) -> Response:
        """The admin's Machines list (WorkerStore.overview), with accounts at a usage limit."""
        denied = admin_only(request)
        if denied:
            return denied
        at = limits.by_host(store, store.clock())
        return JSONResponse([{**h, "limits": at.get(h["host"], [])} for h in ws.overview()])

    @server.custom_route("/api/devices/{device_id:int}/move", methods=["POST"])
    @_bad_request
    async def api_device_move(request: Request) -> Response:
        denied = admin_only(request)
        if denied:
            return denied
        body = await request.json()
        try:
            done = store.device_move(request.path_params["device_id"], str(body["person"]),
                                     request.scope["state"]["principal"].person_name)
        except KaptyorkaError as e:
            return admin_error(e)
        wake_bridges()
        return JSONResponse(done)

    @server.custom_route("/api/machines/{host}/grant", methods=["POST"])
    @_bad_request
    async def api_machine_grant(request: Request) -> Response:
        return await machine_grant(request, True)

    @server.custom_route("/api/machines/{host}/owner", methods=["POST"])
    @_bad_request
    async def api_machine_owner(request: Request) -> Response:
        denied = admin_only(request)
        if denied:
            return denied
        body = await request.json()
        name = str(body.get("person") or "")
        person = store.person_by_name(name) if name else None
        try:
            if name and person is None:
                raise KaptyorkaError("no such person")
            store.set_machine_owner(request.path_params["host"], person["id"] if person else None,
                                    request.scope["state"]["principal"].person_name)
        except KaptyorkaError as e:
            return PlainTextResponse(str(e), status_code=400)
        return Response(status_code=204)

    @server.custom_route("/api/machines/{host}/revoke", methods=["POST"])
    @_bad_request
    async def api_machine_revoke(request: Request) -> Response:
        return await machine_grant(request, False)

    @server.custom_route("/api/machines/{host}/gpu-queue", methods=["POST"])
    @_bad_request
    async def api_machine_gpu_queue(request: Request) -> Response:
        """Read the GPU share queue on this computer: {"lock", "shares"}, or {"off": true}."""
        denied = admin_only(request)
        if denied:
            return denied
        body = await request.json()
        shares = body.get("shares", usage.GPU_SHARES)
        try:
            if not isinstance(shares, int) or isinstance(shares, bool):
                raise KaptyorkaError("shares must be a whole number")
            ws.set_gpu_queue(request.path_params["host"],
                             None if body.get("off") else str(body["lock"]), shares,
                             request.scope["state"]["principal"].person_name)
        except KaptyorkaError as e:
            return admin_error(e)
        return Response(status_code=204)

    @server.custom_route("/api/projects", methods=["GET"])
    async def api_projects(request: Request) -> Response:
        principal = request.scope.get("state", {}).get("principal")
        if principal is None:
            return PlainTextResponse("unauthorized", status_code=401)
        return JSONResponse(store.projects_for(principal))

    @server.custom_route("/api/projects/{name}/activity", methods=["GET"])
    async def api_project_activity(request: Request) -> Response:
        """The page's project view: the state note, a week of git facts, live branches."""
        principal = request.scope["state"]["principal"]
        project = request.path_params["name"]
        # the read check first: a non-member learns nothing, not even that the name exists
        if not store.may_read_project(project, principal):
            return PlainTextResponse("no read role in this project", status_code=403)
        if not ws.registered(project):
            return PlainTextResponse("no such project", status_code=404)
        state = store.notes_read("state", principal, project)
        return JSONResponse({"state": state.text if state else None,
                             "git": ms.git_recent(project, 7),
                             "branches": ms.branches_with_sessions(project, store.clock(),
                                                                  principal.person_id)})

    @server.custom_route("/api/requests", methods=["GET"])
    async def api_requests(request: Request) -> Response:
        principal = request.scope.get("state", {}).get("principal")
        if principal is None:
            return PlainTextResponse("unauthorized", status_code=401)
        if principal.is_admin():
            return JSONResponse(store.requests())
        return JSONResponse(store.pending_for(principal.person_id))

    @server.custom_route("/api/requests", methods=["POST"])
    @_bad_request
    async def api_request_create(request: Request) -> Response:
        principal = request.scope["state"]["principal"]
        body = await request.json()
        try:
            rid = store.request_access(principal.person_id, str(body["kind"]),
                                       str(body["target"]), str(body.get("why") or ""),
                                       str(body.get("role") or "read"))
        except KaptyorkaError as e:
            return PlainTextResponse(str(e), status_code=400)
        return JSONResponse({"id": rid})

    @server.custom_route("/api/connects", methods=["POST"])
    @_bad_request
    async def api_connect_create(request: Request) -> Response:
        """A member asks to connect their page or one of their sessions to a session of the
        directory: {"to": its id, "from": "page" or one of their session names, "why"}."""
        return PlainTextResponse(ASK_MANAGER, status_code=400)

    @server.custom_route("/api/connects/{cid:int}", methods=["POST"])
    @_bad_request
    async def api_connect_decide(request: Request) -> Response:
        denied = admin_only(request)
        if denied:
            return denied
        body = await request.json()
        if not isinstance(body["approve"], bool):
            raise ValueError("approve must be true or false")
        try:
            out = cs.decide(request.path_params["cid"], request.scope["state"]["principal"],
                            body["approve"], str(body.get("reason") or ""),
                            "people" if body.get("how") == "people" else "sessions")
        except KaptyorkaError as e:
            return PlainTextResponse(str(e), status_code=400)
        wake_bridges()
        if out == "offline":
            return PlainTextResponse("a session went offline, so no door was opened;"
                                     " the request is closed", status_code=409)
        return Response(status_code=204)

    @server.custom_route("/api/requests/{rid:int}", methods=["POST"])
    @_bad_request
    async def api_request_decide(request: Request) -> Response:
        denied = admin_only(request)
        if denied:
            return denied
        body = await request.json()
        if not isinstance(body["approve"], bool):
            raise ValueError("approve must be true or false")
        role, reason = str(body.get("role") or "read"), str(body.get("reason") or "").strip()
        if role not in ("read", "write"):
            return PlainTextResponse("role must be read or write", status_code=400)
        principal = request.scope["state"]["principal"]
        rid = request.path_params["rid"]
        try:
            store.decide_request(rid, principal.person_id, body["approve"])
            r = next(x for x in store.requests(pending_only=False) if x["id"] == rid)
            if body["approve"] and r["kind"] == "project" and role == "write" \
                    and store.project_role(r["target"], r["person_id"]) == "read":
                store.grant_project(r["target"], r["person_id"], "write", principal.person_name)
        except KaptyorkaError as e:
            return PlainTextResponse(str(e), status_code=400)
        if not body["approve"] and reason:
            text = f"Your request for the {r['kind']} {r['target']} was denied: {reason[:500]}"
            for key in [f"web:{r['person']}"] + [a.key for a in store.online()
                                                 if a.person_id == r["person_id"]]:
                ws.notify(key, text)
            wake_bridges()
        return Response(status_code=204)

    @server.custom_route("/api/projects", methods=["POST"])
    @_bad_request
    async def api_project_create(request: Request) -> Response:
        denied = admin_only(request)
        if denied:
            return denied
        body = await request.json()
        try:
            store.project_register(str(body["name"]), str(body.get("description", "")),
                                   str(body.get("repo", "")),
                                   by=request.scope["state"]["principal"].person_name)
        except KaptyorkaError as e:
            return PlainTextResponse(str(e), status_code=400)
        return Response(status_code=204)

    @server.custom_route("/api/projects/{name}", methods=["POST"])
    @_bad_request
    async def api_project_update(request: Request) -> Response:
        denied = admin_only(request)
        if denied:
            return denied
        body = await request.json()
        try:
            admin_ops.project_update(store, request.path_params["name"], str(body["description"]),
                                     request.scope["state"]["principal"])
        except KaptyorkaError as e:
            return admin_error(e)
        return Response(status_code=204)

    @server.custom_route("/api/projects/{name}", methods=["DELETE"])
    async def api_project_remove(request: Request) -> Response:
        denied = admin_only(request)
        if denied:
            return denied
        try:
            done = admin_ops.project_remove(store, request.path_params["name"],
                                            request.scope["state"]["principal"])
        except KaptyorkaError as e:
            return admin_error(e)
        done["personal"] = admin_ops.purge_personal(store)  # its sessions are personal now
        brief_cache.clear()
        wake_bridges()
        return JSONResponse(done)

    @server.custom_route("/api/gitlab", methods=["GET"])
    async def api_gitlab_status(request: Request) -> Response:
        """`kaptyorka gitlab status`: the address, the group, the token file and a fresh count."""
        denied = admin_only(request)
        if denied:
            return denied
        return JSONResponse({**await asyncio.to_thread(gl.status), "signin": signin.status(store)})

    @server.custom_route("/api/gitlab", methods=["POST"])
    @_bad_request
    async def api_gitlab_set(request: Request) -> Response:
        denied = admin_only(request)
        if denied:
            return denied
        body = await request.json()
        try:
            gl.set(str(body["url"]), str(body["group"]), request.scope["state"]["principal"].person_name)
        except GitLabError as e:
            return PlainTextResponse(str(e), status_code=400)
        return JSONResponse({**await asyncio.to_thread(gl.status), "signin": signin.status(store)})

    @server.custom_route("/api/gitlab", methods=["DELETE"])
    async def api_gitlab_off(request: Request) -> Response:
        denied = admin_only(request)
        if denied:
            return denied
        gl.off(request.scope["state"]["principal"].person_name)
        return Response(status_code=204)

    @server.custom_route("/api/gitlab/projects", methods=["GET"])
    async def api_gitlab_projects(request: Request) -> Response:
        """The page's Projects section: the group's projects, cached for a few minutes."""
        denied = admin_only(request)
        if denied:
            return denied
        fresh = request.query_params.get("fresh") == "1"
        return JSONResponse(await asyncio.to_thread(gl.projects, fresh))

    async def project_grant(request: Request, grant: bool) -> Response:
        denied = admin_only(request)
        if denied:
            return denied
        body = await request.json()
        person = store.person_by_name(str(body["person"]))
        project = request.path_params["name"]
        by = request.scope["state"]["principal"].person_name
        try:
            if person is None:
                raise KaptyorkaError("no such person")
            if grant:
                store.grant_project(project, person["id"], str(body["role"]), by)
            else:
                store.revoke_project(project, person["id"], by)
        except KaptyorkaError as e:
            return PlainTextResponse(str(e), status_code=400)
        return Response(status_code=204)

    @server.custom_route("/api/projects/{name}/grant", methods=["POST"])
    @_bad_request
    async def api_project_grant(request: Request) -> Response:
        return await project_grant(request, True)

    @server.custom_route("/api/projects/{name}/revoke", methods=["POST"])
    @_bad_request
    async def api_project_revoke(request: Request) -> Response:
        return await project_grant(request, False)

    @server.custom_route("/api/doors", methods=["GET"])
    async def api_doors(request: Request) -> Response:
        denied = admin_only(request)
        if denied:
            return denied
        return JSONResponse(store.doors())

    @server.custom_route("/api/doors", methods=["POST"])
    @_bad_request
    async def api_door_open(request: Request) -> Response:
        denied = admin_only(request)
        if denied:
            return denied
        body = await request.json()
        a, b = store.person_by_name(str(body["a_person"])), store.person_by_name(str(body["b_person"]))
        try:
            if a is None or b is None:
                raise KaptyorkaError("no such person")
            did = store.open_door_group(a["id"], b["id"],
                                        request.scope["state"]["principal"].person_name)
        except KaptyorkaError as e:
            return PlainTextResponse(str(e), status_code=400)
        return JSONResponse({"id": did})

    @server.custom_route("/api/doors/{door_id:int}/close", methods=["POST"])
    async def api_door_close(request: Request) -> Response:
        denied = admin_only(request)
        if denied:
            return denied
        try:
            store.close_door(request.path_params["door_id"],
                             request.scope["state"]["principal"].person_name)
        except KaptyorkaError as e:
            return PlainTextResponse(str(e), status_code=404)
        return Response(status_code=204)

    @server.custom_route("/api/knocks", methods=["GET"])
    async def api_knocks(request: Request) -> Response:
        principal = request.scope.get("state", {}).get("principal")
        if principal is None:
            return PlainTextResponse("unauthorized", status_code=401)
        return JSONResponse(hub_knocks(store, principal))

    @server.custom_route("/api/knocks/{kid:int}/answer", methods=["POST"])
    @_bad_request
    async def api_knock_answer(request: Request) -> Response:
        body = await request.json()
        if body["answer"] not in ("yes", "no"):
            raise ValueError('answer must be "yes" or "no"')
        p, kid, yes = request.scope["state"]["principal"], request.path_params["kid"], \
            body["answer"] == "yes"
        # the viewer's earliest online session answers; with none, a yes opens a group door
        mine = [a for a in store.online() if a.person_id == p.person_id]
        try:
            scope = (store.answer(kid, mine[0].key, yes) if mine
                     else store.answer_by_person(kid, p.person_id, yes))
        except KaptyorkaError as e:
            return PlainTextResponse(str(e), status_code=400)
        wake_bridges()
        return JSONResponse({"door": scope or None})

    @server.custom_route("/i/{code}", methods=["GET"])
    async def invite_page(request: Request) -> Response:
        code = request.path_params["code"]
        headers = {"Cache-Control": "no-store", "Content-Security-Policy": CSP}
        on = signin.status(store) is None
        if not store.invite_valid(code):
            # used by join, not expired: the person can still link their GitLab, until they do
            who = signin.invite_person(store, code) if on else None
            if who is None or who["gitlab_user"]:
                return HTMLResponse(INVITE_PAGE.format(body="<p>This invite is not valid.</p>"),
                                    status_code=404, headers=headers)
            body = ("<h1>Join the team hub</h1>"
                    + f"<p>This invite is for {html.escape(who['name'])}.</p>"
                    + INVITE_GITLAB.format(action=html.escape(f"/i/{code}/gitlab")))
            return HTMLResponse(INVITE_PAGE.format(body=body), headers=headers)
        cmd = html.escape(f"kaptyorka join {invite_url(request, code)}")
        if on:  # sign in first; the join command still works afterwards
            who = signin.invite_person(store, code)
            body = ("<h1>Join the team hub</h1>"
                    + (f"<p>This invite is for {html.escape(who['name'])}.</p>" if who else "")
                    + INVITE_GITLAB.format(action=html.escape(f"/i/{code}/gitlab"))
                    + "<p>Then, on your computer, run this or paste the link to your agent:</p>"
                    f"<pre>{cmd}</pre><p>This link works for 7 days and joins one machine.</p>")
        else:
            body = (f"<h1>Join the team hub</h1><pre>{cmd}</pre>"
                    "<p>Paste this link to your agent and say: join this.</p>"
                    "<p>This link works for 7 days, once.</p>"
                    + INVITE_WEB.format(action=html.escape(f"/i/{code}/web")))
        return HTMLResponse(INVITE_PAGE.format(body=body), headers=headers)

    @server.custom_route("/i/{code}", methods=["POST"])
    @_bad_request
    async def invite_redeem(request: Request) -> Response:
        code = request.path_params["code"]
        key = (request.client.host if request.client else "?", code)
        if limiter.blocked(key):
            return PlainTextResponse("too many failed attempts", status_code=429)
        body = await request.json()
        person = ws.invite_person(code)  # read first: authenticating would mark the bridge seen
        try:
            token, secret = store.invite_redeem(code, str(body["device_name"]),
                                                safe_name(str(body.get("host", ""))),
                                                safe_name(str(body.get("os_user", ""))))
        except KaptyorkaError as e:
            limiter.hit(key)
            return PlainTextResponse(str(e), status_code=400)
        return JSONResponse({"token": token, "secret": secret, "hub": own_url(request),
                             "person": person},
                            headers={"Cache-Control": "no-store"})

    @server.custom_route("/i/{code}/web", methods=["POST"])
    async def invite_web(request: Request) -> Response:
        """Use the invite for this browser: a device named `web` whose token the page keeps."""
        code = request.path_params["code"]
        key = (request.client.host if request.client else "?", code)
        if limiter.blocked(key):
            return PlainTextResponse("too many failed attempts", status_code=429)
        if signin.status(store) is None:  # its token could not open the page
            return PlainTextResponse("sign in with GitLab instead", status_code=403)
        try:
            token, _ = store.invite_redeem(code, "web")
        except KaptyorkaError as e:
            limiter.hit(key)
            return PlainTextResponse(str(e), status_code=400)
        headers = {"Cache-Control": "no-store"}
        if request.headers.get("content-type", "").startswith("application/x-www-form-urlencoded"):
            # the invite page's form: open the page, which keeps the token like a #t= link
            return Response(status_code=303, headers={**headers, "Location": f"/#t={token}"})
        return JSONResponse({"token": token}, headers=headers)

    def signin_page(text: str, status_code: int) -> Response:
        body = (f"<h1>Sign in</h1><p>{html.escape(text[:1].upper() + text[1:])}.</p>"
                "<p><a href='/'>Back to the sign-in page</a></p>")
        return HTMLResponse(INVITE_PAGE.format(body=body), status_code=status_code,
                            headers={**NO_STORE, "Set-Cookie": CLEAR_COOKIE,
                                     "Content-Security-Policy": CSP})

    @server.custom_route("/auth/gitlab", methods=["GET"])
    async def auth_gitlab(request: Request) -> Response:
        """For the lock screen: whether to show Sign in with GitLab."""
        return JSONResponse({"on": signin.status(store) is None}, headers=NO_STORE)

    def state_cookie(state: str) -> str:
        return (f"{signin.COOKIE}={state}; Path=/auth/gitlab; HttpOnly; SameSite=Lax;"
                f" Max-Age={signin.STATE_S}")

    @server.custom_route("/auth/gitlab/start", methods=["GET"])
    async def auth_gitlab_start(request: Request) -> Response:
        # GitLab sends the browser back to the address it reached the hub on
        try:
            to, state = si.start(hub_url(request) + signin.CALLBACK)
        except KaptyorkaError as e:
            return signin_page(str(e), 404)
        return Response(status_code=302, headers={**NO_STORE, "Location": to,
                                                  "Set-Cookie": state_cookie(state)})

    def cross_site(request: Request) -> bool:
        """A form sent from another site: it could link the visitor's GitLab to someone else."""
        site = request.headers.get("sec-fetch-site")
        if site is not None and site not in ("same-origin", "none"):
            return True
        origin = request.headers.get("origin")
        return origin is not None and urlparse(origin).netloc != (request.headers.get("host") or "")

    @server.custom_route("/i/{code}/gitlab", methods=["POST"])
    async def invite_gitlab(request: Request) -> Response:
        """The invite page's Sign in with GitLab: links the invite's person to the GitLab user
        who signs in, while they are unlinked. The invite stays good for join."""
        code = request.path_params["code"]
        key = (request.client.host if request.client else "?", code)
        if limiter.blocked(key):
            return PlainTextResponse("too many failed attempts", status_code=429)
        if cross_site(request):
            return signin_page("open your invite link again and sign in from there", 403)
        if signin.invite_person(store, code) is None:
            limiter.hit(key)
            return signin_page("this invite link is no longer valid; ask the admin for a new one", 404)
        try:
            to, state = si.start(hub_url(request) + signin.CALLBACK, ("invite", code))
        except KaptyorkaError as e:
            return signin_page(str(e), 404)
        return Response(status_code=303, headers={**NO_STORE, "Location": to,
                                                  "Set-Cookie": state_cookie(state)})

    @server.custom_route("/api/me/gitlab", methods=["POST"])
    async def api_me_gitlab(request: Request) -> Response:
        """The page's Connect GitLab: the same sign-in, bound to the person signed in."""
        p = request.scope["state"]["principal"]
        if not signin.can_connect(store, p):
            return PlainTextResponse("you cannot connect GitLab here", status_code=400)
        try:
            to, state = si.start(hub_url(request) + signin.CALLBACK, ("page", p.person_id))
        except KaptyorkaError as e:
            return PlainTextResponse(str(e), status_code=400)
        return JSONResponse({"url": to}, headers={**NO_STORE, "Set-Cookie": state_cookie(state)})

    @server.custom_route(signin.CALLBACK, methods=["GET"])
    async def auth_gitlab_callback(request: Request) -> Response:
        q = request.query_params
        if q.get("error"):
            return signin_page("sign-in was cancelled", 400)
        try:
            token, _ = await asyncio.to_thread(si.finish, q.get("state", ""), q.get("code", ""),
                                               request.cookies.get(signin.COOKIE, ""))
        except KaptyorkaError as e:
            return signin_page(str(e), 403)
        # the page keeps the token like a #t= link; it never shows in the address bar
        t = json.dumps(token)
        script = (f"<!doctype html><meta charset=utf-8><title>kaptyorka</title><script>"
                  f"try{{localStorage.setItem(\"kaptyorka.token\",{t});location.replace(\"/\")}}"
                  f"catch(e){{location.replace(\"/#t=\"+encodeURIComponent({t}))}}</script>")
        return HTMLResponse(script, headers={**NO_STORE, "Set-Cookie": CLEAR_COOKIE,
                                             "Content-Security-Policy": CSP,
                                             "Referrer-Policy": "no-referrer"})

    @server.custom_route("/api/me/signout", methods=["POST"])
    async def api_signout(request: Request) -> Response:
        """Sign out: a GitLab sign-in ends on the hub too; a device token stays good."""
        si.signout(request.scope["state"]["principal"])
        return Response(status_code=204)

    @server.custom_route("/", methods=["GET"])
    async def page(request: Request) -> Response:
        html = (importlib.resources.files("kaptyorka") / "web/index.html").read_text("utf-8")
        return HTMLResponse(html, headers={"Cache-Control": "no-store", "Content-Security-Policy": CSP})

    @server.custom_route("/fonts/{name}", methods=["GET"])
    async def font(request: Request) -> Response:
        name = request.path_params["name"]
        if name not in FONTS:  # only the bundled files: no other path is ever read
            return PlainTextResponse("not found", status_code=404)
        data = (importlib.resources.files("kaptyorka") / "web/fonts" / name).read_bytes()
        return Response(data, media_type="font/woff2",
                        headers={"Cache-Control": "public, max-age=86400"})

    async def signin_loop() -> None:
        while True:
            await asyncio.sleep(60)
            try:
                await asyncio.to_thread(si.recheck)
            except Exception as e:  # one bad check must not stop the next
                print(f"kaptyorka: GitLab sign-in check failed: {type(e).__name__}", flush=True)

    async def tick_loop() -> None:
        while True:
            try:
                tick()
            except Exception as e:  # one bad tick must not stop the workers' clock
                print(f"kaptyorka: worker tick failed: {e}", flush=True)
            await asyncio.sleep(TICK_S)

    app = server.streamable_http_app(host="0.0.0.0")
    app.add_middleware(Auth, store=store, limiter=limiter, gate=page_gate)
    app.add_middleware(NoWebSocket)  # outside Auth: a stray WebSocket client is not a failed login
    quiet_upgrade_warnings()
    app.state.tick = tick  # tests run a tick by hand
    app.state.tell = tell  # and send a hub notice
    app.state.signin = si
    inner = app.router.lifespan_context

    @contextlib.asynccontextmanager
    async def lifespan(a):
        async with inner(a) as state:
            loop = asyncio.create_task(tick_loop())
            checks = asyncio.create_task(signin_loop())
            try:
                yield state
            finally:
                loop.cancel()
                checks.cancel()
    app.router.lifespan_context = lifespan
    return app
