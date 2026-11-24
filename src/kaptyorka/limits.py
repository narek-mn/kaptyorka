"""Usage limits per account. Sessions of one OS account on one computer share that account's
login to an agent tool, and so its usage limit: the simple rule here is the same host, the
same OS user and the same tool (Claude Code keeps its login in the OS user's ~/.claude, Codex
in ~/.codex; a second login on one OS account is not something either supports).

The hub marks an account "at its limit until HH:MM" from:
- a session's own report (session_end.py: Claude Code's StopFailure `rate_limit`, OpenCode's
  plugin), with the reset time when the error text names one;
- the bridge's facts `limits` ({tool: {"until": t} or {"ok": true}}): Claude Code's status line
  input `rate_limits` (documented: code.claude.com/docs/en/statusline, the five_hour and
  seven_day windows with used_percentage and resets_at), which `kaptyorka statusline` keeps in
  ~/.kaptyorka/limits.json, and Codex's own usage read (app-server `account/rateLimits/read`,
  rust-v0.166.0 v2/account.rs);
- the manager's launch failing with a usage limit's text.

It clears at the reset time, when a fresh reading says the account is under its limits, or,
when no reset time is known, once a session of that account wrote again. The hub's manager runs
on one such account on the hub machine: while that account is at its limit the manager waits
for the reset instead of being relaunched, and that never counts as a crash.
"""
import json
import re
import time
from pathlib import Path
from typing import Dict, List, Optional

from kaptyorka import paths

KEY = "limit:"  # settings rows: limit:<host>\t<os_user>\t<tool> -> {until, since, known, source}
UNKNOWN_S = 5 * 3600  # no reset time known: assume one 5-hour window, cleared earlier by work
WORKED_S = 300  # a session of the account wrote this long after the mark: it works again
FILE = "limits.json"  # under ~/.kaptyorka: the last rate_limits the status line saw
FRESH_S = 900  # a status line reading older than this says nothing
TOOLS = {"claude-code": "Claude Code", "codex": "Codex", "opencode": "OpenCode"}
LIMIT_TEXT = re.compile(r"usage limit|rate.?limit|limit reached|quota|too many requests|"
                        r"out of (?:credits|extra usage)|hit your limit", re.I)


def is_limit_text(text: str) -> bool:
    return bool(LIMIT_TEXT.search(text or ""))


def hm(t: Optional[float]) -> str:
    return time.strftime("%H:%M", time.localtime(t)) if t else ""


def _key(host: str, os_user: str, tool: str) -> str:
    return f"{KEY}{host}\t{os_user}\t{tool}"


def mark(store, host: str, os_user: str, tool: str, until: Optional[float], source: str,
         now: float) -> bool:
    """The account is at its limit until `until` (None: unknown). True when this is news (no
    mark yet, or another reset time)."""
    if not host or not tool:
        return False
    known = until is not None and now < until < now + 8 * 86400
    until = until if known else now + UNKNOWN_S
    old = get(store, host, os_user, tool, now)
    if old is not None and (not known or abs(old["until"] - until) < 60):
        return False  # the same episode: keep its start and reset time
    row = {"until": until, "since": old["since"] if old else now, "known": known,
           "source": source[:40]}
    with store.lock:
        store.db.execute("INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key)"
                         " DO UPDATE SET value=excluded.value",
                         (_key(host, os_user, tool), json.dumps(row)))
    return True


def clear(store, host: str, os_user: str, tool: str) -> None:
    with store.lock:
        store.db.execute("DELETE FROM settings WHERE key=?", (_key(host, os_user, tool),))


def active(store, now: float) -> List[dict]:
    """Every account at its limit now: {host, os_user, tool, until, since, known, source}.
    Rows past their reset are deleted."""
    out, done = [], []
    with store.lock:
        rows = store.db.execute("SELECT key, value FROM settings WHERE substr(key, 1, 6)=?",
                                (KEY,)).fetchall()
    for r in rows:
        try:
            host, os_user, tool = r["key"][len(KEY):].split("\t")
            v = json.loads(r["value"])
            until = float(v["until"])
        except (ValueError, KeyError, TypeError):
            done.append(r["key"])
            continue
        if until <= now:
            done.append(r["key"])
            continue
        out.append({"host": host, "os_user": os_user, "tool": tool, "until": until,
                    "since": float(v.get("since") or now), "known": bool(v.get("known")),
                    "source": str(v.get("source") or "")})
    if done:
        with store.lock:
            store.db.executemany("DELETE FROM settings WHERE key=?", [(k,) for k in done])
    return out


def get(store, host: str, os_user: str, tool: str, now: float) -> Optional[dict]:
    for a in active(store, now):
        if (a["host"], a["os_user"], a["tool"]) == (host, os_user, tool):
            return a
    return None


def words(a: dict) -> str:
    """"at its Claude Code usage limit until 19:30" (or "..., reset time unknown")."""
    tool = TOOLS.get(a["tool"], a["tool"])
    return (f"at its {tool} usage limit until {hm(a['until'])}" if a["known"]
            else f"at its {tool} usage limit (reset time unknown)")


def by_host(store, now: float) -> Dict[str, List[str]]:
    """{host: ["u: at its Claude Code usage limit until 19:30", ...]} for Machines and the
    manager's `machines`."""
    out: Dict[str, List[str]] = {}
    for a in active(store, now):
        out.setdefault(a["host"], []).append(f"{a['os_user']}: {words(a)}")
    return out


def skip_limited(store, usable: List[dict], reasons: List[str], tool: str,
                 now: float) -> List[dict]:
    """Work requests: a device whose account is at its limit for this tool is left out, with
    one plain reason, so the worker goes elsewhere or waits."""
    limited = {(a["host"], a["os_user"]): a for a in active(store, now) if a["tool"] == tool}
    keep = []
    for c in usable:
        a = limited.get((c["host"], c["os_user"]))
        if a is None:
            keep.append(c)
        else:
            reasons.append(f"{c['host']} ({c['os_user']}): {words(a)}; pick another machine"
                           + (f" or wait until {hm(a['until'])}" if a["known"] else " or wait"))
    return keep


def from_event(store, row, error: str, resets: Optional[float], now: float) -> bool:
    """A session's own report (session_end.py) of a usage limit marks its account."""
    if error != "rate_limit" or row is None or not row["host"]:
        return False
    return mark(store, row["host"], row["os_user"] or "", row["tool"], resets, "session", now)


def from_facts(store, host: str, os_user: str, limits, now: float) -> None:
    """The bridge's own reading per tool: {"until": t} at the limit, {"ok": true} under it."""
    if not host or not isinstance(limits, dict):
        return
    for tool, v in limits.items():
        if tool not in TOOLS or not isinstance(v, dict):
            continue
        until = v.get("until")
        if isinstance(until, (int, float)) and not isinstance(until, bool) and until > now:
            mark(store, host, os_user, tool, float(until), "reading", now)
        elif v.get("limited") is True:
            mark(store, host, os_user, tool, None, "reading", now)
        elif v.get("ok") is True:
            clear(store, host, os_user, tool)


def tick(store, now: float) -> None:
    """A mark with no known reset time clears once a session of that account wrote again
    (a known reset time is kept: a failed turn writes too)."""
    marks = [a for a in active(store, now) if not a["known"]]
    if not marks:
        return
    online = store.online()
    for a in marks:
        if any(s.host == a["host"] and s.os_user == a["os_user"] and s.tool == a["tool"]
               and s.wrote is not None and s.wrote > a["since"] + WORKED_S for s in online):
            clear(store, a["host"], a["os_user"], a["tool"])


# --- the manager ---------------------------------------------------------------------------

BACK = ("kaptyorka: your usage limit has reset. Read your inbox and answer the messages that"
        " came while you waited.")
SINCE = "manager_limit_since"  # manager setting: the limit episode the admin was told about


def manager_account(mm, host: str) -> Optional[tuple]:
    """(host, os_user) of the account the manager runs on: the bridge it launches through."""
    d = mm.admin_device(host)
    return (d["host"], d["os_user"] or "") if d else None


def manager_limit(store, mm, host: str, now: float) -> Optional[dict]:
    acct = manager_account(mm, host)
    return get(store, acct[0], acct[1], "claude-code", now) if acct else None


def manager_words(lim: dict) -> str:
    """The manager's state line: "waiting for its usage limit, resets 19:30"."""
    return ("waiting for its usage limit, resets " + hm(lim["until"]) if lim["known"]
            else "waiting for its usage limit (reset time unknown)")


def forgive(mm) -> None:
    """A run that ended while its account was at its limit is no crash: judged as none."""
    run = mm._last_run()
    if run is not None and mm._get("manager_judged") != repr(run["created"]):
        mm._set("manager_judged", repr(run["created"]))


def manager_gate(store, mm, tell, admins: List[str], host: str, now: float) -> bool:
    """The hub's tick, before it would relaunch the manager. True: wait (its account is at its
    limit; the admin is told once per episode). At the reset a manager still running hears it
    once; one that ended starts as usual, with no crash counted."""
    from kaptyorka import session_end

    acct = manager_account(mm, host)
    if acct is None:
        return False
    failed = mm.last_failure()  # a launch that failed on the limit's text
    if failed is not None and failed["failed_at"] is not None and now - failed["failed_at"] < 3600 \
            and is_limit_text(failed["error"]):
        mark(store, acct[0], acct[1], "claude-code", session_end.reset_at(failed["error"], now),
             "manager launch", now)
    lim = get(store, acct[0], acct[1], "claude-code", now)
    if lim is None:
        if mm._get(SINCE):
            mm._set(SINCE, "")
            key = mm.session_key(now)
            if key is not None:
                tell(key, BACK)
        return False
    forgive(mm)
    if mm._get(SINCE) != repr(lim["since"]):
        mm._set(SINCE, repr(lim["since"]))
        for name in admins:
            tell(f"web:{name}", f"The manager's account on {acct[0]} is {words(lim)}; the manager"
                 " waits and comes back by itself then. Messages to it wait for it.")
    return True


def to_manager(store, mm, to: str) -> bool:
    to = (to or "").strip()
    if to.lower() == "manager":
        return True
    key = mm.last_key()
    a = store.agent(key) if key else None
    return a is not None and to in (a.name, a.label)


def waiting_line(store, mm, host: str, now: float) -> str:
    """For someone who writes to the manager while it waits: one line, or ""."""
    lim = manager_limit(store, mm, host, now)
    if lim is None:
        return ""
    return ("the manager is at its usage limit until " + hm(lim["until"]) if lim["known"]
            else "the manager is at its usage limit") + "; your message waits"


# --- the bridge side ----------------------------------------------------------------------

def save_statusline(data: dict, now: Optional[float] = None) -> None:
    """`kaptyorka statusline`: keep the input's `rate_limits` for the bridge, when it has them
    (claude.ai subscribers, after the session's first answer)."""
    rl = data.get("rate_limits") if isinstance(data, dict) else None
    if not isinstance(rl, dict) or not rl:
        return
    path = paths.home() / FILE
    if not path.parent.is_dir():
        return  # not joined: nobody reads it
    try:
        old = json.loads(path.read_text())
    except (OSError, ValueError):
        old = {}
    new = {"claude-code": {"rate_limits": rl, "at": time.time() if now is None else now}}
    if old.get("claude-code", {}).get("rate_limits") == rl and \
            (now or time.time()) - float(old.get("claude-code", {}).get("at") or 0) < 60:
        return  # the status line runs often: write at most once a minute when unchanged
    try:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(new))
        tmp.replace(path)
    except OSError:
        pass


def claude_reading(now: float, home: Optional[Path] = None) -> Optional[dict]:
    """{"until": t} when a window is used up, {"ok": True} when a fresh reading is under every
    limit, None with no fresh reading."""
    try:
        v = json.loads(((home or paths.home()) / FILE).read_text())["claude-code"]
        rl, at = v["rate_limits"], float(v["at"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if now - at > FRESH_S or not isinstance(rl, dict):
        return None
    full = [w.get("resets_at") for w in rl.values() if isinstance(w, dict)
            and isinstance(w.get("used_percentage"), (int, float)) and w["used_percentage"] >= 100
            and isinstance(w.get("resets_at"), (int, float)) and w["resets_at"] > now]
    return {"until": float(max(full))} if full else {"ok": True}


def codex_reading(snapshot: dict, now: float) -> Optional[dict]:
    """From Codex's `account/rateLimits/read` answer: {"until": t} at a limit, {"ok": True}
    under, None when it says nothing usable."""
    if not isinstance(snapshot, dict):
        return None
    rl = snapshot.get("rateLimits")
    windows = [w for w in ((rl or {}).get("primary"), (rl or {}).get("secondary"))
               if isinstance(w, dict)] if isinstance(rl, dict) else []
    full = [w.get("resetsAt") for w in windows if isinstance(w.get("usedPercent"), (int, float))
            and w["usedPercent"] >= 100 and isinstance(w.get("resetsAt"), (int, float))
            and w["resetsAt"] > now]
    if full:
        return {"until": float(max(full))}
    if snapshot.get("ordinaryUsageAllowed") is False:
        return {"limited": True}  # blocked, no reset time given
    return {"ok": True} if windows else None


def local(adapters, now: float) -> dict:
    """The bridge's facts `limits`: this OS account's reading per tool, where there is one."""
    out = {}
    claude = claude_reading(now)
    if claude is not None:
        out["claude-code"] = claude
    for a in adapters:
        read = getattr(a, "account_limits", None)
        if a.tool == "codex" and read is not None:
            try:
                got = codex_reading(read(), now)
            except Exception:  # a reading is a bonus: never stop the facts
                got = None
            if got is not None:
                out["codex"] = got
    return out
