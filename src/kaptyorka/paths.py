import hashlib
import json
import os
import re
import socket
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

MANAGER_DIR = "manager"  # the manager session's folder under home()
MANAGER_CONFIG = "manager-config.json"  # its config, under home(): outside its working folder
MANAGER_SETTINGS = "settings.json"  # in its folder, passed with --settings
MANAGER_MCP = "mcp.json"  # in its folder, passed with --mcp-config
MANAGER_TMUX = "kaptyorka-manager"  # the tmux session the manager runs in
WORKTREE = re.compile(r"--\d+$")  # a `<folder>--N` worktree belongs to its base folder's project
NOT_TEAM = "This folder isn't a team project, so nothing from it goes to the hub."
COORDINATOR = "(team)"  # the project of a session that joined with /hub team: no project
# (never a registered name: PROJECT_NAME has no brackets)


# Why the bridge could not push a message, as its sender may see it: a short fixed reason,
# never the adapter's own text (that names the recipient's session key, OS user and paths).
UNREACHABLE = "the session isn't reachable"
PUSH_REASONS = (UNREACHABLE, "the session is off the hub", "the agent tool refused the message",
                "this tool only pulls messages")


def push_reason(error: str) -> str:
    """The short reason for a failed push's raw error text (the bridge logs the full text)."""
    text = (error or "").lower()
    if text in PUSH_REASONS:
        return text
    if "off the hub" in text:
        return PUSH_REASONS[1]
    if "pull only" in text:
        return PUSH_REASONS[3]
    if "refused" in text:
        return PUSH_REASONS[2]
    return UNREACHABLE


# Why a machine still runs an older kaptyorka than the hub: a short fixed reason, never the
# update's own output (that can name paths and the OS user). The bridge sends one of the first
# four in its facts; the hub decides the rest.
UPDATE_FAILED = "auto-update failed"
UPDATE_REASONS = (UPDATE_FAILED, "auto-update timed out", "not installed with pipx",
                  "no newer release found")
UPDATE_OFF = "auto-update is off for this machine"
UPDATE_OFFLINE = "bridge offline"
UPDATE_WAITING = "auto-update has not run yet"


def update_reason(error: str) -> str:
    """The short reason for a failed auto-update's raw error text (the bridge logs the full text)."""
    text = (error or "").lower()
    if text in UPDATE_REASONS:
        return text
    if "timed out" in text or "timeout" in text:
        return UPDATE_REASONS[1]
    if "not installed with pipx" in text:
        return UPDATE_REASONS[2]
    if "already the latest" in text or UPDATE_REASONS[3] in text:
        return UPDATE_REASONS[3]
    return UPDATE_FAILED


def not_team_help(projects, hub: str = "/hub") -> str:
    """The refusal a tool call outside a team folder gets: how to join, for the agent to
    offer, never to do on its own. `projects`: the person's team projects, None when unknown."""
    have = (f" (you have: {', '.join(projects)})" if projects
            else " (you have no role in a team project yet; the admin gives roles)"
            if projects is not None else "")
    return (NOT_TEAM + f" If this session works for the team, run {hub} team (to coordinate"
            f" across projects) or {hub} team <project>" + have + ", or open the agent in your"
            " copy of that project. Agent: offer this to the user and wait for their answer;"
            " never switch on your own. With their yes you may call join_team(project) instead"
            " (project \"\" to coordinate).")
NO_TEAM_LIST = ("the hub has not sent its team project list yet, so nothing goes to it;"
                " if this lasts, the hub needs updating.")


def team_name(project: str) -> str:
    """The project a session folder's name belongs to: proj-a--2 is a worktree of proj-a."""
    return WORKTREE.sub("", project or "")


def home() -> Path:
    p = Path(os.environ.get("KAPTYORKA_HOME") or Path.home() / ".kaptyorka")
    p.mkdir(mode=0o700, parents=True, exist_ok=True)
    return p


def hub_key_digest() -> Optional[str]:
    """The sha256 of the token in ~/.kaptyorka/admin-token on this (the hub) machine: the hub
    key, which has admin rights whatever its person's role. None when the file is missing,
    unreadable or empty."""
    try:
        saved = (home() / "admin-token").read_text().strip()
    except (OSError, UnicodeDecodeError):
        return None
    return hashlib.sha256(saved.encode()).hexdigest() if saved else None


def config_path() -> Path:
    return home() / "config.json"


def in_manager_folder(cwd: Optional[str]) -> bool:
    if not cwd:
        return False
    try:
        return Path(cwd).resolve() == (home() / MANAGER_DIR).resolve()
    except (OSError, RuntimeError):
        return False


def load_config(cwd: Optional[str] = None) -> dict:
    """This machine's config; for a session in the manager's folder only the manager's, and
    {} (no hub) when that file is missing or unreadable: never the admin's."""
    if in_manager_folder(cwd):
        try:
            cfg = json.loads((home() / MANAGER_CONFIG).read_text())
        except (OSError, ValueError):
            return {}
        return cfg if isinstance(cfg, dict) else {}
    try:
        return json.loads(config_path().read_text())
    except FileNotFoundError:
        return {}


def save_config(cfg: dict) -> None:
    p = config_path()
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(cfg, f, indent=2)
    os.chmod(p, 0o600)


def _subdir(name: str) -> Path:
    d = home() / name
    d.mkdir(mode=0o700, exist_ok=True)
    return d


def files_dir() -> Path:
    return _subdir("files")


def inbox_dir() -> Path:
    return _subdir("inbox")


def outbound_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"


def is_local_address(host: str) -> bool:
    """True when this machine can bind to host (one of its own addresses or names)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.bind((host, 0))
        return True
    except OSError:
        return False


def is_own_hub(hub_url: str, saved: dict) -> bool:
    """True when hub_url points at the hub this machine runs (or once ran): same port, one of our addresses."""
    try:
        u = urlparse(hub_url)
        host, port = u.hostname, u.port or (443 if u.scheme == "https" else 80)
    except ValueError:
        return False
    if not host or port != saved.get("port", 8765):
        return False
    return host == saved.get("host") or is_local_address(host)
