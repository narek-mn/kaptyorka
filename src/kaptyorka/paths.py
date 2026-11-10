import json
import os
import socket
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

MANAGER_DIR = "manager"  # the manager session's folder under home()
MANAGER_CONFIG = "manager-config.json"  # its config, under home(): outside its working folder
MANAGER_SETTINGS = "settings.json"  # in its folder, passed with --settings
MANAGER_MCP = "mcp.json"  # in its folder, passed with --mcp-config


def home() -> Path:
    p = Path(os.environ.get("KAPTYORKA_HOME") or Path.home() / ".kaptyorka")
    p.mkdir(mode=0o700, parents=True, exist_ok=True)
    return p


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
