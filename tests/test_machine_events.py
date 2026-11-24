"""Machine events (machine_events.py): what a team machine's bridge confirms becomes one dated
line in the team note `machines` and one message to the manager. A fake clock throughout."""
import time
from types import SimpleNamespace

import pytest

import kaptyorka
from helpers import call, person, run
from test_hub_manager import HOST, clock, hub, inbox, manager_online  # noqa: F401 (fixtures)
from kaptyorka import facts as facts_mod
from kaptyorka import http
from kaptyorka import machine_events as me
from kaptyorka.bridge import Bridge
from kaptyorka.workers import clean_facts

BASE = {"os": "Ubuntu 22.04.5 LTS", "l4t": "R36.4.3", "jetpack": "6.2", "kernel": "5.15.148-tegra",
        "boot": 1_000, "ip": "10.0.0.7", "tools": {"claude-code": "2.1.329"}}


KEEP: list = []  # the manager's session, kept online by each heartbeat a test sends


@pytest.fixture(autouse=True)
def _keep():
    KEEP.clear()


def online(hub):
    mgr, who = manager_online(hub)
    KEEP[:] = [mgr, who]
    return mgr, who


def team(hub, name, host=None, os_user="lab"):
    """A team machine joined just now; its first heartbeat not sent yet."""
    _, code = hub.store.team_machine_add(name, "test")
    token, _ = hub.store.invite_redeem(code, host or name, host=host or name, os_user=os_user)
    return SimpleNamespace(token=token, host=host or name, os_user=os_user, seq=0)


def again(hub, d):
    """The same team machine joins again as a new device (a reinstall: new invite, same host)."""
    pid = hub.store.authenticate(d.token).person_id
    token, _ = hub.store.invite_redeem(hub.store.invite_create(pid), d.host, host=d.host,
                                       os_user=d.os_user)
    return SimpleNamespace(token=token, host=d.host, os_user=d.os_user, seq=0)


def beat(hub, d, new_build=True, **changes):
    if KEEP:
        stay(*KEEP)
    if new_build:
        d.seq += 1
    f = {"host": d.host, "os_user": d.os_user, "version": kaptyorka.__version__, "seq": d.seq,
         **BASE, **changes}
    body = {"machine": d.host, "sessions": [], "tools": [], "facts": f}
    status, answer = http.request("POST", hub.url + "/bridge/heartbeat", d.token, body)
    assert status == 200
    return answer


def events(mgr, who):
    """The machine events the manager got since the last look (reading keeps it online)."""
    return [t[len("machine event: "):t.index(". Follow your machine-events rule")]
            for t in inbox(mgr, who) if t.startswith("machine event: ")]


def note(hub):
    n = hub.store.notes_read("machines")
    return "" if n is None else n.text


def event_lines(hub):
    text = note(hub)
    if me.NOTE_HEADER not in text:
        return []
    section = text.split(me.NOTE_HEADER, 1)[1].split("\n## ", 1)[0]  # up to the next heading
    return [x for x in section.strip().splitlines() if x.strip()]


def stay(mgr, who):
    run(call(mgr, who, "status", text="watching"))  # the manager's session stays online


def test_a_new_team_machine_and_one_line_per_event(hub, clock):
    mgr, who = online(hub)
    d = team(hub, "jet-1")
    assert beat(hub, d)["machine_facts"] == "team"
    assert events(mgr, who) == ["jet-1: new machine joined"]
    day = time.strftime("%Y-%m-%d %H:%M", time.localtime(clock[0]))
    assert event_lines(hub) == [f"- {day} jet-1: new machine joined"]
    beat(hub, d)
    assert events(mgr, who) == [] and len(event_lines(hub)) == 1  # once


def test_a_change_counts_after_three_builds_in_a_row(hub, clock):
    mgr, who = online(hub)
    d = team(hub, "jet-1")
    beat(hub, d)
    events(mgr, who)
    for _ in range(4):  # the same build sent again counts once
        clock[0] += 10
        beat(hub, d, new_build=False, kernel="6.8.0-tegra")
    beat(hub, d, kernel="6.8.0-tegra")
    clock[0] += 60
    beat(hub, d, kernel="6.8.0-tegra")
    assert events(mgr, who) == []  # two builds: not yet
    clock[0] += 60
    beat(hub, d, kernel="6.8.0-tegra", tools={"claude-code": "2.1.331"})
    assert events(mgr, who) == ["jet-1: kernel 5.15.148-tegra -> 6.8.0-tegra"]
    for _ in range(2):
        clock[0] += 60
        beat(hub, d, kernel="6.8.0-tegra", tools={"claude-code": "2.1.331"})
    assert events(mgr, who) == []  # a tool update: the note only, never the manager


def test_tool_updates_go_to_the_note_only_one_line_for_many_machines(hub, clock):
    mgr, who = online(hub)
    ds = [team(hub, f"jet-{i}") for i in (1, 2, 3, 4)]
    for d in ds:
        beat(hub, d)
    events(mgr, who)
    new = {"claude-code": "2.1.332"}
    start = clock[0]
    for i, d in enumerate(ds[:3]):  # confirmed at start+120, +470, +820: the third is late
        clock[0] = start + 350 * i
        for k in range(3):
            clock[0] += 60 if k else 0
            beat(hub, d, tools=new)
    lines = lambda: [x.split(" ", 3)[3] for x in event_lines(hub) if "Claude Code" in x]
    clock[0] = start + 120 + me.TOOL_GROUP_S - 1
    hub.tick()
    assert lines() == []  # the window is still open
    clock[0] += 1
    hub.tick()
    assert lines() == ["Claude Code 2.1.329 -> 2.1.332 on jet-1, jet-2"]
    for _ in range(3):
        clock[0] += 60
        beat(hub, ds[3], tools=new)
    clock[0] += me.TOOL_GROUP_S
    hub.tick()
    assert lines() == ["Claude Code 2.1.329 -> 2.1.332 on jet-1, jet-2",
                       "Claude Code 2.1.329 -> 2.1.332 on jet-3, jet-4"]
    assert events(mgr, who) == []


def test_a_shared_team_machine_gets_only_the_machine_event(hub, clock):
    mgr, who = online(hub)
    d = team(hub, "jet-1")
    beat(hub, d)
    hub.store.device_set_shared(hub.store.authenticate(d.token).device_id, True)
    events(mgr, who)
    clock[0] += 3600
    stay(mgr, who)
    hub.tick()
    texts = inbox(mgr, who)
    assert [t for t in texts if t.startswith("machine event: jet-1: offline since")]
    assert not [t for t in texts if "a shared machine, has been offline" in t]


def test_a_value_that_flips_back_records_nothing(hub, clock):
    mgr, who = online(hub)
    d = team(hub, "jet-1")
    beat(hub, d)
    events(mgr, who)
    for ip in ("10.0.0.9", "10.0.0.9", "10.0.0.7", "10.0.0.9", "10.0.0.9", "10.0.0.7"):
        clock[0] += 60
        beat(hub, d, ip=ip)
    assert events(mgr, who) == []
    assert event_lines(hub) == [event_lines(hub)[0]]  # only the join


def test_reboot_hostname_and_several_changes_in_one_line(hub, clock):
    mgr, who = online(hub)
    d = team(hub, "jet-1")
    beat(hub, d)
    events(mgr, who)
    boot = 1_000_000 + 3600
    for _ in range(3):
        clock[0] += 60
        beat(hub, d, boot=boot, os="Ubuntu 24.04.1 LTS")
    assert events(mgr, who) == [f"jet-1: rebooted at {me.hm(boot)}; OS Ubuntu 22.04.5 LTS ->"
                                " Ubuntu 24.04.1 LTS"]
    d.host = "jet-one"
    for _ in range(3):
        clock[0] += 60
        beat(hub, d, boot=boot, os="Ubuntu 24.04.1 LTS")
    assert events(mgr, who) == ["jet-one: hostname jet-1 -> jet-one"]


def test_offline_after_ten_minutes_and_back(hub, clock):
    mgr, who = online(hub)
    d = team(hub, "jet-1")
    beat(hub, d)
    events(mgr, who)
    gone = clock[0]
    clock[0] += me.OFFLINE_S + me.GROUP_S - 1
    stay(mgr, who)
    hub.tick()
    assert events(mgr, who) == []  # not yet: others may still join the group
    clock[0] += 2
    stay(mgr, who)
    hub.tick()
    hub.tick()
    assert events(mgr, who) == [f"jet-1: offline since {me.hm(gone)}"]
    clock[0] += 3 * 3600
    beat(hub, d)
    assert events(mgr, who) == [f"jet-1: back online after 3 h (offline since {me.hm(gone)})"]
    assert [x.split(" ", 3)[3] for x in event_lines(hub)] == [
        "jet-1: new machine joined", f"jet-1: offline since {me.hm(gone)}",
        f"jet-1: back online after 3 h (offline since {me.hm(gone)})"]


def test_machines_going_offline_together_make_one_event(hub, clock):
    mgr, who = online(hub)
    ds = [team(hub, f"jet-{i}") for i in (1, 2, 3, 4)]
    start = clock[0]
    for i, d in enumerate(ds[:3]):  # silent from start, start+60, start+120
        clock[0] = start + 60 * i
        beat(hub, d)
    clock[0] = start + 1200
    beat(hub, ds[3])  # still reporting: not in the group
    events(mgr, who)
    clock[0] = start + me.OFFLINE_S + me.GROUP_S
    beat(hub, ds[3])
    stay(mgr, who)
    hub.tick()
    assert events(mgr, who) == [f"jet-1, jet-2, jet-3 went offline within 2 min (since"
                                f" {me.hm(start)}): likely network or power"]


def test_a_reinstall_joins_again_as_new_and_reports_the_new_versions(hub, clock):
    mgr, who = online(hub)
    d = team(hub, "jet-1")
    beat(hub, d)
    gone = clock[0]
    clock[0] += 3600
    stay(mgr, who)
    hub.tick()
    events(mgr, who)  # offline since
    clock[0] += 4 * 3600  # reflashed: JetPack 7, its old token gone with the home folder
    n = again(hub, d)
    new = {"l4t": "R38.2.1", "jetpack": "7.2.1", "kernel": "6.8.12-tegra", "boot": 2_000}
    beat(hub, n, **new)
    assert events(mgr, who) == [f"jet-1: joined again as a new device after being offline since"
                                f" {me.hm(gone)}"]
    for _ in range(2):
        clock[0] += 60
        beat(hub, n, **new)
    assert events(mgr, who) == ["jet-1: JetPack 6.2 -> 7.2.1; L4T R36.4.3 -> R38.2.1;"
                                " kernel 5.15.148-tegra -> 6.8.12-tegra"]
    clock[0] += 3600
    beat(hub, n, **new)
    stay(mgr, who)
    hub.tick()  # the replaced device never goes offline again
    assert events(mgr, who) == []


def test_another_account_on_the_same_machine(hub, clock):
    mgr, who = online(hub)
    d = team(hub, "jet-1")
    beat(hub, d)
    events(mgr, who)
    pid = hub.store.authenticate(d.token).person_id
    token, _ = hub.store.invite_redeem(hub.store.invite_create(pid), "jet-1", host="jet-1",
                                       os_user="ci")
    beat(hub, SimpleNamespace(token=token, host="jet-1", os_user="ci", seq=0))
    assert events(mgr, who) == ["jet-1: another account joined (ci)"]


def test_personal_laptops_make_no_events(hub, clock):
    mgr, who = online(hub)
    pid, _ = person(hub.store, "bob")
    token, _ = hub.store.invite_redeem(hub.store.invite_create(pid), "laptop", host="laptop",
                                       os_user="bob")
    d = SimpleNamespace(token=token, host="laptop", os_user="bob", seq=0)
    assert beat(hub, d)["machine_facts"] is None  # it sends nothing more
    clock[0] += 3600
    stay(mgr, who)
    hub.tick()
    beat(hub, d, kernel="6.9")
    assert events(mgr, who) == [] and event_lines(hub) == []


def test_the_hub_machine_goes_offline_but_never_joins(hub, clock):
    mgr, who = online(hub)
    pid, _ = person(hub.store, "root", "admin")
    token, _ = hub.store.invite_redeem(hub.store.invite_create(pid), HOST, host=HOST,
                                       os_user="root")
    d = SimpleNamespace(token=token, host=HOST, os_user="root", seq=0)
    assert beat(hub, d)["machine_facts"] == "hub"  # no address asked for
    assert events(mgr, who) == []
    for _ in range(3):
        clock[0] += 60
        beat(hub, d, ip="10.9.9.9")
    assert events(mgr, who) == []  # its address is not tracked
    gone = clock[0]
    clock[0] += me.OFFLINE_S + me.GROUP_S
    stay(mgr, who)
    hub.tick()
    assert events(mgr, who) == [f"{HOST}: offline since {me.hm(gone)}"]


def test_the_note_keeps_its_text_and_the_last_fifty_lines(hub, clock):
    pid, token = person(hub.store, "root", "admin")
    hub.store.notes_write_from_web("root", "machines", "# Machines\n\n- jet-1: the lab Jetson")
    m = me.MachineEvents(hub.store, lambda: HOST)
    m.write_note([f"jet-1: event {i}" for i in range(30)], clock[0])
    m.write_note([f"jet-1: event {i}" for i in range(30, 55)], clock[0])
    text = note(hub)
    assert text.startswith("# Machines\n\n- jet-1: the lab Jetson\n\n" + me.NOTE_HEADER)
    lines = event_lines(hub)
    assert len(lines) == me.NOTE_CAP and lines[0].endswith("event 5") and lines[-1].endswith("event 54")
    assert hub.store.notes_read("machines").author == "hub"


def test_the_hub_changes_only_its_own_section(hub, clock):
    person(hub.store, "root", "admin")
    m = me.MachineEvents(hub.store, lambda: HOST)
    m.write_note(["jet-1: event 1"], clock[0])
    # someone writes after the hub's section and edits above it
    text = note(hub).replace("## Machine", "# Machines\n\n- jet-1: lab\n\n## Machine") \
        + "\n## GPUs\n\n- jet-1: one Orin\n"
    hub.store.notes_write_from_web("root", "machines", text)
    m.write_note(["jet-1: event 2"], clock[0])
    got = note(hub)
    assert got.startswith("# Machines\n\n- jet-1: lab\n\n" + me.NOTE_HEADER)
    assert got.endswith("## GPUs\n\n- jet-1: one Orin\n")  # never swallowed
    assert [x.rsplit(" ", 2)[-2:] for x in event_lines(hub)] == [["event", "1"], ["event", "2"]]


def test_switched_off_manager_still_gets_the_note(hub, clock):
    d = team(hub, "jet-1")
    beat(hub, d)
    assert len(event_lines(hub)) == 1
    with hub.store.lock:
        assert hub.store.db.execute("SELECT COUNT(*) FROM messages WHERE text LIKE"
                                    " 'machine event:%'").fetchone()[0] == 0


# ---------- the bridge's side ----------

def test_clean_facts_keeps_the_new_facts_and_refuses_a_bad_address():
    got = clean_facts({**BASE, "seq": 4, "ip": "10.0.0.7"})
    assert {k: got[k] for k in ("os", "l4t", "jetpack", "kernel", "boot", "ip", "seq")} == {
        "os": BASE["os"], "l4t": "R36.4.3", "jetpack": "6.2", "kernel": BASE["kernel"],
        "boot": 1000, "ip": "10.0.0.7", "seq": 4}
    assert "ip" not in clean_facts({"ip": "10.0.0.7; rm -rf /"})
    assert "boot" not in clean_facts({"boot": True})


def test_reading_os_l4t_jetpack_and_boot(tmp_path):
    (tmp_path / "etc").mkdir()
    (tmp_path / "etc" / "os-release").write_text('NAME="Ubuntu"\nPRETTY_NAME="Ubuntu 22.04.5 LTS"\n')
    (tmp_path / "etc" / "nv_tegra_release").write_text(
        "# R36 (release), REVISION: 4.3, GCID: 38968081, BOARD: generic, EABI: aarch64\n")
    (tmp_path / "var" / "lib" / "dpkg").mkdir(parents=True)
    (tmp_path / "var" / "lib" / "dpkg" / "status").write_text(
        "Package: zlib1g\nVersion: 1:1.2.11\n\nPackage: nvidia-jetpack\nStatus: install ok\n"
        "Architecture: arm64\nVersion: 6.2+b77\n\n")
    (tmp_path / "proc").mkdir()
    (tmp_path / "proc" / "stat").write_text("cpu  1 2 3\nbtime 1795420989\nprocesses 9\n")
    root = str(tmp_path)
    assert facts_mod.os_release(root) == "Ubuntu 22.04.5 LTS"
    assert facts_mod.l4t(root) == "R36.4.3"
    assert facts_mod.jetpack(root) == "6.2"
    assert facts_mod.boot_time(root) == 1795420989
    empty = str(tmp_path / "none")
    assert (facts_mod.l4t(empty), facts_mod.jetpack(empty), facts_mod.boot_time(empty)) == \
        ("", "", None)


def test_bridge_sends_machine_facts_only_when_the_hub_asks(monkeypatch, tmp_path):
    monkeypatch.setenv("KAPTYORKA_HOME", str(tmp_path))
    asked = []
    monkeypatch.setattr(facts_mod, "infra",
                        lambda hub_url, with_ip: asked.append(with_ip) or {"kernel": "k"})
    b = Bridge({"hub": "http://h", "token": "t", "machine": "m"}, [], lambda *_: None)
    b._build_facts([])
    assert "kernel" not in b._facts and b._facts["seq"] == 1 and asked == []
    b._infra_scope = "team"
    b._build_facts([])
    assert b._facts["kernel"] == "k" and b._facts["seq"] == 2 and asked == [True]
    b._infra_scope = "hub"
    b._build_facts([])
    assert asked == [True, False]  # the hub's own computer: no address


def test_a_hub_that_was_down_does_not_call_every_machine_offline(hub, clock):
    d = team(hub, "jet-1")
    beat(hub, d)
    clock[0] += 3600  # the hub was down for an hour; it starts again
    m = me.MachineEvents(hub.store, lambda: HOST)
    assert m.tick(clock[0]) == []
    clock[0] += me.OFFLINE_S + me.GROUP_S
    assert len(m.tick(clock[0])) == 1  # still silent after the hub is back: offline


# ---------- free disk space ----------

def disk(free, total=100.0, mount="/data"):
    return {"use": {"cpus": 4, "mem_mb": 1024, "users": {},
                    "disk": [{"mount": "/", "total_gb": 100.0, "free_gb": 50.0},
                             {"mount": mount, "total_gb": total, "free_gb": free}]}}


def test_a_team_machine_disk_under_ten_percent_once_per_crossing(hub, clock):
    mgr, who = online(hub)
    d = team(hub, "jet-1")
    beat(hub, d, **disk(40))
    events(mgr, who)

    def run3(free):
        out = []
        for _ in range(3):
            clock[0] += 60
            beat(hub, d, **disk(free))
            out += events(mgr, who)
        return out
    assert run3(12) == []  # between the marks: nothing
    clock[0] += 60
    beat(hub, d, **disk(8))
    clock[0] += 60
    beat(hub, d, **disk(8))
    clock[0] += 60
    beat(hub, d, **disk(11))  # back between the marks before the third report: nothing
    assert events(mgr, who) == []
    assert run3(8) == ["jet-1: disk /data under 10% free (8 of 100 GB)"]
    assert run3(5) == [] and run3(12) == []  # still low, then between: no second warning
    assert run3(9) == []
    assert run3(20) == []  # recovered above 15%: re-armed, and no news
    assert run3(7) == ["jet-1: disk /data under 10% free (7 of 100 GB)"]
    assert any("disk /data under 10% free" in x for x in event_lines(hub))


def test_a_disk_first_seen_nearly_full_is_warned_of(hub, clock):
    mgr, who = online(hub)
    d = team(hub, "jet-1")
    beat(hub, d, **disk(3))
    for _ in range(2):
        clock[0] += 60
        beat(hub, d, **disk(3))
    assert events(mgr, who) == ["jet-1: new machine joined",
                                "jet-1: disk /data under 10% free (3 of 100 GB)"]


def test_the_hub_computer_makes_no_disk_event(hub, clock):
    mgr, who = online(hub)
    pid, _ = person(hub.store, "root", "admin")
    token, _ = hub.store.invite_redeem(hub.store.invite_create(pid), HOST, host=HOST,
                                       os_user="root")
    d = SimpleNamespace(token=token, host=HOST, os_user="root", seq=0)
    for _ in range(4):
        clock[0] += 60
        beat(hub, d, **disk(1))
    assert events(mgr, who) == []


def test_machines_tool_shows_the_disk_line_to_the_manager_only(hub, clock):
    from helpers import call_error
    mgr, who = online(hub)
    d = team(hub, "jet-1")
    beat(hub, d, **disk(1228.8, 3686.4, "/workspace"))
    [m] = [m for m in run(call(mgr, who, "machines")) if m["host"] == "jet-1"]
    assert m["disk"] == "disk: / 50 of 100 GB free, /workspace 1.2 of 3.6 TB free"
    assert m["use"].endswith("disk 50 GB free") and m["online"] and m["team"]
    _, token = person(hub.store, "bob")
    err = call_error(SimpleNamespace(url=hub.url, token=token),
                     {"key": "k:bob", "machine": "laptop", "tool": "codex", "project": "p"},
                     "machines")
    assert "for the admin and the manager" in err


def test_clean_disk_keeps_only_sound_entries():
    from kaptyorka.workers import clean_disk
    assert clean_disk([{"mount": "/", "total_gb": 100, "free_gb": 20},
                       {"mount": "home", "total_gb": 50, "free_gb": 5},
                       {"mount": "rel/path", "total_gb": 1, "free_gb": 1},
                       {"mount": "/x", "total_gb": 10, "free_gb": 11},
                       {"mount": "/y", "total_gb": "10", "free_gb": 1}] + [
                       {"mount": f"/m{i}", "total_gb": 1, "free_gb": 1} for i in range(10)]) == [
        {"mount": "/", "total_gb": 100.0, "free_gb": 20.0},
        {"mount": "home", "total_gb": 50.0, "free_gb": 5.0},
        {"mount": "/m0", "total_gb": 1.0, "free_gb": 1.0},
        {"mount": "/m1", "total_gb": 1.0, "free_gb": 1.0},
        {"mount": "/m2", "total_gb": 1.0, "free_gb": 1.0}]
    assert clean_disk("no") is None


def test_disks_real_filesystems_only_and_paths_only_when_allowed(tmp_path, monkeypatch):
    mounts = tmp_path / "mounts"
    mounts.write_text("\n".join([
        "/dev/sda2 / ext4 rw 0 0", "proc /proc proc rw 0 0", "tmpfs /run tmpfs rw 0 0",
        "/dev/loop3 /snap/core/1 squashfs ro 0 0", "overlay /var/lib/docker/x overlay rw 0 0",
        "/dev/sda2 /var/snap/firefox/common ext4 ro 0 0", "/dev/sdb1 /workspace ext4 rw 0 0",
        "/dev/sdc1 /home ext4 rw 0 0", "/dev/sdd1 /data\\040big xfs rw 0 0",
        "/dev/sda1 /boot/efi vfat rw 0 0", "server:/x /mnt/nfs nfs4 rw 0 0"]) + "\n")
    sizes = {"/": (468.0, 212.0, 1), "/workspace": (3686.4, 1228.8, 2), "/home": (900.0, 100.0, 3),
             "/data big": (50.0, 5.0, 4), "/mnt/nfs": (2000.0, 1000.0, 5),
             "/var/snap/firefox/common": (468.0, 212.0, 1), "/home/u": (900.0, 100.0, 3)}
    monkeypatch.setattr(facts_mod, "_space", lambda p: sizes.get(p))
    got = facts_mod.disks(True, str(mounts), home="/home/u")
    assert [d["mount"] for d in got] == ["/", "/home", "/workspace", "/mnt/nfs", "/data big"]
    assert got[0] == {"mount": "/", "total_gb": 468.0, "free_gb": 212.0}
    personal = facts_mod.disks(False, str(mounts), home="/home/u")
    assert personal == [{"mount": "/", "total_gb": 468.0, "free_gb": 212.0},
                        {"mount": "home", "total_gb": 900.0, "free_gb": 100.0}]
    monkeypatch.setattr(facts_mod, "MAX_DISKS", 2)
    assert len(facts_mod.disks(True, str(mounts), home="/home/u")) == 2


def test_bridge_sends_disk_paths_only_for_team_and_hub(monkeypatch, tmp_path):
    monkeypatch.setenv("KAPTYORKA_HOME", str(tmp_path))
    seen = []
    monkeypatch.setattr(facts_mod, "disks", lambda full: seen.append(full) or [])
    monkeypatch.setattr(facts_mod, "infra", lambda hub_url, with_ip: {})
    b = Bridge({"hub": "http://h", "token": "t", "machine": "m"}, [], lambda *_: None)
    b._build_facts([])
    b._infra_scope = "team"
    b._build_facts([])
    b._infra_scope = "hub"
    b._build_facts([])
    assert seen == [False, True, True] and b._facts["use"]["disk"] == []
