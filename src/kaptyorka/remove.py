import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable, List, Optional

from kaptyorka import paths, service

HUB_ONLY = "this machine runs the hub; run kaptyorka remove in a terminal to confirm"
ASK = "This machine runs the hub. Delete all team messages and notes? [y/N] "


def main(adapters: Optional[list] = None, run: Callable = subprocess.run,
         platform: str = sys.platform, isatty: Optional[bool] = None,
         ask: Callable[[str], str] = input) -> int:
    """Take kaptyorka off this machine: services, tool settings, ~/.kaptyorka, then pipx."""
    home = paths.home()
    if (home / "hub-token").exists():
        if not (sys.stdin.isatty() if isatty is None else isatty):
            print(HUB_ONLY, file=sys.stderr)
            return 1
        if ask(ASK).strip().lower() != "y":
            print("nothing removed")
            return 1
    if adapters is None:
        from kaptyorka.adapters import all_adapters
        adapters = all_adapters(print)
    lines: List[str] = []
    for unit in ("kaptyorka-bridge", "kaptyorka-hub"):
        lines += service.remove(unit, run=run, platform=platform)
    rc = 0
    for a in adapters:
        try:
            lines += a.unconfigure()
        except Exception as e:  # one broken tool must not stop the rest
            print(f"{a.tool}: failed: {e}", file=sys.stderr)
            rc = 1
    if home.is_symlink():
        home.unlink()
    else:
        shutil.rmtree(home)
    lines.append(f"removed {home}")
    for line in lines:
        print(line)
    if (Path(sys.prefix) / "pipx_metadata.json").exists():  # last: this runs from that venv
        r = run(["pipx", "uninstall", "kaptyorka"], capture_output=True, text=True)
        if r.returncode:
            print((r.stderr or r.stdout).strip() or "pipx uninstall failed", file=sys.stderr)
            return 1
        print("kaptyorka uninstalled (pipx)")
    return rc
