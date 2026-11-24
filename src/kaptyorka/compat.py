"""Every agent-tool internal kaptyorka depends on, in one place, each with a probe.

Claude Code, OpenCode and Codex document none of what kaptyorka reads to see sessions and
push to them: session files, an inbox socket, transcript entry types, background task output
files, the `--bg`/`stop`/`rm` commands and their output, a service file, local APIs. A tool
update can change any of them. Each `Dep` below names one, the feature that uses it, and what
that feature does without it (`off`); its probe checks it on a real install.

- `cheap(...)`: the probes that only read (files, `--version`, `--help`, a read-only local API
  call). `kaptyorka doctor` shows them and the bridge sends them in machine facts as
  `claude_compat` (and `opencode_compat`, `codex_compat`): "ok" or the broken ids. The hub
  turns broken ids into a machine warning ("Claude Code 2.1.340 on box-1: background task
  files not found (stuck check off)").
- `full(folder)`: every Claude Code probe, against a real `claude --bg` session it starts in
  `folder` and stops again. Only tests/test_claude_compat_slow.py calls it (real tokens).
- `version_news(...)`: the hub's once-per-version line for the admins when a Claude Code newer
  than CLAUDE_CODE_TESTED appears; CLAUDE_CODE_TESTED moves only once the slow test passes.

A probe answers OK, BROKEN (with the detail) or UNKNOWN (no evidence on this machine now, such
as no live session to read): UNKNOWN is never reported as broken.

Every feature here degrades on its own when its internal breaks (the adapters skip what they
cannot read; tests/test_compat.py checks each); this module makes the break visible."""
import json
import os
import re
import stat
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

OK, BROKEN, UNKNOWN = "ok", "broken", "unknown"
CLAUDE_STATUS = ("busy", "idle", "shell", "waiting")  # session file `status` values seen
TTL_S = 600  # the bridge runs the cheap probes again after this long, or on a new tool version
NEWS_KEY = "compat_news"  # settings row: Claude Code versions the admins were told about
NEWS_WAIT_S = 600  # count the machines this long after the first one shows a new version


@dataclass(frozen=True)
class Dep:
    id: str
    tool: str  # claude-code | opencode | codex
    what: str  # the internal, in one line
    feature: str  # what kaptyorka uses it for
    broken: str  # the warning's words when the probe fails
    off: str  # what the feature does without it
    cheap: bool  # probed by doctor and the bridge (only reads); else only by the slow test
    probed: bool = True  # False: checked against the tool's source only (no probe here)


DEPS: Tuple[Dep, ...] = (
    # Claude Code
    Dep("claude.version", "claude-code", "`claude --version` prints `X.Y.Z (Claude Code)`",
        "the tool version in machine facts", "`claude --version` output changed",
        "version unknown", True),
    Dep("claude.cli", "claude-code", "`claude --help` lists --bg, -n, --settings, --resume, --no-chrome, stop, rm, agents",
        "workers (launch, close)", "`claude --help` no longer lists --bg, stop or rm",
        "Claude Code workers fail to start or stop", True),
    Dep("claude.auth", "claude-code", "`claude auth status` prints JSON with a boolean loggedIn",
        "the login warning", "`claude auth status` gives no loggedIn",
        "login state unknown", True),
    Dep("claude.session_file", "claude-code",
        "~/.claude/sessions/<pid>.json with cwd, sessionId, procStart, version",
        "the session list", "session files not readable",
        "its sessions are not listed", True),
    Dep("claude.status", "claude-code", "session file `status`: busy, idle, shell, waiting",
        "activity and the stuck check", "session status values changed",
        "stuck check off", True),
    Dep("claude.name", "claude-code",
        "session file `name` and `nameSource` (derived: an automatic name; user or peer: one given)",
        "session names and titles", "session names (nameSource) not found",
        "automatic names shown instead of titles", True),
    Dep("claude.kind", "claude-code", "session file `kind`: bg for a background session",
        "worker and manager sessions", "background sessions not marked (kind)",
        "workers not told apart from interactive sessions", True),
    Dep("claude.inbox", "claude-code",
        "session file messagingSocketPath (a Unix socket) and <pid>.*.key with peerToken",
        "push into a session", "inbox socket not found",
        "push off: sessions read the hub on their next turn", True),
    Dep("claude.transcript", "claude-code",
        "~/.claude/projects/<folder>/<sessionId>.jsonl with timestamped lines",
        "titles, last activity, the busy stuck check", "transcripts not found",
        "titles and the busy stuck check off", True),
    Dep("claude.title", "claude-code", "transcript entries ai-title / custom-title",
        "chat titles", "transcript titles not found", "sessions shown by name only", True),
    Dep("claude.tasks", "claude-code",
        "a background task's shell is the session's child, stdout .../tasks/<id>.output",
        "the background task stuck check", "background task files not found",
        "stuck check off for background tasks", True),
    Dep("claude.derived_from", "claude-code",
        "transcript head `derived-from` {sessions: [{id, via: launch | subprocess}]}",
        "sessions left behind (which session started a helper)", "transcript derived-from lines changed",
        "helpers not tied to the session that started them, none closed", True),
    Dep("claude.child_notes", "claude-code",
        "<tmp>/claude-<uid>/<folder>/<id>/started/<child id>.<random> (CLAUDE_CODE_CHILD_NOTES_DIR)",
        "sessions left behind, the stop hook's live helpers", "child notes not found",
        "helpers tied by their transcript only", True),
    Dep("claude.real_lines", "claude-code",
        "transcript user lines: isSidechain, isMeta, isCompactSummary, origin.kind",
        "when a person or the model last did something (left-behind idle time)", "", "", True, probed=False),
    Dep("claude.bg_launch", "claude-code", "`claude --bg -n <name>` prints `backgrounded · <8 hex>`",
        "starting workers", "`claude --bg` printed no session id", "workers fail to start", False),
    Dep("claude.bg_isolation", "claude-code", "--settings worktree.bgIsolation none keeps --bg in its folder",
        "workers in team project folders", "--bg ignored worktree.bgIsolation",
        "workers start in .claude/worktrees, off the hub", False),
    Dep("claude.push", "claude-code", "a {type: user} line on the inbox socket reaches the session",
        "push into a session", "a message on the inbox socket did not arrive",
        "push off: sessions read the hub on their next turn", False),
    Dep("claude.stop", "claude-code", "`claude stop <8 hex>` ends it; again says `No job matching`",
        "closing workers", "`claude stop` output changed", "closing a worker fails", False),
    Dep("claude.rm", "claude-code", "`claude rm <8 hex>` removes a stopped background session",
        "cleaning up after tests", "`claude rm` failed", "stopped sessions stay listed", False),
    # OpenCode
    Dep("opencode.version", "opencode", "`opencode --version` prints X.Y.Z", "V1 or V2 handling",
        "`opencode --version` output changed", "treated as V1", True),
    Dep("opencode.v1_server", "opencode", "V1: a TUI started with --port answers GET /session",
        "V1 sessions and push", "V1 TUI server gives no session list",
        "its sessions are not listed", True),
    Dep("opencode.service", "opencode",
        "V2: $XDG_STATE_HOME/opencode/service.json with url, pid, password",
        "V2 sessions and push", "V2 service.json not readable",
        "its sessions are not listed", True),
    Dep("opencode.api_session", "opencode",
        "V2: GET /api/session lists sessions; POST /api/session/<id>/prompt",
        "V2 sessions and push", "V2 /api/session gives no list",
        "its sessions are not listed", True),
    Dep("opencode.activity", "opencode",
        "session `time.updated` (ms, moves per step); V1 GET /session/status, V2 GET"
        " /api/session/<id> time.busy/time.idle",
        "idle time, working or idle, the busy-stuck check", "session times or status not readable",
        "no idle time or stuck check for OpenCode", True),
    Dep("opencode.limit_events", "opencode",
        "plugin events: V1 `session.error`, V2 session hook `retry` with the error",
        "why a session stopped (usage limit)", "", "", True, probed=False),
    Dep("opencode.tui_plugin", "opencode",
        "V2: a plugins/<dir>/tui.js TUI plugin (sst/opencode v2.3.2 packages/plugin/src/tui)",
        "/hub, the hub indicator, which session a window shows", "", "", True, probed=False),
    # Codex
    Dep("codex.version", "codex", "`codex --version` prints `codex-cli X.Y.Z`",
        "the tool version in machine facts", "`codex --version` output changed",
        "version unknown", True),
    Dep("codex.app_server", "codex",
        "~/.codex/app-server-control/app-server-control.sock: thread/loaded/list, thread/read",
        "the session list", "app-server thread list changed", "its sessions are not listed", True),
    Dep("codex.thread_status", "codex",
        "thread/read `status` {type: idle | active, activeFlags}, parentThreadId, source",
        "working or idle, subagents under their parent", "thread status changed",
        "activity unknown", True),
    Dep("codex.rate_limits", "codex",
        "app-server account/rateLimits/read: rateLimits.primary/secondary {usedPercent,"
        " resetsAt}, ordinaryUsageAllowed (rust-v0.166.0 v2/account.rs)",
        "an account at its Codex usage limit", "", "", False, probed=False),
    Dep("codex.turn_start", "codex", "app-server turn/start {threadId, input} on a loaded thread",
        "push into a session", "", "", False, probed=False),
)
BY_ID: Dict[str, Dep] = {d.id: d for d in DEPS}
FACT = {"claude-code": "claude_compat", "opencode": "opencode_compat", "codex": "codex_compat"}
ID = re.compile(r"^[a-z]+\.[a-z0-9_]{1,40}$")


@dataclass
class Ctx:
    """What the probes read: the home folder (~), how to run a command, and for the slow test
    the facts of the session full() started."""
    home: Path
    run: Callable
    live: Optional[dict] = None  # full(): what the started session did
    versions: Optional[Dict[str, str]] = None  # the bridge's facts: each tool's --version line
    tmp: Optional[Path] = None  # where Claude Code keeps its temp folder (tests)


def _version(ctx: Ctx, tool: str, exe: str) -> Tuple[int, str]:
    """The tool's `--version` first line: the one the bridge's facts already read, if given."""
    if ctx.versions is not None and tool in ctx.versions:
        v = ctx.versions[tool]
        return (0, v) if v else (-1, "no version")
    return _out(ctx, [exe, "--version"])


def _out(ctx: Ctx, argv: List[str], timeout: float = 20, cwd: Optional[Path] = None) -> Tuple[int, str]:
    try:
        r = ctx.run(argv, capture_output=True, text=True, timeout=timeout, **({"cwd": cwd} if cwd else {}))
    except Exception as e:  # not installed, timed out
        return -1, str(e)
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def _claude_sessions(ctx: Ctx) -> List[Tuple[int, dict]]:
    """(pid, data) of the live session files; files of dead processes are skipped."""
    out = []
    for f in sorted((ctx.home / ".claude" / "sessions").glob("*.json")):
        try:
            pid = int(f.stem)
            os.kill(pid, 0)
            data = json.loads(f.read_text())
        except (OSError, ValueError):  # dead (or another user's), or half written
            continue
        if isinstance(data, dict):
            out.append((pid, data))
    return out


# --- Claude Code probes: (state, detail) ------------------------------------------------

def p_claude_version(ctx: Ctx):
    code, out = _version(ctx, "claude-code", "claude")
    if code != 0 or not out.strip():  # no answer is no evidence
        return UNKNOWN, out.strip()[:200]
    return (OK, "") if re.match(r"\d+\.\d+\.\d+ \(Claude Code\)", out.strip()) else (BROKEN, out.strip()[:100])


def p_claude_cli(ctx: Ctx):
    code, out = _out(ctx, ["claude", "--help"])
    if code != 0 or not out.strip():
        return UNKNOWN, out.strip()[:200]
    want = {"--bg": r"--bg\b", "-n, --name": r"-n, --name\b", "--settings": r"--settings\b",
            "--resume": r"--resume\b", "--no-chrome": r"--no-chrome\b", "stop": r"^\s+stop\b", "rm": r"^\s+rm\b", "agents": r"^\s+agents\b"}
    missing = [k for k, rx in want.items() if not re.search(rx, out, re.M)]
    return (BROKEN, "missing " + ", ".join(missing)) if missing else (OK, "")


def p_claude_auth(ctx: Ctx):
    code, out = _out(ctx, ["claude", "auth", "status"])
    if code not in (0, 1) or not out.strip():
        return UNKNOWN, out.strip()[:200]
    m = re.search(r"\{.*\}", out, re.S)  # its JSON; logged out it exits 1 with the same
    try:
        ok = m is not None and isinstance(json.loads(m.group(0)).get("loggedIn"), bool)
    except (ValueError, AttributeError):
        ok = False
    return (OK, "") if ok else (BROKEN, out.strip()[:100])


def p_claude_session_file(ctx: Ctx):
    live = _claude_sessions(ctx)
    if not live:
        return UNKNOWN, "no live session"
    bad = [str(pid) for pid, d in live if not (isinstance(d.get("cwd"), str) and isinstance(d.get("sessionId"), str))]
    if len(bad) == len(live):
        return BROKEN, "no cwd or sessionId in " + ", ".join(bad[:3])
    missing = sorted({k for _, d in live for k in ("procStart", "version") if k not in d})
    return (BROKEN, "missing " + ", ".join(missing)) if missing else (OK, "")


def p_claude_status(ctx: Ctx):
    seen = {str(d.get("status")) for _, d in _claude_sessions(ctx)}
    if not seen:
        return UNKNOWN, "no live session"
    # one known value is enough: a new value beside them is a new state, not a rename
    return (OK, "") if seen & set(CLAUDE_STATUS) else (BROKEN, "seen " + ", ".join(sorted(seen)))


def p_claude_name(ctx: Ctx):
    named = [d for _, d in _claude_sessions(ctx) if isinstance(d.get("name"), str)]
    if not named:
        return UNKNOWN, "no named live session"
    seen = {d.get("nameSource") for d in named}
    return (OK, "") if seen & {"user", "peer", "derived"} else (BROKEN, f"nameSource {sorted(map(str, seen))}")


def p_claude_kind(ctx: Ctx):
    live = _claude_sessions(ctx)
    if not live:
        return UNKNOWN, "no live session"
    return (OK, "") if any(isinstance(d.get("kind"), str) for _, d in live) else (BROKEN, "no kind field")


def p_claude_inbox(ctx: Ctx):
    live = _claude_sessions(ctx)
    if not live:
        return UNKNOWN, "no live session"
    for pid, d in live:
        path = d.get("messagingSocketPath")
        try:
            if isinstance(path, str) and stat.S_ISSOCK(os.stat(path).st_mode):
                keys = sorted((ctx.home / ".claude" / "sessions").glob(f"{pid}.*.key"))
                if not keys or "peerToken" in json.loads(keys[-1].read_text()):
                    return OK, ""
        except (OSError, ValueError, TypeError):
            continue
    return BROKEN, "no live session has a messagingSocketPath socket"


def _transcripts(ctx: Ctx) -> List[Path]:
    from kaptyorka.adapters.claude import _transcript

    out = []
    for _, d in _claude_sessions(ctx):
        try:
            p = _transcript(ctx.home, str(d["sessionId"]), str(d["cwd"]), {})
        except (KeyError, OSError):
            p = None
        if p is not None:
            out.append(p)
    return out


def p_claude_transcript(ctx: Ctx):
    from kaptyorka.adapters.claude import _stamp

    old = [(pid, d) for pid, d in _claude_sessions(ctx)
           if isinstance(d.get("startedAt"), (int, float)) and d["startedAt"] / 1000 < time.time() - 120]
    if not old:
        return UNKNOWN, "no live session older than 2 minutes"
    found = _transcripts(ctx)
    if not found:
        return BROKEN, "no transcript for any live session"
    for p in found:
        try:
            with open(p, "rb") as f:
                f.seek(max(0, p.stat().st_size - 65536))
                lines = f.read().decode("utf-8", "replace").splitlines()
        except OSError:
            continue
        for line in reversed(lines):
            try:
                if _stamp(json.loads(line).get("timestamp")) is not None:
                    return OK, ""
            except (ValueError, AttributeError):
                continue
    return BROKEN, "transcript lines carry no timestamp"


def p_claude_title(ctx: Ctx):
    from kaptyorka.adapters.claude import read_title

    for p in _transcripts(ctx):
        try:
            if read_title(p, p.stat().st_size):
                return OK, ""
        except OSError:
            continue
    # a short session often has no title yet: no evidence either way
    return UNKNOWN, "no live session has a title"


def p_claude_tasks(ctx: Ctx):
    from kaptyorka.adapters.claude import children, live_tasks

    shell = [pid for pid, d in _claude_sessions(ctx) if d.get("status") == "shell"]
    if not shell:
        return UNKNOWN, "no session waiting on a background task"
    kids = children()
    if kids is None:
        return UNKNOWN, "no /proc"
    now = time.time()
    if any(live_tasks(pid, kids, now) for pid in shell):
        return OK, ""
    # a `shell` session's task may have just ended: broken only if none of them shows one
    return BROKEN, f"{len(shell)} session(s) waiting on a task, no .../tasks/<id>.output child"


def p_claude_derived_from(ctx: Ctx):
    from kaptyorka.adapters.claude import LINEAGE_LINES

    for p in _transcripts(ctx):
        try:
            with open(p, "rb") as f:
                head = [f.readline() for _ in range(LINEAGE_LINES)]
        except OSError:
            continue
        for raw in head:
            if b'"derived-from"' not in raw:
                continue
            try:
                rec = json.loads(raw)
            except ValueError:
                continue
            if rec.get("type") != "derived-from":
                continue
            ok = isinstance(rec.get("sessions"), list) and any(
                isinstance(x, dict) and isinstance(x.get("id"), str) and isinstance(x.get("via"), str)
                for x in rec["sessions"])
            return (OK, "") if ok else (BROKEN, "derived-from without sessions [{id, via}]")
    # a session nobody started has none: no evidence
    return UNKNOWN, "no live transcript has a derived-from line"


def p_claude_child_notes(ctx: Ctx):
    base = (ctx.tmp or Path("/tmp")) / f"claude-{os.getuid()}"  # as claude.notes_parent
    if not base.is_dir():
        return UNKNOWN, "no Claude Code temp folder"
    names = [n.name for n in base.glob("*/*/started/*")]
    if not names:
        return UNKNOWN, "no session started another here"
    # "<child id>.<random>" (2.1.331 also writes "unknown-<id>" for a child it has no id for)
    if any(re.fullmatch(r"[0-9a-f-]{36}\.[0-9a-z]+", n) for n in names):
        return OK, ""
    return BROKEN, f"{len(names)} notes, none named <session id>.<random>: {names[0][:60]}"


def _live(key: str):
    def probe(ctx: Ctx):
        if ctx.live is None or key not in ctx.live:
            return UNKNOWN, "only the slow test checks this"
        ok, detail = ctx.live[key]
        return (OK, "") if ok else (BROKEN, detail)
    return probe


# --- OpenCode probes -------------------------------------------------------------------

def p_opencode_version(ctx: Ctx):
    code, out = _version(ctx, "opencode", "opencode")
    if code != 0 or not out.strip():
        return UNKNOWN, out.strip()[:200]
    return (OK, "") if re.search(r"\d+\.\d+", out) else (BROKEN, out.strip()[:100])


def p_opencode_v1(ctx: Ctx):
    from kaptyorka.adapters import opencode

    if opencode.major() >= 2:
        return UNKNOWN, "V2 installed"
    ports = opencode.find_ports()
    if not ports:
        return UNKNOWN, "no V1 TUI listening"
    for pid, port in ports.items():
        try:
            status, data = opencode._call(pid, port, "GET", "/session")
        except (OSError, ValueError):
            continue
        if status == 200 and isinstance(data, list):
            return OK, ""
    return BROKEN, "GET /session gave no list"


def p_opencode_service(ctx: Ctx):
    from kaptyorka.adapters import opencode

    if opencode.major() < 2:
        return UNKNOWN, "V1 installed"
    state = Path(os.environ.get("XDG_STATE_HOME") or ctx.home / ".local/state") / "opencode"
    try:
        info = json.loads((state / "service.json").read_text())
    except FileNotFoundError:
        return UNKNOWN, "no V2 service running"
    except (OSError, ValueError) as e:
        return BROKEN, str(e)[:100]
    missing = [k for k in ("url", "pid") if not isinstance(info, dict) or k not in info]
    return (BROKEN, "missing " + ", ".join(missing)) if missing else (OK, "")


def p_opencode_api(ctx: Ctx):
    from kaptyorka.adapters import opencode

    if opencode.major() < 2:
        return UNKNOWN, "V1 installed"
    svc = opencode.service()
    if svc is None:
        return UNKNOWN, "no V2 service running"
    try:
        status, data = opencode._v2_call(svc, "GET", "/api/session?limit=1")
    except OSError as e:
        return UNKNOWN, str(e)[:100]
    if status == 200 and isinstance(data, (list, dict)):
        return OK, ""
    return BROKEN, f"GET /api/session answered {status}"


def p_opencode_activity(ctx: Ctx):
    from kaptyorka.adapters import opencode

    if opencode.major() < 2:
        for pid, port in opencode.find_ports().items():
            try:
                status, data = opencode._call(pid, port, "GET", "/session/status")
            except (OSError, ValueError):
                continue
            return (OK, "") if status == 200 and isinstance(data, dict) else \
                (BROKEN, f"GET /session/status answered {status}")
        return UNKNOWN, "no V1 TUI listening"
    svc = opencode.service()
    if svc is None:
        return UNKNOWN, "no V2 service running"
    try:
        status, data = opencode._v2_call(svc, "GET", "/api/session?limit=1")
        rows = data.get("data") if isinstance(data, dict) else None
        if not rows:
            return UNKNOWN, "no session yet"
        status, data = opencode._v2_call(svc, "GET", f"/api/session/{rows[0]['id']}")
    except (OSError, KeyError, TypeError) as e:
        return UNKNOWN, str(e)[:100]
    t = ((data or {}).get("data") or {}).get("time") if isinstance(data, dict) else None
    if not isinstance(t, dict) or not isinstance(t.get("updated"), (int, float)):
        return BROKEN, "session has no time.updated"
    return OK, ""


# --- Codex probes ----------------------------------------------------------------------

def p_codex_version(ctx: Ctx):
    code, out = _version(ctx, "codex", "codex")
    if code != 0 or not out.strip():
        return UNKNOWN, out.strip()[:200]
    return (OK, "") if re.search(r"\d+\.\d+\.\d+", out) else (BROKEN, out.strip()[:100])


def p_codex_app_server(ctx: Ctx):
    from kaptyorka.adapters.codex import Rpc, RpcError, codex_home, default_socket

    sock = default_socket(ctx.home) if not os.environ.get("CODEX_HOME") else \
        codex_home() / "app-server-control" / "app-server-control.sock"
    if not sock.exists():
        return UNKNOWN, "app-server daemon not running"
    try:
        with Rpc(sock) as rpc:
            ids = rpc.call("thread/loaded/list", {})["data"]
            if not isinstance(ids, list):
                return BROKEN, "thread/loaded/list gave no list"
            if ids:
                t = rpc.call("thread/read", {"threadId": ids[0]})["thread"]
                if not ({"id", "cwd"} <= set(t)):
                    return BROKEN, "thread/read has no id or cwd"
    except (ConnectionRefusedError, FileNotFoundError):
        return UNKNOWN, "app-server daemon not running"
    except (OSError, RpcError, KeyError, TypeError, ValueError) as e:
        return BROKEN, str(e)[:100]
    return OK, ""


def p_codex_thread_status(ctx: Ctx):
    from kaptyorka.adapters.codex import Rpc, RpcError, codex_home, default_socket

    sock = default_socket(ctx.home) if not os.environ.get("CODEX_HOME") else \
        codex_home() / "app-server-control" / "app-server-control.sock"
    if not sock.exists():
        return UNKNOWN, "app-server daemon not running"
    try:
        with Rpc(sock) as rpc:
            ids = rpc.call("thread/loaded/list", {})["data"]
            if not ids:
                return UNKNOWN, "no loaded thread"
            t = rpc.call("thread/read", {"threadId": ids[0]})["thread"]
    except (ConnectionRefusedError, FileNotFoundError):
        return UNKNOWN, "app-server daemon not running"
    except (OSError, RpcError, KeyError, TypeError, ValueError) as e:
        return BROKEN, str(e)[:100]
    s = t.get("status")
    kind = s.get("type") if isinstance(s, dict) else s
    return (OK, "") if kind in ("idle", "active", "notLoaded", "systemError") else \
        (BROKEN, f"thread status {str(s)[:60]}")


PROBES: Dict[str, Callable[[Ctx], Tuple[str, str]]] = {
    "claude.version": p_claude_version, "claude.cli": p_claude_cli, "claude.auth": p_claude_auth,
    "claude.session_file": p_claude_session_file, "claude.status": p_claude_status,
    "claude.name": p_claude_name, "claude.kind": p_claude_kind, "claude.inbox": p_claude_inbox,
    "claude.transcript": p_claude_transcript, "claude.title": p_claude_title,
    "claude.tasks": p_claude_tasks, "claude.derived_from": p_claude_derived_from,
    "claude.child_notes": p_claude_child_notes,
    **{k: _live(k) for k in ("claude.bg_launch", "claude.bg_isolation", "claude.push",
                             "claude.stop", "claude.rm")},
    "opencode.version": p_opencode_version, "opencode.v1_server": p_opencode_v1,
    "opencode.service": p_opencode_service, "opencode.api_session": p_opencode_api,
    "opencode.activity": p_opencode_activity,
    "codex.version": p_codex_version, "codex.app_server": p_codex_app_server,
    "codex.thread_status": p_codex_thread_status,
}


def probe(dep: Dep, ctx: Ctx) -> Tuple[str, str]:
    """One probe; a probe that raises is UNKNOWN (a bug here is never the tool's break)."""
    fn = PROBES.get(dep.id)
    if fn is None:
        return UNKNOWN, "checked against the tool's source only"
    try:
        return fn(ctx)
    except Exception as e:
        return UNKNOWN, f"probe failed: {e}"[:200]


def cheap(tools: List[str], ctx: Ctx) -> Dict[str, Tuple[str, str]]:
    """{dep id: (state, detail)} for the cheap probes of these tools (installed ones)."""
    return {d.id: probe(d, ctx) for d in DEPS if d.tool in tools and d.cheap and d.probed}


_cache: Dict[str, Tuple[float, str, object]] = {}  # tool -> (when, its version, fact value)


def facts(tools: Dict[str, str], ctx: Ctx, now: Optional[float] = None) -> dict:
    """Machine facts: {"claude_compat": "ok" | [broken ids], ...} for each installed tool,
    from the cheap probes, run again after TTL_S or when the tool's version changes. A
    probe that found no evidence keeps the tool's last answer for it."""
    now = time.monotonic() if now is None else now
    if ctx.versions is None:
        ctx.versions = tools  # the facts read --version already
    out: dict = {}
    for tool, version in tools.items():
        if tool not in FACT or not version:  # no version: the tool does not even answer that
            continue
        hit = _cache.get(tool)
        if hit is None or hit[1] != version or now - hit[0] >= TTL_S:
            res = cheap([tool], ctx)
            broken = sorted(i for i, (s, _) in res.items() if s == BROKEN)
            known_ok = {i for i, (s, _) in res.items() if s == OK}
            if hit is not None and hit[1] == version and isinstance(hit[2], list):
                # no evidence now (no live session) is not a fix: keep what was broken
                broken = sorted(set(broken) | {i for i in hit[2] if i not in known_ok})
            _cache[tool] = (now, version, broken or OK)
        out[FACT[tool]] = _cache[tool][2]
    return out


def clean(value) -> object:
    """The hub's reading of a *_compat fact: "ok", a list of ids (at most 30), or None."""
    if value == OK:
        return OK
    if isinstance(value, list):
        ids = [v for v in value if isinstance(v, str) and ID.match(v)][:30]
        return ids if ids else None
    return None


def warning_text(tool_name: str, version: str, who: str, ids: List[str]) -> Tuple[str, str]:
    """(text, short) of a machine warning for broken internals: "Claude Code 2.1.340 on box-1:
    background task files not found (stuck check off)"."""
    parts = []
    for i in ids:
        d = BY_ID.get(i)
        parts.append(f"{d.broken} ({d.off})" if d and d.broken else i)
    v = f" {version}" if version else ""
    return (f"{tool_name}{v} on {who}: " + "; ".join(parts)
            + ". Run kaptyorka's compatibility test (see CLAUDE.md).",
            f"{tool_name} internals changed")


def _vt(v: str) -> Tuple[int, ...]:
    m = re.search(r"\d+(?:\.\d+)+", str(v or ""))
    return tuple(int(x) for x in m.group(0).split(".")) if m else ()


def version_news(store, now: float) -> List[str]:
    """Once per Claude Code version newer than CLAUDE_CODE_TESTED, NEWS_WAIT_S after the first
    machine reported it: "Claude Code 2.1.340 appeared on 3 machines; kaptyorka is tested with
    2.1.331; run the compatibility test". State in the settings table, so a hub restart does
    not tell it again."""
    from kaptyorka.adapters.claude import CLAUDE_CODE_TESTED

    tested = _vt(CLAUDE_CODE_TESTED)
    with store.lock:
        rows = store.db.execute(
            "SELECT f.facts FROM device_facts f JOIN devices d ON d.id=f.device_id"
            " WHERE d.revoked_at IS NULL").fetchall()
        row = store.db.execute("SELECT value FROM settings WHERE key=?", (NEWS_KEY,)).fetchone()
    try:
        state = json.loads(row["value"]) if row else {}
    except ValueError:
        state = {}
    counts: Dict[str, int] = {}
    for r in rows:
        try:
            v = (json.loads(r["facts"]).get("tools") or {}).get("claude-code") or ""
        except (ValueError, AttributeError):
            continue
        t = _vt(v)
        if t and t > tested:
            key = ".".join(map(str, t))
            counts[key] = counts.get(key, 0) + 1
    out, changed = [], False
    for v, n in sorted(counts.items(), key=lambda kv: _vt(kv[0])):
        seen = state.setdefault(v, {"first": now, "told": False})
        changed |= seen["first"] == now
        if not seen["told"] and now - seen["first"] >= NEWS_WAIT_S:
            seen["told"], changed = True, True
            out.append(f"Claude Code {v} appeared on {n} machine{'s' if n != 1 else ''}; kaptyorka"
                       f" is tested with {CLAUDE_CODE_TESTED}; run the compatibility test"
                       " (tests/test_claude_compat_slow.py) before relying on push, workers and"
                       " the stuck check there")
    if changed:
        with store.lock:
            store.db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                             (NEWS_KEY, json.dumps(state)))
    return out


# --- the slow test's real session ------------------------------------------------------

TASK = ("Use the Bash tool with run_in_background set to true to run exactly"
        " `python3 -c 'import time; time.sleep(900)'`. Do not wait for it and do not check on"
        " it. Reply with the single word started and end your turn.")
PING = "kaptyorka compatibility check: reply with the single word pong."


def _wait(cond, timeout: float, step: float = 1.0):
    deadline = time.monotonic() + timeout
    while True:
        got = cond()
        if got or time.monotonic() >= deadline:
            return got
        time.sleep(step)


def full(folder: Path, home: Path, run: Callable, extra: List[str] = (), log=print) -> Dict[str, Tuple[str, str]]:
    """Every Claude Code probe against a real `claude --bg` session started in `folder` the way
    a worker starts (launch.WORKER_SETTINGS), which starts a silent background task; then a
    push, `claude stop` twice and `claude rm`. Spends real tokens: the slow test only.
    `extra`: more `claude` arguments (model, tools). Returns {dep id: (state, detail)}."""
    from kaptyorka import launch
    from kaptyorka.adapters.claude import ClaudeAdapter

    live: Dict[str, Tuple[bool, str]] = {}
    folder = Path(os.path.realpath(folder))
    name = "kaptyorka-compat"
    code, out = _out(Ctx(home, run), ["claude", "--bg", "-n", name, "--settings", launch.WORKER_SETTINGS,
                                      *extra, TASK], timeout=launch.CLAUDE_WAIT_S, cwd=folder)
    m = launch.BACKGROUNDED.search(out)
    live["claude.bg_launch"] = (code == 0 and m is not None, f"exit {code}: {out.strip()[:200]}")
    job = m.group(1) if m else ""
    adapter = ClaudeAdapter(home=home, log=log)
    try:
        def mine():
            for pid, d in _claude_sessions(Ctx(home, run)):
                cwd = os.path.realpath(str(d.get("cwd", "")))
                if cwd == str(folder) or cwd.startswith(str(folder) + os.sep):
                    return pid, d
            return None
        found = _wait(mine, 60) if job else mine()  # a launch that failed: no wait
        if found is None:
            live["claude.bg_isolation"] = (False, "no session file for the started session")
            return {d.id: probe(d, Ctx(home, run, live)) for d in DEPS if d.tool == "claude-code"}
        pid, data = found
        job = job or str(data.get("sessionId", ""))[:8]
        live["claude.bg_isolation"] = (os.path.realpath(data["cwd"]) == str(folder),
                                       f"started in {data['cwd']}, not {folder}")
        key = f"claude:{data['sessionId']}"

        def waiting():
            s = next((s for s in adapter.sessions() if s.key == key), None)
            return s if s is not None and s.activity == "shell" and s.tasks else None
        seen = _wait(waiting, 150)
        ctx = Ctx(home, run, live)
        res = {d.id: probe(d, ctx) for d in DEPS if d.tool == "claude-code" and d.cheap}
        # the started session itself, not only whatever else runs here
        data = next((d for p, d in _claude_sessions(ctx) if p == pid), data)
        sock = data.get("messagingSocketPath")
        own = {
            "claude.tasks": (seen is not None, "the started session never showed status shell with its"
                             f" task's .../tasks/<id>.output child (status {data.get('status')})"),
            "claude.kind": (data.get("kind") == "bg", f"kind {data.get('kind')!r}, not 'bg'"),
            "claude.name": (data.get("name") == name and isinstance(data.get("nameSource"), str)
                            and data["nameSource"] != "derived",  # 2.1.331 says "peer" for --bg -n
                            f"name {data.get('name')!r}, nameSource {data.get('nameSource')!r}"),
            "claude.inbox": (isinstance(sock, str) and os.path.exists(sock), f"messagingSocketPath {sock!r}"),
            "claude.bg_launch": (live["claude.bg_launch"][0] and str(data.get("sessionId", "")).startswith(job),
                                 f"printed {job}, session id {data.get('sessionId')}"),
        }
        # started from inside a Claude Code session (its Bash tool), the starter's notes and the
        # new transcript's derived-from name that session; from a plain terminal neither exists
        notes = os.environ.get("CLAUDE_CODE_CHILD_NOTES_DIR")
        if notes and os.path.isdir(notes):
            noted = _wait(lambda: any(n.startswith(str(data["sessionId"]) + ".") for n in os.listdir(notes)), 10)
            own["claude.child_notes"] = (bool(noted), f"{data['sessionId']} not noted in {notes}")
            from kaptyorka.adapters.claude import _transcript, lineage

            path = _transcript(home, data["sessionId"], data["cwd"], {})
            parent = lineage(path, data["sessionId"])[0] if path else ""
            own["claude.derived_from"] = (bool(parent), "no derived-from line naming the starting session")
            if parent:
                res["claude.derived_from"] = (OK, "")
            if noted:
                res["claude.child_notes"] = (OK, "")
        for i, (ok, detail) in own.items():
            if not ok:
                res[i] = (BROKEN, detail)
        live["claude.bg_launch"] = own["claude.bg_launch"]
        # push: the line arrives in the session's transcript
        try:
            adapter.deliver(key, PING)
            from kaptyorka.adapters.claude import _transcript

            def arrived():
                p = _transcript(home, data["sessionId"], data["cwd"], {})
                return p is not None and "compatibility check" in p.read_text(errors="replace")
            live["claude.push"] = (bool(_wait(arrived, 90)), "the message never reached the transcript")
        except Exception as e:
            live["claude.push"] = (False, str(e)[:200])
        c1, o1 = _out(ctx, ["claude", "stop", job], 30)
        gone = _wait(lambda: not os.path.exists(home / ".claude" / "sessions" / f"{pid}.json")
                     or mine() is None, 30)
        c2, o2 = _out(ctx, ["claude", "stop", job], 30)
        live["claude.stop"] = (c1 == 0 and bool(gone) and (c2 == 0 or any(g in o2 for g in launch.GONE)),
                               f"stop: exit {c1} {o1.strip()[:80]}; again: exit {c2} {o2.strip()[:80]}")
        c3, o3 = _out(ctx, ["claude", "rm", job], 30)
        live["claude.rm"] = (c3 == 0 or any(g in o3 for g in launch.GONE), f"exit {c3}: {o3.strip()[:120]}")
        job = ""
        for i in ("claude.bg_launch", "claude.bg_isolation", "claude.push", "claude.stop", "claude.rm"):
            res[i] = probe(BY_ID[i], ctx)
        return res
    finally:
        if job:
            for step in ("stop", "rm"):
                _out(Ctx(home, run), ["claude", step, job], 30)


def report(res: Dict[str, Tuple[str, str]]) -> List[str]:
    """One line per broken dependency, for a failing test: "claude.tasks: background task files
    not found (stuck check off): <detail> [<what>]"."""
    return [f"{i}: {BY_ID[i].broken} ({BY_ID[i].off}): {detail} [{BY_ID[i].what}]"
            for i, (s, detail) in sorted(res.items()) if s == BROKEN]
