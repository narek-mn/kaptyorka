"""Machine use per OS user: /proc and nvidia-smi parsing (from fixtures), and the heavy rule."""
import os
import subprocess
from types import SimpleNamespace

from kaptyorka import usage
from kaptyorka.workers import clean_use

GPUS = """0, GPU-aaa, 95, 30000, 32000
1, GPU-bbb, 90, 28000, 32000
2, GPU-ccc, 0, 500, 32000
3, GPU-ddd, [N/A], [N/A], 32000
"""
APPS = """GPU-aaa, 101, 29000
GPU-bbb, 101, 27000
GPU-ccc, 202, 400
GPU-aaa, 999, [N/A]
"""


def stat_line(pid, utime, stime, rss, name="py (x) y"):
    rest = ["S"] + ["0"] * 10 + [str(utime), str(stime)] + ["0"] * 8 + [str(rss), "0", "0"]
    return f"{pid} ({name}) " + " ".join(rest)


def fake_proc(tmp_path, procs):
    proc = tmp_path / "proc"
    proc.mkdir(exist_ok=True)
    (proc / "meminfo").write_text("MemTotal:       65536000 kB\nMemFree: 1 kB\n")
    for pid, (ut, st, rss) in procs.items():
        (proc / str(pid)).mkdir(exist_ok=True)
        (proc / str(pid) / "stat").write_text(stat_line(pid, ut, st, rss))
    (proc / "self").mkdir(exist_ok=True)  # not a pid: skipped
    return proc


class SMI:
    def __init__(self, gpus=GPUS, apps=APPS, hang=False, code=0):
        self.gpus, self.apps, self.hang, self.code, self.calls = gpus, apps, hang, code, []

    def __call__(self, argv, **kw):
        self.calls.append(argv)
        assert kw["timeout"] == usage.SMI_TIMEOUT_S
        if self.hang:
            raise subprocess.TimeoutExpired(argv, kw["timeout"])
        out = self.gpus if any("query-gpu" in a for a in argv) else self.apps
        return SimpleNamespace(returncode=self.code, stdout=out, stderr="")


def test_parse_gpus_and_apps():
    g = usage.parse_gpus(GPUS + "garbage\n")
    assert [x["index"] for x in g] == [0, 1, 2, 3]
    assert g[0] == {"index": 0, "uuid": "GPU-aaa", "util": 95.0, "mem": 30000.0, "total": 32000.0}
    assert g[3]["util"] is None and g[3]["mem"] is None
    assert usage.parse_apps(APPS) == [("GPU-aaa", 101, 29000.0), ("GPU-bbb", 101, 27000.0),
                                      ("GPU-ccc", 202, 400.0), ("GPU-aaa", 999, 0.0)]


def test_cpu_and_memory_per_user_from_proc(tmp_path):
    clock = [100.0]
    page = os.sysconf("SC_PAGE_SIZE")
    hz = os.sysconf("SC_CLK_TCK")
    proc = fake_proc(tmp_path, {11: (0, 0, 1048576 * 200 // page)})
    s = usage.Sampler(proc, clock=lambda: clock[0])
    s._user = lambda uid: "alice"
    first = s.read(run=None)
    assert first["mem_mb"] == 64000 and "gpus" not in first  # no nvidia-smi: CPU and memory only
    assert first["users"] == {"alice": {"mem": 200, "gpus": {}}}  # no CPU before a window
    clock[0] += 10
    fake_proc(tmp_path, {11: (15 * hz, 5 * hz, 1048576 * 200 // page)})
    assert s.read(run=None)["users"]["alice"]["cpu"] == 2.0  # 20 s of cpu in 10 s


def test_gpu_holders_mapped_to_os_users(tmp_path, monkeypatch):
    monkeypatch.setattr(usage, "_nvidia_smi", lambda: "/x/nvidia-smi")
    s = usage.Sampler(fake_proc(tmp_path, {}))
    owners = {101: "alice", 202: "bob"}
    s._owner = lambda pid: owners.get(pid)
    out = s.read(run=SMI())
    assert len(out["gpus"]) == 4 and "uuid" not in out["gpus"][0]
    assert out["users"]["alice"]["gpus"] == {"0": 29000, "1": 27000}
    assert out["users"]["bob"]["gpus"] == {"2": 400}
    assert out["users"]["unknown"]["gpus"] == {"0": 0}  # a pid this account cannot see
    for u in out["users"].values():  # numbers only: no pid, name or command line
        assert set(u) <= {"cpu", "mem", "gpus"}


def test_hung_nvidia_smi_is_left_out_for_ten_minutes(tmp_path, monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(usage, "_nvidia_smi", lambda: "/x/nvidia-smi")
    s = usage.Sampler(fake_proc(tmp_path, {}), clock=lambda: clock[0])
    smi = SMI(hang=True)
    assert "gpus" not in s.read(run=smi) and len(smi.calls) == 1
    clock[0] += 60
    assert "gpus" not in s.read(run=smi) and len(smi.calls) == 1  # not asked again yet
    clock[0] += usage.SMI_RETRY_S
    smi.hang = False
    assert len(s.read(run=smi)["gpus"]) == 4
    failed = SMI(code=9)
    clock[0] += usage.SMI_RETRY_S
    assert "gpus" not in s.read(run=failed)


USE = {"cpus": 32, "mem_mb": 65536,
       "gpus": [{"index": i, "util": u, "mem": 0, "total": 32000} for i, u in
                enumerate((95, 90, 10, 0))],
       "users": {"alice": {"cpu": 1.0, "mem": 4096, "gpus": {"0": 29000, "1": 27000}},
                 "alice2": {"cpu": 2.0, "mem": 1024, "gpus": {"2": 2000}},
                 "bob": {"cpu": 0.5, "mem": 2048, "gpus": {"3": 500}},
                 "root": {"cpu": 20.0, "mem": 100, "gpus": {}}}}
OWNERS = {"alice": ["alice"], "alice2": ["alice"], "bob": ["bob"]}


def test_one_person_two_os_users_add_up():
    by = usage.people_use(USE, OWNERS)
    assert by["alice"]["cpu"] == 3.0 and by["alice"]["mem"] == 5120
    assert usage.held(by["alice"]) == ["0", "1", "2"] and usage.held(by["bob"]) == []
    assert "account root" in by  # an account no one joined from still counts


def test_heavy_rule():
    by = usage.people_use(USE, OWNERS)
    assert usage.heavy(by["alice"], USE)  # 3 of 4 GPUs
    assert not usage.heavy(by["bob"], USE)
    assert usage.heavy(by["account root"], USE)  # 20 of 32 cores
    one = {"cpu": 0.0, "mem": 0, "gpus": {"0": 2000}}
    assert usage.heavy(one, USE)  # GPU 0 is 95% busy
    quiet = {**USE, "gpus": [{**g, "util": 0} for g in USE["gpus"]]}
    assert not usage.heavy({**one, "gpus": {"2": 2000}}, quiet)  # one quiet GPU of four
    assert usage.heavy({"cpu": 0, "mem": 40000, "gpus": {}}, {"cpus": 8, "mem_mb": 64000})
    no_gpu = {"cpus": 8, "mem_mb": 64000}  # a machine without GPUs: CPU and memory only
    assert not usage.heavy({"cpu": 3.9, "mem": 100, "gpus": {}}, no_gpu)
    assert usage.heavy({"cpu": 4.0, "mem": 100, "gpus": {}}, no_gpu)


def test_describe_and_others():
    by = usage.people_use(USE, OWNERS)
    assert usage.describe("alice", by["alice"], USE) == (
        "alice is using 3 of 4 GPUs (95% busy at most), 3.0 of 32 CPU cores and 5 of 64 GB memory")
    assert usage.describe("bob", by["bob"], {"cpus": 8, "mem_mb": 16384}) == (
        "bob is using 0.5 of 8 CPU cores and 2 of 16 GB memory")
    lines = usage.others(by, USE, "bob")
    assert lines[0].startswith("alice is using 3 of 4 GPUs") and len(lines) == 2
    assert all(not x.startswith("alice") for x in usage.others(by, USE, "alice"))


def test_hub_keeps_only_numbers():
    raw = {"age": 5, "cpus": 8, "mem_mb": 1000, "pids": [1, 2],
           "gpus": [{"index": 0, "util": 50, "mem": 1, "total": 2, "name": "x"}, "bad"],
           "users": {"alice": {"cpu": 1.5, "mem": 10, "gpus": {"0": 5}, "cmd": "python x"},
                     "-oProxy": {"mem": 1}, "bob": "x"}}
    assert clean_use(raw) == {"age": 5.0, "cpus": 8, "mem_mb": 1000,
                              "gpus": [{"index": 0, "util": 50.0, "mem": 1.0, "total": 2.0}],
                              "users": {"alice": {"cpu": 1.5, "mem": 10, "gpus": {"0": 5}}}}
    assert clean_use("x") is None


def test_bridge_sends_the_age_of_its_use_reading():
    import time

    from kaptyorka.bridge import Bridge

    sent = Bridge._sendable({"use": {"read_at": time.time() - 30, "cpus": 4}, "git": {}}, set())
    assert "read_at" not in sent["use"] and 29 <= sent["use"]["age"] <= 31
