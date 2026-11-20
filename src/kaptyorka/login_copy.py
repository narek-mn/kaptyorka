import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Callable, Dict, List, NamedTuple, Optional, Tuple

from kaptyorka import fleet
from kaptyorka.fleet import PATH_FIX


def remote_write(folder: str, name: str) -> str:
    """The ssh write step: stdin goes to a 600 temp file, then moves into place; no temp left."""
    return (f'umask 077; d="{folder}"; t="$d/{name}.kaptyorka"; '
            f'mkdir -p "$d" && cat > "$t" && chmod 600 "$t" && mv -f "$t" "$d/{name}" '
            '|| { rm -f "$t"; exit 1; }')


REMOTE_WRITE = remote_write("${CLAUDE_CONFIG_DIR:-$HOME/.claude}", ".credentials.json")
CODEX_WRITE = remote_write("${CODEX_HOME:-$HOME/.codex}", "auth.json")
OPENCODE_WRITE = remote_write("${XDG_DATA_HOME:-$HOME/.local/share}/opencode", "auth.json")
STATUS = PATH_FIX + "claude auth status"
CODEX_STATUS = PATH_FIX + "codex login status"
# OpenCode's own installer puts it in ~/.opencode/bin. Its version comes first: OpenCode 1
# keeps logins in auth.json, OpenCode 2 in its database, reached through a private server
# (--standalone) that ends with the command, so no background service is ever started.
OC_PATH = 'export PATH="$HOME/.local/bin:$HOME/.opencode/bin:$PATH"; '
OPENCODE_STATUS = (OC_PATH + 'command -v opencode >/dev/null || exit 127; v=$(opencode --version) || exit 1; '
                   'echo "$v"; case "$v" in 1.*) opencode auth list </dev/null;; '
                   '2.*) opencode auth list --standalone --format json </dev/null;; esac')
OPENCODE_IMPORT = OC_PATH + "opencode auth import --standalone"  # reads stdin
OPENCODE_EXPORT = ["opencode", "auth", "export", "--standalone"]
NOT_LINUX = "login-copy works from Linux only for now"


def _logged_in(result) -> Optional[bool]:
    """`loggedIn` from the status JSON on stdout, whatever the exit code; None when there is none."""
    try:
        got = json.loads(result.stdout)
    except (ValueError, TypeError):
        return None
    return got.get("loggedIn") is True if isinstance(got, dict) and "loggedIn" in got else None


def _codex_logged_in(result) -> Optional[bool]:
    from kaptyorka.adapters.codex import login_state

    return login_state(f"{result.stdout or ''}{result.stderr or ''}", result.returncode)


def _opencode_major(result) -> Optional[int]:
    lines = (result.stdout or "").strip().splitlines()
    m = re.match(r"v?(\d+)\.\d+", lines[0].strip()) if lines else None
    return int(m.group(1)) if m else None


def _opencode2_ids(result) -> Optional[set]:
    """Ids of the stored credentials in an OpenCode 2 `auth list --format json` (after the
    version line): integrations, each with `connections` of type "credential" (stored, with an
    id) or "env" (a provider key in the environment). None when it is not that list."""
    try:
        listed = json.loads("\n".join((result.stdout or "").strip().splitlines()[1:]))
        return {c["id"] for i in listed for c in i.get("connections", []) if c.get("type") == "credential"}
    except (ValueError, TypeError, KeyError, AttributeError):
        return None


def _opencode_logged_in(result) -> Optional[bool]:
    """Whether OpenCode there has a stored login. OpenCode 1 `auth list` ends with "N
    credentials"; OpenCode 2 lists credential connections. None when unreadable. Provider keys
    in the environment are no stored login and do not count."""
    major = _opencode_major(result)
    if result.returncode != 0 or major not in (1, 2):
        return None
    if major == 2:
        ids = _opencode2_ids(result)
        return None if ids is None else bool(ids)
    out = re.sub(r"\x1b\[[0-9;]*m", "", result.stdout or "")
    m = re.search(r"(\d+)\s+credentials?\b", out)
    return int(m.group(1)) > 0 if m else None


class Tool(NamedTuple):
    name: str
    label: str
    login: str  # the login command, for messages
    write: str
    status: str
    state: Callable  # a status result -> Optional[bool]
    check: str  # the status command, for messages


TOOLS: Dict[str, Tool] = {
    "claude": Tool("claude", "Claude Code", "claude auth login", REMOTE_WRITE, STATUS, _logged_in,
                   "claude auth status"),
    "codex": Tool("codex", "Codex", "codex login", CODEX_WRITE, CODEX_STATUS, _codex_logged_in,
                  "codex login status"),
    "opencode": Tool("opencode", "OpenCode", "opencode auth login", OPENCODE_WRITE, OPENCODE_STATUS,
                     _opencode_logged_in, "opencode auth list"),
}


def _home_file(tool: str, config_dir: Optional[Path] = None) -> Path:
    env = os.environ
    if tool == "claude":
        return Path(config_dir or env.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude") / ".credentials.json"
    if tool == "codex":
        return Path(config_dir or env.get("CODEX_HOME") or Path.home() / ".codex") / "auth.json"
    data = Path(env.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return Path(config_dir or data / "opencode") / "auth.json"


def _codex_store(codex_dir: Path) -> str:
    """cli_auth_credentials_store from the top level of Codex's config.toml; "file" by default."""
    try:
        text = (codex_dir / "config.toml").read_text()
    except (OSError, UnicodeDecodeError):
        return "file"
    for line in text.splitlines():
        if line.lstrip().startswith("["):
            break  # a table: the key is top-level only
        m = re.match(r"\s*cli_auth_credentials_store\s*=\s*[\"']?(\w+)", line)
        if m:
            return m.group(1)
    return "file"


def _local_opencode(cred: Path, run, not_here: str) -> Tuple[str, str, Optional[dict]]:
    """OpenCode 1: its auth.json, read at copy time. OpenCode 2: `auth export` (secrets) kept
    only in memory and only for the import's stdin."""
    try:
        r = run(["opencode", "--version"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return "absent", not_here, None
    major = _opencode_major(r)
    if major == 1:
        try:  # OpenCode 1 reads only this file: a non-empty one is a login
            stored = json.loads(cred.read_text())
        except (OSError, ValueError):
            stored = None
        return ("ok", "", {"major": 1}) if isinstance(stored, dict) and stored else ("absent", not_here, None)
    if major != 2:
        return "absent", not_here, None
    try:
        r = run(OPENCODE_EXPORT, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        r = None
    try:  # its error text may quote what it read: never shown
        ids = [e["id"] for e in json.loads(r.stdout)] if r is not None and r.returncode == 0 else None
    except (ValueError, TypeError, KeyError):
        ids = None
    if ids is None:
        return "failed", "OpenCode: could not export this machine's login (opencode auth export failed)", None
    return ("ok", "", {"major": 2, "text": r.stdout, "ids": set(ids)}) if ids else ("absent", not_here, None)


def _local(t: Tool, cred: Path, run, platform: str) -> Tuple[str, str, Optional[dict]]:
    """("ok", "", what) when this machine's login may be copied; else (kind, line, None): absent
    (no login here), note (a login that cannot be copied by design: a Mac, a keyring), failed."""
    not_here = f"{t.label} is not logged in on this machine; run {t.login} first"
    if t.name != "claude" and platform == "darwin":
        return "note", f"{t.label}: {NOT_LINUX}", None
    if t.name == "opencode":
        return _local_opencode(cred, run, not_here)
    store = _codex_store(cred.parent) if t.name == "codex" else "file"
    if store in ("keyring", "ephemeral") or store == "auto" and not cred.is_file():
        return "note", "Codex keeps its login in the system keyring on this machine, so there is no file to copy", None
    if not cred.is_file():
        if platform == "darwin":
            return "note", "this Mac keeps the Claude login in the keychain; login-copy works from Linux only for now", None
        return "absent", f"no {t.label} login on this machine; run {t.login} first", None
    argv = ["claude", "auth", "status"] if t.name == "claude" else ["codex", "login", "status"]
    try:
        local = run(argv, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return "absent", not_here, None
    return ("ok", "", {}) if t.state(local) is True else ("absent", not_here, None)


def _copy(target: str, t: Tool, force: bool, run, cred: Path, platform: str) -> Tuple[str, str]:
    """(kind, one result line); kind: copied, skipped (logged in there), missing (not installed
    there), absent (no login here), note (cannot be copied from here), failed."""
    kind, line, here = _local(t, cred, run, platform)
    if kind != "ok":
        return kind, line
    v2 = here.get("major") == 2

    def ssh(command: str, **kw):
        try:  # an OpenCode 2 private server takes a while to start
            return run(fleet._ssh(target, command), capture_output=True, text=True,
                       timeout=90 if t.name == "opencode" else 30, **kw), None
        except subprocess.TimeoutExpired:
            reason = "timed out"
        except OSError as e:
            reason = fleet._first_line(str(e))
        return None, f"{target}: could not reach it over ssh: {reason}"

    r, err = ssh(t.status)
    if err:
        return "failed", err
    if r.returncode == 127:
        return "missing", f"{target}: {t.label} is not installed there"
    there = _opencode_major(r) if t.name == "opencode" else None
    if t.name == "opencode" and there in (1, 2) and there != here["major"]:
        return "note", (f"{target}: OpenCode {there} there and OpenCode {here['major']} here keep logins "
                        "differently (a file, a database); login-copy copies OpenCode only between the same major version")
    state = t.state(r)
    if state is True and not force:
        return "skipped", f"{target}: {t.label} is already logged in there; add --force to replace that login"
    if state is None:
        if r.returncode == 255:
            return "failed", f"{target}: could not reach it over ssh: {fleet._first_line(r.stderr)}"
        if t.name == "claude":
            return "failed", f"{target}: could not read Claude Code's login state there: {fleet._first_line(r.stderr)}"
        # Codex's status shows part of an API key: never echo it, nor OpenCode's provider list
        return "failed", f"{target}: could not read {t.label}'s login state there; run {t.check} there to see why"
    if v2:
        text = here["text"]
    else:
        try:
            text = cred.read_text()
        except OSError as e:
            return "failed", f"could not read the login file here: {e.strerror or 'error'}"
    r, err = ssh(OPENCODE_IMPORT if v2 else t.write, input=text)
    if err:
        return "failed", err
    if r.returncode != 0:
        if v2:  # its error text may quote the credentials it was given: never shown
            return "failed", f"{target}: copy failed: opencode auth import there exited with {r.returncode}"
        return "failed", f"{target}: copy failed: {fleet._first_line(r.stderr)}"
    r, err = ssh(t.status)
    if err:
        return "failed", err
    if (here["ids"] <= (_opencode2_ids(r) or set()) if v2 else t.state(r) is True):
        return "copied", f"{target}: {t.label} is logged in there"
    return "failed", (f"{target}: copied, but {t.label} there still says it is not logged in; "
                      f"run {t.login} there")


def _bad(target: str) -> bool:
    return target.startswith("-") or re.search(r"\s", target) is not None


def copy(target: str, force: bool = False, run=subprocess.run, config_dir: Optional[Path] = None,
         platform: str = sys.platform, tool: str = "claude") -> Tuple[bool, str]:
    """Copy this machine's login of one tool to target over ssh. (ok, one result line).
    The file text goes only on the stdin of the write step."""
    if _bad(target):
        return False, f"{target}: not a user@host"
    kind, line = _copy(target, TOOLS[tool], force, run, _home_file(tool, config_dir), platform)
    return kind == "copied", line


def copy_all(target: str, force: bool = False, run=subprocess.run,
             dirs: Optional[Dict[str, Path]] = None, platform: str = sys.platform) -> Tuple[bool, List[str]]:
    """Copy every tool logged in here. One line per such tool; ok when none failed and one was
    more than a note. dirs: a tool's folder here (tests)."""
    if _bad(target):
        return False, [f"{target}: not a user@host"]
    dirs = dirs or {}
    lines, failed, done = [], False, False
    for name, t in TOOLS.items():
        kind, line = _copy(target, t, force, run, _home_file(name, dirs.get(name)), platform)
        if kind == "absent":
            continue  # not logged in here: nothing to copy, nothing to say
        lines.append(line)
        failed = failed or kind == "failed"
        done = done or kind != "note"
    if not lines:
        return False, ["no tool is logged in on this machine; log in to Claude Code, Codex or OpenCode first"]
    return not failed and done, lines
