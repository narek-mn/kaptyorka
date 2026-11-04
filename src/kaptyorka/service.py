import getpass
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional
from xml.sax.saxutils import escape

from kaptyorka import paths


class ServiceError(Exception):
    pass


def _systemd_env(k: str, v: str) -> str:
    v = v.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
    return f'Environment="{k}={v}"'


def unit_text(name: str, argv: List[str], platform: str,
              env: Optional[Dict[str, str]] = None) -> str:
    if platform == "darwin":
        env_xml = "".join(f"\n    <key>{escape(k)}</key><string>{escape(v)}</string>"
                          for k, v in (env or {}).items())
        env_xml = f"\n  <key>EnvironmentVariables</key>\n  <dict>{env_xml}\n  </dict>" if env_xml else ""
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
  <key>RunAtLoad</key><true/>{env_xml}
  <key>StandardOutPath</key><string>{log}</string>
  <key>StandardErrorPath</key><string>{log}</string>
</dict>
</plist>
"""
    exec_start = shlex.join(argv).replace("%", "%%")
    env_lines = "".join(_systemd_env(k, v) + "\n" for k, v in (env or {}).items())
    return f"""[Unit]
Description={name}

[Service]
{env_lines}ExecStart={exec_start}
Restart=always
RestartSec=2
TimeoutStopSec=10

[Install]
WantedBy=default.target
"""


def _runner(run: Callable) -> Callable:
    def sh(*cmd: str, must: bool = False) -> bool:
        try:
            r = run(list(cmd), capture_output=True, text=True)
        except FileNotFoundError:
            raise ServiceError(f"{cmd[0]} not found") from None
        if r.returncode != 0 and must:
            raise ServiceError((r.stderr or "").strip() or f"{' '.join(cmd)} failed")
        return r.returncode == 0
    return sh


def stop(name: str, run: Callable = subprocess.run, platform: str = sys.platform) -> List[str]:
    """Stop the service and keep it off after a reboot. Not installed counts as fine."""
    sh = _runner(run)
    if platform == "darwin":
        target = f"gui/{os.getuid()}/io.kaptyorka.{name}"
        plist = Path.home() / "Library/LaunchAgents" / f"io.kaptyorka.{name}.plist"
        out = sh("launchctl", "bootout", target)
        off = sh("launchctl", "disable", target)  # bootout alone returns at the next login
        ok = (out or off) and plist.exists()
    else:
        ok = sh("systemctl", "--user", "disable", "--now", name)
    return [f"{name}: stopped" if ok else f"{name}: not running"]


def is_active(name: str, run: Callable = subprocess.run, platform: str = sys.platform) -> Optional[bool]:
    """Whether the service runs now; None if it is not installed."""
    sh = _runner(run)
    try:
        if platform == "darwin":
            label = f"io.kaptyorka.{name}"
            if not (Path.home() / "Library/LaunchAgents" / f"{label}.plist").exists():
                return None
            return sh("launchctl", "print", f"gui/{os.getuid()}/{label}")
        if not (Path.home() / ".config/systemd/user" / f"{name}.service").exists():
            return None
        return sh("systemctl", "--user", "is-active", "--quiet", name)
    except ServiceError:  # no systemctl or launchctl
        return False


def restart(name: str, run: Callable = subprocess.run, platform: str = sys.platform) -> List[str]:
    """Restart the service; start it if `stop` turned it off. Raises ServiceError on failure."""
    sh = _runner(run)
    if platform == "darwin":
        label = f"io.kaptyorka.{name}"
        target = f"gui/{os.getuid()}/{label}"
        plist = Path.home() / "Library/LaunchAgents" / f"{label}.plist"
        if not plist.exists():
            return [f"{name}: not installed"]
        if sh("launchctl", "print", target):
            sh("launchctl", "kickstart", "-k", target, must=True)
        else:
            sh("launchctl", "enable", target)
            sh("launchctl", "bootstrap", f"gui/{os.getuid()}", str(plist), must=True)
    else:
        if not (Path.home() / ".config/systemd/user" / f"{name}.service").exists():
            return [f"{name}: not installed"]
        if sh("systemctl", "--user", "is-enabled", "--quiet", name):
            sh("systemctl", "--user", "restart", name, must=True)
        else:
            sh("systemctl", "--user", "enable", "--now", name, must=True)
    return [f"{name}: restarted"]


def remove(name: str, run: Callable = subprocess.run, platform: str = sys.platform) -> List[str]:
    """Stop the service and delete its unit file. Nothing installed: nothing to do."""
    sh = _runner(run)

    def quiet(*cmd: str) -> None:
        try:
            sh(*cmd)
        except ServiceError:  # no systemctl or launchctl: the file is what we can still remove
            pass

    if platform == "darwin":
        plist = Path.home() / "Library/LaunchAgents" / f"io.kaptyorka.{name}.plist"
        if not plist.exists():
            return []
        quiet("launchctl", "bootout", f"gui/{os.getuid()}/io.kaptyorka.{name}")
        plist.unlink()
    else:
        unit = Path.home() / ".config/systemd/user" / f"{name}.service"
        if not unit.exists():
            return []
        quiet("systemctl", "--user", "disable", "--now", name)
        unit.unlink()
        quiet("systemctl", "--user", "daemon-reload")
    return [f"{name}: service removed"]


def install(name: str, argv: List[str], run: Callable = subprocess.run,
            platform: str = sys.platform, sleep: Callable = time.sleep,
            env: Optional[Dict[str, str]] = None) -> List[str]:
    sh = _runner(run)

    if platform == "darwin":
        plist = Path.home() / "Library/LaunchAgents" / f"io.kaptyorka.{name}.plist"
        plist.parent.mkdir(parents=True, exist_ok=True)
        plist.write_text(unit_text(name, argv, "darwin", env))
        domain = f"gui/{os.getuid()}"
        sh("launchctl", "enable", f"{domain}/io.kaptyorka.{name}")  # bootstrap fails if disabled
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
    unit.write_text(unit_text(name, argv, "linux", env))
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
