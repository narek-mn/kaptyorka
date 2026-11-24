"""What the bridge tells the hub about this computer: load, tools, git state, project folders."""
import getpass
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from kaptyorka import doctor, paths
from kaptyorka.adapters.base import write_atomic

GIT_TTL_S = 30
GIT_BUDGET_S = 2
VERSION_TTL_S = 600
LOGGED_OUT_TTL_S = 60  # a "not logged in" is checked again soon: the person may log in
COMMANDS = {"claude-code": "claude", "opencode": "opencode", "codex": "codex", "cursor": "cursor-agent"}
_cache: Dict[Tuple[str, str], Tuple[float, object]] = {}
_jetpack_seen: Dict[str, tuple] = {}  # dpkg status path: ((mtime, size), version)


def _cached(key: Tuple[str, str], ttl: float, make: Callable[[], object]):
    now = time.monotonic()
    hit = _cache.get(key)
    if hit and now - hit[0] < ttl:
        return hit[1]
    value = make()
    _cache[key] = (now, value)
    return value


def git(cwd: str, run=subprocess.run, cached: bool = True,
        budget: float = GIT_BUDGET_S) -> Optional[dict]:
    """Branch, dirty flag and last subject of the repo at cwd; None when it is not one.
    cached=False reads it now (the stop hook), within `budget` seconds."""
    def make() -> Optional[dict]:
        deadline = time.monotonic() + budget  # one budget for all calls on this folder

        def out(*args: str) -> Optional[str]:
            left = max(deadline - time.monotonic(), 0.01)
            r = run(["git", "-C", cwd, *args], capture_output=True, text=True, timeout=left)
            return r.stdout if r.returncode == 0 else None

        try:
            branch = out("rev-parse", "--abbrev-ref", "HEAD")
            if branch is None:
                return None
            status, subject = out("status", "--porcelain"), out("log", "-1", "--format=%s")
        except (subprocess.TimeoutExpired, OSError):
            return None
        return {"branch": branch.strip(),
                "dirty": None if status is None else bool(status.strip()),
                "subject": (subject or "").strip(),
                "read_at": time.time()}  # kept across cached resends; the heartbeat sends it as an age

    if not cached:
        return make()
    return _cached(("git", cwd), GIT_TTL_S, make)  # type: ignore[return-value]


def _tool_version(tool: str, run) -> str:
    def make() -> str:
        exe = shutil.which(COMMANDS.get(tool, tool))
        if not exe:
            return ""
        try:
            r = run([exe, "--version"], capture_output=True, text=True, timeout=5)
        except Exception:
            return ""
        lines = (r.stdout or "").strip().splitlines() if r.returncode == 0 else []
        return lines[0].strip() if lines else ""

    return _cached(("version", tool), VERSION_TTL_S, make)  # type: ignore[return-value]


def _logins(installed: list, run) -> Dict[str, Optional[bool]]:
    """Per installed tool: logged in, not, or None when unknown; at most every 10 minutes,
    every minute while it is not logged in."""
    out: Dict[str, Optional[bool]] = {}
    for a in installed:
        key = ("login", a.tool)
        hit = _cache.get(key)
        ttl = LOGGED_OUT_TTL_S if hit and hit[1] is False else VERSION_TTL_S
        check = getattr(a, "logged_in", None)  # a test double may not have one
        out[a.tool] = _cached(key, ttl, lambda: check(run) if check else None)  # type: ignore
    return out


def _rules_in_place(adapter, home: Path) -> bool:
    """The one hook (or plugin) that carries the start brief is installed."""
    tool = adapter.tool
    if tool == "claude-code":
        return doctor._has_hooks(doctor._json(home / ".claude" / "settings.json").get("hooks"),
                                 tool, (("SessionStart", "start"),), True)
    if tool == "codex":
        from kaptyorka.adapters.codex import codex_home

        return doctor._has_hooks(doctor._json(codex_home(home) / "hooks.json").get("hooks"),
                                 tool, (("SessionStart", "start"),), True)
    if tool == "cursor":
        return doctor._has_hooks(doctor._json(home / ".cursor" / "hooks.json").get("hooks"),
                                 tool, (("stop", "stop"),), False)
    if tool == "opencode":
        return (home / ".config" / "opencode" / "plugins" / "kaptyorka.js").exists()
    return False


def harness(adapters: list, home: Path, skills_current: Optional[bool] = False) -> Dict[str, dict]:
    """Per installed tool: is the rules text, the hooks, and the team skills in place.
    Skills are None for a tool that has no skills folder (codex), or while skills_current is unknown."""
    out: Dict[str, dict] = {}
    for a in adapters:
        if not a.installed():
            continue
        h = getattr(a, "home", home)
        out[a.tool] = {"rules": bool(_rules_in_place(a, h)),
                       "hooks": bool(doctor.tool_configured(a, h)),
                       "skills": None if a.tool == "codex" or skills_current is None
                       else bool(skills_current)}
    return out


INDICATOR_TOOLS = ("claude-code", "opencode")  # Codex and Cursor have no place for one


def machine(adapters: list, run=subprocess.run, platform: str = sys.platform,
            skills_current: Optional[bool] = False) -> dict:
    from kaptyorka import __version__

    try:
        load = float(os.getloadavg()[0])
    except OSError:
        load = 0.0
    installed = [a for a in adapters if a.installed()]
    hooks_ok = {a.tool: doctor.tool_configured(a, getattr(a, "home", Path.home())) for a in installed}
    # the hub indicator (and /hub) where the tool has a place for one; the bridge puts it back at
    # start, so False means it could not (a status line of a kind kaptyorka does not wrap)
    indicator_ok = {a.tool: doctor.hub_installed(a, getattr(a, "home", Path.home()))
                    for a in installed if a.tool in INDICATOR_TOOLS and hooks_ok[a.tool]}
    return {"os_user": getpass.getuser(), "tmux": shutil.which("tmux") is not None,
            "load": load, "cpus": os.cpu_count() or 1,
            "tools": {a.tool: _tool_version(a.tool, run) for a in installed},
            "hooks_ok": hooks_ok, "indicator_ok": indicator_ok, "version": __version__,
            "logins": _logins(installed, run),
            "harness": harness(adapters, Path.home(), skills_current)}


def _read(path: str, limit: int = 4096) -> str:
    try:
        with open(path, "rb") as f:
            return f.read(limit).decode("utf-8", "replace")
    except OSError:
        return ""


def os_release(root: str = "") -> str:
    """PRETTY_NAME from /etc/os-release, such as "Ubuntu 22.04.5 LTS"; "" when unknown."""
    for line in _read(root + "/etc/os-release").splitlines():
        if line.startswith("PRETTY_NAME="):
            return line.split("=", 1)[1].strip().strip('"')[:100]
    return ""


def l4t(root: str = "") -> str:
    """A Jetson's L4T release from /etc/nv_tegra_release ("# R36 (release), REVISION: 4.3, ..."
    gives "R36.4.3"); "" on any other machine."""
    head = _read(root + "/etc/nv_tegra_release", 512)
    major = re.search(r"#\s*R(\d+)", head)
    rev = re.search(r"REVISION:\s*([\d.]+)", head)
    return f"R{major.group(1)}.{rev.group(1)}" if major and rev else ""


def jetpack(root: str = "") -> str:
    """The installed nvidia-jetpack package's version ("6.2+b77" gives "6.2"), from dpkg's own
    status file: no command run, and read again only when that file changed. "" when the
    package is not installed."""
    path = root + "/var/lib/dpkg/status"
    try:
        st = os.stat(path)
        hit = _jetpack_seen.get(path)
        if hit is not None and hit[0] == (st.st_mtime, st.st_size):
            return str(hit[1])
        with open(path, "rb") as f:
            text = f.read(32 * 1024 * 1024).decode("utf-8", "replace")
    except OSError:
        return ""
    m = re.search(r"^Package: nvidia-jetpack\n(?:[^\n]+\n)*?Version: ([^\s+~-]+)", text, re.M)
    found = m.group(1)[:20] if m else ""
    _jetpack_seen[path] = ((st.st_mtime, st.st_size), found)
    return found


def boot_time(root: str = "") -> Optional[int]:
    """When the machine last booted (Linux /proc/stat btime, seconds); None when unknown."""
    m = re.search(r"^btime (\d+)$", _read(root + "/proc/stat", 1 << 16), re.M)
    return int(m.group(1)) if m else None


def primary_ip(hub_url: str) -> str:
    """The address this machine reaches the hub from: a UDP socket "connected" to the hub picks
    the route; no packet is sent. "" when unknown."""
    import socket
    from urllib.parse import urlparse
    host = urlparse(hub_url).hostname or ""
    if not host:
        return ""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect((host, 9))
            return s.getsockname()[0]
    except OSError:
        return ""


PSEUDO_FS = {"tmpfs", "devtmpfs", "overlay", "squashfs", "proc", "sysfs", "cgroup", "cgroup2",
             "devpts", "mqueue", "debugfs", "tracefs", "securityfs", "pstore", "bpf", "autofs",
             "fusectl", "configfs", "hugetlbfs", "binfmt_misc", "nsfs", "efivarfs", "ramfs",
             "rpc_pipefs", "fuse.snapfuse", "fuse.portal", "fuse.gvfsd-fuse", "nfsd", "zram"}
SKIP_UNDER = ("/proc", "/sys", "/dev", "/run", "/snap", "/var/snap", "/var/lib/docker",
              "/var/lib/snapd", "/boot")
MAX_DISKS = 8
GB = 1024 ** 3


def _space(path: str) -> Optional[Tuple[float, float, int]]:
    """(total GB, free GB for users, device id) of the filesystem holding path."""
    try:
        st, dev = os.statvfs(path), os.stat(path).st_dev
    except OSError:
        return None
    if not st.f_blocks:
        return None
    return (round(st.f_blocks * st.f_frsize / GB, 1), round(st.f_bavail * st.f_frsize / GB, 1),
            dev)


def disks(full: bool, mounts: str = "/proc/self/mounts", home: Optional[str] = None) -> List[dict]:
    """Free disk space, from os.statvfs (no command): [{"mount", "total_gb", "free_gb"}].
    `full` (a team machine or the hub's computer): every real mounted filesystem, one per
    device, "/", the home filesystem and /workspace first, at most MAX_DISKS. Otherwise only
    "/" and the home filesystem, named "home": a personal laptop sends no other path."""
    home = home or str(Path.home())
    out, seen = [], set()

    def add(path: str, name: str) -> None:
        got = _space(path)
        if got is not None and got[2] not in seen and len(out) < MAX_DISKS:
            seen.add(got[2])
            out.append({"mount": name, "total_gb": got[0], "free_gb": got[1]})
    if not full:
        add("/", "/")
        add(home, "home")
        return out
    found = []
    for line in _read(mounts, 1 << 20).splitlines():
        parts = line.split()
        if len(parts) < 3 or parts[2] in PSEUDO_FS or parts[2].startswith("fuse."):
            continue
        point = parts[1].replace("\\040", " ")
        if any(point == p or point.startswith(p + "/") for p in SKIP_UNDER):
            continue
        found.append(point)
    home_mount = max((p for p in found if home == p or home.startswith(p.rstrip("/") + "/")),
                     key=len, default="/")
    first = ["/", home_mount] + (["/workspace"] if "/workspace" in found else [])
    rest = sorted((p for p in found if p not in first),
                  key=lambda p: -((_space(p) or (0, 0, 0))[0]))
    for p in dict.fromkeys(first + rest):
        add(p, p)
    return out


def infra(hub_url: str, with_ip: bool) -> dict:
    """What a team machine (or the hub's own machine) adds for the hub's machine events:
    OS release, Jetson L4T and JetPack, kernel, boot time, and for a team machine its address.
    Empty values are left out."""
    import platform
    tegra = l4t()
    out = {"os": os_release(), "l4t": tegra, "jetpack": jetpack() if tegra else "",
           "kernel": platform.release()[:100], "boot": boot_time(),
           "ip": primary_ip(hub_url) if with_ip else ""}
    return {k: v for k, v in out.items() if v}


def _learned() -> Dict[str, str]:
    try:
        data = json.loads((paths.home() / "folders.json").read_text())
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, str)} \
        if isinstance(data, dict) else {}


def folders(projects: List[str], sessions: list, home: Path) -> dict:
    """{project: folder}: every folder learned from session cwds (and remembered), registered
    or not, plus home/project for the hub's projects not learned. A remembered folder that is
    gone is dropped."""
    learned = _learned()
    seen = dict(learned)
    for s in sessions:
        cwd = getattr(s, "cwd", "") or ""
        if cwd:
            seen[os.path.basename(cwd.rstrip("/"))] = cwd
    seen = {k: v for k, v in seen.items() if os.path.isdir(v)}
    if seen != learned:
        try:
            write_atomic(paths.home() / "folders.json", json.dumps(seen, indent=2))
        except OSError:
            pass  # remembering is a convenience; never stop the heartbeat
    out = dict(seen)
    for p in projects:
        if p not in out and (home / p).is_dir():
            out[p] = str(home / p)
    return out
