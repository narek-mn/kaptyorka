import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Optional, Tuple

from kaptyorka import fleet
from kaptyorka.fleet import PATH_FIX

REMOTE_WRITE = ('umask 077; d="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"; t="$d/.credentials.json.kaptyorka"; '
                'mkdir -p "$d" && cat > "$t" && chmod 600 "$t" && mv -f "$t" "$d/.credentials.json" '
                '|| { rm -f "$t"; exit 1; }')
STATUS = PATH_FIX + "claude auth status"


def _logged_in(result) -> Optional[bool]:
    """`loggedIn` from the status JSON on stdout, whatever the exit code; None when there is none."""
    try:
        got = json.loads(result.stdout)
    except (ValueError, TypeError):
        return None
    return got.get("loggedIn") is True if isinstance(got, dict) and "loggedIn" in got else None


def copy(target: str, force: bool = False, run=subprocess.run, config_dir: Optional[Path] = None,
         platform: str = sys.platform) -> Tuple[bool, str]:
    """Copy this machine's Claude Code login to target over ssh. (ok, one result line).
    The file text goes only on the stdin of the write step."""
    if target.startswith("-") or re.search(r"\s", target):
        return False, f"{target}: not a user@host"
    config_dir = Path(config_dir or os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
    cred = config_dir / ".credentials.json"
    if not cred.is_file():
        if platform == "darwin":
            return False, "this Mac keeps the Claude login in the keychain; login-copy works from Linux only for now"
        return False, "no Claude Code login on this machine; run claude auth login first"
    not_here = "Claude Code is not logged in on this machine; run claude auth login first"
    try:
        local = run(["claude", "auth", "status"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return False, not_here
    if _logged_in(local) is not True:
        return False, not_here

    def ssh(command: str, **kw):
        try:
            return run(fleet._ssh(target, command), capture_output=True, text=True, timeout=30, **kw), None
        except subprocess.TimeoutExpired:
            reason = "timed out"
        except OSError as e:
            reason = fleet._first_line(str(e))
        return None, f"{target}: could not reach it over ssh: {reason}"

    r, err = ssh(STATUS)
    if err:
        return False, err
    state = _logged_in(r)
    if r.returncode == 127:
        return False, f"{target}: Claude Code is not installed there"
    if state is True and not force:
        return False, f"{target}: Claude Code is already logged in there; add --force to replace that login"
    if state is None:
        if r.returncode == 255:
            return False, f"{target}: could not reach it over ssh: {fleet._first_line(r.stderr)}"
        return False, f"{target}: could not read Claude Code's login state there: {fleet._first_line(r.stderr)}"
    try:
        text = cred.read_text()
    except OSError as e:
        return False, f"could not read the login file here: {e.strerror or 'error'}"
    r, err = ssh(REMOTE_WRITE, input=text)
    if err:
        return False, err
    if r.returncode != 0:
        return False, f"{target}: copy failed: {fleet._first_line(r.stderr)}"
    r, err = ssh(STATUS)
    if err:
        return False, err
    if _logged_in(r) is True:
        return True, f"{target}: Claude Code is logged in there"
    return False, f"{target}: copied, but Claude Code there still says it is not logged in; run claude auth login there"
