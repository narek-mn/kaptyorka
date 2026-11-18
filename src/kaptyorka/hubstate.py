"""The hub indicator for a Claude Code status line: is this session on the hub?

`kaptyorka hub-state <session id>` prints one word:
  on    the hub lists the session as online
  off   the session stays off the hub: /hub off or KAPTYORKA_HUB=off, or its folder is not a
        team project, or the hub has not sent its team project list (offhub decides, as for
        the bridge, the shim and the hooks)
  lost  it should be on, but the hub does not list it or cannot be reached
and nothing at all without a kaptyorka config, or before the first answer from the hub.

It never waits on the network. The hub's list of online sessions (GET /api/feed: a member sees
their own sessions, which is enough) is cached in ~/.kaptyorka/hub-state.json for every session
of the machine and refreshed in a background process when it is older than FRESH_S.

`kaptyorka statusline [-- <command>]` is what `join` puts in the status line: it runs the
person's own status line command (if any) with the same input and a time limit, prints its
output, and appends the field.
"""
import json
import os
import signal
import subprocess
import sys
import time
from typing import Callable, Optional

from kaptyorka import offhub, paths

CACHE = "hub-state.json"
REFRESHING = ".hub-state.refresh"  # one refresh at a time for the whole machine
FRESH_S = 30  # the cache is refreshed when older than this
STALE_S = 300  # older than this, the cache cannot vouch for anything: lost
REFRESH_LOCK_S = 20  # a refresh that left its lock behind this long ago has died
GRACE_S = 45  # a new session reaches the hub's list within a heartbeat and a refresh
COMMAND_S = 3.0  # the person's own status line command gets this long

FIELDS = {"on": "\033[1;38;5;48mhub\033[0m",
          "off": "\033[38;5;231mhub off\033[0m",
          "lost": "\033[1;48;5;214;38;5;16m hub ✗ \033[0m"}


def _cache_path():
    return paths.home() / CACHE


def read_cache() -> Optional[dict]:
    try:
        c = json.loads(_cache_path().read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(c, dict) or not isinstance(c.get("at"), (int, float)):
        return None
    return c


def _spawn() -> None:
    subprocess.Popen([sys.executable, "-c", "from kaptyorka.hubstate import refresh; refresh()"],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True)


def refresh_soon(spawn: Callable[[], None] = _spawn) -> bool:
    """Start one background refresh unless one is running; True when one was started."""
    lock = paths.home() / REFRESHING
    for _ in range(2):
        try:
            os.close(os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
            break
        except FileExistsError:
            try:
                if time.time() - lock.stat().st_mtime < REFRESH_LOCK_S:
                    return False
                lock.unlink()
            except OSError:
                return False
        except OSError:
            return False
    else:
        return False
    try:
        spawn()
    except OSError:
        try:
            lock.unlink()
        except OSError:
            pass
        return False
    return True


def refresh(timeout: float = 5) -> dict:
    """Ask the hub which sessions it lists and save the answer; the background process."""
    from kaptyorka import http

    try:
        cfg = paths.load_config()
        out = {"at": time.time(), "ok": False, "ids": []}
        if cfg.get("hub") and cfg.get("token"):
            try:
                status, body = http.request("GET", cfg["hub"].rstrip("/") + "/api/feed", cfg["token"],
                                            timeout=timeout)
            except http.HubUnreachable:
                status, body = 0, None
            if status == 200 and isinstance(body, dict) and isinstance(body.get("agents"), list):
                out["ok"] = True
                out["ids"] = sorted({a["id"] for a in body["agents"]
                                     if isinstance(a, dict) and isinstance(a.get("id"), str)})
        out["at"] = time.time()
        tmp = paths.home() / f".{CACHE}.{os.getpid()}.tmp"
        tmp.write_text(json.dumps(out))
        tmp.replace(_cache_path())
        return out
    finally:
        try:
            (paths.home() / REFRESHING).unlink()
        except OSError:
            pass


def state(session_id: str, cwd: str = "", now: Optional[float] = None,
          spawn: Callable[[], None] = _spawn) -> Optional[str]:
    """"on", "off", "lost", or None (no kaptyorka config, or no answer from the hub yet)."""
    cfg = paths.load_config()
    if not session_id or not cfg.get("hub") or not cfg.get("token"):
        return None
    from kaptyorka.store import session_id as handle_of  # the hub's own handle

    now = time.time() if now is None else now
    key = f"claude:{session_id}"
    found = offhub.session(session_id)
    data = found[1] if found else {}
    if found is not None and offhub.decide(session_id, found[0], data):
        return "off"
    cwd = str(data.get("cwd") or cwd or "")
    project = os.path.basename(cwd.rstrip("/"))
    if project and offhub.personal(project, cwd, key):
        return "off"
    cache = read_cache()
    if cache is None or now - cache["at"] >= FRESH_S:
        refresh_soon(spawn)
    if cache is None:
        return None
    if now - cache["at"] >= STALE_S or not cache.get("ok"):
        return "lost"
    if handle_of(key) in (cache.get("ids") or []):
        return "on"
    started = data.get("startedAt")
    if isinstance(started, (int, float)) and now - started / 1000 < GRACE_S:
        return None  # just started: the hub has not had a heartbeat for it yet
    return "lost"


def main_state(args) -> int:
    word = state(args.session_id, os.getcwd())
    if word:
        print(word)
    return 0


def _run(command: str, stdin: str):
    """(stdout, exit status) of the person's own status line command, given COMMAND_S."""
    p = subprocess.Popen(command, shell=True, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, text=True, start_new_session=True)
    try:
        out, err = p.communicate(stdin, timeout=COMMAND_S)
        rc = p.returncode
    except subprocess.TimeoutExpired:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except OSError:
            pass
        try:
            out, err = p.communicate(timeout=0.5)
        except subprocess.TimeoutExpired:
            out, err = "", ""
        rc = 0  # their line was too slow this time: show what it had, and the field
    if err:
        sys.stderr.write(err)
    return out or "", rc


def statusline(command: str, stdin: str) -> int:
    try:
        data = json.loads(stdin) if stdin.strip() else {}
    except ValueError:
        data = {}
    data = data if isinstance(data, dict) else {}
    out, rc = _run(command, stdin) if command else ("", 0)
    sid = data.get("session_id")
    try:
        word = state(sid, str(data.get("cwd") or "")) if isinstance(sid, str) else None
    except Exception:
        word = None  # the field must never break their line
    field = FIELDS.get(word or "", "")
    out = out.rstrip("\n")
    if field:
        out = f"{out} {field}" if out else field
    if out:
        print(out)
    return rc


def main_statusline(args) -> int:
    try:
        stdin = "" if sys.stdin.isatty() else sys.stdin.read()
    except (OSError, ValueError):
        stdin = ""
    words = list(args.command or [])
    if words and words[0] == "--":
        words = words[1:]
    return statusline(" ".join(words), stdin)
