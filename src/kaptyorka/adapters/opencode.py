"""OpenCode: sessions and push through its local server.

V1: each TUI runs its own server, but only when started with `--port` (OpenCode 1.19 reads
`server.port` from opencode.json for `opencode serve` and `web` only, never for the TUI). So
kaptyorka installs a small `opencode` wrapper in ~/.local/bin, ahead of the real binary on
PATH, that adds `--port 0`; where it cannot, the older shell alias does it for new terminals.
V2: one shared background service, found through its service.json; the TUI is only a client
of it. Its `/hub` and hub indicator come from a TUI plugin (TUI_V2), since only the TUI can
show anything and knows which session is open. The same plugin notes that session in
~/.kaptyorka/opencode-open/<TUI pid>.json, which the bridge lists: the service's session list
alone cannot tell two windows in one folder apart, nor a resumed old session from a stale one.

Checked against OpenCode's own source (github.com/sst/opencode, tags v1.19.6 and v2.3.2):
- V2 service registration: packages/client/src/effect/service.ts:142-148 (url, pid, password;
  Basic auth user "opencode", service-probe.ts:52), file name service.json in the state dir
  (packages/cli/src/services/service-config.ts:41-44,116-131). A TUI uses a private server
  instead only with --standalone or `disabled` in the service config
  (packages/cli/src/services/server-connection.ts:51-53).
- V2 routes: GET /api/session (directory, limit, order) and POST /api/session/:id/prompt
  (text, delivery, resume): packages/protocol/src/groups/session.ts:128,342-352.
- V2 plugins: a .js file in plugins/ is a server plugin only (packages/core/src/plugin/
  source-directory.ts:23-24, module.ts:114-118); a TUI part must be a folder with tui.js, which
  the server leaves to the TUI (packages/core/src/config/plugin/source.ts:170-172,
  packages/plugin/src/host.ts:56-58, packages/tui/src/plugin/discovery.ts:24-53).
"""
import base64
import re
import http.client
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable, Dict, List, Optional

from kaptyorka.adapters.base import (DeliveryError, Session, add_alias, login_output, merge_json,
                                     remove_alias, write_atomic)

TIMEOUT_S = 5
V2_LOGIN_TIMEOUT_S = 15  # `auth list --standalone` starts a private server first
ALIAS = "alias opencode='opencode --port 0'"
SESSION_ID = re.compile(r"^ses_[A-Za-z0-9]+$")  # OpenCode's own ids: "ses_" + base62
WRAPPER_MARK = "kaptyorka-opencode-wrapper"
WRAPPER = """#!/bin/sh
# %(mark)s: starts the OpenCode TUI with its local server (--port 0), so the kaptyorka hub
# can reach its sessions. Written by `kaptyorka join`; `kaptyorka leave` removes it.
real=%(real)s
if [ ! -x "$real" ]; then  # moved or reinstalled: the next opencode on PATH that is not this
  real=""
  here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
  old_ifs=$IFS; IFS=:
  for d in $PATH; do
    [ -n "$d" ] && [ -x "$d/opencode" ] || continue
    [ "$(CDPATH= cd -- "$d" 2>/dev/null && pwd -P)" = "$here" ] && continue
    grep -q %(mark)s "$d/opencode" 2>/dev/null && continue
    real="$d/opencode"; break
  done
  IFS=$old_ifs
  [ -n "$real" ] || { echo "opencode: not found (kaptyorka's wrapper in $here)" >&2; exit 127; }
fi
case "${1-}" in
  ""|-*) ;;
  *) [ -d "$1" ] || exec "$real" "$@" ;;  # a subcommand (run, serve, auth...): as it is
esac
for a in "$@"; do
  case "$a" in --port|--port=*|--hostname|--hostname=*|--mini) exec "$real" "$@" ;; esac
done
exec "$real" --port 0 "$@"
"""
LIMIT_JS = """\
// A usage or rate limit stopped a session: `kaptyorka hook opencode failure` tells the hub
// (session_end.py), once per session in 10 minutes. Only limits: an abort (Esc) is an error too.
const LIMIT = /usage limit|rate.?limit|quota|too many requests|429|insufficient.?(credit|balance)/i;
const told = {};
const limit = (sessionID, error, cwd) => {
  let text = "";
  try { text = typeof error === "string" ? error : JSON.stringify(error); } catch {}
  if (!sessionID || !LIMIT.test(text) || Date.now() - (told[sessionID] || 0) < 600000) return;
  told[sessionID] = Date.now();
  try {
    const child = execFile(KAPTYORKA, ["hook", "opencode", "failure"], { cwd, timeout: 4000,
      env: { ...process.env, KAPTYORKA_OPENCODE_SESSION: sessionID } }, () => {});
    child.stdin.on("error", () => {});
    child.stdin.end(JSON.stringify({ session_id: sessionID, cwd, error: "rate_limit",
      error_details: text.slice(0, 600) }));
  } catch {}
};
"""
PLUGIN = """\
// kaptyorka: adds the team brief to every OpenCode chat. Written by `kaptyorka join`.
import { execFile } from "node:child_process";
import { promisify } from "node:util";
const exec = promisify(execFile);
const KAPTYORKA = %s;
export const Kaptyorka = async ({ directory, client }) => {
  const brief = {};
  const run = async (sessionID, args, ms = 4000, input = "") => {
    const p = exec(KAPTYORKA, args, { cwd: directory, timeout: ms,
      env: { ...process.env, KAPTYORKA_OPENCODE_SESSION: sessionID } });
    p.child.stdin.on("error", () => {});  // it may exit before reading: no EPIPE crash
    p.child.stdin.end(input || undefined);
    return (await p).stdout.trim();
  };
  const hook = async (sessionID, event, prompt = "") => {  // the prompt's text: hub hints
    try {
      return await run(sessionID, ["hook", "opencode", event], 4000, JSON.stringify({ prompt }));
    } catch {
      return "";
    }
  };
  // /hub: `kaptyorka hub [on|off|team [project|off]]` for this session, shown as a toast and as the command's text
  const hub = async (sessionID, words) => {
    let line, ok = true;
    try {
      line = await run(sessionID, ["hub", ...words], words[0] === "team" ? 8000 : 4000);  // a join waits for the hub
    } catch (e) {
      line = String(e.stdout || "").trim() || "kaptyorka: /hub did not run";
      ok = false;
    }
    try {
      await client.tui.showToast({ body: { title: "kaptyorka hub", message: line, duration: 8000,
        variant: !ok ? "error" : /off the hub/.test(line) ? "warning" : "success" } });
    } catch {}
    return line;
  };
  return {
    "chat.message": async (input, output) => {
      const text = ((output && output.parts) || []).filter((p) => p.type === "text").map((p) => p.text).join("\\n");
      brief[input.sessionID] = await hook(input.sessionID, input.sessionID in brief ? "prompt" : "start", text);
    },
    "experimental.chat.system.transform": async (input, output) => {
      if (brief[input.sessionID]) output.system.push(brief[input.sessionID]);
    },
    "command.execute.before": async (input, output) => {
      if (input.command !== "hub") return;
      const line = await hub(input.sessionID, String(input.arguments || "").split(/\\s+/).filter(Boolean));
      for (const part of output.parts)
        if (part.type === "text") part.text = line + "\\n\\n" + SAY;
    },
    event: async ({ event }) => {  // a new chat says whether it is on the hub
      const info = event.type === "session.created" ? event.properties.info : null;
      if (info && !info.parentID) await hub(info.id, []);
      if (event.type === "session.error" && event.properties && event.properties.sessionID)
        limit(event.properties.sessionID, event.properties.error, directory);
    },
  };
};
const SAY = "Tell the user the line above, word for word, and nothing else.";
""" + LIMIT_JS
COMMAND_MARK = "<!-- written by kaptyorka; it rewrites this file -->"
COMMAND = f"""\
---
description: Show or switch whether this session is on the kaptyorka hub ([on|off|team [project|off]])
---
{COMMAND_MARK}
kaptyorka: /hub did not run, because kaptyorka's OpenCode plugin did not answer. Restart
OpenCode.

Tell the user the line above, word for word, and nothing else.
"""
PLUGIN_V2 = """\
// kaptyorka: adds the team brief to every OpenCode chat. Written by `kaptyorka join`.
import { execFile } from "node:child_process";
import { promisify } from "node:util";
const exec = promisify(execFile);
const KAPTYORKA = %s;
export default {
  id: "kaptyorka",
  setup: async (ctx) => {
    const brief = {};
    const directory = ctx.location.directory;
    const hook = async (sessionID, event, prompt = "") => {  // the prompt's text: hub hints
      try {
        const run = exec(KAPTYORKA, ["hook", "opencode", event], { cwd: directory, timeout: 4000,
          env: { ...process.env, KAPTYORKA_OPENCODE_SESSION: sessionID } });
        run.child.stdin.on("error", () => {});
        run.child.stdin.end(JSON.stringify({ prompt }));
        return (await run).stdout.trim();
      } catch {
        return "";
      }
    };
    await ctx.session.hook("prompt", async (event) => {
      const p = event.prompt;
      const text = typeof p === "string" ? p : (p && typeof p.text === "string" ? p.text : "");
      brief[event.sessionID] = await hook(event.sessionID, event.sessionID in brief ? "prompt" : "start", text);
    });
    await ctx.session.hook("context", async (event) => {
      if (brief[event.sessionID]) event.system.push({ type: "text", text: brief[event.sessionID] });
    });
    // OpenCode 2.3.2's `retry` hook gets each failed model call's error; its decision is kept
    await ctx.session.hook("retry", async (event) => { limit(event.sessionID, event.error, directory); });
  },
};
""" + LIMIT_JS


TUI_MARK = "kaptyorka-opencode-tui"
TUI_DIR = "kaptyorka-hub"  # ~/.config/opencode/plugins/<this>/tui.js: a TUI-only plugin folder
# OpenCode 2's TUI plugin API, sst/opencode v2.3.2 packages/plugin/src/tui/context.ts:
# keymap.layer with a `slash` command (407-432; a slash command with `arguments` runs before
# the server's commands, packages/tui/src/component/prompt/index.tsx:1136-1149), ui.toast
# (271-283), ui.slot "prompt.footer.status" (191-201, 540), storage.memory (31-51) and
# data.on (63-66). A slot renders OpenTUI elements: createElement/insert/effect/setProp from
# @opentui/solid 0.8.4 (what its JSX compiles to). OpenCode's Bun build provides that module to
# plugins (@opentui/solid scripts/runtime-plugin-support-configure.js); its Node build does
# not (packages/tui/src/plugin/runtime-plugin-support.node.ts), so there it is toasts only.
TUI_V2 = """\
// %(mark)s: /hub and the hub indicator in OpenCode 2's terminal UI. Written by
// `kaptyorka join`; `kaptyorka leave` removes it.
import { execFile } from "node:child_process";
import { mkdirSync, renameSync, unlinkSync, writeFileSync } from "node:fs";
const KAPTYORKA = %(exe)s;
const OPEN = %(open)s;  // the bridge reads <this>/<pid>.json: which session this window shows
const LABEL = { on: "hub", off: "hub off", lost: "hub \\u2717" };
const run = (args, sessionID, cwd, ms) => new Promise((resolve) => {
  try {
    const child = execFile(KAPTYORKA, args, { cwd, timeout: ms,
      env: { ...process.env, KAPTYORKA_OPENCODE_SESSION: sessionID } },
      (err, stdout) => resolve({ ok: !err, out: String(stdout || "").trim() }));
    if (child.stdin) child.stdin.end();
  } catch {
    resolve({ ok: false, out: "" });
  }
});
export default {
  id: "kaptyorka.hub",
  setup: async (ctx) => {
    const [state, setState] = ctx.storage.memory("state", { initial: { words: {} } });
    const seen = {};  // session id -> when this window first showed it (ms)
    let shown = "", solid = null;
    const where = (sid) => {
      const s = ctx.data.session.get(sid);
      return (s && s.location && s.location.directory) || (ctx.location && ctx.location.directory)
        || process.cwd();
    };
    const refresh = async (sid) => {  // on, off or lost, as in Claude Code's status line
      const r = await run(["hub-state", "opencode:" + sid, String(seen[sid] || Date.now())], sid,
        where(sid), 4000);
      setState((d) => { d.words[sid] = r.ok ? r.out : ""; });
    };
    // `kaptyorka hub [on|off|team [project|off]]` for this session, shown as a toast
    const hub = async (sid, words) => {
      const r = await run(["hub", ...words], sid, where(sid), words[0] === "team" ? 8000 : 4000);
      const line = r.out || "kaptyorka: /hub did not run";
      ctx.ui.toast.show({ title: "kaptyorka hub", message: line, duration: 8000,
        variant: !r.ok ? "error" : /off the hub/.test(line) ? "warning" : "success" });
      if (solid) refresh(sid);
    };
    // Which session this window shows, for the bridge: OpenCode lists sessions but not which
    // window has which open, so two windows in one folder would look like one.
    const mine = OPEN + "/" + process.pid + ".json";
    let wrote = null;
    const note = () => {
      let sid = "";
      try {
        const r = ctx.ui.router.current();
        if (r && r.type === "session") sid = r.sessionID;
        if (sid && ctx.data.session.root) sid = ctx.data.session.root(sid) || sid;  // a subagent's chat
      } catch {}
      if (sid === wrote) return;
      try {
        mkdirSync(OPEN, { recursive: true, mode: 0o700 });
        writeFileSync(mine + ".tmp", JSON.stringify({ session: sid, directory: sid ? where(sid) : "" }));
        renameSync(mine + ".tmp", mine);
        wrote = sid;
      } catch {}
    };
    const drop = () => { try { unlinkSync(mine); } catch {} };
    note();
    const watch = setInterval(note, 2000);
    if (watch.unref) watch.unref();
    process.on("exit", drop);
    const stop = () => { clearInterval(watch); process.off("exit", drop); drop(); };
    ctx.keymap.layer(() => ({
      mode: "global",
      commands: [{
        id: "kaptyorka.hub",
        title: "kaptyorka hub",
        description: "Show or switch whether this session is on the kaptyorka hub ([on|off|team [project|off]])",
        slash: { name: "hub", arguments: true },
        run: async (input) => {
          const route = ctx.ui.router.current();
          if (route.type !== "session") {
            ctx.ui.toast.show({ title: "kaptyorka hub", variant: "warning",
              message: "kaptyorka: start a chat first; /hub works for one session" });
            return;
          }
          await hub(route.sessionID, String(input || "").split(/\\s+/).filter(Boolean));
        },
      }],
    }));
    try {
      solid = await import("@opentui/solid");
    } catch {}
    if (!solid || !solid.createElement || !solid.insert || !solid.effect || !solid.setProp) {
      solid = null;  // no OpenTUI for plugins here: a new chat says whether it is on the hub
      const off = ctx.data.on("session.created", (event) => {
        const info = event.data || {};
        const route = ctx.ui.router.current();
        if (!info.parentID && route.type === "session" && route.sessionID === info.sessionID)
          hub(info.sessionID, []);
      });
      return () => { off(); stop(); };
    }
    const text = ctx.theme && ctx.theme.text;
    const color = (word) => !text ? undefined : word === "on" ? text.feedback.success.base
      : word === "lost" ? text.feedback.warning.base : text.muted;
    ctx.ui.slot({
      append: "prompt.footer.status",
      render: (input) => {
        const word = () => {
          const sid = input.sessionID;
          if (!sid) return "";
          if (!(sid in seen)) {
            seen[sid] = Date.now();
            refresh(sid);
          }
          shown = sid;
          return state.words[sid] || "";
        };
        const el = solid.createElement("text");
        solid.setProp(el, "flexShrink", 0);
        solid.effect(() => solid.setProp(el, "fg", color(word())));
        solid.insert(el, () => LABEL[word()] || "");
        return el;
      },
    });
    const timer = setInterval(() => { if (shown) refresh(shown); }, 15000);
    return () => { clearInterval(timer); stop(); };
  },
};
"""


def valid_id(sid: object) -> bool:
    """An OpenCode session id as OpenCode makes them (ses_...); anything else (a made-up
    KAPTYORKA_OPENCODE_SESSION) never becomes an identity no server could deliver to."""
    return isinstance(sid, str) and SESSION_ID.match(sid) is not None


def major() -> int:
    """OpenCode's major version from `opencode --version`; 1 if it cannot be read."""
    try:
        r = subprocess.run(["opencode", "--version"], capture_output=True, text=True,
                           timeout=TIMEOUT_S)
    except (OSError, subprocess.SubprocessError):
        return 1
    m = re.search(r"(\d+)\.\d+", r.stdout)
    return int(m.group(1)) if m else 1


def service() -> Optional[Dict[str, str]]:
    """The V2 background service's address and auth header, from its service.json; None if
    no V2 service runs."""
    state = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state") / "opencode"
    try:
        info = json.loads((state / "service.json").read_text())
        url = info["url"].rstrip("/")
        if info.get("pid"):
            os.kill(int(info["pid"]), 0)  # a file left by a service that has stopped
    except PermissionError:
        pass  # alive, run by another user
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None
    token = base64.b64encode(f"opencode:{info.get('password') or ''}".encode()).decode()
    return {"url": url, "Authorization": f"Basic {token}"}


def _v2_call(svc: Dict[str, str], method: str, path: str, body: Optional[dict] = None,
             timeout: float = TIMEOUT_S):
    """(status, decoded JSON or None) from the V2 service; raises OSError if unreachable."""
    headers = {"Authorization": svc["Authorization"]}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(svc["url"] + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return r.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        e.close()
        return e.code, None


def _v2_newest(svc: Dict[str, str], pid: int, cwd: str,
               skip: frozenset = frozenset()) -> Optional[str]:
    """Like _newest, for a V2 TUI: its folder's newest top-level session since it started (not
    one in skip, the keys other windows have open). Only a guess: kaptyorka's TUI plugin says
    exactly (_opened); this is for a window that has no note yet. Same response in OpenCode
    v2.2.5 and v2.3.2: {"data": [...], "cursor": ...}, each with id, parentID for a subagent,
    location.directory, time.created/updated (ms), plus fields not used here."""
    since = _started_ms(pid)
    status, data = _v2_call(svc, "GET", "/api/session?" + urllib.parse.urlencode(
        {"directory": cwd, "limit": 20, "order": "desc"}))
    rows = data.get("data") if status == 200 and isinstance(data, dict) else None
    here = [s for s in rows or [] if not s.get("parentID") and f"opencode:{s.get('id')}" not in skip
            and (s.get("location") or {}).get("directory", cwd) == cwd
            and s.get("time", {}).get("updated", 0) >= since]
    if not here:
        return None
    return "opencode:" + max(here, key=lambda s: s.get("time", {}).get("updated", 0))["id"]


def activity(updated, busy: bool) -> "tuple[Optional[float], str, Optional[float]]":
    """(idle_s, activity, write_s) of a session from its `time.updated` (ms) and whether a turn
    runs. Checked on OpenCode 1.19.5 and 2.3.2: `updated` moves with each step of a turn (a
    message, a tool starting or ending) and stays still while the session is idle and while
    one tool runs long, so it is the session's own last write, like a Claude Code transcript's
    last real line (stuck.py judges "busy with no new step" from it)."""
    if isinstance(updated, bool) or not isinstance(updated, (int, float)) or updated <= 0:
        return None, "busy" if busy else "", None
    age = max(0.0, round(time.time() - updated / 1000, 1))
    return age, "busy" if busy else "idle", age if busy else None


def _v2_activity(svc: Dict[str, str], sid: str) -> "tuple[Optional[float], str, Optional[float]]":
    """activity() for a V2 session: GET /api/session/:id has time.updated, and time.busy and
    time.idle (ms; a turn runs while busy is later than idle), checked on 2.3.2."""
    try:
        status, data = _v2_call(svc, "GET", f"/api/session/{urllib.parse.quote(sid)}")
        t = (data.get("data") or {}).get("time") or {} if status == 200 and isinstance(data, dict) else {}
    except (OSError, ValueError, TypeError, AttributeError, http.client.HTTPException):
        return None, "", None
    busy, idle = t.get("busy"), t.get("idle")
    running = isinstance(busy, (int, float)) and not (isinstance(idle, (int, float)) and idle >= busy)
    return activity(t.get("updated"), running)


def open_dir() -> Path:
    """Where kaptyorka's V2 TUI plugin notes the session each window shows: <pid>.json."""
    from kaptyorka import paths

    return paths.home() / "opencode-open"


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except (OSError, OverflowError):
        return False
    return True


def _opened() -> Dict[int, Optional[Dict[str, str]]]:
    """pid -> {"session", "directory"} that running V2 TUI shows, from the TUI plugin's notes
    (None while the window shows no chat). A note whose process is gone, or older than the
    process with its pid (a reused pid), is removed."""
    out: Dict[int, Optional[Dict[str, str]]] = {}
    try:
        notes = list(open_dir().glob("*.json"))
    except OSError:
        return out
    for f in notes:
        try:
            pid = int(f.stem)
            if not _alive(pid) or f.stat().st_mtime * 1000 < _started_ms(pid) - 1000:
                f.unlink()
                continue
            data = json.loads(f.read_text())
        except (OSError, ValueError, IndexError, StopIteration, subprocess.SubprocessError):
            continue  # half written, or the window just closed
        sid = data.get("session") if isinstance(data, dict) else None
        d = data.get("directory") if isinstance(data, dict) else None
        out[pid] = ({"session": sid, "directory": d} if valid_id(sid) and isinstance(d, str) and d
                    else None)
    return out


def _tuis() -> List[int]:
    return [p for p in _opencode_pids() if not _is_service(p)]


def _is_service(pid: int) -> bool:
    """True for the V2 background service (`opencode serve ...`), not a TUI."""
    try:
        if sys.platform.startswith("linux"):
            argv = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
            return b"serve" in argv
        r = subprocess.run(["ps", "-o", "args=", "-p", str(pid)], capture_output=True, text=True,
                           timeout=TIMEOUT_S)
        return "serve" in r.stdout.split()
    except (OSError, subprocess.SubprocessError):
        return False


def _listen_ports(pid: int) -> List[int]:
    """TCP ports the process listens on: /proc on Linux, lsof elsewhere."""
    try:
        if sys.platform.startswith("linux"):
            inodes = set()
            for fd in os.listdir(f"/proc/{pid}/fd"):
                try:
                    link = os.readlink(f"/proc/{pid}/fd/{fd}")
                except OSError:
                    continue
                if link.startswith("socket:["):
                    inodes.add(link[8:-1])
            ports = set()
            for table in ("/proc/net/tcp", "/proc/net/tcp6"):
                with open(table) as f:
                    for line in f.readlines()[1:]:
                        c = line.split()
                        if c[3] == "0A" and c[9] in inodes:  # 0A = LISTEN
                            ports.add(int(c[1].rsplit(":", 1)[1], 16))
            return sorted(ports)
        r = subprocess.run(["lsof", "-a", "-p", str(pid), "-iTCP", "-sTCP:LISTEN", "-P", "-n", "-Fn"],
                           capture_output=True, text=True, timeout=TIMEOUT_S)
        return sorted({int(line.rsplit(":", 1)[1]) for line in r.stdout.splitlines()
                       if line.startswith("n")})
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        return []


def _opencode_pids() -> List[int]:
    if sys.platform.startswith("linux"):
        pids = []
        for d in os.listdir("/proc"):
            try:
                if d.isdigit() and Path(f"/proc/{d}/comm").read_text().strip() == "opencode":
                    pids.append(int(d))
            except OSError:
                continue
        return pids
    try:
        r = subprocess.run(["pgrep", "-x", "opencode"], capture_output=True, text=True,
                           timeout=TIMEOUT_S)
    except (OSError, subprocess.SubprocessError):
        return []
    return [int(p) for p in r.stdout.split()]


def deaf_tuis(v2: Optional[bool] = None) -> List[int]:
    """Running OpenCode TUIs the hub cannot reach. V1: no listening port (started without
    --port, e.g. from a terminal opened before kaptyorka joined). V2: every TUI while no
    background service runs (--standalone, or the service is disabled): each then talks to a
    private server of its own, and the bridge reaches V2 only through the service."""
    if service():
        return []
    if major() >= 2 if v2 is None else v2:
        return _tuis()
    return [pid for pid in _tuis() if not _listen_ports(pid)]


def _path_dirs(path: str) -> List[str]:
    return [os.path.realpath(d) for d in path.split(os.pathsep) if d]


def login_path(run: Callable = subprocess.run) -> str:
    """PATH as a login shell of this user sets it (what a new terminal gets), else ours."""
    shell = os.environ.get("SHELL") or "/bin/sh"
    try:
        r = run([shell, "-lc", 'printf %s "$PATH"'], capture_output=True, text=True,
                timeout=TIMEOUT_S, stdin=subprocess.DEVNULL)
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip().splitlines()[-1]
    except (OSError, subprocess.SubprocessError):
        pass
    return os.environ.get("PATH", "")


def _ours(path: Path) -> bool:
    try:
        return WRAPPER_MARK in path.read_text(errors="replace")[:400]
    except OSError:
        return False


def find_ports() -> Dict[int, int]:
    """pid -> server port of every running OpenCode TUI that listens (started with --port 0)."""
    out = {}
    for pid in _opencode_pids():
        ports = _listen_ports(pid)
        if ports:
            out[pid] = ports[0]
    return out


def _auth(pid: int) -> Dict[str, str]:
    """Basic auth if the TUI was started with OPENCODE_SERVER_PASSWORD (Linux only)."""
    try:
        raw = Path(f"/proc/{pid}/environ").read_bytes()
    except OSError:
        return {}
    env = dict(v.split("=", 1) for v in raw.decode(errors="replace").split("\0") if "=" in v)
    if not env.get("OPENCODE_SERVER_PASSWORD"):
        return {}
    user = env.get("OPENCODE_SERVER_USERNAME") or "opencode"
    token = base64.b64encode(f"{user}:{env['OPENCODE_SERVER_PASSWORD']}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def _call(pid: int, port: int, method: str, path: str, body: Optional[dict] = None,
          timeout: float = TIMEOUT_S):
    """(status, decoded JSON or None); raises OSError (URLError included) if unreachable."""
    headers = _auth(pid)
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data, headers=headers,
                                 method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return r.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        e.close()
        return e.code, None


def _started_ms(pid: int) -> int:
    """When the process started, in ms since the epoch (rounded down to the second)."""
    if sys.platform.startswith("linux"):
        ticks = int(Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19])  # field 22
        with open("/proc/stat") as f:
            btime = next(int(line.split()[1]) for line in f if line.startswith("btime "))
        return (btime + ticks // os.sysconf("SC_CLK_TCK")) * 1000
    r = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True,
                       timeout=TIMEOUT_S, env={**os.environ, "LC_ALL": "C"})
    return int(time.mktime(time.strptime(r.stdout.strip(), "%a %b %d %H:%M:%S %Y"))) * 1000


class NeedsPassword(Exception):
    """The TUI's server answered 401 and its password could not be read."""


def _list(pid: int, port: int, timeout: float = TIMEOUT_S) -> list:
    status, data = _call(pid, port, "GET", "/session", timeout=timeout)
    if status == 401:
        raise NeedsPassword(f"opencode server on port {port} needs a password "
                            "(not readable on this OS)")
    return data if status == 200 and isinstance(data, list) else []


def _newest_row(pid: int, port: int, cwd: str) -> Optional[dict]:
    """Most recently updated session in cwd since the TUI started; None before its first prompt.

    Sessions from earlier runs in the same folder are stored too; nobody sees those.
    """
    since = _started_ms(pid)
    here = [s for s in _list(pid, port) if s.get("directory") == cwd
            and s.get("time", {}).get("updated", 0) >= since]
    if not here:
        return None
    return max(here, key=lambda s: s.get("time", {}).get("updated", 0))


def _newest(pid: int, port: int, cwd: str) -> Optional[str]:
    row = _newest_row(pid, port, cwd)
    return "opencode:" + row["id"] if row else None


def _busy(pid: int, port: int) -> set:
    """V1: the ids GET /session/status names busy or retrying (it lists only those; 1.19.5)."""
    status, data = _call(pid, port, "GET", "/session/status")
    if status != 200 or not isinstance(data, dict):
        return set()
    return {k for k, v in data.items() if isinstance(v, dict) and v.get("type") in ("busy", "retry")}


def session_key(pid: int, cwd: str) -> Optional[str]:
    """identity.RESOLVERS["opencode"]: the key of the TUI's current session, or None for now."""
    ports = _listen_ports(pid)
    if not ports:
        return None
    try:
        return _newest(pid, ports[0], cwd)
    except (OSError, ValueError, TypeError, KeyError, StopIteration, IndexError,
            http.client.HTTPException, subprocess.SubprocessError, NeedsPassword):
        return None


def _v2_logged_in(run: Callable) -> Optional[bool]:
    """OpenCode 2: `opencode auth list --standalone --format json`. Without --standalone every
    auth command connects to, and so starts, the long-lived background service
    (sst/opencode v2.3.2 packages/cli/src/commands/handlers/auth/list.ts:19-34 via
    auth/shared.ts:9-12, ServerConnection.resolve; --standalone: server-connection.ts:51-53).
    The JSON lists integrations with their connections, each {type: "credential", ...} (stored)
    or {type: "env", ...} (a provider variable): packages/schema/src/connection.ts:18-37.
    A stored credential is logged in; anything else (env keys only, none, unreadable) is None,
    never False: env keys and free models are real ways to use OpenCode. 15 s: --standalone
    boots a private server first."""
    out = login_output("opencode", ["auth", "list", "--standalone", "--format", "json"], run,
                       timeout=V2_LOGIN_TIMEOUT_S)
    try:
        rows = json.loads(out or "")
    except ValueError:
        return None
    if not isinstance(rows, list):
        return None
    stored = any(isinstance(c, dict) and c.get("type") == "credential"
                 for r in rows if isinstance(r, dict) and isinstance(r.get("connections"), list)
                 for c in r["connections"])
    return True if stored else None


class OpenCodeAdapter:
    tool = "opencode"
    prefix = "opencode"
    restart_note = ("OpenCode: open a new terminal and restart OpenCode to pick up kaptyorka "
                    "(on OpenCode 2, its background service too)")

    def __init__(self, home: Optional[Path] = None,
                 find_ports: Callable[[], Dict[int, int]] = find_ports,
                 log: Callable[[str], None] = print, run: Callable = subprocess.run):
        self.home = home or Path.home()
        self.find_ports = find_ports
        self.log = log
        self.run = run
        self._locked: set = set()  # ports already logged as needing a password

    def _skip(self, e: NeedsPassword, port: int) -> None:
        if port not in self._locked:
            self._locked.add(port)
            self.log(str(e))

    def installed(self) -> bool:
        return shutil.which("opencode") is not None

    def logged_in(self, run: Callable) -> Optional[bool]:
        """True when OpenCode has a credential: its auth.json ($XDG_DATA_HOME/opencode, which
        `opencode auth login` writes in 1.x), else what `opencode auth list` counts ("N
        credentials", then "M environment variables" for provider keys in the environment).
        Never False on V1: OpenCode also runs free models and providers set in opencode.json
        with no login, so finding none is "unknown", not "not logged in". V2: the same rule, see
        _v2_logged_in."""
        data = Path(os.environ.get("XDG_DATA_HOME") or self.home / ".local" / "share")
        try:
            auth = json.loads((data / "opencode" / "auth.json").read_text())
            if isinstance(auth, dict) and auth:
                return True
        except (OSError, ValueError):
            pass
        if major() >= 2:
            return _v2_logged_in(run)
        out = re.sub(r"\x1b\[[0-9;]*m", "", login_output("opencode", ["auth", "list"], run) or "")
        counts = [int(n) for n in re.findall(r"(\d+)\s+(?:credentials?|environment variables?)\b", out)]
        return True if any(counts) else None

    def sessions(self) -> List[Session]:
        from kaptyorka.identity import proc_cwd

        svc = service()
        if svc:
            return self._v2_sessions(svc)
        out = []
        for pid, port in self.find_ports().items():
            try:
                cwd = proc_cwd(pid)
                row = _newest_row(pid, port, cwd)
                busy = _busy(pid, port) if row else set()
            except NeedsPassword as e:
                self._skip(e, port)
                continue
            except (OSError, ValueError, TypeError, KeyError, StopIteration, IndexError,
                    http.client.HTTPException, subprocess.SubprocessError):
                continue  # TUI just exited
            if row:
                idle_s, act, write_s = activity(row.get("time", {}).get("updated"), row["id"] in busy)
                out.append(Session("opencode:" + row["id"], self.tool,
                                   os.path.basename(cwd.rstrip("/")), act, cwd=cwd,
                                   idle_s=idle_s, write_s=write_s))
        return out

    def _v2_sessions(self, svc: Dict[str, str]) -> List[Session]:
        from kaptyorka.identity import proc_cwd

        out, seen = [], set()
        opened = _opened()  # what each window shows, as its TUI plugin says: exact, idle or not
        for _, note in sorted(opened.items()):
            key = f"opencode:{note['session']}" if note else ""
            if key and key not in seen:
                seen.add(key)
                cwd = note["directory"]
                out.append(self._v2_session(svc, key, cwd))
        for pid in _tuis():
            if pid in opened:
                continue
            try:  # no note (a TUI started before the plugin had it): guess from its folder
                cwd = proc_cwd(pid)
                key = _v2_newest(svc, pid, cwd, frozenset(seen))
            except (OSError, ValueError, TypeError, KeyError, StopIteration, IndexError,
                    AttributeError, http.client.HTTPException, subprocess.SubprocessError):
                continue  # TUI just exited, or the service is restarting
            if key and key not in seen:
                seen.add(key)
                out.append(self._v2_session(svc, key, cwd))
        return out

    def _v2_session(self, svc: Dict[str, str], key: str, cwd: str) -> Session:
        idle_s, act, write_s = _v2_activity(svc, key.partition(":")[2])
        return Session(key, self.tool, os.path.basename(cwd.rstrip("/")), act, cwd=cwd,
                       idle_s=idle_s, write_s=write_s)

    def deliver(self, key: str, text: str) -> None:
        sid = key.partition(":")[2]
        if not valid_id(sid):
            raise DeliveryError(f"not an OpenCode session id: {key}")
        svc = service()
        if svc:
            try:
                status, _ = _v2_call(svc, "POST", f"/api/session/{urllib.parse.quote(sid)}/prompt",
                                     {"text": text, "delivery": "queue", "resume": True})
            except (OSError, ValueError, http.client.HTTPException) as e:
                raise DeliveryError(f"opencode session {key}: {e}") from e
            if not 200 <= status < 300:
                raise DeliveryError(f"opencode session {key} refused: HTTP {status}")
            return
        deadline = time.monotonic() + TIMEOUT_S
        for pid, port in self.find_ports().items():
            left = deadline - time.monotonic()
            if left <= 0:
                break
            try:
                if not any(s.get("id") == sid for s in _list(pid, port, left)):
                    continue
                status, _ = _call(pid, port, "POST", f"/session/{sid}/prompt_async",
                                  {"parts": [{"type": "text", "text": text}]},
                                  max(deadline - time.monotonic(), 0.1))
            except NeedsPassword as e:
                self._skip(e, port)
                continue
            except (OSError, ValueError, AttributeError, http.client.HTTPException) as e:
                raise DeliveryError(f"opencode session {key}: {e}") from e
            if not 200 <= status < 300:
                raise DeliveryError(f"opencode session {key} refused: HTTP {status}")
            return
        raise DeliveryError(f"opencode session {key}: no running OpenCode server has it")

    def configure(self, kaptyorka: str) -> List[str]:
        lines = []
        conf = self.home / ".config" / "opencode"
        entry = {"type": "local", "command": [kaptyorka, "mcp"], "enabled": True}

        def add_mcp(data: dict) -> bool:
            servers = data.setdefault("mcp", {})
            if servers.get("kaptyorka") == entry:
                return False
            servers["kaptyorka"] = entry
            return True

        result = merge_json(conf / "opencode.json", add_mcp)
        if result == "broken":
            self.log(f"left alone, not valid JSON: {conf / 'opencode.json'}")
        elif result == "changed":
            lines.append(f"OpenCode: kaptyorka MCP server added to {conf / 'opencode.json'}")

        v2 = major() >= 2
        lines += self.install_hub(kaptyorka, v2)

        return lines + self._server_on(v2)

    def install_command(self, kaptyorka: str) -> List[str]:
        """At bridge start: the wrapper for a machine that joined before it existed, or that
        changed OpenCode's version since. Only when OpenCode is joined (its plugin is there)."""
        if not (self.home / ".config" / "opencode" / "plugins" / "kaptyorka.js").exists():
            return []
        return self._server_on(major() >= 2)

    def wrapper(self) -> Path:
        return self.home / ".local" / "bin" / "opencode"

    def _server_on(self, v2: bool) -> List[str]:
        """Make every new V1 TUI listen: the wrapper when ~/.local/bin comes before the real
        opencode on a login shell's PATH, else the alias. V2 has no --port flag: neither."""
        w = self.wrapper()
        if v2:
            return self._drop_wrapper() + remove_alias(self.home, ALIAS, "OpenCode")
        path = login_path(self.run)
        dirs = _path_dirs(path)
        here = os.path.realpath(w.parent)
        if w.exists() and not _ours(w):
            why = f"{w} is OpenCode itself (or not ours)"
        elif here not in dirs:
            why = f"{w.parent} is not on PATH"
        else:
            rest = os.pathsep.join(d for d in path.split(os.pathsep)
                                   if d and os.path.realpath(d) != here)
            real = shutil.which("opencode", path=rest)
            if real is None:
                why = "no opencode found on PATH"
            else:
                real_dir = os.path.realpath(os.path.dirname(real))
                if real_dir in dirs and dirs.index(real_dir) < dirs.index(here):
                    why = f"{os.path.dirname(real)} comes before {w.parent} on PATH"
                else:
                    text = WRAPPER % {"mark": WRAPPER_MARK, "real": shlex.quote(real)}
                    lines = []
                    if not w.exists() or w.read_text(errors="replace") != text:
                        w.parent.mkdir(parents=True, exist_ok=True)
                        write_atomic(w, text)
                        os.chmod(w, 0o755)
                        lines.append(f"OpenCode: wrapper written to {w}: new OpenCode windows"
                                     " start with their local server, so kaptyorka can push")
                    return lines + remove_alias(self.home, ALIAS, "OpenCode")
        lines = self._drop_wrapper()
        line = add_alias(self.home, ALIAS, "OpenCode", self.log)
        if line:
            self.log(f"OpenCode: no wrapper ({why}); using the shell alias instead")
            lines.append(line)
        return lines

    def _drop_wrapper(self) -> List[str]:
        w = self.wrapper()
        if (w.exists() or w.is_symlink()) and _ours(w):
            w.unlink()
            return [f"OpenCode: wrapper removed ({w})"]
        return []

    def install_hub(self, kaptyorka: str, v2: Optional[bool] = None) -> List[str]:
        """The plugin and /hub; also run at bridge start, so an update (or a new OpenCode major)
        brings them. V1: the plugin answers /hub (commands/hub.md) and shows toasts. V2: the TUI
        plugin (plugins/kaptyorka-hub/tui.js) has /hub and the indicator, and our hub.md goes,
        since OpenCode 2 would list it next to the plugin's /hub. Files someone else wrote are
        left alone."""
        conf = self.home / ".config" / "opencode"
        lines = []
        plugin = conf / "plugins" / "kaptyorka.js"
        v2 = major() >= 2 if v2 is None else v2
        text = (PLUGIN_V2 if v2 else PLUGIN) % json.dumps(kaptyorka)
        if not plugin.exists() or plugin.read_text() != text:
            plugin.parent.mkdir(parents=True, exist_ok=True)
            plugin.write_text(text)
            lines.append(f"OpenCode: kaptyorka plugin written to {plugin}")
        if v2:
            return lines + self._write_tui(kaptyorka) + self._drop_command()
        command = conf / "commands" / "hub.md"
        try:
            old = command.read_text() if command.exists() else None
            if old is not None and COMMAND_MARK not in old:
                self.log(f"left alone, not written by kaptyorka: {command}")
            elif old != COMMAND:
                command.parent.mkdir(parents=True, exist_ok=True)
                write_atomic(command, COMMAND)
                lines.append(f"OpenCode: /hub command written to {command}")
        except OSError as e:
            self.log(f"could not write {command}: {e}")
        return lines + self._drop_tui()

    def tui_plugin(self) -> Path:
        return self.home / ".config" / "opencode" / "plugins" / TUI_DIR / "tui.js"

    def _write_tui(self, kaptyorka: str) -> List[str]:
        f = self.tui_plugin()
        text = TUI_V2 % {"mark": TUI_MARK, "exe": json.dumps(kaptyorka),
                         "open": json.dumps(str(open_dir()))}
        try:
            old = f.read_text(errors="replace") if f.exists() else None
            others = f.parent.is_dir() and any(p.name != "tui.js" for p in f.parent.iterdir())
            if (old is not None and TUI_MARK not in old[:400]) or (old is None and others):
                self.log(f"left alone, not written by kaptyorka: {f.parent}")
                return []
            if old == text:
                return []
            f.parent.mkdir(parents=True, exist_ok=True)
            write_atomic(f, text)
        except OSError as e:
            self.log(f"could not write {f}: {e}")
            return []
        return [f"OpenCode: /hub and the hub indicator written to {f}"]

    def _drop_tui(self) -> List[str]:
        f = self.tui_plugin()
        try:
            if TUI_MARK not in f.read_text(errors="replace")[:400]:
                return []
            f.unlink()
        except OSError:
            return []
        try:
            f.parent.rmdir()  # only when nothing else is in it
        except OSError:
            pass
        return [f"OpenCode: /hub plugin removed ({f})"]

    def _drop_command(self) -> List[str]:
        command = self.home / ".config" / "opencode" / "commands" / "hub.md"
        try:
            if COMMAND_MARK in command.read_text():
                command.unlink()
                return [f"OpenCode: /hub command removed ({command})"]
        except OSError:
            pass
        return []

    def unconfigure(self) -> List[str]:
        conf = self.home / ".config" / "opencode"
        lines = self._drop_command() + self._drop_tui()

        def drop_mcp(data: dict) -> bool:
            servers = data.get("mcp")
            if not isinstance(servers, dict) or "kaptyorka" not in servers:
                return False
            del servers["kaptyorka"]
            if not servers:
                del data["mcp"]
            return True

        result = merge_json(conf / "opencode.json", drop_mcp)
        if result == "broken":
            self.log(f"left alone, not valid JSON: {conf / 'opencode.json'}")
        elif result == "changed":
            lines.append(f"OpenCode: kaptyorka MCP server removed from {conf / 'opencode.json'}")
        plugin = conf / "plugins" / "kaptyorka.js"
        if plugin.exists() or plugin.is_symlink():
            plugin.unlink()
            lines.append(f"OpenCode: kaptyorka plugin removed ({plugin})")
        return lines + self._drop_wrapper() + remove_alias(self.home, ALIAS, "OpenCode")
