"""Who uses this machine and how much: CPU, memory and GPU per OS user, read by the bridge; and
the one rule the hub and the manager use to call a person's use "heavy". Only numbers per OS
user leave the machine: never pids, process names or command lines."""
import os
import pwd
import re
import select
import shutil
import subprocess
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

# the rule of thumb, in one place (README "Shared machines" and the manager's notices say it)
GPU_HELD_MB = 1024  # a person holds a GPU when their processes use this much of its memory
HEAVY_SHARE = 0.5  # of the GPUs, the CPU cores or the memory
GPU_FULL = 0.8  # a held GPU this busy or this full
RULE = ("heavy means holding half the GPUs or more (1 GB or more on a GPU holds it), a held GPU"
        " 80% busy or full, or half the CPU cores or memory")

SMI_TIMEOUT_S = 5
SMI_RETRY_S = 600  # after nvidia-smi hung or failed
KEEP_CPU, KEEP_MB = 0.1, 100  # an OS user below both, with no GPU memory, is not reported
STALE_S = 180  # older use is ignored by the hub


def _nvidia_smi() -> Optional[str]:
    return shutil.which("nvidia-smi")


def _num(text: str) -> Optional[float]:
    try:
        return float(text.strip())
    except ValueError:
        return None  # [N/A], [Not Supported]


def parse_gpus(text: str) -> List[dict]:
    """`--query-gpu=index,uuid,utilization.gpu,memory.used,memory.total` csv, no header/units."""
    out = []
    for line in text.splitlines():
        f = [x.strip() for x in line.split(",")]
        if len(f) != 5 or _num(f[0]) is None:
            continue
        out.append({"index": int(_num(f[0])), "uuid": f[1], "util": _num(f[2]),
                    "mem": _num(f[3]), "total": _num(f[4])})
    return out


def parse_apps(text: str) -> List[Tuple[str, int, float]]:
    """`--query-compute-apps=gpu_uuid,pid,used_memory` csv: (gpu uuid, pid, MB)."""
    out = []
    for line in text.splitlines():
        f = [x.strip() for x in line.split(",")]
        if len(f) != 3 or not f[1].isdigit():
            continue
        out.append((f[0], int(f[1]), _num(f[2]) or 0.0))
    return out


# Jetson: no nvidia-smi before JetPack 7. The GPU's busy share is in sysfs, as jetson-stats
# (jtop) reads it: a devfreq device whose device tree name is a known GPU, or the older fixed
# path. The value is per mille. Per-user GPU memory is not available, so only busy % is kept.
JETSON_GPU_NAMES = {"gv11b", "gp10b", "ga10b", "gb10b", "gm20b", "gpu"}
JETSON_LOAD_PATHS = ("devices/gpu.0/load", "devices/platform/gpu.0/load")
TEGRASTATS_TIMEOUT_S = 2


def jetson_gpu_load(sys_root: Path = Path("/sys")) -> Optional[float]:
    """The integrated GPU's busy % from sysfs, or None when no readable GPU load is found."""
    files = []
    try:
        entries = sorted(os.listdir(sys_root / "class/devfreq"))
    except OSError:
        entries = []
    for e in entries:
        dev = sys_root / "class/devfreq" / e / "device"
        try:
            name = (dev / "of_node/name").read_text().strip("\x00\n ").lower()
        except OSError:
            name = e.lower()
        if name in JETSON_GPU_NAMES or "gpu" in name:
            files.append(dev / "load")
    files += [sys_root / p for p in JETSON_LOAD_PATHS]
    for f in files:
        try:
            return min(max(float(f.read_text().strip()) / 10.0, 0.0), 100.0)
        except (OSError, ValueError):
            continue
    return None


def parse_tegrastats(text: str) -> Optional[float]:
    """GR3D_FREQ (the GPU) busy % from tegrastats output: "GR3D_FREQ 45%" or "GR3D_FREQ 45%@1300"."""
    import re
    m = re.search(r"GR3D_FREQ (\d+)%", text)
    return float(m.group(1)) if m else None


class Sampler:
    """Keeps the last CPU ticks per pid, so CPU is the average since the previous read."""

    def __init__(self, proc: Path = Path("/proc"), clock: Callable[[], float] = time.monotonic,
                 popen=None, sys_root: Path = Path("/sys")):
        self.proc, self.clock, self.sys_root = Path(proc), clock, Path(sys_root)
        self._tegra_off_until = 0.0
        self.popen = popen or subprocess.Popen
        self._stuck: list = []  # killed nvidia-smi children not yet exited
        self._ticks: Dict[int, int] = {}
        self._at: Optional[float] = None
        self._smi_off_until = 0.0
        self._names: Dict[int, str] = {}

    def _user(self, uid: int) -> str:
        if uid not in self._names:
            try:
                self._names[uid] = pwd.getpwuid(uid).pw_name
            except KeyError:
                self._names[uid] = str(uid)
        return self._names[uid]

    def _owner(self, pid: int) -> Optional[str]:
        try:
            return self._user((self.proc / str(pid)).stat().st_uid)
        except OSError:
            return None  # gone, or in another pid namespace

    def _procs(self) -> List[Tuple[int, str, int, int]]:
        """(pid, os user, cpu ticks, rss pages) of every process this account can see."""
        out = []
        try:
            entries = os.listdir(self.proc)
        except OSError:
            return out
        for name in entries:
            if not name.isdigit():
                continue
            d = self.proc / name
            try:
                uid = d.stat().st_uid
                text = (d / "stat").read_text()
            except OSError:
                continue
            rest = text[text.rfind(")") + 2:].split()
            if len(rest) < 22:
                continue
            out.append((int(name), self._user(uid), int(rest[11]) + int(rest[12]), int(rest[21])))
        return out

    def _reap(self) -> None:
        """Drop the nvidia-smi children that exited since; never wait on one."""
        self._stuck = [p for p in self._stuck if p.poll() is None]

    def _gpus(self) -> Optional[Tuple[List[dict], list]]:
        """Both queries at once, within one SMI_TIMEOUT_S. On a hung driver nvidia-smi can sit
        where a kill does nothing: it is killed and left, never waited on (reaped on a later
        read), so the heartbeat never blocks past the timeout."""
        self._reap()
        exe = _nvidia_smi()
        if not exe or self.clock() < self._smi_off_until:
            return None
        deadline = time.monotonic() + SMI_TIMEOUT_S
        procs, outs = [], []
        try:
            for query in ("--query-gpu=index,uuid,utilization.gpu,memory.used,memory.total",
                          "--query-compute-apps=gpu_uuid,pid,used_memory"):
                procs.append(self.popen([exe, query, "--format=csv,noheader,nounits"],
                                        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                        stdin=subprocess.DEVNULL, text=True))
            for p in procs:
                out, _ = p.communicate(timeout=max(deadline - time.monotonic(), 0.01))
                if p.returncode != 0:
                    raise OSError("nvidia-smi failed")
                outs.append(out or "")
        except (subprocess.TimeoutExpired, OSError):
            for p in procs:
                if p.poll() is None:
                    try:
                        p.kill()
                    except OSError:
                        pass
                    self._stuck.append(p)
            self._smi_off_until = self.clock() + SMI_RETRY_S
            return None
        return parse_gpus(outs[0]), parse_apps(outs[1])

    def _jetson(self) -> Optional[List[dict]]:
        """A Jetson's one GPU with its busy % only: sysfs first, then one tegrastats line within
        TEGRASTATS_TIMEOUT_S (killed and left like nvidia-smi; off for SMI_RETRY_S after a
        failure). None when neither gives a number."""
        util = jetson_gpu_load(self.sys_root)
        if util is None:
            exe = shutil.which("tegrastats")
            if exe and self.clock() >= self._tegra_off_until:
                try:
                    p = self.popen([exe, "--interval", "200"], stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, text=True)
                except OSError:
                    p = None
                out = ""
                if p is not None:
                    out = self._first_gr3d(p)
                util = parse_tegrastats(out if isinstance(out, str) else out.decode(errors="replace"))
                if util is None:
                    self._tegra_off_until = self.clock() + SMI_RETRY_S
        if util is None:
            return None
        return [{"index": 0, "util": util, "mem": None, "total": None}]

    def _first_gr3d(self, p) -> str:
        """tegrastats' output up to its first complete GR3D line, within TEGRASTATS_TIMEOUT_S;
        then it is killed and reaped (or kept in _stuck). It runs until stopped."""
        out = ""
        fileno = getattr(getattr(p, "stdout", None), "fileno", None)
        if fileno is not None:  # a real pipe: stop at the first GPU line
            fd, end = fileno(), time.monotonic() + TEGRASTATS_TIMEOUT_S
            try:
                while not re.search(r"GR3D[^\n]*\n", out):
                    left = end - time.monotonic()
                    if left <= 0 or not select.select([fd], [], [], left)[0]:
                        break
                    chunk = os.read(fd, 4096)
                    if not chunk:
                        break
                    out += chunk.decode(errors="replace")
            except (OSError, ValueError):
                pass
        else:
            try:
                return p.communicate(timeout=TEGRASTATS_TIMEOUT_S)[0] or ""
            except subprocess.TimeoutExpired:
                pass
        try:  # stop it, keep what it still had
            p.kill()
            more = p.communicate(timeout=0.5)[0] or ""
            out += more if isinstance(more, str) else more.decode(errors="replace")
        except (OSError, ValueError, subprocess.TimeoutExpired):
            self._stuck.append(p)
        return out

    def read(self) -> dict:
        now = self.clock()
        procs = self._procs()
        page = os.sysconf("SC_PAGE_SIZE") / 1048576
        hz = os.sysconf("SC_CLK_TCK")
        users: Dict[str, dict] = {}
        window = None if self._at is None else now - self._at
        for pid, user, ticks, rss in procs:
            u = users.setdefault(user, {"cpu": 0.0, "mem": 0.0, "gpus": {}})
            u["mem"] += rss * page
            if window:
                u["cpu"] += max(ticks - self._ticks.get(pid, 0), 0) / hz / window
        self._ticks, self._at = {pid: t for pid, _, t, _ in procs}, now
        out: dict = {"read_at": time.time(), "cpus": os.cpu_count() or 1,
                     "mem_mb": _mem_total(self.proc)}
        smi = self._gpus()
        if smi is not None:
            gpus, apps = smi
            index = {g["uuid"]: g["index"] for g in gpus}
            out["gpus"] = [{k: g[k] for k in ("index", "util", "mem", "total")} for g in gpus]
            for uuid, pid, mb in apps:
                user = self._owner(pid) or "unknown"
                u = users.setdefault(user, {"cpu": 0.0, "mem": 0.0, "gpus": {}})
                i = str(index.get(uuid, -1))
                u["gpus"][i] = u["gpus"].get(i, 0) + mb
        elif not _nvidia_smi():
            jetson = self._jetson()
            if jetson is not None:
                out["gpus"] = jetson
        out["users"] = {
            name: {**({"cpu": round(u["cpu"], 1)} if window else {}), "mem": int(u["mem"]),
                   "gpus": {i: int(mb) for i, mb in u["gpus"].items()}}
            for name, u in users.items()
            if u["cpu"] >= KEEP_CPU or u["mem"] >= KEEP_MB or u["gpus"]}
        return out


def _mem_total(proc: Path) -> int:
    try:
        for line in (proc / "meminfo").read_text().splitlines():
            if line.startswith("MemTotal:"):
                return int(line.split()[1]) // 1024
    except (OSError, ValueError, IndexError):
        pass
    return 0


# the hub's side: people, heavy, words

def people_use(use: dict, owners: Dict[str, List[str]], team=()) -> Dict[str, dict]:
    """{person: {"cpu", "mem", "gpus": {index: MB}, "people"}}, each OS user's numbers added under the
    people whose devices run as it ("alice and bob" for a shared account); an OS user no one
    joined from shows as "account <name>", and a team machine's (`team`) as
    "account <name> (team)"."""
    out: Dict[str, dict] = {}
    for user, u in (use.get("users") or {}).items():
        people = sorted(set(owners.get(user) or []))
        label = " and ".join(people) or f"account {user}" + (" (team)" if user in team else "")
        p = out.setdefault(label, {"cpu": 0.0, "mem": 0, "gpus": {}, "people": people})
        p["cpu"] += u.get("cpu") or 0.0
        p["mem"] += u.get("mem") or 0
        for i, mb in (u.get("gpus") or {}).items():
            p["gpus"][i] = p["gpus"].get(i, 0) + mb
    return out


def others(by_person: Dict[str, dict], use: dict, me: str) -> List[str]:
    """One line for each person other than `me` whose use is heavy, heaviest GPU use first."""
    rows = [(n, p) for n, p in by_person.items() if me not in p.get("people", []) and heavy(p, use)]
    rows.sort(key=lambda r: (-len(held(r[1])), -r[1].get("cpu", 0), r[0]))
    return [describe(n, p, use) for n, p in rows]


def held(p: dict) -> List[str]:
    return sorted(i for i, mb in p.get("gpus", {}).items() if mb >= GPU_HELD_MB)


def heavy(p: dict, use: dict) -> bool:
    gpus = {str(g["index"]): g for g in use.get("gpus") or []}
    mine = held(p)
    if gpus and len(mine) >= HEAVY_SHARE * len(gpus):
        return True
    for i in mine:
        g = gpus.get(i) or {}
        if (g.get("util") or 0) >= GPU_FULL * 100 or \
                (g.get("total") and (g.get("mem") or 0) >= GPU_FULL * g["total"]):
            return True
    cpus, mem = use.get("cpus") or 0, use.get("mem_mb") or 0
    return bool((cpus and p.get("cpu", 0) >= HEAVY_SHARE * cpus)
                or (mem and p.get("mem", 0) >= HEAVY_SHARE * mem))


def describe(name: str, p: dict, use: dict) -> str:
    """"alice is using 3 of 4 GPUs (90% busy at most), 12.0 of 32 CPU cores and 40 of 64 GB
    memory" """
    parts = []
    gpus = use.get("gpus") or []
    if gpus:
        mine = held(p)
        busy = [g.get("util") for g in gpus if str(g["index"]) in mine and g.get("util") is not None]
        parts.append(f"{len(mine)} of {len(gpus)} GPUs"
                     + (f" ({int(max(busy))}% busy at most)" if busy else ""))
    if use.get("cpus"):
        parts.append(f"{p.get('cpu', 0):.1f} of {use['cpus']} CPU cores")
    if use.get("mem_mb"):
        parts.append(f"{p.get('mem', 0) / 1024:.0f} of {use['mem_mb'] / 1024:.0f} GB memory")
    words = ", ".join(parts[:-1]) + (" and " if len(parts) > 1 else "") + parts[-1] if parts \
        else "the machine"
    return f"{name} is using {words}"


def kind(use: Optional[dict]) -> str:
    """A computer's size in a few words for the admin's Machines list, from its last report:
    "2 GPUs 24 GB · 32 CPUs · 128 GB RAM", "Jetson · 8 CPUs · 16 GB RAM" (a Jetson's one GPU
    reports no memory), "" when nothing was reported. The bridge sends no GPU model."""
    if not isinstance(use, dict):
        return ""
    parts, gpus = [], use.get("gpus") or []
    if len(gpus) == 1 and gpus[0].get("total") is None:
        parts.append("Jetson")
    elif gpus:
        total = max((g.get("total") or 0) for g in gpus)
        n = f"{len(gpus)} GPUs" if len(gpus) > 1 else "GPU"
        parts.append(f"{n} {round(total / 1024)} GB" if total >= 1024 else n)
    if use.get("cpus"):
        parts.append(f"{use['cpus']} CPUs")
    if use.get("mem_mb"):
        parts.append(f"{round(use['mem_mb'] / 1024)} GB RAM")
    return " · ".join(parts)


def short(use: Optional[dict]) -> str:
    """A machine's use in a few words for the admin's Machines list: "GPU 90% · CPU 35% · memory
    40%" (GPU: the busiest one), "" when nothing was reported."""
    if not isinstance(use, dict):
        return ""
    parts = []
    utils = [g.get("util") for g in use.get("gpus") or [] if g.get("util") is not None]
    if utils:
        parts.append(f"GPU {round(max(utils))}%")
    users = (use.get("users") or {}).values()
    cpus, mem = use.get("cpus") or 0, use.get("mem_mb") or 0
    if cpus:
        parts.append(f"CPU {round(100 * sum(u.get('cpu') or 0 for u in users) / cpus)}%")
    if mem:
        parts.append(f"memory {round(100 * sum(u.get('mem') or 0 for u in users) / mem)}%")
    return " · ".join(parts)


# the GPU share queue: jobs coordinate with flock(1) on one lock file L. A job holds L shared
# and K of L.share1..L.shareN exclusively; a job wanting the whole GPU holds L exclusively;
# waiting jobs queue on L.queue (one holds it while it looks for room, the rest block on it).
# Read from /proc/locks only: never a lock taken, never a file's content trusted.
GPU_SHARES = 10


def parse_locks(text: str) -> List[dict]:
    """/proc/locks lines as {"pid", "ino", "write", "waiting"}: FLOCK locks only. A blocked
    waiter's line is marked "->". Lines that do not parse are skipped."""
    out = []
    for line in text.splitlines():
        f = line.split()
        if len(f) < 6 or not f[0].endswith(":"):
            continue
        waiting = f[1] == "->"
        f = f[2:] if waiting else f[1:]
        if len(f) < 5 or f[0] != "FLOCK":
            continue
        dev = f[4].split(":")
        if len(dev) != 3 or not f[3].isdigit() or not dev[2].isdigit():
            continue
        out.append({"pid": int(f[3]), "ino": int(dev[2]), "write": f[2] == "WRITE",
                    "waiting": waiting})
    return out


def queue_state(locks: List[dict], lock_ino: int, share_inos: Dict[int, int],
                queue_ino: Optional[int], user: Callable[[int], str], shares: int) -> dict:
    """{"shares", "holders": {user: shares held}, "whole": user or "", "waiting": n,
    "waiters": {user: n}} from parsed /proc/locks. Matched on inode only: btrfs and overlay
    report a device in /proc/locks other than stat's. `share_inos`: inode -> share number.
    Waiting: the job holding L.queue (it looks for room) and every job blocked on L.queue or
    L, each pid once."""
    held: Dict[int, set] = {}
    whole, waiters = "", set()
    for lk in locks:
        ino, pid = lk["ino"], lk["pid"]
        if lk["waiting"]:
            if ino == queue_ino or ino == lock_ino:
                waiters.add(pid)
        elif ino in share_inos and lk["write"]:
            held.setdefault(pid, set()).add(share_inos[ino])
        elif ino == lock_ino and lk["write"]:
            whole = user(pid)
        elif ino == queue_ino and lk["write"]:
            waiters.add(pid)
    holders: Dict[str, int] = {}
    for pid, nums in held.items():
        u = user(pid)
        holders[u] = holders.get(u, 0) + len(nums)
    waiting: Dict[str, int] = {}
    for pid in waiters - set(held):
        u = user(pid)
        waiting[u] = waiting.get(u, 0) + 1
    return {"shares": shares, "holders": holders, "whole": whole,
            "waiting": sum(waiting.values()), "waiters": waiting}


def pid_user(proc: Path, pid: int, name: Callable[[int], str]) -> str:
    """The OS user a pid runs as, from the real uid in /proc/<pid>/status; "unknown" when it
    is gone or in another pid namespace."""
    try:
        for line in (proc / str(pid) / "status").read_text().splitlines():
            if line.startswith("Uid:"):
                return name(int(line.split()[1]))
    except (OSError, ValueError, IndexError):
        pass
    return "unknown"


def read_queue(lock: str, shares: int, proc: Path = Path("/proc"),
               name: Optional[Callable[[int], str]] = None) -> dict:
    """The queue on lock file `lock` now; {"unknown": True} when L or /proc/locks cannot be
    read. Never blocks: stat and one read of /proc/locks."""
    def ino(path: str) -> Optional[int]:
        try:
            return os.stat(path).st_ino
        except OSError:
            return None
    lock_ino = ino(lock)
    try:
        text = (proc / "locks").read_text()
    except OSError:
        text = None
    if lock_ino is None or text is None:
        return {"unknown": True, "shares": shares}
    share_inos = {}
    for i in range(1, shares + 1):
        n = ino(f"{lock}.share{i}")
        if n is not None:
            share_inos[n] = i
    names: Dict[int, str] = {}

    def by_uid(uid: int) -> str:
        if uid not in names:
            try:
                names[uid] = pwd.getpwuid(uid).pw_name
            except KeyError:
                names[uid] = str(uid)
        return names[uid]
    return queue_state(parse_locks(text), lock_ino, share_inos, ino(f"{lock}.queue"),
                       lambda pid: pid_user(proc, pid, name or by_uid), shares)


def queue_used(q: dict) -> int:
    return min(sum((q.get("holders") or {}).values()), q.get("shares") or GPU_SHARES)


def queue_full(q: Optional[dict]) -> bool:
    """Heavy for the manager: every share held, the GPU taken whole, or someone waiting."""
    if not isinstance(q, dict) or q.get("unknown"):
        return False
    return bool(q.get("whole")) or q.get("waiting", 0) > 0 \
        or queue_used(q) >= (q.get("shares") or GPU_SHARES)


def queue_counts(q: dict) -> dict:
    """The queue with no names, for anyone but an admin: {"shares", "used", "waiting",
    "whole": bool}, or {"shares", "unknown": True}."""
    if q.get("unknown"):
        return {"shares": q.get("shares") or GPU_SHARES, "unknown": True}
    return {"shares": q.get("shares") or GPU_SHARES, "used": queue_used(q),
            "waiting": q.get("waiting", 0), "whole": bool(q.get("whole"))}


def queue_line(q: Optional[dict], names: bool, label: Callable[[str], str] = str) -> str:
    """"GPU queue: bob 4/10 · carol 6/10 · 1 waiting" (names, for admins), or only counts:
    "GPU queue: 10/10 in use · 1 waiting". `label` turns an OS user into a person's name.
    "" when nothing was reported."""
    if not isinstance(q, dict):
        return ""
    if q.get("unknown"):
        return "GPU queue: unknown"
    n = q.get("shares") or GPU_SHARES
    wait = f" · {q['waiting']} waiting" if q.get("waiting") else ""
    if q.get("whole"):
        return f"GPU queue: taken whole{' by ' + label(q['whole']) if names else ''}{wait}"
    used = queue_used(q)
    if not used:
        return "GPU queue: free" + wait
    if not names:
        return f"GPU queue: {used}/{n} in use{wait}"
    by: Dict[str, int] = {}
    for u, k in (q.get("holders") or {}).items():
        by[label(u)] = by.get(label(u), 0) + k
    return "GPU queue: " + " · ".join(f"{who} {k}/{n}" for who, k in sorted(by.items())) + wait
