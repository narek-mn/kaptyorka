import json
import os
from pathlib import Path


def home() -> Path:
    p = Path(os.environ.get("KUBRIK_HOME") or Path.home() / ".kubrik")
    p.mkdir(mode=0o700, parents=True, exist_ok=True)
    return p


def config_path() -> Path:
    return home() / "config.json"


def load_config() -> dict:
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
