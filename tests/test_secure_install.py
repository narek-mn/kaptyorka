"""Getting the personal-mode installer and what it installs (secure design v2, 9.2, 9.4): the one
installer file (src/kaptyorka/secure/kap_installer.py) is built byte for byte the same from the
same files; it runs kap_setup from itself and keeps a copy for rejoin and repair; the box's AI
tools come from each vendor's installer in a locked unit (kap_tools.py), never from the
person's home; rejoin and repair use only what this computer has."""
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import zipfile

import pytest

from kaptyorka.secure import kap_installer as ki
from kaptyorka.secure import kap_setup as setup
from kaptyorka.secure import kap_tools as kt

PKG = ki.package_dir()
FP = "ab" * 32


# ---------------------------------------------------------------------------- the installer file

def test_the_installer_file_is_the_same_bytes_every_time_and_from_a_copy_of_the_files(tmp_path):
    a = ki.build()
    assert a == ki.build() and a.startswith(b"#!/usr/bin/python3 -I\n")
    copy = tmp_path / "kaptyorka"   # as a wheel installs them: the same files elsewhere, other times
    shutil.copytree(PKG, copy, ignore=shutil.ignore_patterns("__pycache__"))
    for dirpath, _, files in os.walk(copy):
        for n in files:
            os.utime(os.path.join(dirpath, n), (1, 1))
    os.makedirs(copy / "secure" / "__pycache__", exist_ok=True)
    (copy / "secure" / "__pycache__" / "x.pyc").write_bytes(b"junk")
    assert ki.build(str(copy)) == a
    assert ki.sha256(str(copy)) == hashlib.sha256(a).hexdigest()


def test_the_installer_holds_kap_setup_and_every_file_it_installs():
    z = zipfile.ZipFile(io.BytesIO(ki.build()))
    names = z.namelist()
    assert names[0] == "__main__.py" and names[1:] == sorted(names[1:])
    for need in ("kaptyorka/__init__.py", "kaptyorka/secure/kap_setup.py", "kaptyorka/secure/kap_tools.py",
                 "kaptyorka/secure/kap_ttyfilter.py", "kaptyorka/secure/kap_installer.py",
                 "kaptyorka/secure/files/units/kapbox-base@.service", "kaptyorka/secure/files/box/claude/settings.json",
                 "kaptyorka/relay/relay.py", "kaptyorka/relay/units/kaptyorka-relay.service"):
        assert need in names
    for src, name, _ in setup.PROGRAMS:   # every program kap_setup copies is inside
        assert f"kaptyorka/secure/{src}" in names
    assert not [n for n in names if "__pycache__" in n or n.endswith(".pyc")]
    assert not [n for n in names if n.startswith("kaptyorka/") and not n.startswith(
        ("kaptyorka/__init__.py", "kaptyorka/secure/", "kaptyorka/relay/"))]
    for info in z.infolist():
        assert info.date_time == ki.STAMP and info.compress_type == zipfile.ZIP_STORED
        assert info.external_attr == 0o100644 << 16


def test_the_installer_runs_from_its_one_file_with_python_isolated(tmp_path):
    f = tmp_path / "kaptyorka-secure-setup.pyz"
    f.write_bytes(ki.build())
    f.chmod(0o755)
    for cmd in ([sys.executable, "-I", str(f), "install"], [str(f), "remove"]):
        r = subprocess.run(cmd, capture_output=True, text=True, cwd=tmp_path, timeout=30,
                           env={"PATH": "/usr/bin:/bin"})
        if os.geteuid() != 0:
            assert r.returncode == 2 and "run it with sudo" in r.stderr, r.stderr
    assert not [p for p in os.listdir("/tmp") if p.startswith("kaptyorka-setup-") and
                os.stat(os.path.join("/tmp", p)).st_uid == os.getuid() and
                os.stat(os.path.join("/tmp", p)).st_mtime > f.stat().st_mtime]   # its folder is gone again


def test_a_damaged_installer_file_stops_before_running_anything(tmp_path):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("__main__.py", ki.MAIN)
        z.writestr("../evil.py", "print('ran')")
    f = tmp_path / "bad.pyz"
    f.write_bytes(buf.getvalue())
    r = subprocess.run([sys.executable, "-I", str(f), "install"], capture_output=True, text=True, timeout=30)
    assert "damaged" in r.stderr and "ran" not in r.stdout


def test_the_hub_s_sentence_names_only_the_release_page_never_a_command():
    """Finding 5a (design 9.2): a hostile hub could print any command, so the hub prints none:
    one fixed sentence that sends the person to the release page of its version."""
    got = ki.install_sentence("0.9.40")
    assert got == ("Install with the steps on the kaptyorka release page https://github.com/narek-mn/kaptyorka/"
                   "releases/tag/v0.9.40: they download the installer from this hub with your link, check its "
                   "signature against the release key printed on that page, and only then run it.")
    for word in ("sudo", "curl", "python3", "sha256sum", "ssh-keygen", "ssh-ed25519", "&&", "|", "'"):
        assert word not in got
    for bad in ("", "x; sudo rm -rf /", "1.0 && curl evil", "../../evil", None):
        assert ki.release_page(bad) == "https://github.com/narek-mn/kaptyorka/releases"
        assert "evil" not in ki.install_sentence(bad) and "sudo" not in ki.install_sentence(bad)
    from kaptyorka import __version__
    assert ki.version() == __version__


def test_the_release_workflow_builds_the_installer_and_attaches_it():
    wf = open(os.path.join(os.path.dirname(os.path.dirname(PKG)), ".github", "workflows", "publish.yml")).read()
    assert "kap_installer.py build" in wf and "kaptyorka-secure-setup.pyz" in wf
    assert "installer/kaptyorka-secure-setup.pyz" in wf.split("gh release create", 1)[1]
    # never on PyPI: the PyPI step uploads dist/ only, and the installer is built outside it
    assert "dist/kaptyorka-secure-setup" not in wf


def test_install_keeps_the_installer_file_itself_or_builds_the_same(monkeypatch, tmp_path):
    wrote = {}
    monkeypatch.setattr(setup.kr, "write_file", lambda p, d, mode=0o644, **k: wrote.__setitem__(p, (d, mode)))
    monkeypatch.setattr(setup.kc, "LIBEXEC", str(tmp_path))
    monkeypatch.setattr(setup.kc, "INSTALLER", str(tmp_path / "installer"))
    monkeypatch.setattr(setup, "INSTALLER_FILE", "")
    setup.install_installer()
    assert wrote[str(tmp_path / "installer")] == (ki.build(), 0o755)
    assert os.readlink(tmp_path / "kaptyorka-secure-setup") == "installer"
    f = tmp_path / "given.pyz"
    f.write_bytes(b"#!/usr/bin/python3 -I\nTHE-FILE")
    monkeypatch.setattr(setup, "INSTALLER_FILE", str(f))
    setup.install_installer()
    assert wrote[str(tmp_path / "installer")][0] == b"#!/usr/bin/python3 -I\nTHE-FILE"


# ---------------------------------------------------------------------------- rejoin, repair, update-tools

@pytest.fixture
def etc(monkeypatch, tmp_path):
    e = tmp_path / "etc"
    (e / "secret").mkdir(parents=True)
    (e / "people" / "alice").mkdir(parents=True)
    (e / "mode").write_text("personal\n")
    (e / "relay.conf").write_text(f"hub_ip=192.0.2.10\nhub_port=8444\nhub_sha256={FP}\nname=laptop-2\nversion=1\n")
    (e / "secret" / "device-token").write_text("D" * 43 + "\n")
    monkeypatch.setattr(setup.kc, "MODE_FILE", str(e / "mode"))
    monkeypatch.setattr(setup.kc, "ETC", str(e))
    monkeypatch.setattr(setup.kc, "SECRET", str(e / "secret"))
    monkeypatch.setattr(setup.kc, "PEOPLE", str(e / "people"))
    return e


def test_the_installed_values_are_checked_again(etc):
    assert setup.installed_values() == {"ip": "192.0.2.10", "port": 8444, "fp": FP, "name": "laptop-2",
                                        "token": "D" * 43, "claude": ""}
    (etc / "relay.conf").write_text("hub_ip=192.0.2.10\nIPAddressAllow=any\nhub_port=8444\nhub_sha256=x\nname=a\n")
    with pytest.raises(setup.Refuse, match="damaged: run  rejoin"):
        setup.installed_values()
    (etc / "mode").write_text("")
    with pytest.raises(setup.Refuse, match="not installed"):
        setup.installed_values()


def test_rejoin_takes_a_new_link_and_changes_only_the_hubs_values(etc, monkeypatch):
    calls = []
    monkeypatch.setattr(setup, "redeem", lambda inv, name: (calls.append(("redeem", inv["host"], name)), {
        "ip": "198.51.100.7", "port": inv["port"], "fp": inv["fp"], "token": "N" * 43})[1])
    monkeypatch.setattr(setup, "install_relay_conf", lambda v: calls.append(("conf", v)))
    monkeypatch.setattr(setup.kr, "systemctl", lambda *a, **k: calls.append(("systemctl",) + a))
    for bad in ("http://hub/i/x", "https://hub/i/x", f"https://hub/i/x\"#sha256={FP}"):
        with pytest.raises(setup.Refuse):
            setup.rejoin({"link": bad})
    assert calls == []
    assert setup.rejoin({"link": f"https://new-hub:9000/i/abc#sha256={'cd' * 32}"}) == 0
    assert calls[0] == ("redeem", "new-hub", "laptop-2")
    assert calls[1] == ("conf", {"name": "laptop-2", "claude": "", "ip": "198.51.100.7", "port": 9000,
                                 "fp": "cd" * 32, "token": "N" * 43})
    assert calls[2] == ("systemctl", "try-restart", "kaptyorka-relay.service")


def test_repair_writes_everything_again_with_the_values_it_has(etc, monkeypatch):
    got = []
    monkeypatch.setattr(setup, "apply", lambda person, v, rt, a, specs: got.append((person, v["ip"], v["token"], rt, specs)) or 0)
    assert setup.repair({}) == 0
    assert got == [("alice", "192.0.2.10", "D" * 43, "", {})]


def test_the_installer_command_line_has_rejoin_repair_and_update_tools(monkeypatch):
    got = []
    monkeypatch.setattr(setup.os, "geteuid", lambda: 0)
    monkeypatch.setattr(setup, "rejoin", lambda a: got.append(("rejoin", a["link"])) or 0)
    monkeypatch.setattr(setup, "repair", lambda a: got.append(("repair",)) or 0)
    monkeypatch.setattr(setup, "update_tools", lambda a, pos: got.append(("update-tools", pos)) or 0)
    assert setup.main(["rejoin", "L"]) == 0 and setup.main(["repair"]) == 0
    assert setup.main(["update-tools", "codex"]) == 0
    assert setup.main(["rejoin"]) == 2 and setup.main(["repair", "x"]) == 2
    assert got == [("rejoin", "L"), ("repair",), ("update-tools", ["codex"])]


def test_hubchat_update_tools_lets_only_tool_names_and_versions_through(monkeypatch, capsys):
    from kaptyorka.secure import kap_hubchat as hc
    got = []
    monkeypatch.setattr(hc.kr, "caller_person", lambda: "alice")
    monkeypatch.setattr(hc.kr, "old_login_hint", lambda p: [])
    monkeypatch.setattr(hc.kap_tools, "install", lambda specs, say: (got.append(specs), ({}, {}))[1])
    monkeypatch.setattr(hc.kap_tools, "installed", lambda: {"claude": "2.1.3"})
    assert hc.main(["update-tools", "codex=0.172.0", "opencode=2"]) == 0
    assert hc.main(["update-tools"]) == 0
    for bad in (["update-tools", "codex;id"], ["update-tools", "../../bin/sh"], ["update-tools", "claude=latest"],
                ["update-tools", "--force"], ["update-tools", "codex=1.2.3$(id)"]):
        assert hc.main(bad) == 2
    assert got == [{"codex": "0.172.0", "opencode": "2"}, {"claude": ""}]


# ---------------------------------------------------------------------------- the tools

@pytest.mark.parametrize("text, want", [
    ("claude,codex", {"claude": "", "codex": ""}),
    ("claude codex=0.172.0 cursor-agent", {"claude": "", "codex": "0.172.0", "cursor-agent": ""}),
    ("opencode=2", {"opencode": "2"}), ("opencode=1.19.6", {"opencode": "1.19.6"}),
    ("claude=2.1.300-beta.1", {"claude": "2.1.300-beta.1"}), ("none", {}), ("", {}),
])
def test_tool_choices(text, want):
    assert kt.parse_specs(text) == want


@pytest.mark.parametrize("bad", ["git", "claude=2", "claude=latest", "codex=1.2.3;id", "claude\nIPAddressAllow=any",
                                 "cursor-agent=$(id)", "opencode=2.x", "../claude"])
def test_bad_tool_choices_are_refused(bad):
    with pytest.raises(kt.Refuse):
        kt.parse_specs(bad)


def test_the_persons_tools_are_found_by_name_only(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".local" / "bin").mkdir(parents=True)
    (home / ".opencode" / "bin").mkdir(parents=True)
    os.symlink("/nonexistent/claude-binary", home / ".local" / "bin" / "claude")   # a dangling link still counts
    (home / ".opencode" / "bin" / "opencode").write_text("#!/bin/sh\necho ran > " + str(tmp_path / "RAN") + "\n")
    (home / ".opencode" / "bin" / "opencode").chmod(0o755)
    monkeypatch.setattr(kt, "SYSTEM_BIN", (str(tmp_path / "sys"),))
    assert kt.detect(str(home)) == ["claude", "opencode"]
    assert not (tmp_path / "RAN").exists()


def test_opencode_2_has_its_own_installer():
    assert kt.vendor_of("opencode", "") == ("opencode", "")
    assert kt.vendor_of("opencode", "1.19.6") == ("opencode", "1.19.6")
    assert kt.vendor_of("opencode", "2") == ("opencode2", "")
    assert kt.vendor_of("opencode", "2.4.2") == ("opencode2", "2.4.2")


def test_the_vendor_installer_unit_is_locked(tmp_path):
    addrs = {"claude.ai": ["160.79.104.10"], "downloads.claude.ai": ["34.36.57.103", "2600:1901::1"]}
    cmd = kt.unit_cmd("kaptyorka-tool-install-claude-x", "claude", "claude", "2.1.3", "/var/lib/kaptyorka-tool-stage-claude",
                      str(tmp_path / "hosts"), addrs)
    props = [cmd[i + 1] for i, a in enumerate(cmd) if a == "-p"]
    for want in ("DynamicUser=yes", "ProtectHome=yes", "ProtectSystem=strict", "NoNewPrivileges=yes", "CapabilityBoundingSet=",
                 "IPAddressDeny=any", "IPAddressAllow=160.79.104.10 2600:1901::1 34.36.57.103",
                 f"BindReadOnlyPaths={tmp_path / 'hosts'}:/etc/hosts", "RestrictAddressFamilies=AF_INET AF_INET6",
                 "KillMode=control-group", f"RuntimeMaxSec={kt.UNIT_LIMIT}", "Environment=KAP_VERSION=2.1.3",
                 "Environment=KAP_URL=https://claude.ai/install.sh", "Environment=HOME=/var/lib/kaptyorka-tool-stage-claude"):
        assert want in props, want
    assert any(p.startswith("InaccessiblePaths=") and "/etc/kaptyorka" in p and "/var/lib/kap-team" in p for p in props)
    assert cmd[-4:] == ["--", "/bin/bash", "-c", kt.SCRIPT]
    assert "$KAP" in kt.SCRIPT and "2.1.3" not in kt.SCRIPT     # values come in as variables, never as text
    assert kt.hosts_text(addrs).splitlines()[2:] == ["160.79.104.10 claude.ai", "34.36.57.103 downloads.claude.ai",
                                                    "2600:1901::1 downloads.claude.ai"]


def _stage(tmp_path, key):
    stage = tmp_path / "stage"
    if key == "claude":
        (stage / ".local" / "share" / "claude" / "versions").mkdir(parents=True)
        b = stage / ".local" / "share" / "claude" / "versions" / "2.1.400"
        b.write_bytes(b"\x7fELF claude")
        b.chmod(0o4755)
        (stage / ".local" / "bin").mkdir(parents=True)
        os.symlink(str(b), stage / ".local" / "bin" / "claude")
        (stage / ".kap-version").write_text("2.1.400 (Claude Code)\n")
    else:
        v = stage / ".local" / "share" / "cursor-agent" / "versions" / "2026.11.22-e9042b2"
        (v / "lib").mkdir(parents=True)
        (v / "cursor-agent").write_text("#!/bin/sh\n")
        (v / "cursor-agent").chmod(0o755)
        (v / "lib" / "index.js").write_text("x")
        os.symlink("lib/index.js", v / "main.js")
        (stage / ".local" / "bin").mkdir(parents=True)
        os.symlink(str(v / "cursor-agent"), stage / ".local" / "bin" / "cursor-agent")
        (stage / ".kap-version").write_text("2026.11.22-e9042b2\n")
    return stage


def test_root_takes_only_the_program_and_its_folder_from_inside_the_stage(tmp_path):
    stage = _stage(tmp_path, "claude")
    src, inner, ver = kt.collect(str(stage), "claude")
    assert (src, inner, ver) == (str(stage / ".local/share/claude/versions/2.1.400"), "2.1.400", "2.1.400")
    stage2 = _stage(tmp_path / "c", "cursor-agent")
    src, inner, ver = kt.collect(str(stage2), "cursor-agent")
    assert inner == "cursor-agent" and ver == "2026.11.22-e9042b2" and src.endswith("versions/2026.11.22-e9042b2")
    # a program that points out of the stage (at the person's own copy, say) is refused
    os.unlink(stage / ".local" / "bin" / "claude")
    os.symlink("/usr/bin/true", stage / ".local" / "bin" / "claude")
    with pytest.raises(kt.Refuse, match="no program"):
        kt.collect(str(stage), "claude")


def test_the_copy_is_read_only_for_others_and_keeps_only_links_inside(tmp_path, monkeypatch):
    monkeypatch.setattr(kt.kr, "mkdir", lambda p, mode, **k: os.makedirs(p, mode, exist_ok=True))
    stage = _stage(tmp_path, "cursor-agent")
    src, inner, ver = kt.collect(str(stage), "cursor-agent")
    dst = tmp_path / "opt"
    kt.copy_tree(src, str(dst))
    assert (dst / "cursor-agent").stat().st_mode & 0o7777 == 0o755
    assert os.readlink(dst / "main.js") == "lib/index.js" and (dst / "lib" / "index.js").read_text() == "x"
    os.symlink("/home/alice/.ssh/id_ed25519", os.path.join(src, "key"))
    with pytest.raises(kt.Refuse, match="outside its folder"):
        kt.copy_tree(src, str(tmp_path / "opt2"))
    single = tmp_path / "one"
    single.mkdir()
    s = _stage(tmp_path / "cl", "claude")
    kt.copy_tree(kt.collect(str(s), "claude")[0], str(single))
    assert (single / "2.1.400").stat().st_mode & 0o7777 == 0o755    # the set-id bit is gone



def test_a_file_that_is_not_the_installers_own_is_refused_and_no_half_copy_stays(tmp_path, monkeypatch):
    f = tmp_path / "f"
    f.write_text("x")
    with pytest.raises(kt.Refuse, match="not its own"):
        kt._copy_file(str(f), str(tmp_path / "g"), os.getuid() + 1)    # a hard link to someone else's file
    os.mkfifo(tmp_path / "fifo")
    with pytest.raises(kt.Refuse, match="not its own"):
        kt._copy_file(str(tmp_path / "fifo"), str(tmp_path / "h"), os.getuid())   # swapped in: no hang
    assert not (tmp_path / "g").exists() and not (tmp_path / "h").exists()


def test_install_one_records_the_version_and_points_bin_at_it(tmp_path, monkeypatch):
    top = tmp_path / "tools"
    (top / "bin").mkdir(parents=True)
    monkeypatch.setattr(kt.kc, "TOOLS_DIR", str(top))
    monkeypatch.setattr(kt, "RECORD", str(tmp_path / "tools.json"))
    monkeypatch.setattr(kt, "STAGE_TOP", str(tmp_path / "varlib"))
    monkeypatch.setattr(kt, "RUNDIR", str(tmp_path / "run"))
    monkeypatch.setattr(kt.kr, "mkdir", lambda p, mode, **k: os.makedirs(p, mode, exist_ok=True))
    monkeypatch.setattr(kt.kr, "write_file", lambda p, d, mode=0o644, **k: open(p, "w").write(d))
    monkeypatch.setattr(kt, "addresses", lambda hosts: {h: ["192.0.2.1"] for h in hosts})
    monkeypatch.setattr(kt, "unreachable", lambda specs: "")
    ran = []

    def unit(cmd, **kw):   # the unit: the vendor's installer leaves its program in the stage
        ran.append(cmd)
        made = _stage(tmp_path / "made", "claude")
        shutil.copytree(made, tmp_path / "varlib" / "private" / "kaptyorka-tool-stage-claude", symlinks=True)
        st = tmp_path / "varlib" / "private" / "kaptyorka-tool-stage-claude"
        os.unlink(st / ".local" / "bin" / "claude")
        os.symlink(str(st / ".local/share/claude/versions/2.1.400"), st / ".local" / "bin" / "claude")
        return subprocess.CompletedProcess(cmd, 0, "", "")
    monkeypatch.setattr(kt.kr, "run", unit)
    assert kt.install_one("claude", "", say=lambda *a: None) == "2.1.400"
    assert os.readlink(top / "bin" / "claude") == "../claude/2.1.400/2.1.400"
    assert (top / "claude" / "2.1.400" / "2.1.400").stat().st_mode & 0o777 == 0o711   # execute-only
    rec = json.loads((tmp_path / "tools.json").read_text())
    assert rec["claude"]["version"] == "2.1.400" and rec["claude"]["from"] == "https://claude.ai/install.sh"
    assert rec["claude"]["sha256"] == hashlib.sha256(b"\x7fELF claude").hexdigest()
    assert not (tmp_path / "varlib" / "private" / "kaptyorka-tool-stage-claude").exists()   # the stage is gone
    assert kt.installed() == {"claude": "2.1.400"}


def test_a_failed_vendor_installer_changes_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(kt.kc, "TOOLS_DIR", str(tmp_path / "tools"))
    monkeypatch.setattr(kt, "STAGE_TOP", str(tmp_path / "varlib"))
    monkeypatch.setattr(kt, "RUNDIR", str(tmp_path / "run"))
    monkeypatch.setattr(kt, "RECORD", str(tmp_path / "tools.json"))
    monkeypatch.setattr(kt.kr, "mkdir", lambda p, mode, **k: os.makedirs(p, mode, exist_ok=True))
    monkeypatch.setattr(kt.kr, "write_file", lambda p, d, mode=0o644, **k: open(p, "w").write(d))
    monkeypatch.setattr(kt, "addresses", lambda hosts: {h: ["192.0.2.1"] for h in hosts})
    monkeypatch.setattr(kt, "unreachable", lambda specs: "")
    monkeypatch.setattr(kt.kr, "run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, "", "curl: (7) Failed\n"))
    with pytest.raises(kt.Refuse, match="its installer failed"):
        kt.install_one("codex", "", say=lambda *a: None)
    assert not (tmp_path / "tools").exists() and not (tmp_path / "tools.json").exists()
