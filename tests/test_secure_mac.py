"""The macOS box (TEST VERSION; src/kaptyorka/secure/kap_mac*.py): the parts that are pure text or
pure decisions, checked here on any system. The real walls are checked on a Mac by
tests/mac-box/run.sh in .github/workflows/mac-box.yml."""
import ast
import json
import os
import plistlib
import re
import subprocess
import sys

import pytest

from kaptyorka.relay import relay as R
from kaptyorka.secure import kap_mac as km
from kaptyorka.secure import kap_mac_setup as ms
from kaptyorka.secure import kap_mac_wallcheck as mw
from kaptyorka.secure import kap_tools as kt
from kaptyorka.secure import kapcommon as kc

SECURE = os.path.join(os.path.dirname(__file__), "..", "src", "kaptyorka", "secure")
ROOT = os.path.join(os.path.dirname(__file__), "..")


def profile(**kw):
    a = dict(box="alice", person="alice", proxy_port=47110, darwin_dir="/private/var/folders/ab/abcdefgh12345678",
             py_top="/Library/Frameworks/Python.framework/Versions/3.12")
    a.update(kw)
    return km.box_profile(**a)


def rules(text):
    return [ln for ln in text.splitlines() if ln.startswith("(")]


def balanced(text):
    depth = 0
    for ch in re.sub(r'"[^"]*"|#"[^"]*"', "", text):
        depth += {"(": 1, ")": -1}.get(ch, 0)
        assert depth >= 0
    return depth == 0


# ---------------------------------------------------------------------------- the box profile

def test_box_profile_denies_by_default_and_never_allows_everything():
    p = profile()
    assert balanced(p)
    r = rules(p)
    assert r[:3] == ["(version 1)", "(deny default)", '(import "system.sb")']
    assert "(allow default)" not in p and "(allow network*" not in p and "(allow file-write* (subpath \"/\"" not in p


def test_box_profile_network_is_the_forwarder_port_and_the_relay_door_only():
    out = [ln for ln in rules(profile()) if "network" in ln]
    assert out == [
        '(allow network-outbound (remote ip "localhost:47110"))',
        '(allow network-outbound (remote unix-socket (path-literal "/private/var/run/kaptyorka/relay/alice.sock")))',
        '(allow network-bind network-inbound (local unix-socket (subpath "/private/var/run/kapbox/alice")))',
        '(allow network-outbound (remote unix-socket (subpath "/private/var/run/kapbox/alice")))']


def test_box_profile_writes_only_the_box_places_and_this_person_s_team_area():
    rw = next(ln for ln in rules(profile()) if ln.startswith("(allow file-read* file-write*"))
    places = re.findall(r'\(subpath "([^"]+)"\)', rw)
    assert "/Users/Shared/kaptyorka/alice" in places
    for p in places:
        assert p.startswith(("/private/var/lib/kap-team/kap-alice/", "/private/var/run/kapbox/alice",
                             "/Users/Shared/kaptyorka/alice", "/private/var/folders/ab/")), p
    for bad in ("/private/tmp", "/private/var/tmp", "/Users\"", "/Users/alice", "/Users/Shared\""):
        assert bad not in rw


def test_box_profile_reads_no_home_no_tmp_and_not_kaptyorka_s_own_files():
    p = profile()
    reads = " ".join(ln for ln in rules(p) if ln.startswith("(allow file-read*"))
    for bad in ('"/Users"', '"/Users/alice"', '"/private/tmp"', '"/private/var/tmp"', '"/Users/Shared"', '"/opt"',
                '"/Applications"', '(literal "/Users")'):
        assert bad not in reads, bad
    r = rules(p)
    deny = next(i for i, ln in enumerate(r) if ln.startswith("(deny file-read*"))
    carve = next(i for i, ln in enumerate(r) if "/private/etc/kaptyorka/box\")" in ln)
    assert deny < carve, "the carve-out must come after the deny (the last matching rule wins)"
    for d in km.SYSTEM_DENY:
        assert f'(subpath "{d}")' in r[deny]
    assert '"/private/etc/kaptyorka/boxes/alice"' in r[carve] and "boxes\")" not in r[carve]


def test_box_profile_locks_the_tool_settings_after_allowing_their_folders():
    r = rules(profile())
    rw = next(i for i, ln in enumerate(r) if ln.startswith("(allow file-read* file-write*"))
    lock = next(i for i, ln in enumerate(r) if ln.startswith("(deny file-write*"))
    assert rw < lock
    for _, dst in kc.LOCKED_FILES:
        assert f'(literal "/private/var/lib/kap-team/kap-alice/home/{dst}")' in r[lock]


def test_box_profile_has_no_clipboard_screen_or_apple_events_and_only_same_sandbox_processes():
    p = profile()
    deny = next(ln for ln in rules(p) if ln.startswith("(deny mach-lookup"))
    for n in ("com.apple.pasteboard.1", "com.apple.windowserver.active", "com.apple.coreservices.appleevents"):
        assert f'"{n}"' in deny
        assert f'"{n}"' not in next(ln for ln in rules(p) if ln.startswith("(allow mach-lookup"))
    assert "(allow process-info* (target same-sandbox))" in p and "(allow signal (target same-sandbox))" in p
    assert "(allow process-info*)" not in p and "(allow signal)" not in p


def test_box_profile_refuses_paths_that_could_break_out_of_its_text():
    for bad in ('/private/var/folders/x" (allow default) "', "/x\n(allow default)", "relative", "/a/../b"):
        with pytest.raises(kc.Refuse):
            profile(darwin_dir=bad)
    with pytest.raises(kc.Refuse):
        profile(proxy_port=80)
    with pytest.raises(kc.Refuse):
        profile(box="Alice")


def test_box_profile_local_additions_come_last_and_are_marked():
    p = profile(extra='(allow file-read* (subpath "/private/tmp"))')
    assert p.rstrip().endswith('(allow file-read* (subpath "/private/tmp"))')
    assert km.PROFILE_EXTRA in p
    assert km.PROFILE_EXTRA not in profile()


def test_real_paths_are_the_private_ones():
    assert km.real("/etc/kaptyorka/box") == "/private/etc/kaptyorka/box"
    assert km.real("/var/run/kaptyorka") == "/private/var/run/kaptyorka"
    assert km.real("/tmp") == "/private/tmp"
    assert km.real("/Users/Shared/kaptyorka") == "/Users/Shared/kaptyorka"
    assert km.real("/etcetera") == "/etcetera"


def test_service_and_filter_profiles():
    s = km.service_profile(["/private/var/run/kaptyorka/relay-cred"])
    assert balanced(s) and "(deny file-write*)" in s
    for d in ("/Users", "/private/tmp", "/private/var/lib/kap-team", "/private/etc/kaptyorka/secret"):
        assert f'(subpath "{d}")' in next(ln for ln in s.splitlines() if ln.startswith("(deny file-read*"))
    f = km.filter_profile("/Library/Frameworks/Python.framework/Versions/3.12")
    assert balanced(f) and "(deny default)" in f and "network" not in f and "file-write" not in f


# ---------------------------------------------------------------------------- pf

def test_pf_rules_pass_each_user_s_one_way_before_its_block():
    t = km.pf_rules({450: 47110, 455: 47111}, 451, 452, 453, "192.0.2.10", 8444)
    lines = t.splitlines()

    def idx(s):
        return lines.index(s)
    for uid, port in ((450, 47110), (455, 47111)):
        assert idx(f"pass out quick on lo0 proto tcp from any to 127.0.0.1 port {port} user {uid}") < \
            idx(f"block drop out quick proto {{ tcp udp }} all user {uid}")
    assert idx("pass out quick proto tcp from any to 192.0.2.10 port 8444 user 451") < \
        idx("block drop out quick proto { tcp udp } all user 451")
    assert "pass out quick on lo0 proto tcp from 127.0.0.1 port { 47110 47111 } to any user 452" in lines
    priv = [i for i, ln in enumerate(lines) if "user 452" in ln and ln.startswith("block drop out quick inet")]
    assert priv and max(priv) < idx("pass out quick proto tcp from any to any port { 443 22 } user 452")
    assert all(not ln.startswith("pass") or "user" in ln for ln in lines)   # nothing for other users


def test_pf_rules_refuse_bad_values():
    for args in (({450: 47110}, 0, 452, 453, "192.0.2.10", 443), ({450: 47110}, 451, 452, 453, "hub.example", 443),
                 ({450: 47110}, 451, 452, 453, "192.0.2.10", 0), ({0: 47110}, 451, 452, 453, "192.0.2.10", 443)):
        with pytest.raises((kc.Refuse, ValueError)):
            km.pf_rules(*args)
    assert "to 2001:db8::1 port 443 user 451" in km.pf_rules({}, 451, 452, 453, "2001:db8::1", 443)


def test_pf_uids_from_pfctl_listing():
    shown = ("pass out quick on lo0 inet proto tcp from any to 127.0.0.1 port = 47110 flags S/SA keep state user = 450\n"
             "block drop out quick proto tcp all user = 450\nblock drop out quick proto udp all user = 451\n")
    assert km.pf_expected_uids(shown) == ["450", "451"]


# ---------------------------------------------------------------------------- launchd, sudo, ACL

def test_daemons_are_the_five_jobs_with_this_python():
    d = km.daemons("/Library/Frameworks/Python.framework/Versions/3.12/bin/python3.12", ["alice"])
    assert set(d) == {"pf", "canary", "relay", "update", "egress.alice"}
    for name, data in d.items():
        p = plistlib.loads(data)
        assert p["Label"] == f"com.kaptyorka.{name}"
        assert p["ProgramArguments"][:2] == ["/Library/Frameworks/Python.framework/Versions/3.12/bin/python3.12", "-I"]
        assert "UserName" not in p   # root starts it; the starter hands over to the service's user
    assert plistlib.loads(d["update"])["StartInterval"] == 3600 and "KeepAlive" not in plistlib.loads(d["update"])
    for n in ("canary", "relay", "egress.alice"):
        assert plistlib.loads(d[n])["KeepAlive"] is True
    assert plistlib.loads(d["canary"])["ProgramArguments"][-1] == kc.CANARY_PROC
    assert plistlib.loads(d["egress.alice"])["ProgramArguments"][-2:] == ["egress", "alice"]
    with pytest.raises(kc.Refuse):
        km.daemons("/usr/bin/python3; rm -rf /", [])


def test_sudoers_allows_only_enter_and_hubchat_as_root():
    t = km.sudoers_text("alice")
    rule = [ln for ln in t.splitlines() if ln.startswith("alice ")]
    assert rule == [f"alice ALL=(root) NOPASSWD: {kc.ENTER}, {kc.HUBCHAT_ROOT}"]
    assert "env_reset" in t and "SETENV" not in t and "ALL=(ALL)" not in t
    with pytest.raises(kc.Refuse):
        km.sudoers_text("alice ALL=(ALL) ALL")


def test_acl_entry_is_inherited_and_names_only_a_share_group():
    a = km.acl_entry("kapshare-alice")
    assert a.startswith("group:kapshare-alice allow ") and a.endswith("file_inherit,directory_inherit")
    for bad in ("staff", "kapshare-alice,everyone", "kapshare-"):
        with pytest.raises(kc.Refuse):
            km.acl_entry(bad)


def test_parse_launchctl_and_ifconfig():
    assert km.parse_launchctl("system/com.kaptyorka.relay = {\n\tstate = running\n\tpid = 412\n") == \
        {"state": "running", "pid": 412}
    assert km.parse_launchctl("") == {"state": "", "pid": 0}
    text = ("lo0: flags\n\tinet 127.0.0.1 netmask 0xff000000\n\tinet6 ::1 prefixlen 128\n"
            "\tinet6 fe80::1%lo0 prefixlen 64\nen0: flags\n\tinet 192.168.1.20 netmask\n"
            "\tinet6 2001:db8::5 prefixlen 64\n")
    assert km.parse_ifconfig(text) == ["192.168.1.20", "2001:db8::5"]


# ---------------------------------------------------------------------------- places and decisions

@pytest.mark.parametrize("cwd, want", [
    ("/Users/Shared/kaptyorka/alice/proj-a", ("proj-a", "")),
    ("/Users/Shared/kaptyorka/alice/proj-a/src/x", ("proj-a", "src/x")),
    ("/Users/Shared/kaptyorka/alice/_memory", ("_memory", "")),
    ("/Users/Shared/kaptyorka/alice", None),
    ("/Users/Shared/kaptyorka/bob/proj-a", None),
    ("/Users/Shared/kaptyorka/alice/.adding-1234", None),
    ("/Users/Shared/kaptyorka/alice/proj-a/../../bob/x", None),
    ("/Users/alice/proj-a", None),
    ("/srv/team/alice/proj-a", None),
])
def test_team_folder_is_decided_by_the_real_place_only(cwd, want):
    assert km.team_folder(cwd, "alice") == want


def test_python_problem():
    assert km.python_problem("/usr/bin/python3", (3, 9), "") .startswith("Python 3.9 is too old")
    assert km.python_problem("/opt/py/bin/python3", (3, 12), "/opt/py is not root-owned") == "/opt/py is not root-owned"
    assert km.python_problem("/Library/Frameworks/Python.framework/Versions/3.12/bin/python3", (3, 12), None) == ""
    assert km.python_problem("python3", (3, 12), None) == "the Python path is not a plain path"
    assert ms.python_fixable("/Library/Frameworks/Python.framework/Versions/3.12")
    assert not ms.python_fixable("/opt/homebrew/Cellar/python@3.12/3.12.8")


def test_py_prefix():
    assert km.py_prefix("/nonexistent/x/bin/python3") == "/nonexistent/x"


# ---------------------------------------------------------------------------- the inside check

def test_wallcheck_knows_the_same_places_as_root():
    assert mw.CANARY_SOCK == km.real(km.CANARY_SOCK)
    assert mw.TEAMAREA == km.TEAMAREA
    assert mw.BOXCONF == km.real(kc.BOXCONF)
    assert mw.LOCKED == kc.LOCKED_FILES
    assert set(mw.MACH_DENIED) <= set(km.MACH_DENY)
    assert (mw.CANARY_TCP_LO, mw.CANARY_TCP_ANY) == (km.CANARY_TCP_LO, km.CANARY_TCP_ANY)
    assert mw.CANARY_PROC == kc.CANARY_PROC.encode() and mw.MARK == kc.CANARY_MARK
    for p in ("/private/tmp/kaptyorka-wall-canary-0123456789abcdef", "/Users/alice/.kaptyorka-wall-canary",
              km.real(km.CANARY_FIXED)):
        assert mw.CANARY_RX.fullmatch(p), p
    for k in ("PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "TMPDIR", "TERM", "HTTPS_PROXY", "HTTP_PROXY",
              "ALL_PROXY", "NO_PROXY", "CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR"):   # what kap_mac.box_env sets
        assert k in mw.ENV_OK, k


def test_wallcheck_env_names_hold_no_secret_carrier():
    for bad in ("SSH_AUTH_SOCK", "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GIT_ASKPASS",
                "DYLD_INSERT_LIBRARIES", "PYTHONPATH"):
        assert bad not in mw.ENV_OK


@pytest.mark.parametrize("argv", [
    [], ["--box", "alice", "--person", "alice", "--", "/bin/true"][:4],
    ["--box", "Alice", "--person", "alice", "--", "/bin/true"],
    ["--box", "alice", "--person", "alice", "--canaries", "/etc/passwd", "--", "/bin/true"],
    ["--box", "alice", "--person", "alice", "--canary-pid", "1;2", "--", "/bin/true"],
])
def test_wallcheck_refuses_bad_arguments(argv):
    with pytest.raises(SystemExit) as e:
        mw.parse(argv)
    assert e.value.code == 2


def test_wallcheck_fail_names_the_wall_and_never_starts(capsys):
    with pytest.raises(SystemExit) as e:
        mw.fail("M2", "/private/tmp/x\x1b[2J is readable")
    assert e.value.code == 1
    err = capsys.readouterr().err
    assert "wall M2 is missing" in err and "\x1b" not in err and "not started" in err


def test_relay_reads_the_caller_from_a_macos_xucred():
    import struct
    raw = struct.pack(R.XUCRED, 0, 7701, 2, 7701, 12, *([0] * 14))
    assert R.xucred_uid(raw) == 7701
    with pytest.raises(OSError):
        R.xucred_uid(struct.pack(R.XUCRED, 1, 0, 0, *([0] * 16)))


# ---------------------------------------------------------------------------- the installer

def test_setup_hands_over_to_the_mac_installer_on_macos_only(monkeypatch):
    from kaptyorka.secure import kap_setup as ks
    called = []
    monkeypatch.setattr(ms, "main", lambda argv: called.append(argv) or 7)
    monkeypatch.setattr(ks.sys, "platform", "darwin")
    assert ks.main(["repair"]) == 7 and called == [["repair"]]
    monkeypatch.setattr(ks.sys, "platform", "linux")
    monkeypatch.setattr(ks.os, "geteuid", lambda: 1000)
    assert ks.main(["repair"]) == 2 and called == [["repair"]]


def test_mac_programs_exist_and_get_this_mac_s_python():
    py = "/Library/Frameworks/Python.framework/Versions/3.12/bin/python3.12"
    names = [n for _, n, _ in ms.PROGRAMS]
    assert len(names) == len(set(names))
    for src, name, mode in ms.PROGRAMS:
        data = ms.program_text(src, py)
        if mode == 0o755 and src.endswith(".py"):
            assert data.startswith(f"#!{py} -I\n".encode()), src
            ast.parse(data.decode())
    assert {"enter", "hubchat-root", "wallcheck", "box-exec", "tty-filter", "launch"} <= set(names)


def test_mac_box_configs_name_this_mac_s_python_and_still_parse():
    py = "/Library/Frameworks/Python.framework/Versions/3.12/bin/python3.12"
    top = os.path.join(SECURE, "files", "box")
    for dirpath, _, files in os.walk(top):
        for n in files:
            rel = os.path.relpath(os.path.join(dirpath, n), top)
            data = ms.box_file(rel, py)
            assert b"/usr/bin/python3" not in data
            if n.endswith(".json"):
                json.loads(data)
            if n.endswith(".toml") and sys.version_info >= (3, 11):
                import tomllib
                tomllib.loads(data.decode())


def test_mac_launcher_routes_only_its_own_team_area():
    p = os.path.join(SECURE, "files", "mac", "launch")
    text = open(p).read()
    assert 'team="/Users/Shared/kaptyorka/$me/"' in text and f"exec sudo -n {kc.ENTER} " in text
    assert subprocess.run(["sh", "-n", p]).returncode == 0
    r = subprocess.run(["sh", p, "rm"], capture_output=True, text=True)
    assert r.returncode == 2 and "unknown tool" in r.stderr


def test_the_test_version_notice_is_everywhere_a_person_looks():
    from kaptyorka.secure import kap_mac_hubchat as mh
    assert "TEST VERSION" in km.TEST_NOTICE
    assert km.TEST_NOTICE in mh.USAGE and any(km.TEST_NOTICE in ln for ln in ms.BANNER)
    src = open(os.path.join(SECURE, "kap_mac_hubchat.py")).read()
    assert "kr.say(km.TEST_NOTICE" in src     # hubchat list starts with it
    assert "TEST VERSION" in open(os.path.join(ROOT, "README.md")).read()


def test_tool_installer_profile_on_macos():
    p = kt.mac_profile("/private/var/lib/kaptyorka-tool-stage-claude")
    assert balanced(p) and '(allow file-write* (subpath "/private/var/lib/kaptyorka-tool-stage-claude")' in p
    for d in ("/Users", "/opt/kap-tools", "/private/etc/kaptyorka"):
        assert f'(subpath "{d}")' in p
    with pytest.raises(kc.Refuse):
        kt.mac_profile('/private/var/lib/kaptyorka-tool-stage-x") (allow file-write* (subpath "/')


def test_mac_workflow_runs_only_on_its_branch_or_by_hand_with_read_rights():
    wf = open(os.path.join(ROOT, ".github", "workflows", "mac-box.yml")).read()
    assert re.search(r"push:\s*\n\s*branches:\s*\[mac-box-test\]", wf)
    assert "workflow_dispatch" in wf and "pull_request" not in wf
    assert re.search(r"permissions:\s*\n\s*contents: read", wf)
    assert "secrets." not in wf
