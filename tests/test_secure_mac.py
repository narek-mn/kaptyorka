"""The macOS box (TEST VERSION; src/kaptyorka/secure/kap_mac*.py): the parts that are pure text or
pure decisions, checked here on any system. The real walls are checked on a Mac by
tests/mac-box/run.sh in .github/workflows/mac-box.yml."""
import ast
import errno
import importlib
import json
import os
import plistlib
import re
import subprocess
import sys

import pytest

from kaptyorka.relay import relay as R  # noqa: F401  (the Linux copies; the darwin ones below)
from kaptyorka.secure import kap_mac as km
from kaptyorka.secure import kap_mac_setup as ms
from kaptyorka.secure import kap_mac_wallcheck as mw
from kaptyorka.secure import kap_tools as kt
from kaptyorka.secure import kapcommon as kc

MAC_MODULES = ("kapcommon", "kaproot", "kap_mac", "kap_mac_setup", "kap_mac_enter", "kap_mac_hubchat",
               "kap_mac_wallcheck", "kap_mac_daemon", "kap_tools", "kap_update", "kap_setup", "kap_claude_token")


def _ours(name: str) -> bool:
    return name in ("kaptyorka.secure", "kaptyorka.relay") or name.startswith(("kaptyorka.secure.", "kaptyorka.relay."))


def load_darwin() -> dict:
    """Fresh copies of the secure modules imported as on a Mac (sys.platform "darwin"), so every
    place and text they build is the Mac's. The Linux copies in sys.modules are put back after."""
    saved = {n: sys.modules.pop(n) for n in [n for n in sys.modules if _ours(n)]}
    real = sys.platform
    sys.platform = "darwin"
    try:
        out = {n: importlib.import_module(f"kaptyorka.secure.{n}") for n in MAC_MODULES}
        out["relay"] = importlib.import_module("kaptyorka.relay.relay")
    finally:
        sys.platform = real
        for n in [n for n in sys.modules if _ours(n)]:
            del sys.modules[n]
        sys.modules.update(saved)
        for n, m in saved.items():
            parent, _, child = n.rpartition(".")
            if parent in sys.modules:
                setattr(sys.modules[parent], child, m)
    return out


MAC = load_darwin()
km, ms, mw, kt, kc, R = (MAC[n] for n in ("kap_mac", "kap_mac_setup", "kap_mac_wallcheck", "kap_tools", "kapcommon",
                                          "relay"))

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


def test_box_profile_allows_terminal_ioctls_only_on_ttys():
    # OpenCode's raw mode (tcsetattr) got EPERM without it (CI run 7); never the master side or
    # other devices
    t = profile()
    ioctl = [ln for ln in t.splitlines() if "file-ioctl" in ln]
    assert ioctl == ['(allow file-ioctl (literal "/dev/tty") (regex #"^/dev/ttys[0-9]+$"))']


def test_box_profile_has_no_clipboard_screen_or_apple_events_and_only_same_sandbox_processes():
    p = profile()
    deny = next(ln for ln in rules(p) if ln.startswith("(deny mach-lookup"))
    for n in ("com.apple.pasteboard.1", "com.apple.windowserver.active", "com.apple.coreservices.appleevents"):
        assert f'"{n}"' in deny
        assert f'"{n}"' not in next(ln for ln in rules(p) if ln.startswith("(allow mach-lookup"))
    assert "(allow process-info* (target same-sandbox))" in p and "(allow signal (target same-sandbox))" in p
    assert "(allow process-info*)" not in p and "(allow signal)" not in p


def test_box_and_filter_profiles_deny_other_processes_after_every_allow():
    # process-info* is allowed by default even under (deny default) and later rules win: the
    # deny must be written after system.sb and the plain sysctl-read, the same-sandbox allow after it
    for p in (profile(), km.filter_profile("/opt/py")):
        r = rules(p)
        at = {ln: i for i, ln in enumerate(r)}
        assert at['(import "system.sb")'] < at["(deny process-info*)"] < at["(allow process-info* (target same-sandbox))"]
        assert at["(deny signal)"] < at["(allow signal (target same-sandbox))"]
        deny = next(i for i, ln in enumerate(r) if ln.startswith("(deny sysctl-read"))
        assert at["(allow sysctl-read)"] < deny
        for name in ('(sysctl-name "kern.proc")', '(sysctl-name-prefix "kern.proc.")',
                     '(sysctl-name-prefix "kern.procargs")'):
            assert name in r[deny]
    # a local addition still comes after the wall, so wallcheck M7 catches one that opens it
    r = rules(profile(extra="(allow process-info*)"))
    assert r.index("(allow process-info*)") > r.index("(deny process-info*)")


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

def test_daemons_are_the_six_jobs_with_this_python():
    d = km.daemons("/Library/Frameworks/Python.framework/Versions/3.12/bin/python3.12", ["alice"])
    assert set(d) == {"pf", "canary", "relay", "update", "tools-update", "egress.alice"}
    for name, data in d.items():
        p = plistlib.loads(data)
        assert p["Label"] == f"com.kaptyorka.{name}"
        assert p["ProgramArguments"][:2] == ["/Library/Frameworks/Python.framework/Versions/3.12/bin/python3.12", "-I"]
        assert "UserName" not in p   # root starts it; the starter hands over to the service's user
    assert plistlib.loads(d["update"])["StartInterval"] == 3600 and "KeepAlive" not in plistlib.loads(d["update"])
    tu = plistlib.loads(d["tools-update"])   # the daily tool update (the Linux timer's place)
    assert tu["StartInterval"] == 86400 and "KeepAlive" not in tu
    assert tu["ProgramArguments"][2:] == [f"{kc.LIBEXEC}/kap_tools.py", "auto-update"]
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
    assert mw.CANARY_SOCK == km.CANARY_SOCK == km.real(km.CANARY_SOCK)
    assert mw.TEAMAREA == km.TEAMAREA == kc.VIEW
    assert mw.BOXCONF == kc.BOXCONF == km.real(kc.BOXCONF)
    assert mw.TEAM == kc.TEAM and f"{mw.TEAM}/kap-alice/home" == kc.box_home("alice")   # M11's HOME
    assert mw.LOCKED == kc.LOCKED_FILES
    assert set(mw.MACH_DENIED) <= set(km.MACH_DENY)
    assert (mw.CANARY_TCP_LO, mw.CANARY_TCP_ANY) == (km.CANARY_TCP_LO, km.CANARY_TCP_ANY)
    assert mw.CANARY_PROC == kc.CANARY_PROC.encode() and mw.MARK == kc.CANARY_MARK
    for p in ("/private/tmp/kaptyorka-wall-canary-0123456789abcdef", "/Users/alice/.kaptyorka-wall-canary",
              km.real(km.CANARY_FIXED)):
        assert mw.CANARY_RX.fullmatch(p), p
    for k in ("PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "TMPDIR", "TERM", "HTTPS_PROXY", "HTTP_PROXY",
              "ALL_PROXY", "NO_PROXY", "CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR", "KAP_OPENCODE_PORT"):   # box_env's
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


def test_wallcheck_m13_names_owner_group_and_mode(tmp_path):
    import errno as E
    d = tmp_path / "team"
    d.mkdir(mode=0o750)
    line = mw.team_problem(str(d), OSError(E.EACCES, "denied"))
    assert line.startswith(f"{d} gave EACCES") and "mode 750" in line and f"group {os.getegid()})" in line
    assert mw.team_problem(str(d), OSError(E.EPERM, "x")) == f"{d} gave EPERM"


def test_wallcheck_m10_keeps_standard_error_and_refuses_off_a_mac(capfd):
    # off a Mac there is no sandbox_init: no second profile, and descriptor 2 is left as it was
    assert mw.nested_sandbox_applies() is False
    print("still here", file=sys.stderr, flush=True)
    assert "still here" in capfd.readouterr().err


def test_box_processes_keep_the_share_group(monkeypatch):
    # macOS keeps the effective group in the group list's first place and setregid overwrites it
    # (CI run 3: M13 EACCES), so the primary group goes first and the share group after it
    seen = {}
    monkeypatch.setattr(km, "ids", lambda box: (404, 404, 405))
    monkeypatch.setattr(km, "box_argv", lambda *a, **k: ["/usr/bin/true"])
    monkeypatch.setattr(km, "box_env", lambda *a, **k: {})
    monkeypatch.setattr(km.kr, "mkdir", lambda *a, **k: None)
    monkeypatch.setattr(km.os, "makedirs", lambda *a, **k: None)
    monkeypatch.setattr(km.subprocess, "Popen", lambda argv, **kw: seen.update(kw) or "p")
    assert km.popen_box("alice", "alice", ["/usr/bin/true"], "/") == "p"
    assert seen["group"] == 404 and seen["extra_groups"] == [404, 405]


def test_box_profile_reads_the_developer_app_only_when_named():
    assert "/Applications" not in profile()
    text = profile(dev_app="/Applications/Xcode_16.4.app")
    assert '(subpath "/Applications/Xcode_16.4.app")' in text and balanced(text)
    read = next(ln for ln in rules(text) if ln.startswith("(allow file-read* (subpath"))
    assert "/Applications/Xcode_16.4.app" in read and "file-write" not in read
    for bad in ("/Applications", "/Applications/Xcode.app/../x.app", "/Users/alice/Xcode.app", "/Applications/x\"\n.app"):
        with pytest.raises(kc.Refuse):
            profile(dev_app=bad)


def test_developer_app_is_only_an_app_in_applications(tmp_path, monkeypatch):
    link = tmp_path / "xcode_select_link"
    link.symlink_to("/Library/Developer/CommandLineTools")
    assert km.developer_app(str(link)) == ""
    link.unlink()
    link.symlink_to("/Users/alice/Xcode.app/Contents/Developer")
    assert km.developer_app(str(link)) == ""
    link.unlink()
    link.symlink_to("/Applications/Xcode_16.4.app/Contents/Developer")
    monkeypatch.setattr(km.os.path, "realpath", lambda p: p)
    monkeypatch.setattr(km.os.path, "isdir", lambda p: p == "/Applications/Xcode_16.4.app/Contents/Developer")
    assert km.developer_app(str(link)) == "/Applications/Xcode_16.4.app"
    # no link: the default Xcode, when it is there
    assert km.developer_app(str(tmp_path / "none")) == ""


def test_relay_reads_the_caller_from_a_macos_xucred():
    import struct
    raw = struct.pack(R.XUCRED, 0, 7701, 2, 7701, 12, *([0] * 14))
    assert R.xucred_uid(raw) == 7701
    with pytest.raises(OSError):
        R.xucred_uid(struct.pack(R.XUCRED, 1, 0, 0, *([0] * 16)))


# ---------------------------------------------------------------------------- the installer

def test_setup_hands_over_to_the_mac_installer_on_macos_only(monkeypatch):
    from kaptyorka.secure import kap_mac_setup as ms   # the copies kap_setup itself imports
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



# ---------------------------------------------------------------------------- no Linux place on a Mac

# Linux-only places, and the macOS links into /private: root's walks never follow a link
# (kaproot.open_dir: the first CI run stopped on /var/lib/kap-team) and Seatbelt matches real paths
LINUX_TOP = re.compile(r"/(etc|var|tmp|run|srv|home|proc|sys|lib|lib64|dev/shm|media|mnt)(/|$)")
LINUX_CALLS = {"systemctl", "props_args", "ensure_box_running", "outside_check", "host_addrs", "shared_canaries",
               "chats", "boxes_of", "start_helper", "kill_box_procs_in", "add_view_mount", "drop_view_mount",
               "hide_view", "mount_flags", "old_login_hint", "ensure_box", "drop_box", "unit_cmd", "verify_in_box"}
LINUX_WORDS = re.compile(r"systemd-run|systemctl|setfacl|getfacl|/proc/|\bnsenter\b|\bunshare\b|\bbwrap\b|loginctl")
MAC_SOURCES = ("kap_mac.py", "kap_mac_setup.py", "kap_mac_enter.py", "kap_mac_hubchat.py", "kap_mac_wallcheck.py",
               "kap_mac_daemon.py")
NOT_PLACES = {"HERE", "FILES", "RELAY_SRC", "INSTALLER_FILE"}   # where this checkout is


def _paths(text: str):
    return re.findall(r"(?<![\w.~/-])(/[A-Za-z0-9._/+-]+)", text)


def _values(v):
    if isinstance(v, str):
        yield v
    elif isinstance(v, (tuple, list, set, frozenset)):
        for x in v:
            yield from _values(x)
    elif isinstance(v, dict):
        for x in v.values():
            yield from _values(x)


def test_mac_constants_name_no_linux_place():
    bad = []
    for name in ("kapcommon", "kap_mac", "kap_mac_setup", "kap_mac_wallcheck", "kap_mac_enter", "kap_mac_hubchat",
                 "kap_mac_daemon", "kap_update"):
        for k, v in vars(MAC[name]).items():
            if k.startswith("_") or k in NOT_PLACES:
                continue
            bad += [f"{name}.{k} = {x}" for x in _values(v) if x.startswith("/") and LINUX_TOP.match(x)]
    for k in ("LOCK", "MAC_STAGE", "RECORD"):
        x = getattr(kt, k)
        if LINUX_TOP.match(x):
            bad.append(f"kap_tools.{k} = {x}")
    assert not bad, bad


def test_what_the_mac_code_builds_names_no_linux_place(monkeypatch):
    monkeypatch.setattr(km, "proxy_port", lambda b: 47110)
    monkeypatch.setattr(km, "opencode_port", lambda b: 47310)
    py = "/Library/Frameworks/Python.framework/Versions/3.12/bin/python3.12"
    texts = [profile(opencode_port=47310), km.service_profile(["/private/var/run/kaptyorka/relay"]),
             km.filter_profile("/Library/Frameworks/Python.framework/Versions/3.12"), km.sudoers_text("alice"),
             kt.mac_profile("/private/var/lib/kaptyorka-tool-stage-claude"),
             json.dumps(km.box_env("alice", "alice", "xterm")),
             *[json.dumps(plistlib.loads(d)) for d in km.daemons(py, ["alice"]).values()],
             *[f(x) for f, x in ((kc.box_home, "alice"), (kc.box_root, "alice"), (kc.view_root, "alice"),
                                 (km.box_run, "alice"), (km.box_tmp, "alice"), (km.relay_door, "alice"),
                                 (km.profile_path, "alice"), (km.plist_path, "relay"), (km.team_root, "alice"))],
             km.team_path("alice", "proj-a"), kc.view_path("alice", "proj-a"), kc.INSTALLING, kc.CANARY_STATE,
             " ".join(ms.HOOK_FILES), km.PF_CONF, km.SUDOERS_DIR]
    bad = [p for t in texts for p in _paths(t) if LINUX_TOP.match(p)]
    assert not bad, bad
    assert kc.view_path("alice", "proj-a") == km.team_path("alice", "proj-a")   # one real place on a Mac


def _calls_and_strings(node):
    for n in ast.walk(node):
        if isinstance(n, ast.Call):
            f = n.func
            yield "call", f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")
        elif isinstance(n, ast.Constant) and isinstance(n.value, str):
            yield "str", n.value


def _no_doc(tree):
    for n in ast.walk(tree):
        if isinstance(n, (ast.Module, ast.FunctionDef, ast.ClassDef)) and n.body and \
                isinstance(n.body[0], ast.Expr) and isinstance(getattr(n.body[0], "value", None), ast.Constant):
            n.body = n.body[1:] or [ast.Pass()]
    return tree


def test_mac_code_calls_nothing_linux_only_even_one_step_down():
    """The Mac programs, and every kaproot, kap_setup, kap_hubchat and kap_enter function they
    call, run no systemd, ACL, mount or /proc tool (docstrings aside)."""
    shared = {"kr": "kaproot.py", "ks": "kap_setup.py", "hc": "kap_hubchat.py", "ke": "kap_enter.py"}
    defs = {}
    for alias, f in shared.items():
        t = _no_doc(ast.parse(open(os.path.join(SECURE, f)).read()))
        defs[alias] = {n.name: n for n in t.body if isinstance(n, ast.FunctionDef)}
    bad = []
    for f in MAC_SOURCES:
        tree = _no_doc(ast.parse(open(os.path.join(SECURE, f)).read()))
        for kind, v in _calls_and_strings(tree):
            if kind == "str" and LINUX_WORDS.search(v):   # (kap_mac has its own host_addrs, chats, ...)
                bad.append(f"{f}: {kind} {v[:60]}")
        for n in ast.walk(tree):
            if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id in shared:
                if n.value.id == "kr" and n.attr in LINUX_CALLS:
                    bad.append(f"{f}: kr.{n.attr}")
                fn = defs[n.value.id].get(n.attr)
                if fn is None:
                    continue
                for kind, v in _calls_and_strings(fn):
                    if (kind == "call" and v in LINUX_CALLS) or (kind == "str" and LINUX_WORDS.search(v)):
                        bad.append(f"{f} -> {n.value.id}.{n.attr}: {kind} {v[:60]}")
    # the Mac halves of kap_tools
    t = _no_doc(ast.parse(open(os.path.join(SECURE, "kap_tools.py")).read()))
    for fn in (n for n in t.body if isinstance(n, ast.FunctionDef) and n.name in ("install_one_mac", "verify_in_box_mac",
                                                                                  "mac_profile")):
        for n in ast.walk(fn):
            if isinstance(n, ast.Attribute) and getattr(n.value, "id", "") == "kr" and n.attr in LINUX_CALLS:
                bad.append(f"kap_tools.{fn.name}: kr.{n.attr}")
            if isinstance(n, ast.Constant) and isinstance(n.value, str) and LINUX_WORDS.search(n.value):
                bad.append(f"kap_tools.{fn.name}: {n.value[:60]}")
    assert not bad, sorted(set(bad))


# ---------------------------------------------------------------------------- OpenCode's one port

def test_box_profile_opens_only_the_box_s_own_opencode_port():
    out = [ln for ln in rules(profile(opencode_port=47310)) if "network" in ln]
    assert '(allow network-bind network-inbound (local ip "localhost:47310"))' in out
    assert '(allow network-outbound (remote ip "localhost:47310"))' in out
    assert not [ln for ln in out if "local ip" in ln and "47310" not in ln]
    assert not [ln for ln in out if re.search(r'ip "(\*|localhost:\*|[0-9.]+:)', ln)]
    assert "47310" not in profile()   # no port named: no opening
    for bad in (47110, 80, 70000, "47310"):
        with pytest.raises(kc.Refuse):
            profile(opencode_port=bad)


def test_pf_keeps_everyone_but_the_box_off_its_opencode_port():
    t = km.pf_rules({450: 47110, 455: 47111}, 451, 452, 453, "192.0.2.10", 8444, {450: 47310, 455: 47311})
    lines = t.splitlines()
    for uid, oc in ((450, 47310), (455, 47311)):
        own = lines.index(f"pass out quick on lo0 proto tcp from any to 127.0.0.1 port {oc} user {uid}")
        # the box's listener answers through the state of the SYN it takes in on lo0 (pf does not
        # see the listener's user on its answer out, CI run 7), so no pass out from the port at all
        listen = lines.index(f"pass in quick on lo0 proto tcp from 127.0.0.1 to 127.0.0.1 port {oc} user {uid}")
        others = lines.index(f"block drop out quick proto {{ tcp udp }} from any to any port {oc}")
        squat = lines.index(f"block drop out quick proto {{ tcp udp }} from any port {oc} to any")
        assert own < others < squat < lines.index(f"block drop out quick proto {{ tcp udp }} all user {uid}")
        assert not [ln for ln in lines if ln.startswith("pass out") and f"from 127.0.0.1 port {oc}" in ln]
        # no pass for anyone else names the port
        assert not [ln for ln in lines if ln.startswith("pass") and f"port {oc}" in ln and f"user {uid}" not in ln]
        # pf is the address wall: nothing from outside lo0 reaches the port, nor any box socket, for
        # every user (Seatbelt's "localhost" lets the box bind 0.0.0.0); the only passes before
        # those drops are the listeners' own, on lo0 alone
        off = lines.index(f"block drop in quick on ! lo0 proto {{ tcp udp }} from any to any port {oc}")
        box_in = lines.index(f"block drop in quick on ! lo0 proto {{ tcp udp }} all user {uid}")
        assert listen < box_in
        before = [ln for ln in lines[:max(off, box_in)] if ln.startswith("pass")]
        assert before and all(ln.startswith("pass in quick on lo0 proto tcp from 127.0.0.1 to 127.0.0.1 port ")
                              for ln in before)
        first_out = min(i for i, ln in enumerate(lines) if ln.startswith("pass out"))
        assert off < first_out and box_in < first_out
    assert not [ln for ln in km.pf_rules({450: 47110}, 451, 452, 453, "192.0.2.10", 8444).splitlines()
                if ln.startswith("pass in")]
    for oc in ({450: 47110}, {999: 47310}, {450: 80}):
        with pytest.raises(kc.Refuse):
            km.pf_rules({450: 47110}, 451, 452, 453, "192.0.2.10", 443, oc)


def test_the_box_gets_its_opencode_port_and_wallcheck_tries_it(monkeypatch):
    monkeypatch.setattr(km, "proxy_port", lambda b: 47110)
    monkeypatch.setattr(km, "opencode_port", lambda b: 47310)
    env = km.box_env("alice", "alice")
    assert env["KAP_OPENCODE_PORT"] == "47310" and set(env) <= mw.ENV_OK
    o, cmd = mw.parse(["--box", "alice", "--person", "alice", "--opencode-port", "47310", "--", "/bin/true"])
    assert o["opencode-port"] == "47310" and cmd == ["/bin/true"]
    with pytest.raises(SystemExit):
        mw.parse(["--box", "alice", "--person", "alice", "--opencode-port", "x", "--", "/bin/true"])
    # bind_problem: refused binds pass, an allowed bind on a busy port too; a wrong bind is named
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen(1)
    busy = s.getsockname()[1]
    try:
        assert mw.bind_problem("127.0.0.1", busy, True) == ""            # the box's own server has it
        assert "could be bound" in mw.bind_problem("127.0.0.1", 0, False)  # here (no profile) a wall is missing
    finally:
        s.close()
    src = open(os.path.join(SECURE, "kap_mac_wallcheck.py")).read()
    assert '("127.0.0.1", oc, True), ("127.0.0.1", oc + 1, False), ("127.0.0.1", 0, False)' in src
    assert 'fail("M15"' in src and '"0.0.0.0", oc' not in src   # the address is pf's wall, tried by root


def test_pf_check_wants_the_opencode_port_drops_loaded():
    loaded = ("block drop in quick on ! lo0 proto tcp from any to any port = 47310\n"
              "block drop in quick on ! lo0 proto udp from any to any port = 47310\n"
              "block drop out quick proto tcp from any to any port = 47310\n")
    assert km.pf_opencode_problem(loaded, [47310]) == ""
    assert km.pf_opencode_problem(loaded, []) == ""
    assert "from outside lo0" in km.pf_opencode_problem(loaded.split("\n", 2)[2], [47310])
    assert "from other users" in km.pf_opencode_problem(loaded.rsplit("block drop out", 1)[0], [47310])
    assert "47311" in km.pf_opencode_problem(loaded.replace("47310", "473100"), [47311])
    assert km.pf_opencode_problem(loaded.replace("47310", "473101"), [47310])   # no prefix match


def test_root_tries_the_opencode_port_as_another_user(monkeypatch):
    """Without pf (here) the port answers: a listener accepts, a closed port refuses; both are a
    missing wall. A port that gives no answer at all passes."""
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen(1)
    port = s.getsockname()[1]
    try:
        monkeypatch.setattr(km, "opencode_port", lambda b: port)
        assert "127.0.0.1:%d" % port in km.opencode_reach_problem("alice", [])
    finally:
        s.close()
    monkeypatch.setattr(km, "opencode_port", lambda b: port)   # now closed: refused
    assert "reach" in km.opencode_reach_problem("alice", [])
    monkeypatch.setattr(km, "opencode_port", lambda b: 0)
    assert km.opencode_reach_problem("alice", ["192.0.2.1"]) == ""
    monkeypatch.setattr(km, "opencode_port", lambda b: 47310)
    monkeypatch.setattr(km.socket.socket, "connect_ex", lambda self, a: errno.EINPROGRESS)
    monkeypatch.setattr(km.select, "select", lambda r, w, x, t: ([], [], []))   # pf drops: no answer
    assert km.opencode_reach_problem("alice", ["192.0.2.1"], timeout=0.05) == ""


def test_box_exec_uses_the_box_s_one_port_on_a_mac():
    from kaptyorka.secure import kap_box_exec as be
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    assert be.oc_port({"KAP_OPENCODE_PORT": str(port)}, mac=True) == port
    s = socket.socket()
    s.bind(("127.0.0.1", port))
    s.listen(1)
    try:
        with pytest.raises(SystemExit, match="another OpenCode window"):
            be.oc_port({"KAP_OPENCODE_PORT": str(port)}, mac=True)
    finally:
        s.close()
    with pytest.raises(SystemExit, match="no OpenCode port"):
        be.oc_port({}, mac=True)
    assert 1024 < be.oc_port({"KAP_OPENCODE_PORT": "47310"}) < 65536   # Linux: any free one


def test_opencode_is_offered_on_a_mac():
    enter = open(os.path.join(SECURE, "kap_mac_enter.py")).read()
    setup = open(os.path.join(SECURE, "kap_mac_setup.py")).read()
    assert "NOT_YET" not in enter and 'specs.pop("opencode"' not in setup
    assert "no OpenCode" not in open(os.path.join(ROOT, "README.md")).read()
    helper = open(os.path.join(SECURE, "kap_helper.py")).read()
    assert 'os.environ.get("KAP_OPENCODE_PORT", "") if MAC' in helper   # the V2 service takes the box's port


# ---------------------------------------------------------------------------- tools on a Mac

def test_mac_tool_updates_go_the_mac_way(monkeypatch):
    got = []
    monkeypatch.setattr(kt, "install_one_mac", lambda tool, ver, say, auto=False: got.append((tool, ver, auto)) or "9.9.9")
    assert kt.install_one("claude", "", auto=True) == "9.9.9" and got == [("claude", "", True)]
    calls = []
    monkeypatch.setattr(kt, "verify_in_box_mac", lambda person, prog, want: calls.append((person, prog, want)) or "")
    monkeypatch.setattr(kt, "people", lambda: ["alice"])
    assert kt.verify_in_box("/opt/kap-tools/claude/9.9.9/claude", "9.9.9") == "" and calls[0][0] == "alice"
    assert kt.version_problem("2.1.400 (Claude Code)\n", "wallcheck ok: 15 walls\n", "2.1.400") == ""
    assert "not 2.1.400" in kt.version_problem("2.1.399\n", "", "2.1.400")
    assert MAC["kap_mac"].plist_path("tools-update").endswith("com.kaptyorka.tools-update.plist")


def test_mac_ci_script_skips_after_a_failed_install_and_tries_the_opencode_port():
    for f in ("run.sh", "fake-claude.sh"):
        assert subprocess.run(["bash", "-n", os.path.join(ROOT, "tests", "mac-box", f)]).returncode == 0
    run = open(os.path.join(ROOT, "tests", "mac-box", "run.sh")).read()
    # one failed install: its output in full, every later row SKIP, the cleanup still checked
    assert 'printf \'%s\\n\' "$o" >"$OUT/install.txt"' in run and 'SKIP="the install failed' in run
    assert 'if [ -n "$SKIP" ] && [ "$r" != INFO ]; then r=SKIP' in run
    assert run.index('SKIP=""   # the cleanup') < run.index("check REMOVE")
    for row in ("OC-LISTENS", "OC-REAL-INSTALL", "OC-REAL-WINDOW"):
        assert f"check {row} " in run, row
    for row in ("OC-PERSON-BLOCKED", "OC-OTHER-USER-BLOCKED", "OC-ROOT-BLOCKED", "OC-LAN-ADDR-BLOCKED"):
        assert f"oc_blocked {row} " in run, row   # counted only while the box's listener is up


MAC_HOME_LS = ("drwxr-x---+ 15 alice  staff  480 Nov 27 10:53 /Users/alice\n"
               " 0: group:everyone deny delete\n")


def test_home_750_with_staff_is_closed_to_the_box_but_not_its_group_or_an_allow_acl():
    # macOS's own home (CI run 5, macOS 15 and 26): 750, group staff, a deny-only ACL; none of
    # kaptyorka's accounts is in staff, so nothing to do
    ours = {"kap-alice": {404, 12, 61, 405}, "_kaptyorka": {401}, "nobody": {4294967294}}
    assert km.home_mode_problems("alice", 0o750, 20, "staff", ours, MAC_HOME_LS) == []
    assert km.home_mode_problems("alice", 0o700, 405, "kapshare-alice", ours, "") == []
    bad = km.home_mode_problems("alice", 0o755, 20, "staff", ours, MAC_HOME_LS)
    assert bad == ["alice's home has mode 755; it must be 700 (chmod 700 /Users/alice)"]
    bad = km.home_mode_problems("alice", 0o750, 405, "kapshare-alice", ours, "")
    assert bad and "its group kapshare-alice holds kap-alice" in bad[0] and "chmod 700 /Users/alice" in bad[0]
    bad = km.home_mode_problems("alice", 0o700, 20, "staff", ours, MAC_HOME_LS + " 1: user:kap-alice allow list,search\n")
    assert bad == ["alice's home has an ACL entry that lets others in (user:kap-alice allow list,search); "
                   "remove it: chmod -a# 1 /Users/alice"]
    assert km.acl_allows(MAC_HOME_LS) == []


def test_installer_keeps_the_mac_s_750_home_and_closes_anything_wider(monkeypatch, tmp_path):
    import types
    home = tmp_path / "alice"
    home.mkdir()
    monkeypatch.setattr(km, "person_home", lambda p: types.SimpleNamespace(pw_dir=str(home)))
    calls, said = [], []
    acl = {"out": ""}
    monkeypatch.setattr(km, "run", lambda cmd, **kw: calls.append(cmd) or types.SimpleNamespace(stdout=acl["out"]))
    monkeypatch.setattr(ms, "say", lambda *a: said.append(" ".join(a)))
    monkeypatch.setattr(km, "PRIVILEGED", {**km.PRIVILEGED, os.stat(home).st_gid: "staff"})
    os.chmod(home, 0o750)
    ms.home_700("alice")
    assert os.stat(home).st_mode & 0o777 == 0o750 and not said
    os.chmod(home, 0o755)
    ms.home_700("alice")
    assert os.stat(home).st_mode & 0o777 == 0o700 and "now 700" in said[-1]
    monkeypatch.setattr(km, "PRIVILEGED", {})       # a group that is not staff, wheel or admin: closed
    os.chmod(home, 0o750)
    ms.home_700("alice")
    assert os.stat(home).st_mode & 0o777 == 0o700
    acl["out"] = MAC_HOME_LS + " 1: group:everyone allow list\n 2: user:bob allow read\n"
    ms.home_700("alice")                             # the person's own ACL entries are never taken off
    assert not [c for c in calls if c[0] == "/bin/chmod"]
    with pytest.raises(kc.Refuse) as e:              # the install stops before any change and names them
        ms.home_acl_check("alice")
    assert "chmod -a# 2" in str(e.value) and "chmod -a# 1" in str(e.value) and "Nothing was changed" in str(e.value)
    acl["out"] = MAC_HOME_LS
    ms.home_acl_check("alice")


def test_mac_ci_script_limits_every_step_and_every_attack():
    run = open(os.path.join(ROOT, "tests", "mac-box", "run.sh")).read()
    fake = open(os.path.join(ROOT, "tests", "mac-box", "fake-claude.sh")).read()
    # nothing a box process could hold open: no command substitution reads a person's command or
    # sudo through a pipe (the box runs in a session of its own; CI run 5 hung 40 minutes so)
    assert "tmo \"${2:-120}\" sudo" not in run and 'o=$(tmo' not in run and "| tmo" not in run.replace("| tmo 30", "")
    assert 'as_person_to "$f"' in run and '>"$1" 2>&1; rc=$?' in run and "reap_box" in run
    assert "watchdog.pl" in run and 'kill "$WD"' in run and "grep -c 'FAIL$' \"$TO\"" in run
    # every as_person, root command with a pipe and the install and remove is inside a named step
    body = run[run.index('echo "== facts"'):]
    steps = re.findall(r"^\s*step (\S+) (\d+)$", body, re.M)
    assert len(steps) >= 20 and all(int(s) <= 960 for _, s in steps)
    assert body.index("step attacks") < body.index("claude attacks")
    # every attack probe has its own limit, and a timed-out probe is a FAIL, never a PASS
    for line in fake[fake.index("attacks)"):fake.index("oc-listen)")].splitlines():
        if re.match(r"\s*(res [A-Z]|echo \"RESULT [A-Z])", line) and "$(" in line and "OC-PORT-NAMED" not in line:
            assert "$(lim " in line, line
    assert "*KAPCI-PROBE-TIMEOUT*)" in fake and "FAIL timed out" in fake
    assert 'Timeout=>6' in fake and "accept or do" in fake    # OC-SELF never waits forever on accept
