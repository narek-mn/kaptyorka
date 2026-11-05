import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable, List, Optional

from kaptyorka import paths, service

HUB_ONLY = "this machine runs the hub; run kaptyorka remove in a terminal to confirm"
ASK = "This machine runs the hub. Delete all team messages and notes? [y/N] "


def _undo_join(adapters: Optional[list], run: Callable, platform: str, units) -> tuple:
    """Remove the services and what `join` set up in each agent tool: (lines, exit code)."""
    if adapters is None:
        from kaptyorka.adapters import all_adapters
        adapters = all_adapters(print)
    lines: List[str] = []
    for unit in units:
        lines += service.remove(unit, run=run, platform=platform)
    rc = 0
    for a in adapters:
        try:
            lines += a.unconfigure()
        except Exception as e:  # one broken tool must not stop the rest
            print(f"{a.tool}: failed: {e}", file=sys.stderr)
            rc = 1
    return lines, rc


def leave(adapters: Optional[list] = None, run: Callable = subprocess.run,
          platform: str = sys.platform) -> int:
    """Leave the hub: undo `join` here, keep kaptyorka, the saved hub address and any hub."""
    lines, rc = _undo_join(adapters, run, platform, ("kaptyorka-bridge",))
    for line in lines:
        print(line)
    print("left the hub; kaptyorka join brings this machine back")
    return rc


def main(adapters: Optional[list] = None, run: Callable = subprocess.run,
         platform: str = sys.platform, isatty: Optional[bool] = None,
         ask: Callable[[str], str] = input) -> int:
    """Take kaptyorka off this machine: services, tool settings, ~/.kaptyorka, then pipx."""
    home = paths.home()
    if (home / "hub-token").exists():
        if not (sys.stdin.isatty() if isatty is None else isatty):
            print(HUB_ONLY, file=sys.stderr)
            return 1
        try:
            answer = ask(ASK)
        except (EOFError, KeyboardInterrupt):
            answer = ""
        if answer.strip().lower() != "y":
            print("nothing removed")
            return 1
    lines, rc = _undo_join(adapters, run, platform, ("kaptyorka-bridge", "kaptyorka-hub"))
    if home.is_symlink():
        home.unlink()
    else:
        shutil.rmtree(home)
    lines.append(f"removed {home}")
    for line in lines:
        print(line)
    if (Path(sys.prefix) / "pipx_metadata.json").exists():  # last: this runs from that venv
        try:
            r = run(["pipx", "uninstall", "kaptyorka"], capture_output=True, text=True)
        except FileNotFoundError:
            print("pipx not found; run: pipx uninstall kaptyorka", file=sys.stderr)
            return 1
        if r.returncode:
            print((r.stderr or r.stdout).strip() or "pipx uninstall failed", file=sys.stderr)
            return 1
        print("kaptyorka uninstalled (pipx)")
    return rc
