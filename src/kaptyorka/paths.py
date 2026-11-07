import json
import os
from pathlib import Path
from typing import Optional

MANAGER_DIR = "manager"  # the manager session's folder under home()
MANAGER_CONFIG = "manager-config.json"  # its config, under home(): outside its working folder


def home() -> Path:
    p = Path(os.environ.get("KAPTYORKA_HOME") or Path.home() / ".kaptyorka")
    p.mkdir(mode=0o700, parents=True, exist_ok=True)
    return p


def config_path() -> Path:
    override = os.environ.get("KAPTYORKA_CONFIG") or ""
    return Path(override) if os.path.isabs(override) else home() / "config.json"


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
