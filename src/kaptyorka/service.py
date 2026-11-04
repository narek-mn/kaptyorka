import getpass
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, List
from xml.sax.saxutils import escape

from kaptyorka import paths


class ServiceError(Exception):
    pass


def unit_text(name: str, argv: List[str], platform: str) -> str:
    if platform == "darwin":
        args = "".join(f"\n    <string>{escape(a)}</string>" for a in argv)
        log = escape(str(paths.home() / f"{name}.log"))
        return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>io.kaptyorka.{name}</string>
  <key>ProgramArguments</key>
  <array>{args}
  </array>
  <key>KeepAlive</key><true/>
  <key>RunAtLoad</key><true/>
  <key>StandardOutPath</key><string>{log}</string>
  <key>StandardErrorPath</key><string>{log}</string>
</dict>
</plist>
"""
    exec_start = shlex.join(argv).replace("%", "%%")
    return f"""[Unit]
Description={name}

[Service]
ExecStart={exec_start}
Restart=always
RestartSec=2

[Install]
WantedBy=default.target
"""


def install(name: str, argv: List[str], run: Callable = subprocess.run,
            platform: str = sys.platform, sleep: Callable = time.sleep) -> List[str]:
    def sh(*cmd: str, must: bool = False) -> bool:
        try:
            r = run(list(cmd), capture_output=True, text=True)
        except FileNotFoundError:
            raise ServiceError(f"{cmd[0]} not found") from None
        if r.returncode != 0 and must:
            raise ServiceError((r.stderr or "").strip() or f"{' '.join(cmd)} failed")
        return r.returncode == 0

    if platform == "darwin":
        plist = Path.home() / "Library/LaunchAgents" / f"io.kaptyorka.{name}.plist"
        plist.parent.mkdir(parents=True, exist_ok=True)
        plist.write_text(unit_text(name, argv, "darwin"))
        domain = f"gui/{os.getuid()}"
        sh("launchctl", "bootout", domain, str(plist))
        for attempt in range(5):
            try:
                sh("launchctl", "bootstrap", domain, str(plist), must=True)
                break
            except ServiceError:
                if attempt == 4:
                    raise
                sleep(0.5)
        return [f"installed {plist}"]
    unit = Path.home() / ".config/systemd/user" / f"{name}.service"
    unit.parent.mkdir(parents=True, exist_ok=True)
    unit.write_text(unit_text(name, argv, "linux"))
    lines = [f"installed {unit}"]
    sh("systemctl", "--user", "daemon-reload", must=True)
    active = sh("systemctl", "--user", "is-active", "--quiet", name)
    sh("systemctl", "--user", "enable", "--now", name, must=True)
    if active:
        sh("systemctl", "--user", "restart", name)
    user = getpass.getuser()
    if not sh("loginctl", "enable-linger", user):
        lines.append(f"run once: sudo loginctl enable-linger {user}")
    return lines
