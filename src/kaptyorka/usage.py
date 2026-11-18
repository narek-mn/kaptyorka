"""Who uses this machine and how much: CPU, memory and GPU per OS user, read by the bridge; and
the one rule the hub and the manager use to call a person's use "heavy". Only numbers per OS
user leave the machine: never pids, process names or command lines."""
import os
import pwd
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


class Sampler:
    """Keeps the last CPU ticks per pid, so CPU is the average since the previous read."""

    def __init__(self, proc: Path = Path("/proc"), clock: Callable[[], float] = time.monotonic,
                 popen=None):
        self.proc, self.clock = Path(proc), clock
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

def people_use(use: dict, owners: Dict[str, List[str]]) -> Dict[str, dict]:
    """{person: {"cpu", "mem", "gpus": {index: MB}, "people"}}, each OS user's numbers added under the
    people whose devices run as it ("alice and bob" for a shared account); an OS user no one
    joined from shows as "account <name>"."""
    out: Dict[str, dict] = {}
    for user, u in (use.get("users") or {}).items():
        people = sorted(set(owners.get(user) or []))
        label = " and ".join(people) or f"account {user}"
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
