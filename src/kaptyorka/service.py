import getpass
import os
import subprocess
import sys
from pathlib import Path
from typing import Callable, List
from xml.sax.saxutils import escape

from kaptyorka import paths


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
    exec_start = " ".join(argv)
    return f"""[Unit]
Description={name}

[Service]
ExecStart={exec_start}
Restart=always

[Install]
WantedBy=default.target
"""


def install(name: str, argv: List[str], run: Callable = subprocess.run,
            platform: str = sys.platform) -> List[str]:
    def sh(*cmd: str) -> bool:
        return run(list(cmd), capture_output=True).returncode == 0

    if platform == "darwin":
        plist = Path.home() / "Library/LaunchAgents" / f"io.kaptyorka.{name}.plist"
        plist.parent.mkdir(parents=True, exist_ok=True)
        plist.write_text(unit_text(name, argv, "darwin"))
        domain = f"gui/{os.getuid()}"
        sh("launchctl", "bootout", domain, str(plist))
        if not sh("launchctl", "bootstrap", domain, str(plist)):
            return [f"could not start {name}: run launchctl bootstrap {domain} {plist}"]
        return [f"installed {plist}"]
    unit = Path.home() / ".config/systemd/user" / f"{name}.service"
    unit.parent.mkdir(parents=True, exist_ok=True)
    unit.write_text(unit_text(name, argv, "linux"))
    lines = [f"installed {unit}"]
    sh("systemctl", "--user", "daemon-reload")
    active = sh("systemctl", "--user", "is-active", "--quiet", name)
    sh("systemctl", "--user", "enable", "--now", name)
    if active:
        sh("systemctl", "--user", "restart", name)
    user = getpass.getuser()
    if not sh("loginctl", "enable-linger", user):
        lines.append(f"run once: sudo loginctl enable-linger {user}")
    return lines
