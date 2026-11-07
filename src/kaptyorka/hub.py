"""The hub app: MCP tools and plain routes over HTTP behind per-device tokens."""
import asyncio
import contextlib
import functools
import html
import importlib.resources
import inspect
import math
import mimetypes
import re
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from dataclasses import asdict, replace
from typing import Dict, List, Mapping, Optional, Union
from urllib.parse import unquote

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response

from kaptyorka import __version__, brief, identity, manager, paths
from kaptyorka import rules as rules_mod
from kaptyorka.auth import Auth, RateLimiter, bearer
from kaptyorka.brief import message_text
from kaptyorka.hub_feed import feed, session_row
from kaptyorka.hub_feed import knocks as hub_knocks
from kaptyorka.manager import ManagerStore
from kaptyorka.memory import MemoryStore, check_state
from kaptyorka.store import MAX_FILE, MAX_IDENT, Agent, KaptyorkaError, Principal, Store, clean
from kaptyorka.workers import MAX_FACT, WorkerStore, _git as clean_git, clean_facts, safe_name

MAX_WAIT_S = 25
TICK_S = 15
OWN_TOKEN = ("use your own device token: run kaptyorka join with an invite, or ask the admin to"
             " assign this machine")
ON_SHARED = "a worker on a shared account cannot start, adopt or close workers; ask your boss"
CSP = ("default-src 'none'; connect-src 'self'; script-src 'unsafe-inline'; "
       "style-src 'unsafe-inline'; img-src 'self' data:")

INVITE_PAGE = ("<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width'>"
               "<title>kaptyorka</title><style>body{{background:#111418;color:#e6e8eb;"
               "font:16px system-ui,sans-serif;max-width:40rem;margin:4rem auto;padding:0 1rem}}"
               "pre{{background:#1b2027;padding:1rem;border-radius:6px;overflow-x:auto}}</style>"
               "{body}")

# the invite page's second way in, without script: the form's POST redirects to the page
INVITE_WEB = ("<form method=post action='{action}'><button style='font:inherit;padding:.5rem 1rem;"
              "border-radius:4px;border:1px solid #3d71d9;background:#3d71d9;color:#fff;"
              "cursor:pointer'>Open the page as this person</button></form>")

INSTRUCTIONS = (
    "kaptyorka connects you with the other coding agents on this team. "
    "who() lists online sessions; send(to, text) messages one by name or 'all'. "
    "You may message your own person's sessions; for anyone else, knock(to, about) first "
    "and wait for a yes; answer(knock_id, 'yes' or 'no') replies to a knock, only after "
    "asking your person; door_close(to) ends a door; "
    "inbox() reads your messages; status(text) says what you are working on; "
    "notes_list/notes_read/notes_write share a team board, one text per topic; "
    "pass project to use that project's notes. projects() lists the projects you have a role in. "
    "work(project, task) starts a worker session for you on another machine; workers() lists "
    "yours; adopt(name) takes over an unattended one; close_worker(name) ends one. "
    "state(project) reads the project's state note; state_write(project, text) updates it "
    "(five headings: What it is, Where it stands, Decisions, Open problems, How to run and test); "
    "decide(project, text) records one decision. "
    "rules() reads the team rules; propose_rule(text, evidence) suggests one after your person "
    "corrected you twice; conflict(rule_a, rule_b) reports two rules that clash."
)


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
    brief_cache: dict = {}  # per session key; see brief.cached
    decide_lock = threading.Lock()
    work_decide_lock = threading.Lock()  # one decision per request, even when two race
    started = {"first": True}  # the first tick closes a manager left running while off

    def principal_of(headers: Mapping) -> Principal:
        """The caller's principal from its bearer token (the Auth middleware already let it in)."""
        machine = unquote(headers.get("x-kaptyorka-machine") or "") or None
        principal = store.authenticate(bearer(headers), machine=machine)
        if principal is None:
            raise KaptyorkaError("unauthorized")
        return principal

    def identify(h, principal: "Principal | None" = None) -> Agent:
        # values are percent-encoded by Identity.headers(), since headers must be ASCII
        ident = [unquote(h.get(f"x-kaptyorka-{k}") or "") for k in ("key", "machine", "tool", "project")]
        ident[1:] = [clean(v, MAX_IDENT) for v in ident[1:]]  # one line: names and push texts
        if not all(ident):
            raise KaptyorkaError("no kaptyorka identity; run kaptyorka join")
        return store.seen(*ident, principal=principal or principal_of(h))

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
        """Online sessions: name, label, person, machine, tool, project, status, activity, last
        seen. A project you may not see shows as (private), in the name too."""
        agent, principal = caller(ctx)
        now = store.clock()
        names = {p["id"]: p["name"] for p in store.people()}
        live = ws.live_keys()
        mine = ws.by_key(agent.key) if agent.key in live else None
        boss = ws.boss_key(mine["name"]) if mine else None  # a worker sees its boss marked
        rows, mid = [], mm.person_id()
        for a in store.online():
            row = {**session_row(store, a, principal, names, now, live, ws, mid),
                   "you": a.key == agent.key}
            if a.key == boss:
                row["boss"] = True
            rows.append(row)
        return rows

    def wake_bridges():
        sent["event"].set()
        sent["event"] = asyncio.Event()

    def tell(to_key: str, text: str) -> None:
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
        if token or mm.rules_written_version() != rules_now["version"] \
                or not (folder / "CLAUDE.md").exists():
            folder.mkdir(mode=0o700, parents=True, exist_ok=True)
            mm.write_files(folder, manager_url, token, rules_now["text"])
            mm.mark_rules_written(rules_now["version"])
        # put back at once when changed or removed; the shim is this hub's own executable
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        manager.write_launch_files(folder, str(Path(sys.argv[0]).resolve()))

    def manager_pushes(now: float) -> None:
        """Tell the online manager session what needs the admin; each item once."""
        key = mm.session_key(now) if mm.enabled() else None
        if key is None:
            return

        def push(push_key: str, ttl: Optional[float], text: str) -> None:
            if mm.push_once(push_key, now, ttl):
                ws.notify(key, text)
        for w in ws.abandoned():
            push(f"abandoned:{w['name']}", None,
                 f"worker `{w['name']}` of {w['person']} is abandoned (unattended a day)"
                 f" on {w['host']}; tell the admin in one line")
        for d in ws.shared_offline(now):
            push(f"offline:{d['id']}", 86400,
                 f"bridge on {d['host']} ({d['os_user']}), a shared machine, has been offline"
                 f" {d['minutes']} minutes; tell the admin")
        for f in ws.failed_twice(now):
            push(f"failed:{f['person_id']}", 3600,
                 f"{f['person']}'s workers failed to start twice ({f['error']}); tell the admin")
        old = now - manager.REMIND_AFTER_S
        for p in rs.proposals("open"):
            if p["time"] < old:
                push(f"remind:proposal:{p['id']}", 86400,
                     f"proposal from {p['person']} has waited a day: {p['text'][:80]};"
                     " remind the admin")
        for r in store.requests(pending_only=True):
            if r["created"] < old:
                what = f"{r['kind']} {r['target']}: {r['why']}"
                push(f"remind:request:{r['id']}", 86400,
                     f"access request from {r['person']} has waited a day: {what[:80]};"
                     " remind the admin")
        push("weekly", manager.WEEKLY_S,
             "weekly review: read log(500) for repeated conflicts and corrections; file"
             " propose_rule for what repeats; never edit rules")

    def manager_tick(now: float) -> None:
        if started["first"]:
            started["first"] = False
            close_if_off()
        keep_manager(now)
        manager_pushes(now)

    def close_if_off() -> None:
        """A hub started with the manager switched off closes a manager still running."""
        if mm.enabled() or ws.close_manager() is None:
            return
        store.log_add("hub", "manager", "closed: switched off")
        wake_bridges()

    def keep_manager(now: float) -> None:
        """Keep the manager session alive through the admin's own bridge on this machine."""
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
            if admin is not None:
                names = {p["id"]: p["name"] for p in store.people()}
                tell(f"web:{names.get(admin['person_id'], '')}", MANAGER_UNTRUSTED)
        if admin is not None and mm.failures_last_hour(now) >= 2 \
                and mm.push_once("manager-failed", now, 3600):
            names = {p["id"]: p["name"] for p in store.people()}
            tell(f"web:{names.get(admin['person_id'], '')}", MANAGER_FAILED)
        ok, _ = mm.may_launch(now)
        if not ok or admin is None:
            return
        folder = paths.home() / manager.DIR
        ws.create_manager(admin["person_id"], mm.person_id(), admin, str(folder), MANAGER_FIRST)
        mm.record_launch(now)
        wake_bridges()

    def tick() -> None:
        """Move workers on; deliver what the bosses hear; keep the manager alive."""
        now = store.clock()
        for to_key, text in ws.tick(now):
            tell(to_key, text)
        try:
            manager_tick(now)
        except Exception as e:  # the manager must never stop the workers' clock
            print(f"kaptyorka: manager tick failed: {e}", flush=True)

    @server.tool()
    @_tool_errors
    async def send(to: str, text: str, ctx: Context, file: str = "") -> str:
        """Send text to one session by name or label, or to 'all'. file: the local path of one
        file to attach (png jpg gif webp svg txt md log json csv pdf, up to 5 MB)."""
        file_id = None
        if file:
            m = re.fullmatch(r"id:(\d+)", file)
            if m is None:
                raise KaptyorkaError("attach a file by passing its local path; "
                                     "the kaptyorka tool uploads it")
            file_id = int(m.group(1))
        key = me(ctx).key
        n = None if file_id is not None else ws.send_via_manager(key, to, text, mm.person_id())
        if n is None:
            n = store.send(key, to, text, file_id)
        wake_bridges()
        return f"sent to {n} session" + ("" if n == 1 else "s")

    @server.tool()
    @_tool_errors
    async def knock(to: str, about: str, ctx: Context) -> str:
        """Ask another person's session (name or label), or the person by name, to open a door
        so you can message it. Say in about what you want."""
        kid = store.knock(me(ctx).key, to, about)
        wake_bridges()
        return f"knocked (#{kid}); waiting for an answer"

    @server.tool()
    @_tool_errors
    async def answer(knock_id: int, answer: str, ctx: Context) -> str:
        """Answer a knock with "yes" (opens a door with this session) or "no". Ask your person
        first."""
        if answer not in ("yes", "no"):
            raise KaptyorkaError('answer must be "yes" or "no"')
        store.answer(knock_id, me(ctx).key, answer == "yes")
        wake_bridges()
        return "door open" if answer == "yes" else "knock declined"

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
        return [{"from": m.sender, "text": m.text, "time": m.created, "kind": m.kind,
                 "file": m.file} for m in store.inbox(me(ctx).key)]

    @server.tool()
    @_tool_errors
    def fetch_file(id: int, ctx: Context) -> Dict[str, Union[int, str]]:
        """Download a file attached to a message you sent or received (id from inbox)."""
        _, principal = caller(ctx)
        info = store.file_meta(id, principal)
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
        Decisions, Open problems, How to run and test, in that order."""
        agent, principal = caller(ctx)
        if not project:
            raise KaptyorkaError("project is empty")
        problem = check_state(text)
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
        (kind "machine"), with a short reason."""
        _, principal = caller(ctx)
        rid = store.request_access(principal.person_id, kind, target, why)
        return f"requested #{rid}; the admin sees it on the page"

    def foreign(principal: Principal, person_id: Optional[int]) -> Optional[int]:
        """The person behind a session on a shared device that is not the device's owner (a
        worker the bridge there started for someone else); None for the owner's own."""
        if principal.shared and person_id is not None and person_id != principal.person_id:
            return person_id
        return None

    def page_foreign(request: Request, principal: Principal) -> Optional[int]:
        key = unquote(request.headers.get("x-kaptyorka-key") or "")
        return foreign(principal, ws.session_person(key)) if key else None

    def own_workers(principal: Principal, other: Optional[int] = None) -> List[dict]:
        if other is not None:  # a session of another person on a shared device: theirs only
            return ws.list(replace(principal, person_id=other, role="member"))
        # the shared token is no admin here: anyone may hold it
        return ws.list(replace(principal, role="member") if principal.legacy else principal)

    def no_machine(project: str, machine: Optional[str], tool: str, usable: List[dict],
                   reasons: List[str]) -> str:
        if machine is not None and not usable and not reasons:
            reasons = [f"no machine named {machine} that you may use"]
        for c in usable:
            who = f"{c['host']} ({c['os_user']})"
            if not c.get("folder"):
                reasons.append(f"{who}: no folder for {project}")
            elif tool != "claude-code" and not c.get("tmux"):
                reasons.append(f"{who}: no tmux (only Claude Code workers can run there)")
        return f"no machine can take {project}: " + ("; ".join(reasons)
                                                     or "no machine you may use is joined")

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
        if foreign(principal, agent.person_id) is not None:
            raise KaptyorkaError(ON_SHARED)
        if not (store.may_write_project(project, principal)
                or (agent.project == project and not ws.registered(project))):
            raise KaptyorkaError(f"you may not start workers in {project}: that takes the write"
                                 " role (or your own unregistered folder)")
        tool = clean(tool, MAX_IDENT) or "claude-code"
        usable, reasons = ws.candidates(principal, project, machine or None)
        chosen = ws.choose(usable, project, tool, store.clock(), principal.person_id)
        if chosen is None:
            return no_machine(project, machine or None, tool, usable, reasons)
        if chosen["occupied"]:
            others = [n for n in ws.folder_people(chosen["host"], chosen["os_user"],
                                                  chosen["folder"], store.clock())
                      if n and n != principal.person_name]
            manager_key = mm.session_key(store.clock()) if others and mm.enabled() else None
            if manager_key is not None:
                rid = mm.request_add(principal.person_id, agent.key, project, task, tool,
                                     resume or None, chosen["host"], others[0],
                                     chosen["folder"])
                tell(manager_key, manager.text_busy(principal.person_name, project,
                                                    chosen["host"], others[0],
                                                    chosen["folder"], rid))
                return (f"busy: {chosen['host']} has {others[0]}'s session in that folder; the"
                        f" manager is asking them (request {rid}); you will hear back here")
            if others:  # no manager online: the asker picks again
                return (f"busy: {chosen['host']} has {others[0]}'s session in that folder;"
                        " ask again later or name another machine")
        w = ws.create(principal, project, task, chosen, tool, agent.key, resume or None,
                      chosen["occupied"])
        wake_bridges()
        return (f"starting `{w['name']}` on {w['host']} ({w['os_user']})"
                + (" in a worktree, the folder is busy" if w["worktree"] else ""))

    @server.tool()
    @_tool_errors
    def work_decide(request_id: int, machine: str, reason: str, ctx: Context) -> str:
        """Manager only: decide a work request. machine: a host to start the worker on, or
        "wait". reason: why, 1 to 500 characters."""
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
        if r["decided_at"] is not None:
            raise KaptyorkaError(f"request {request_id} is already decided: {r['decision']}")
        now = store.clock()
        names = {p["id"]: p["name"] for p in store.people()}
        page = "web:" + names.get(r["person_id"], "")
        online = ws.session_online(r["boss_key"], now)
        asker = r["boss_key"] if online else page
        project = r["project"]
        if machine == "wait":
            mm.request_decide(request_id, "wait", reason)
            tell(asker, f"the manager says wait on {project}: {reason}")
            store.log_add("manager", "work_decide", f"request {request_id}: wait — {reason}")
            return f"request {request_id}: told them to wait"
        row = next((p for p in store.people() if p["id"] == r["person_id"]), None)
        boss = store.agent(r["boss_key"])
        who = replace(principal, person_id=r["person_id"], person_name=row["name"] if row else "",
                      role=row["role"] if row else "member")  # the asker's rights, not the manager's
        if not (store.may_write_project(project, who)
                or (boss is not None and boss.project == project and not ws.registered(project))):
            mm.request_decide(request_id, "refused", reason)
            note = f"the manager could not start {project} for you: you no longer have the write role"
            tell(asker, note)
            store.log_add("manager", "work_decide", f"request {request_id}: refused — {reason}")
            return note
        usable, reasons = ws.candidates_for(r["person_id"], project, machine)
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
        if foreign(principal, agent.person_id) is not None:
            raise KaptyorkaError(ON_SHARED)
        w = ws.adopt(principal, name, agent.key)
        wake_bridges()
        return f"you are now the boss of `{name}`; {w['delivered']} held messages delivered"

    @server.tool()
    @_tool_errors
    async def close_worker(name: str, ctx: Context) -> str:
        """End one of your workers (its session on that machine is stopped)."""
        agent, principal = caller(ctx)
        if principal.legacy:
            raise KaptyorkaError(OWN_TOKEN)
        if foreign(principal, agent.person_id) is not None:
            raise KaptyorkaError(ON_SHARED)
        w = ws.close(principal, name)
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
        if principal.role not in ("admin", "manager"):
            raise KaptyorkaError("the log is for the admin and the manager")
        return store.log(max(1, min(limit, 1000)))

    @server.tool()
    @_tool_errors
    def propose_rule(text: str, evidence: str, ctx: Context) -> str:
        """Suggest a team rule when your person corrected you the same way twice. Say what
        the rule is and what happened (evidence). The admin decides."""
        agent, principal = caller(ctx)
        if principal.legacy:
            raise KaptyorkaError(OWN_TOKEN)
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
        store.heartbeat(body["machine"], body["sessions"], body.get("tools", ()),
                        principal=principal, facts=facts if isinstance(facts, dict) else None)
        if principal is None:
            return Response(status_code=204)
        own = not principal.legacy
        if own and (isinstance(facts, dict) or isinstance(body.get("folders"), dict)):
            ws.facts_seen(principal.device_id, facts, body.get("folders"))
            try:  # history is a bonus: a bad git payload must not fail the heartbeat
                ms.git_seen(principal, principal.device_id,
                            clean_facts(facts).get("git") or {},
                            body.get("folders") if isinstance(body.get("folders"), dict) else {})
            except Exception as e:
                print(f"memory: git facts skipped: {e}", file=sys.stderr, flush=True)
        return JSONResponse({"version": __version__,
                             "auto_update": own and ws.auto_update(principal.device_id),
                             "projects": [] if principal.legacy else
                             [p["name"] for p in store.projects_for(principal)],
                             "you": principal.person_name, "shared": principal.shared,
                             "skills_version": rs.skills_version()})

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
            if not ws.ack(int(body["id"]), body["ok"] is True, str(body.get("error") or ""),
                          principal.device_id):
                return PlainTextResponse("no such launch", status_code=404)
            return Response(status_code=204)
        if not store.ack(int(body["id"]), body["ok"] is True, principal):
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
        store.page_seen(p)  # only the page asks who it is
        return JSONResponse({"person": p.person_name, "role": p.role, "device": p.device_name,
                             "person_id": p.person_id})

    @server.custom_route("/api/feed", methods=["GET"])
    async def api_feed(request: Request) -> Response:
        return JSONResponse(feed(store, request.scope["state"]["principal"], ws))

    @server.custom_route("/api/files", methods=["POST"])
    async def api_file_upload(request: Request) -> Response:
        principal = request.scope["state"]["principal"]
        name = unquote(request.headers.get("x-file-name") or "") or "file"
        def too_large(n):
            return PlainTextResponse(f"file too large: {n} bytes, limit {MAX_FILE}", status_code=400)

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
        return Response(data, media_type=mimetypes.guess_type(info["name"])[0]
                        or "application/octet-stream",
                        headers={"Content-Disposition": f'attachment; filename="{safe or "file"}"',
                                 "X-Content-Type-Options": "nosniff"})

    @server.custom_route("/api/send", methods=["POST"])
    @_bad_request
    async def api_send(request: Request) -> Response:
        body = await request.json()
        p = request.scope["state"]["principal"]  # the body's "from" (0.1 pages) is ignored
        try:
            n = ws.send_web_via_manager(p.person_name, str(body["to"]), str(body["text"]),
                                        p.person_id, mm.person_id())
            if n is None:
                n = store.send_from_web(p.person_name, str(body["to"]), str(body["text"]),
                                        person_id=p.person_id)
        except KaptyorkaError as e:
            return PlainTextResponse(str(e), status_code=400)
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
        return f"http://{request.headers.get('host') or request.url.netloc}"

    @server.custom_route("/api/log", methods=["GET"])
    @_bad_request
    async def api_log(request: Request) -> Response:
        denied = admin_only(request)
        if denied:
            return denied
        q = request.query_params
        limit = max(1, min(int(q.get("limit") or 200), 1000))
        return JSONResponse(store.log(limit, q.get("person") or None))

    @server.custom_route("/api/settings", methods=["GET"])
    async def api_settings(request: Request) -> Response:
        denied = admin_only(request)
        if denied:
            return denied
        return JSONResponse({"retention_days": store.retention_days()})

    @server.custom_route("/api/settings", methods=["POST"])
    @_bad_request
    async def api_settings_set(request: Request) -> Response:
        denied = admin_only(request)
        if denied:
            return denied
        body = await request.json()
        try:
            store.set_retention_days(body.get("retention_days"),
                                     request.scope["state"]["principal"].person_name)
        except KaptyorkaError as e:
            return PlainTextResponse(str(e), status_code=400)
        return JSONResponse({"retention_days": store.retention_days()})

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
        return JSONResponse(rs.history())

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
            text = f"Your rule proposal {pid} was rejected by {by}: {reason}"
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
        try:
            pid = store.person_create(str(body["name"]), str(body.get("role", "member")),
                                      by=request.scope["state"]["principal"].person_name)
        except KaptyorkaError as e:
            status = 409 if "already exists" in str(e) else 400
            return PlainTextResponse(str(e), status_code=status)
        return JSONResponse({"id": pid})

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
        code = store.invite_create(person["id"], by=request.scope["state"]["principal"].person_name)
        return JSONResponse({"code": code, "url": f"{hub_url(request)}/i/{code}"})

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
        return Response(status_code=204)

    @server.custom_route("/api/workers", methods=["GET"])
    async def api_workers(request: Request) -> Response:
        principal = request.scope.get("state", {}).get("principal")
        if principal is None:
            return PlainTextResponse("unauthorized", status_code=401)
        return JSONResponse(own_workers(principal, page_foreign(request, principal)))

    async def worker_action(request: Request, close: bool) -> Response:
        principal = request.scope.get("state", {}).get("principal")
        if principal is None:
            return PlainTextResponse("unauthorized", status_code=401)
        if principal.legacy:
            return PlainTextResponse(OWN_TOKEN, status_code=403)
        if page_foreign(request, principal) is not None:
            return PlainTextResponse(ON_SHARED, status_code=403)
        name = request.path_params["name"]
        try:
            # the page acts as the person: a web boss hears its notices on the page
            w = (ws.close(principal, name) if close
                 else ws.adopt(principal, name, f"web:{principal.person_name}"))
        except KaptyorkaError as e:
            return PlainTextResponse(str(e), status_code=400)
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
        denied = admin_only(request)
        if denied:
            return denied
        return JSONResponse(ws.warnings(store.clock(), __version__))

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
                             "branches": ms.branches_with_sessions(project, store.clock())})

    @server.custom_route("/api/requests", methods=["GET"])
    async def api_requests(request: Request) -> Response:
        principal = request.scope.get("state", {}).get("principal")
        if principal is None:
            return PlainTextResponse("unauthorized", status_code=401)
        if principal.is_admin():
            return JSONResponse(store.requests())
        return JSONResponse(store.pending_for(principal.person_id))

    @server.custom_route("/api/requests/{rid:int}", methods=["POST"])
    @_bad_request
    async def api_request_decide(request: Request) -> Response:
        denied = admin_only(request)
        if denied:
            return denied
        body = await request.json()
        if not isinstance(body["approve"], bool):
            raise ValueError("approve must be true or false")
        try:
            store.decide_request(request.path_params["rid"],
                                 request.scope["state"]["principal"].person_id, body["approve"])
        except KaptyorkaError as e:
            return PlainTextResponse(str(e), status_code=400)
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
        headers = {"Cache-Control": "no-store"}
        if not store.invite_valid(code):
            return HTMLResponse(INVITE_PAGE.format(body="<p>This invite is not valid.</p>"),
                                status_code=404, headers=headers)
        cmd = html.escape(f"kaptyorka join {hub_url(request)}/i/{code}")
        body = (f"<h1>Join the team hub</h1><pre>{cmd}</pre>"
                "<p>Paste this link to your agent and say: join this.</p>"
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
        return JSONResponse({"token": token, "secret": secret, "hub": hub_url(request),
                             "person": person},
                            headers={"Cache-Control": "no-store"})

    @server.custom_route("/i/{code}/web", methods=["POST"])
    async def invite_web(request: Request) -> Response:
        """Use the invite for this browser: a device named `web` whose token the page keeps."""
        code = request.path_params["code"]
        key = (request.client.host if request.client else "?", code)
        if limiter.blocked(key):
            return PlainTextResponse("too many failed attempts", status_code=429)
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

    @server.custom_route("/", methods=["GET"])
    async def page(request: Request) -> Response:
        html = (importlib.resources.files("kaptyorka") / "web/index.html").read_text("utf-8")
        return HTMLResponse(html, headers={"Cache-Control": "no-store", "Content-Security-Policy": CSP})

    async def tick_loop() -> None:
        while True:
            try:
                tick()
            except Exception as e:  # one bad tick must not stop the workers' clock
                print(f"kaptyorka: worker tick failed: {e}", flush=True)
            await asyncio.sleep(TICK_S)

    app = server.streamable_http_app(host="0.0.0.0")
    app.add_middleware(Auth, store=store, limiter=limiter)
    app.state.tick = tick  # tests run a tick by hand
    inner = app.router.lifespan_context

    @contextlib.asynccontextmanager
    async def lifespan(a):
        async with inner(a) as state:
            loop = asyncio.create_task(tick_loop())
            try:
                yield state
            finally:
                loop.cancel()
    app.router.lifespan_context = lifespan
    return app
