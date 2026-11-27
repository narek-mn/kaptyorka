"""kaptyorka-secure-setup on macOS (secure design v2, 9 and 10; TEST VERSION, not proven yet): the
same commands as on Linux (kap_setup.py, which hands over here on a Mac):

  install --personal '<https invite link>' [--name NAME] [--person NAME] [--claude-token-file FILE]
          [--runtime-max 7d] [--tools claude,codex,cursor-agent|none] [--python PATH]
  rejoin '<new https invite link>' [--name NAME]
  repair
  update-tools [claude codex ...]
  remove [--person NAME] [--delete-box-home] [--yes]

What a Mac gets instead of the Linux parts: dscl accounts (the box kap-<person>, the share group,
the service users _kaptyorka, _kapproxy, _kaptools), the person's home set to 700, a sudo rule,
a Seatbelt profile per box (written anew at every start), a pf anchor, LaunchDaemons (pf at boot,
canaries, relay, one forwarder per box, the hourly signed update check), the launcher in the
system shell files, the team area /Users/Shared/kaptyorka. One Python runs every root program and
the box: 3.10 or newer, its whole install root-owned (--python; default: the one running this).
"""
import json
import os
import pwd
import re
import shutil
import subprocess
import sys

if __package__:
    from . import kap_mac as km, kap_setup as ks, kap_tools, kap_update, kapcommon as kc, kaproot as kr
else:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import kap_mac as km
    import kap_setup as ks
    import kap_tools
    import kap_update
    import kapcommon as kc
    import kaproot as kr

Refuse = kc.Refuse
HERE = os.path.dirname(os.path.abspath(__file__))
FILES = os.path.join(HERE, "files")
RELAY_SRC = os.path.join(os.path.dirname(HERE), "relay")
SHEBANG = "#!/usr/bin/python3 -I\n"
PROGRAMS = (  # (source, name in /usr/local/libexec/kaptyorka, mode); programs get this Mac's Python
    ("kap_mac_enter.py", "enter", 0o755), ("kap_mac_hubchat.py", "hubchat-root", 0o755),
    ("kap_mac_wallcheck.py", "wallcheck", 0o755), ("kap_box_exec.py", "box-exec", 0o755),
    ("kap_ttyfilter.py", "tty-filter", 0o755), ("kap_mac_daemon.py", "kap_mac_daemon.py", 0o755),
    ("kap_update.py", "kap_update.py", 0o755), ("files/mac/launch", "launch", 0o755),
    ("kap_mac.py", "kap_mac.py", 0o644), ("kap_hubchat.py", "kap_hubchat.py", 0o644),
    ("kap_enter.py", "kap_enter.py", 0o644), ("kapcommon.py", "kapcommon.py", 0o644),
    ("kaproot.py", "kaproot.py", 0o644), ("kap_tools.py", "kap_tools.py", 0o644),
    ("kap_release.py", "kap_release.py", 0o644), ("kap_installer.py", "kap_installer.py", 0o644),
    ("kap_setup.py", "kap_setup.py", 0o644), ("kap_mac_setup.py", "kap_mac_setup.py", 0o644),
    ("kap_claude_token.py", "kap_claude_token.py", 0o644),
)
RC_FILES = (".zshrc", ".zshenv", ".zprofile", ".bashrc", ".bash_profile", ".profile", ".config/fish/config.fish")
HOOK_FILES = km.SHELL_FILES
BANNER = ["", "*** " + km.TEST_NOTICE + " ***", ""]


def say(*a):
    kr.say(*a)


# ---------------------------------------------------------------------------- checks

def py_version(py: str):
    try:
        r = subprocess.run([py, "-I", "-c", "import sys; print(sys.version_info[0], sys.version_info[1])"],
                           capture_output=True, text=True, timeout=30, env=kr.CLEAN_ENV)
        a, b = r.stdout.split()
        return int(a), int(b)
    except (OSError, ValueError, subprocess.SubprocessError):
        return (0, 0)


def python_fixable(prefix: str) -> bool:
    """A python.org framework install: kaptyorka may make it root-only-writable itself."""
    return bool(re.fullmatch(r"/Library/Frameworks/Python\.framework/Versions/3\.[0-9]{1,2}", prefix))


def fix_python(prefix: str) -> None:
    """chown root and chmod go-w over a python.org framework and the folders above it up to
    /Library/Frameworks (the installer leaves them group admin, writable): a Python root runs must
    be root's alone."""
    top = "/Library/Frameworks/Python.framework"
    for p in ("/Library/Frameworks", top, top + "/Versions"):
        st = os.lstat(p)
        if not os.path.islink(p):
            os.chown(p, 0, st.st_gid)
            os.chmod(p, st.st_mode & 0o7755)
    for dirpath, dirs, files in os.walk(prefix):
        for n in [dirpath] + [os.path.join(dirpath, x) for x in dirs + files]:
            st = os.lstat(n)
            if st.st_uid != 0:
                os.lchown(n, 0, st.st_gid)
            if not os.path.islink(n) and st.st_mode & 0o022:
                os.chmod(n, st.st_mode & 0o7755)


def preflight(person: str, py: str):
    stop, warn, fix = [], [], []
    try:
        ver = tuple(int(x) for x in km.run(["/usr/bin/sw_vers", "-productVersion"]).stdout.strip().split(".")[:2])
    except (Refuse, ValueError):
        ver = (0, 0)
    if ver < km.MIN_MACOS:
        stop.append(f"macOS {km.MIN_MACOS[0]} or newer is needed")
    for tool in ("/usr/bin/sandbox-exec", "/sbin/pfctl", "/usr/bin/dscl", "/usr/sbin/dseditgroup", "/usr/sbin/visudo",
                 "/usr/sbin/lsof", "/usr/bin/pkill", "/usr/bin/find", "/bin/launchctl", "/usr/bin/nc"):
        if not os.path.exists(tool):
            stop.append(f"the program {tool} is missing")
    if not os.path.exists("/usr/bin/ssh-keygen"):
        warn.append("ssh-keygen is missing, so kaptyorka cannot check the signature of its own updates")
    mode = kr.read_text(kc.MODE_FILE).strip()
    if mode and mode != "personal":
        stop.append(f"this computer was installed as {mode}; the mode is fixed at install")
    if 'anchor "com.apple/*"' not in kr.read_text(km.PF_CONF):
        stop.append("/etc/pf.conf does not load Apple's anchor com.apple/*, so kaptyorka's pf rules would not apply")
    v = py_version(py)
    prefix = km.py_prefix(py) if py else ""
    p = km.python_problem(py, v, km.tree_problem(prefix) if prefix else "no Python")
    if p:
        if v >= (3, 10) and python_fixable(prefix) and km.PY_RX.fullmatch(py):
            fix.append(prefix)
        else:
            stop.append(f"{p} ({py}). Install Python 3.12 from python.org (it goes to /Library/Frameworks; this "
                        "installer then makes it writable by root only) and run this again with it, or name one "
                        "with --python")
    # the folders above root's programs (/usr/local/bin may be the person's: hubchat there is only
    # `sudo -n hubchat-root`, which the sudo rule names by its own path)
    for d in ("/usr/local", "/usr/local/libexec", "/opt"):
        if os.path.isdir(d):
            st = os.lstat(d)
            if st.st_uid != 0 or st.st_mode & 0o022 or os.path.islink(d):
                stop.append(f"{d} belongs to someone else or others can write it (Homebrew on an Intel Mac?); "
                            "kaptyorka's root programs cannot live under it")
    if os.path.lexists(km.TEAMAREA):
        st = os.lstat(km.TEAMAREA)
        if os.path.islink(km.TEAMAREA) or not os.path.isdir(km.TEAMAREA) or st.st_uid != 0:
            stop.append(f"{km.TEAMAREA} exists and is not root's folder: move it away first")
    try:
        pw = pwd.getpwnam(person)
    except KeyError:
        return [f"there is no user {person}"], warn, fix
    if pw.pw_uid < 500 or pw.pw_dir != f"/Users/{person}" or os.path.realpath(pw.pw_dir) != pw.pw_dir:
        stop.append(f"{person}'s home must really be /Users/{person}")
    old = [p for p in ("/.kaptyorka", "/.local/bin/kaptyorka", "/.local/share/pipx/venvs/kaptyorka",
                       "/Library/LaunchAgents/kaptyorka.plist") if os.path.lexists(pw.pw_dir + p)]
    for rc in RC_FILES:
        text = ks._read_nofollow(f"{pw.pw_dir}/{rc}")
        if re.search(r"^\s*(alias\s+(claude|codex|opencode|cursor-agent|git)=|(function\s+)?(claude|codex|opencode|cursor-agent)\s*\(\))",
                     text, re.M):
            stop.append(f"{pw.pw_dir}/{rc} defines its own claude/codex/opencode/cursor-agent; it would win over the "
                        "launcher: remove it first")
        if "kaptyorka" in text:
            old.append(f"~/{rc} mentions kaptyorka")
    if old:
        stop.append("today's kaptyorka is still in this account (" + ", ".join(old[:4]) +
                    "): run  kaptyorka leave  and  kaptyorka remove  first")
    if km.privileged_groups(person, pw.pw_gid):
        warn.append("your account is an admin, so it can become root. The box still protects you from kaptyorka, "
                    "because no kaptyorka code runs as you.")
    return stop, warn, fix


# ---------------------------------------------------------------------------- install

def program_text(src: str, py: str) -> bytes:
    with open(os.path.join(HERE, src), "rb") as f:
        data = f.read()
    if src.endswith(".py") and data.startswith(SHEBANG.encode()):
        data = f"#!{py} -I\n".encode() + data[len(SHEBANG):]
    return data


def install_code(ver: str, py: str) -> None:
    for d in ("/usr/local", "/usr/local/libexec", "/usr/local/bin", "/opt"):
        if not os.path.isdir(d):
            kr.mkdir(d, 0o755)
    kr.mkdir(kc.LIBEXEC, 0o755)
    for src, name, mode in PROGRAMS:
        kr.write_file(f"{kc.LIBEXEC}/{name}", program_text(src, py), mode)
    ks.copy(os.path.join(FILES, "hubchat"), "/usr/local/bin/hubchat", 0o755)
    kr.mkdir(kc.SHIMS, 0o755)
    for tool in ("claude", "opencode", "codex", "cursor-agent"):
        kr.write_file(f"{kc.SHIMS}/{tool}", f"#!/bin/sh\n# kaptyorka: {tool} through the launcher (a team folder: the box)\n"
                                            f"exec {kc.LIBEXEC}/launch {tool} \"$@\"\n", 0o755)
    kr.mkdir(kc.OPT, 0o755)
    for part, files in (("egress", {"kap_egress.py": "egress.py"}),
                        ("box", {"kap_proxybridge.py": "proxybridge.py", "kap_helper.py": "helper.py",
                                 "kap_fence.py": "fence.py", "kap_toolwire.py": "toolwire.py"}),
                        ("relay", {os.path.join(RELAY_SRC, "relay.py"): "relay.py"})):
        kr.mkdir(f"{kc.OPT}/{part}", 0o755)
        kr.mkdir(f"{kc.OPT}/{part}/{ver}", 0o755)
        for src, name in files.items():
            ks.copy(os.path.join(HERE, src), f"{kc.OPT}/{part}/{ver}/{name}", 0o644)
        cur = f"{kc.OPT}/{part}/current"
        if os.path.lexists(cur + ".new"):
            os.unlink(cur + ".new")
        os.symlink(ver, cur + ".new")
        os.replace(cur + ".new", cur)
    kr.mkdir(kc.TOOLS_DIR, 0o755)
    kr.mkdir(kc.TOOLS_DIR + "/bin", 0o755)


def box_file(rel: str, py: str) -> bytes:
    with open(os.path.join(FILES, "box", rel), "rb") as f:
        return f.read().replace(b"/usr/bin/python3", py.encode())


def install_etc(ver: str, py: str) -> None:
    kr.mkdir(kc.ETC, 0o755)
    kr.write_file(f"{kc.ETC}/version", ver + "\n")
    kr.write_file(km.PYTHON_FILE, py + "\n", 0o644)
    top = os.path.join(FILES, "box")
    for dirpath, dirs, files in os.walk(top):
        rel = os.path.relpath(dirpath, top)
        kr.mkdir(os.path.normpath(f"{kc.BOXCONF}/{rel}"), 0o755)
        for n in files:
            kr.write_file(os.path.normpath(f"{kc.BOXCONF}/{rel}/{n}"), box_file(os.path.normpath(f"{rel}/{n}"), py), 0o644)
    ks.copy(os.path.join(FILES, "launcher.sh"), f"{kc.ETC}/launcher.sh", 0o644)
    ks.copy(os.path.join(FILES, "launcher.fish"), f"{kc.ETC}/launcher.fish", 0o644)
    for d in (kc.PEOPLE, kc.BOXES):
        kr.mkdir(d, 0o755)
    kr.mkdir(kc.SECRET, 0o700)
    if not os.path.exists(km.PROFILE_EXTRA):
        kr.write_file(km.PROFILE_EXTRA, "", 0o644)
    if not os.path.exists(kc.MODE_FILE):
        kr.write_file(kc.MODE_FILE, "personal\n", 0o444)


def own_shebang(py: str) -> None:
    """The kept installer runs with this Mac's Python when started by its path (a zip application
    still opens with a longer first line: its offsets are counted from the end)."""
    with open(kc.INSTALLER, "rb") as f:
        data = f.read()
    if data.startswith(SHEBANG.encode()):
        kr.write_file(kc.INSTALLER, f"#!{py} -I\n".encode() + data[len(SHEBANG):], 0o755)


def install_dirs() -> None:
    for d, mode in ((km.RUN, 0o755), (km.RELAY_DIR, 0o755), (km.CHATS, 0o700), (km.RUNBOX, 0o755), (km.LOGS, 0o700),
                    ("/private/var/lib", 0o755), (kc.TEAM, 0o755)):
        if d == "/private/var/lib" and os.path.isdir(d):
            continue
        kr.mkdir(d, mode)


def install_relay_conf(v: dict) -> None:
    kr.write_file(f"{kc.ETC}/relay.conf", f"hub_ip={v['ip']}\nhub_port={v['port']}\nhub_sha256={v['fp']}\n"
                                          f"name={v['name']}\nversion={ks.version()}\nidle_exit=86400\n", 0o644)
    kr.mkdir(kc.SECRET, 0o700)
    kr.write_file(f"{kc.SECRET}/device-token", v["token"] + "\n", 0o600)
    if not os.path.exists(f"{kc.SECRET}/chat-key"):
        import secrets
        kr.write_file(f"{kc.SECRET}/chat-key", secrets.token_hex(32) + "\n", 0o600)


def free_port(box: str, kind: str = "proxy-port", base: int = km.PROXY_PORT_BASE) -> int:
    """A loopback port for the box (its forwarder's, or its OpenCode port), not another box's and
    free now."""
    taken = set()
    for b in km.boxes():
        for n in ("proxy-port", "opencode-port"):
            t = kr.read_text(f"{kc.BOXES}/{b}/{n}").strip()
            if t.isdigit() and (b != box or n != kind):
                taken.add(int(t))
    import socket
    for port in range(base, base + km.PORT_SPAN):
        if port in taken:
            continue
        s = socket.socket()
        try:
            s.bind(("127.0.0.1", port))
            return port
        except OSError:
            continue
        finally:
            s.close()
    raise Refuse(f"no free loopback port for the box ({kind})")


def ensure_box(person: str) -> None:
    """The box of a person on macOS: accounts, groups, the files in /etc/kaptyorka/boxes/<box>, the
    home skeleton (as on Linux), the team area, its macOS temp folder. Idempotent."""
    box = person
    u, g = kc.box_user(box), kc.share_group(box)
    if os.path.exists(f"{kc.BOXES}/{box}/owner") and kr.read_text(f"{kc.BOXES}/{box}/owner").split("\n")[0] != person:
        raise Refuse(f"the box {box} belongs to someone else")
    buid, bgid = km.ensure_account(u, kc.box_home(box), "/bin/zsh")
    sgid = km.ensure_group(g)
    km.add_member(g, u)
    km.add_member(g, person)
    bdir = f"{kc.BOXES}/{box}"
    kr.mkdir(bdir, 0o755)
    kr.write_file(f"{bdir}/owner", person + "\n")
    settings = json.loads(kr.read_text(f"{kc.BOXCONF}/claude/settings.json") or "{}")
    settings["autoMemoryDirectory"] = km.team_path(person, kc.MEMORY)
    kr.write_file(f"{bdir}/claude-settings.json", json.dumps(settings, indent=1, sort_keys=True) + "\n")
    if not os.path.exists(f"{bdir}/egress-allow"):
        kr.write_file(f"{bdir}/egress-allow", kr.read_text(f"{kc.BOXCONF}/egress-base"))
    if not kr.read_text(f"{bdir}/proxy-port").strip().isdigit():
        kr.write_file(f"{bdir}/proxy-port", f"{free_port(box)}\n")
    if not km.opencode_port(box):
        kr.write_file(f"{bdir}/opencode-port", f"{free_port(box, 'opencode-port', km.OPENCODE_PORT_BASE)}\n")
    root, home = kc.box_root(box), kc.box_home(box)
    kr.mkdir(kc.TEAM, 0o755)
    kr.mkdir(root, 0o710, 0, bgid)
    kr.mkdir(home, 0o750, 0, bgid)
    kr.mkdir(home + "/state", 0o750, 0, bgid)
    for d in kc.STATE_DIRS + ("state/opencode/cfg", kc.OPENCODE_CFG):
        kr.mkdir(f"{home}/{d}", 0o700, buid, bgid)
    kr.lock_dirs(f"{home}/{kc.OPENCODE_CFG}", kc.OPENCODE_LOCKED_DIRS)
    km.lock_settings(box)
    kr.mkdir(km.box_tmp(box), 0o700, buid, bgid)
    kr.mkdir(km.box_run(box), 0o700, buid, bgid)
    kr.mkdir(home + "/.ssh", 0o750, 0, bgid)
    port = km.proxy_port(box)
    kr.write_file(f"{home}/.ssh/config",
                  "# root-owned (kaptyorka): git over ssh goes through the forwarder\n"
                  "Host *\n"
                  f"  ProxyCommand /usr/bin/nc -X connect -x 127.0.0.1:{port} %h %p\n"
                  "  UserKnownHostsFile ~/state/ssh/known_hosts\n"
                  "  IdentityFile ~/.ssh/id_ed25519\n  IdentitiesOnly yes\n"
                  "  StrictHostKeyChecking accept-new\n", 0o644)
    key = f"{home}/.ssh/id_ed25519"
    if not os.path.exists(key) and os.path.exists("/usr/bin/ssh-keygen"):
        km.run(["/usr/bin/ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", f"{u}@team-box", "-f", key])
        os.chown(key, buid, bgid)
        os.chmod(key, 0o600)
        os.chmod(key + ".pub", 0o644)
    kr.write_file(f"{home}/.gitconfig", f"[user]\n\tname = {person} (team box)\n\temail = {u}@team-box.invalid\n"
                                        "[init]\n\tdefaultBranch = main\n", 0o644)
    # the team area: root's folders, the person's folder readable only by the share group
    kr.mkdir(km.TEAMAREA, 0o755)
    kr.mkdir(km.team_root(person), 0o750, 0, sgid)
    mem = km.team_path(person, kc.MEMORY)
    fresh = not os.path.isdir(mem)
    kr.mkdir(mem, 0o2770, buid, sgid)
    if fresh:
        km.run(["/bin/chmod", "+a", km.acl_entry(g), mem])
    # the box user's own macOS temp folder (confstr DARWIN_USER_TEMP_DIR makes it, mode 0700)
    try:
        r = subprocess.run(["/usr/bin/getconf", "DARWIN_USER_TEMP_DIR"], capture_output=True, text=True, timeout=30,
                           env={"PATH": "/usr/bin:/bin", "HOME": home}, user=buid, group=bgid, extra_groups=[],
                           cwd="/")
        d = os.path.dirname(os.path.realpath(r.stdout.strip().rstrip("/")))
        kr.write_file(f"{bdir}/darwin-dir", d + "\n")
    except (OSError, subprocess.SubprocessError):
        pass


def install_sudoers(person: str) -> None:
    tmp = f"{km.SUDOERS_DIR}/.kaptyorka-{person}.new"
    kr.write_file(tmp, km.sudoers_text(person), 0o440)
    km.run(["/usr/sbin/visudo", "-cf", tmp])
    os.replace(tmp, f"{km.SUDOERS_DIR}/kaptyorka-{person}")
    if km.run(["/usr/sbin/visudo", "-c"], check=False).returncode != 0:
        os.unlink(f"{km.SUDOERS_DIR}/kaptyorka-{person}")
        raise Refuse("sudo did not accept kaptyorka's rule together with this computer's other rules; it was taken "
                     "out again")


def install_hooks() -> None:
    hook = open(os.path.join(FILES, "shell-hook.sh")).read()
    for t in HOOK_FILES:
        ks.strip_hook(t)
        text = kr.read_text(t)
        kr.write_file(t, text + ("" if text.endswith("\n") or not text else "\n") + hook, 0o644)


def install_services(py: str) -> None:
    km.ensure_account(km.RELAY_USER, "/private/var/empty", "/usr/bin/false")
    km.ensure_account(km.PROXY_USER, "/private/var/empty", "/usr/bin/false")
    km.ensure_account(km.TOOLS_USER, "/private/var/empty", "/usr/bin/false")


def install_daemons(py: str) -> None:
    kr.write_file(km.PF_FILE, km.pf_text(), 0o644)
    km.pf_load()
    for name, data in km.daemons(py, km.boxes()).items():
        km.load_daemon(name, data)
    # a relay that already ran has only the doors it started with
    km.launchctl("kickstart", "-k", f"system/{km.label('relay')}")


def self_check(person: str) -> int:
    km.repair_before_start(person, person)
    problems = km.outside_check(person, person)
    if problems:
        say("Self-check: PROBLEMS", *[f"  {p}" for p in problems])
        return 3
    p = km.popen_box(person, person, ["/usr/bin/true"], km.team_path(person, kc.MEMORY), verbose=True,
                     stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                     start_new_session=True)
    out, err = p.communicate(timeout=120)
    km.end_session(p.pid)
    if p.returncode != 0:
        say("Self-check (inside the box): PROBLEMS", *["  " + ln for ln in (err or out).strip()[-600:].splitlines()])
        return 3
    say("Self-check: all walls in place. " + " ".join(err.split()))
    return 0


def tools_step(a, specs: dict) -> list:
    if a.get("--tools-from"):
        ks.install_tools(a["--tools-from"])
    if not specs:
        return []
    say("Installing the AI tools into the box (each from its vendor; this takes a few minutes) ...")
    done, failed = kap_tools.install(specs, say)
    return [f"  {t} is not in the box yet ({kc.shown(e, 160)}); try again later:  hubchat update-tools {t}"
            for t, e in failed.items()]


def apply(person: str, v: dict, rt: str, a, specs: dict, py: str) -> int:
    ver = ks.version()
    say(*BANNER, f"Installing kaptyorka {ver} for {person}, personal-laptop mode, macOS ...")
    kr.mkdir(kc.ETC, 0o755)
    tools = [t for t in kap_tools.NAMES if t in specs] or ks.marker().get("tools", [])
    kr.write_file(kc.INSTALLING, f"person={person}\ntools={','.join(tools)}\n", 0o644)
    install_dirs()
    install_code(ver, py)
    install_etc(ver, py)
    note = kap_update.pin_key(kr.write_file)
    if note:
        say(note)
    install_relay_conf(v)
    ks.install_person_files(person, v)
    ks.install_installer()
    own_shebang(py)
    if rt:
        kr.write_file(kc.LIMITS_FILE, f"runtime_max={rt}\n", 0o644)
    elif not os.path.exists(kc.LIMITS_FILE):
        kr.write_file(kc.LIMITS_FILE, "runtime_max=7d\n", 0o644)
    install_services(py)
    ensure_box(person)
    a["_later"] = tools_step(a, specs)
    install_sudoers(person)
    install_daemons(py)
    install_hooks()
    rc = self_check(person)
    if rc == 0:
        os.unlink(kc.INSTALLING)
    return rc


def ask_claude_token(person: str, v: dict) -> None:
    """The box's Claude token, pasted (made with `claude setup-token` run as the person). Asked
    before the invite is used, so stopping here costs no link. [Mac] The Linux installer runs
    setup-token in the box itself (kap_claude_token); on a Mac that needs its own way through the
    box profile and wallcheck, not built yet."""
    if v["claude"] or not sys.stdin.isatty() or os.path.exists(f"{kc.SECRET}/{person}/claude-oauth-token"):
        return
    import getpass
    tok = getpass.getpass("Paste the token from `claude setup-token` (run it as yourself), or Enter to skip: ").strip()
    if tok and not kc.TOKEN_RX.fullmatch(tok):
        raise Refuse("that does not look like a token")
    v["claude"] = tok


def home_700(person: str) -> None:
    """Closes the person's home to kaptyorka's accounts. macOS's own 750 with group staff stays
    (km.home_mode_problems: none of kaptyorka's accounts is in staff, wheel or admin, and the self-
    check makes sure); rights for others or any other group make it 700; ACL entries that allow
    something are taken off (macOS puts only a deny entry there)."""
    home = km.person_home(person).pw_dir
    st = os.stat(home)
    m = st.st_mode & 0o777
    if m & 0o007 or (m & 0o070 and st.st_gid not in km.PRIVILEGED):
        os.chmod(home, 0o700)
        say(f"Your home folder {home} was open to more than you and this Mac's own people (mode {m:o});",
            "it is now 700, so the box and other users cannot look inside. It stays so after a remove.")
    allows = km.acl_allows(km.run(["/bin/ls", "-lde", home], check=False, timeout=10).stdout)
    for i, entry in sorted(allows, reverse=True):
        km.run(["/bin/chmod", "-a#", str(i), home])
        say(f"An ACL entry on your home folder let others in ({entry}); it was taken off. It stays so after a remove.")


def install(a) -> int:
    ks.refuse_unfinished()
    person = a.get("--person") or os.environ.get("SUDO_USER", "")
    kc.check_person(person)
    if not a.get("--personal"):
        raise Refuse("say --personal (a team machine is installed with pipx install kaptyorka and kaptyorka join)")
    link = a.get("link")
    by_hand = [k for k in ks.BY_HAND if a.get(k)]
    if link and by_hand:
        raise Refuse("give the invite link alone: " + " ".join(by_hand) + " are for tests")
    if not link and len(by_hand) < len(ks.BY_HAND):
        raise Refuse("give the personal invite link your team admin sent you, in quotes")
    rt = a.get("--runtime-max") or "7d"
    if not kc.RUNTIME_RX.fullmatch(rt):
        raise Refuse("--runtime-max: like 7d, 12h, 90min or infinity")
    py = a.get("--python") or sys.executable
    say(*BANNER)
    inv = ks.parse_invite(link) if link else None
    if inv:
        ks.ask_name(a)
        if not a.get("--name"):
            raise Refuse("add --name THIS-COMPUTER: the name this computer gets on the hub, such as laptop-2." + ks.UNCHANGED)
    try:
        v = ks.check_local_values(a) if inv else ks.check_relay_values(a)
    except OSError as e:
        raise Refuse(f"cannot read a token file: {e.strerror}")
    stop, warn, fix = preflight(person, py)
    for w in warn:
        say("warning: " + w)
    if stop:
        for s in stop:
            print(kc.term("kaptyorka-secure-setup: " + s), file=sys.stderr)
        if inv:
            say("Nothing was changed on this computer, and the link was not used: fix the above and run this again.")
        return 2
    specs = ks.choose_tools(a, person)
    host = kap_tools.unreachable(specs)
    if host:
        raise Refuse(f"this computer cannot reach {host}, where the AI tools come from: connect it and run this again, "
                     "or install now with --tools none." + ks.UNCHANGED)
    ask_claude_token(person, v)
    if inv:
        say(f"Joining the hub at {inv['host']}:{inv['port']} (fingerprint {inv['fp'][:16]}...) as {v['name']} ...")
        v.update(ks.redeem(inv, v["name"]))
        say(f"Joined: this computer is {v['name']} on the hub (address {v['ip']}).")
    for prefix in fix:
        fix_python(prefix)
        say(f"The Python in {prefix} (and /Library/Frameworks above it) is now writable by root only (python.org",
            "leaves it writable by admins), so no program of yours can change what kaptyorka's root programs run.",
            "pip install --user still works.")
    home_700(person)
    rc = apply(person, v, rt, a, specs, py)
    if rc:
        return rc
    pub = kr.read_text(f"{kc.box_home(person)}/.ssh/id_ed25519.pub").strip()
    have = kap_tools.installed()
    say("", f"Done. This computer is {v['name']} on the hub; team chats reach it only through the box.",
        "AI tools in the box: " + (", ".join(f"{t} {have[t]}" for t in sorted(have)) or "none yet"),
        *a.get("_later", []),
        "Next steps:",
        "  1. Open a new terminal (or log out and in). Until then your own programs get",
        "     \"Permission denied\" in team folders.",
        "  2. Make a folder a team folder:   hubchat add ~/path/to/folder",
        "     or get a repository as one:    hubchat add <git URL>",
        "  3. cd into it and run claude, codex, opencode or cursor-agent as usual.",
        *([] if v["claude"] else ["  Claude in the box has no token yet: run  claude setup-token  as yourself, then",
                                  "  hubchat token-rotate  and paste it."]),
        *([f"  Delete the token file now: rm {kc.shown(a['--claude-token-file'], 200)}"]
          if a.get("--claude-token-file") else []),
        *(["For private repositories: add the box's own SSH key to your git account (as an extra key):",
           "  " + pub] if pub else []),
        f"A copy of this installer stays in {kc.INSTALLER} (rejoin, repair, remove).", *BANNER)
    return 0


def repair(a) -> int:
    half = ks.marker()
    try:
        v = ks.installed_values()
    except Refuse:
        if half:
            raise Refuse(f"the install stopped before it kept the hub's settings: run  sudo {kc.INSTALLER} remove  and "
                         "install again with a new link from your team admin")
        raise
    people = ks.installed_people() or ([half["person"]] if half.get("person") else [])
    if not people:
        raise Refuse("no person is set up on this computer: install it first")
    py = km.python() or sys.executable
    stop, warn, fix = preflight(people[0], py)
    stop = [s for s in stop if "today's kaptyorka" not in s]
    if stop:
        raise Refuse("; ".join(stop))
    for prefix in fix:
        fix_python(prefix)
    rc = 0
    for person in people:
        rc = rc or apply(person, dict(v), "", a, {}, py)
    if rc == 0:
        say("Repaired: every file, account, rule and daemon is as this installer writes them, and the self-check passed.")
    return rc


def rejoin(a) -> int:
    ks.refuse_unfinished()
    old = ks.installed_values()
    inv = ks.parse_invite(a.get("link", ""))
    name = a.get("--name") or old["name"]
    if not ks.CNAME_RX.fullmatch(name):
        raise Refuse("--name: letters, digits, '.', '_', '-', at most 32, not starting with '.' or '-'")
    v = {"name": name, "claude": "", **ks.redeem(inv, name)}
    install_relay_conf(v)
    kr.write_file(km.PF_FILE, km.pf_text(), 0o644)
    km.pf_load()
    km.launchctl("kickstart", "-k", f"system/{km.label('relay')}")
    say(f"Joined again: this computer is {name} on the hub (address {v['ip']}).")
    return 0


def remove(a) -> int:
    people = ks.installed_people()
    if a.get("--person"):
        people = [kc.check_person(a["--person"])]
    delete_home = bool(a.get("--delete-box-home"))
    if not delete_home and not a.get("--yes") and sys.stdin.isatty():
        delete_home = input("Delete the box homes and team memory too? [y/N] ").strip().lower() in ("y", "yes")
    if __package__:
        from . import kap_mac_hubchat as mh
    else:
        import kap_mac_hubchat as mh
    failed = []
    for person in people:
        say(f"Removing the team box of {person} ...")
        try:
            folders = kr.load_folders(person)
        except (Refuse, ValueError, KeyError):
            folders = {}
        for name in sorted(folders):
            try:
                mh.remove(person, name, uninstall=True)
            except (Refuse, OSError) as e:
                failed.append(f"{name}: {e}")
        box = person
        for rec in km.chats(box):
            km.stop_chat(rec)
        try:
            km.run(["/usr/bin/pkill", "-KILL", "-U", str(pwd.getpwnam(kc.box_user(box)).pw_uid)], check=False)
        except KeyError:
            pass
        km.unload_daemon(f"egress.{box}")
        try:
            km.run(["/usr/sbin/dseditgroup", "-o", "edit", "-d", person, "-t", "user", kc.share_group(box)], check=False)
        except Refuse:
            pass
        km.delete_record("Users", kc.box_user(box))
        km.delete_record("Groups", kc.box_user(box))
        km.delete_record("Groups", kc.share_group(box))
        for p in (f"{km.SUDOERS_DIR}/kaptyorka-{person}", f"{km.RUNBOX}/{box}.helper.pid"):
            if os.path.lexists(p):
                os.unlink(p)
        for p in (f"{kc.BOXES}/{box}", km.box_run(box), f"{kc.PEOPLE}/{person}", f"{kc.SECRET}/{person}"):
            shutil.rmtree(p, ignore_errors=True)
        if delete_home:
            shutil.rmtree(kc.box_root(box), ignore_errors=True)
            shutil.rmtree(km.team_root(person), ignore_errors=True)
        try:
            os.unlink(f"{km.person_home(person).pw_dir}/{km.CANARY_HOME}")
        except (OSError, Refuse, KeyError):
            pass
    if not ks.installed_people():
        for name in ("tools-update", "update", "relay", "canary", "pf"):
            km.unload_daemon(name)
        for b in km.boxes():
            km.unload_daemon(f"egress.{b}")
        km.run(["/sbin/pfctl", "-a", km.ANCHOR, "-F", "rules"], check=False)
        tok = kr.read_text(km.PF_TOKEN).strip()
        if tok.isdigit():
            km.run(["/sbin/pfctl", "-X", tok], check=False)
        for s in km.SERVICE_USERS:
            km.delete_record("Users", s)
            km.delete_record("Groups", s)
        for t in HOOK_FILES:
            ks.strip_hook(t)
        for p in kr.read_text(km.CANARY_STATE).split():
            if km.CANARY_SHARED_RX.fullmatch(p) and kr.canary_ok(p):
                os.unlink(p)
        for p in ("/usr/local/bin/hubchat",):
            if os.path.lexists(p):
                os.unlink(p)
        for p in (kc.ETC, kc.OPT, kc.TOOLS_DIR, kc.LIBEXEC, km.RUN, km.RUNBOX, km.LOGS):
            shutil.rmtree(p, ignore_errors=True)
        if delete_home:
            shutil.rmtree(kc.TEAM, ignore_errors=True)
            try:
                os.rmdir(km.TEAMAREA)
            except OSError:
                pass
        km.run(["/usr/bin/dscacheutil", "-flushcache"], check=False)
    say("Removed." if not failed else "Removed, except:", *[f"  {f}" for f in failed],
        "Open a new terminal: then claude, codex, opencode and cursor-agent are your own again everywhere.",
        *([] if delete_home else ["The box homes and the team memory were kept. To delete them:",
                                  f"  sudo rm -rf {kc.TEAM} {km.TEAMAREA}"]),
        "Your home folder keeps its mode (700 if the install closed it); macOS's own is 750: chmod 750 ~ gives it back.",
        "Last, end what the box used:",
        "  - the box's Codex and Cursor sign-ins: on each vendor's account page",
        "  - the box's SSH key: in your git account",
        "  - this computer on the hub: ask your team admin",
        *kc.REVOKE_CLAUDE_TOKEN)
    return 1 if failed else 0


def update_tools(a, pos) -> int:
    ks.refuse_unfinished()
    ks.installed_values()
    specs = kap_tools.parse_specs(",".join(pos)) if pos else {t: "" for t in kap_tools.installed()}
    if not specs:
        raise Refuse("no AI tool is in the box yet: name the ones you want, such as  update-tools claude codex")
    done, failed = kap_tools.update(specs, say)
    say(*[f"{t}: now {ver}" for t, ver in done.items()], *[f"{t}: not changed ({e})" for t, e in failed.items()])
    return 1 if failed else 0


def main(argv) -> int:
    a, pos = {}, []
    it = iter(argv)
    for x in it:
        if x in ("--personal", "--delete-box-home", "--yes"):
            a[x] = True
        elif x.startswith("--"):
            a[x] = next(it, "")
        else:
            pos.append(x)
    try:
        if os.geteuid() != 0:
            raise Refuse("run it with sudo")
        if pos[:1] == ["install"] and len(pos) <= 2:
            if len(pos) == 2:
                a["link"] = pos[1]
            return install(a)
        if pos[:1] == ["rejoin"] and len(pos) == 2:
            a["link"] = pos[1]
            return rejoin(a)
        if pos == ["repair"]:
            return repair(a)
        if pos[:1] == ["update-tools"]:
            return update_tools(a, pos[1:])
        if pos == ["remove"]:
            return remove(a)
        print(ks.USAGE + "\n\n" + km.TEST_NOTICE + "\nmacOS: --python PATH names the Python 3.10+ kaptyorka runs with "
              "(default: the one running this installer).")
        return 2
    except BrokenPipeError:
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return 0
    except Refuse as e:
        print(kc.term(f"kaptyorka-secure-setup: {e}"), file=sys.stderr)
        return 2
