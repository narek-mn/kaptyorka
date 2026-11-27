"""The daily tool update (kap_tools.update / install_one(auto=True)): newer versions only, a new
tool must start in the box first, the switch waits for the chats of that tool to end, the version
before stays for a rollback. No real vendor, unit or box: the vendor's installer and the box check
are stand-ins."""
import json
import os
import shutil
import subprocess

import pytest

from kaptyorka.secure import kap_tools as kt


def make_stage(path, version):
    b = path / ".local" / "share" / "claude" / "versions" / version
    b.parent.mkdir(parents=True)
    b.write_bytes(b"\x7fELF claude " + version.encode())
    (path / ".local" / "bin").mkdir(parents=True)
    os.symlink(str(b), path / ".local" / "bin" / "claude")
    (path / ".kap-version").write_text(f"{version} (Claude Code)\n")


class Env:
    def __init__(self, tmp_path, monkeypatch):
        self.tmp, self.top = tmp_path, tmp_path / "tools"
        (self.top / "bin").mkdir(parents=True)
        for k, v in (("TOOLS_DIR", str(self.top)),):
            monkeypatch.setattr(kt.kc, k, v)
        monkeypatch.setattr(kt, "RECORD", str(tmp_path / "tools.json"))
        monkeypatch.setattr(kt, "LOCK", str(tmp_path / "lock"))
        monkeypatch.setattr(kt, "STAGE_TOP", str(tmp_path / "varlib"))
        monkeypatch.setattr(kt, "RUNDIR", str(tmp_path / "run"))
        monkeypatch.setattr(kt.kr, "mkdir", lambda p, mode, **k: os.makedirs(p, mode, exist_ok=True))
        monkeypatch.setattr(kt.kr, "write_file", lambda p, d, mode=0o644, **k: open(p, "w").write(d))
        monkeypatch.setattr(kt, "addresses", lambda hosts: {h: ["192.0.2.1"] for h in hosts})
        monkeypatch.setattr(kt, "unreachable", lambda specs: "")
        self.vendor = "2.1.400"
        self.problem = ""
        self.chats = 0
        self.verified = []
        monkeypatch.setattr(kt.kr, "run", self.unit)
        monkeypatch.setattr(kt, "verify_in_box", self.verify)
        monkeypatch.setattr(kt, "running_chats", lambda tool: self.chats)

    def unit(self, cmd, **kw):
        st = self.tmp / "varlib" / "private" / "kaptyorka-tool-stage-claude"
        make_stage(st, self.vendor)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    def verify(self, prog, want):
        self.verified.append((prog, want))
        assert os.path.isfile(prog)
        return self.problem

    def say(self, *a):
        pass

    def record(self):
        return json.loads((self.tmp / "tools.json").read_text())

    def link(self):
        return os.readlink(self.top / "bin" / "claude")


@pytest.fixture
def env(tmp_path, monkeypatch):
    e = Env(tmp_path, monkeypatch)
    e.vendor = "2.1.399"
    assert kt.install_one("claude", "", say=e.say) == "2.1.399"    # the first install, as before
    e.vendor = "2.1.400"
    return e


def test_versions_are_compared_by_their_numbers_never_a_suffix():
    assert kt.is_newer("2.1.400", "2.1.399") and kt.is_newer("2.2", "2.1.9") and kt.is_newer("1.0.0.1", "1.0.0")
    assert not kt.is_newer("2.1.399", "2.1.399") and not kt.is_newer("2.1.9", "2.1.10")
    assert kt.is_newer("2026.11.23-aaa", "2026.11.22-zzz") and not kt.is_newer("2026.11.22-zzz", "2026.11.22-aaa")
    assert not kt.is_newer("garbage", "1.0.0") and kt.is_newer("1.0.0", "")


def test_a_newer_tool_is_checked_in_the_box_then_switched_and_the_old_one_stays(env):
    done, failed = kt.update({"claude": ""}, env.say)
    assert (done, failed) == ({"claude": "2.1.400"}, {})
    assert env.verified == [(f"{env.top}/claude/2.1.400/2.1.400", "2.1.400")]
    assert env.link() == "../claude/2.1.400/2.1.400"
    rec = env.record()["claude"]
    assert rec["version"] == "2.1.400" and rec["previous"] == "2.1.399" and "pending" not in rec
    assert (env.top / "claude" / "2.1.399" / "2.1.399").is_file()
    assert (env.top / "claude" / "2.1.400" / "2.1.400").stat().st_mode & 0o777 == 0o711


@pytest.mark.parametrize("vendor", ["2.1.399", "2.1.300"])
def test_the_same_or_an_older_version_changes_nothing(env, vendor):
    env.vendor = vendor
    before = env.record()
    done, failed = kt.update({"claude": ""}, env.say)
    assert (done, failed) == ({"claude": "2.1.399"}, {})
    assert env.record() == before and env.link() == "../claude/2.1.399/2.1.399" and env.verified == []
    assert sorted(os.listdir(env.top / "claude")) == ["2.1.399"]


def test_a_tool_that_does_not_start_in_the_box_is_never_switched_to(env):
    env.problem = "it did not start in the box: boom"
    done, failed = kt.update({"claude": ""}, env.say)
    assert done == {} and "not switched to" in failed["claude"] and "the old one stays" in failed["claude"]
    assert env.link() == "../claude/2.1.399/2.1.399" and env.record()["claude"]["version"] == "2.1.399"
    assert sorted(os.listdir(env.top / "claude")) == ["2.1.399"]      # the bad copy is gone


def test_a_running_chat_holds_the_switch_until_it_ends_and_nothing_is_downloaded_again(env, monkeypatch):
    env.chats = 1
    done, failed = kt.update({"claude": ""}, env.say)
    assert failed == {} and done == {"claude": "2.1.399"} and env.link() == "../claude/2.1.399/2.1.399"
    assert env.record()["claude"]["pending"]["version"] == "2.1.400"
    assert (env.top / "claude" / "2.1.400" / "2.1.400").is_file()
    ran = []
    monkeypatch.setattr(kt.kr, "run", lambda *a, **k: ran.append(1))      # the vendor must not be asked again
    kt.update({"claude": ""}, env.say)                                     # chat still running
    assert env.link() == "../claude/2.1.399/2.1.399" and ran == []
    env.chats = 0
    done, failed = kt.update({"claude": ""}, env.say)
    assert done == {"claude": "2.1.400"} and failed == {} and ran == []
    assert env.link() == "../claude/2.1.400/2.1.400" and "pending" not in env.record()["claude"]
    assert env.record()["claude"]["previous"] == "2.1.399"


def test_a_pending_copy_that_changed_on_disk_is_dropped_not_switched_to(env):
    env.chats = 1
    kt.update({"claude": ""}, env.say)
    p = env.top / "claude" / "2.1.400" / "2.1.400"
    p.chmod(0o644)
    p.write_bytes(b"tampered")
    env.chats = 0
    kt.finish_pending("claude", env.say)
    assert env.link() == "../claude/2.1.399/2.1.399" and "pending" not in env.record()["claude"]


def test_a_version_named_by_hand_installs_as_asked_and_a_new_tool_installs_plainly(env):
    env.vendor = "2.0.1"
    done, failed = kt.update({"claude": "2.0.1"}, env.say)
    assert done == {"claude": "2.0.1"} and env.verified == [] and env.link() == "../claude/2.0.1/2.0.1"


def test_rollback_points_back_at_the_kept_version(env):
    kt.update({"claude": ""}, env.say)
    assert kt.rollback("claude", env.say) == "2.1.399"
    assert env.link() == "../claude/2.1.399/2.1.399" and env.record()["claude"]["version"] == "2.1.399"
    env.chats = 1
    with pytest.raises(kt.Refuse, match="chat of it is running"):
        kt.rollback("claude", env.say)
    with pytest.raises(kt.Refuse, match="no earlier version"):
        kt.rollback("codex", env.say)


def test_auto_update_main_needs_root_and_a_tool(monkeypatch, tmp_path):
    monkeypatch.setattr(kt.os, "geteuid", lambda: 1000)
    assert kt.main(["auto-update"]) == 2
    monkeypatch.setattr(kt.os, "geteuid", lambda: 0)
    monkeypatch.setattr(kt.kc, "unfinished_install", lambda: "")
    monkeypatch.setattr(kt, "installed", lambda: {})
    assert kt.main(["auto-update"]) == 0
    assert kt.main(["bogus"]) == 2
    got = []
    monkeypatch.setattr(kt, "installed", lambda: {"claude": "1.0.0"})
    monkeypatch.setattr(kt, "update", lambda specs: (got.append(specs), ({}, {"claude": "x"}))[1])
    assert kt.main(["auto-update"]) == 1 and got == [{"claude": ""}]


def test_the_timer_units_ship_and_the_installer_removes_them():
    from kaptyorka.secure import kap_setup as setup
    d = os.path.join(setup.FILES, "units")
    assert "OnCalendar=daily" in open(f"{d}/kaptyorka-tools-update.timer").read()
    svc = open(f"{d}/kaptyorka-tools-update.service").read()
    assert "kap_tools.py auto-update" in svc and "Type=oneshot" in svc
    assert {"kaptyorka-tools-update.service", "kaptyorka-tools-update.timer"} <= set(setup.OUR_UNITS)
