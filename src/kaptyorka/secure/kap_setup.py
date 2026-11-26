#!/usr/bin/python3 -I
"""kaptyorka-secure-setup (secure design v2, 9.1, 9.2, 9.6): install and remove the personal-laptop
mode. Root, run once by the person: sudo python3 -I kap_setup.py install --personal ...

  install --personal [--person NAME] --hub-ip IP --hub-port N --hub-sha256 HEX --name NAME
          --device-token-file FILE [--claude-token-file FILE] [--runtime-max 7d] [--tools-from DIR]
  remove [--person NAME] [--delete-box-home] [--yes]

install: checks the computer; makes the box user and groups; writes the files of 4.2 (root
programs, launcher, box configs, units); installs relay, forwarder and box code; makes the
namespace, relay door, forwarder, canary; stores the Claude token root-only; makes the box's SSH
key; adds the launcher to the system shell files; runs the self-check. The mode is fixed here
(/etc/kaptyorka/mode = personal) and never changes afterwards.

remove: hands every team folder back (made safe, as hubchat remove), stops and deletes every
unit, file, user and group it made, the namespace and the launcher lines.

Every outside value is checked against a strict pattern before root uses it. Fetching the device
token from an invite and the tools' own sign-ins are later slices (7 and 5): here they are files.
"""
import grp
import ipaddress
import json
import os
import pwd
import re
import secrets
import shutil
import subprocess
import sys

if __package__:
    from . import kapcommon as kc, kaproot as kr
else:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import kapcommon as kc
    import kaproot as kr

Refuse = kc.Refuse
HERE = os.path.dirname(os.path.abspath(__file__))
FILES = os.path.join(HERE, "files")
RELAY_SRC = os.path.join(os.path.dirname(HERE), "relay")
SYSTEMD = kr.SYSTEMD
HOOK_BEGIN, HOOK_END = "# >>> kaptyorka >>>", "# <<< kaptyorka <<<"
FP_RX = re.compile(r"[0-9a-f]{64}")
CNAME_RX = re.compile(r"[A-Za-z0-9._-]{1,32}")
TOKEN_RX = re.compile(r"[A-Za-z0-9_-]{20,400}")
PROGRAMS = (  # (source in this folder, name in /usr/local/libexec/kaptyorka, mode)
    ("kap_enter.py", "enter", 0o755), ("kap_hubchat.py", "hubchat-root", 0o755),
    ("kap_hubchat.py", "kap_hubchat.py", 0o644), ("kap_wallcheck.py", "wallcheck", 0o755),
    ("kap_box_exec.py", "box-exec", 0o755), ("kap_canary.py", "canary", 0o755),
    ("kap_setup.py", "kaptyorka-secure-setup", 0o755), ("kapcommon.py", "kapcommon.py", 0o644),
    ("kaproot.py", "kaproot.py", 0o644), ("files/launch", "launch", 0o755),
    ("files/kapbox-base", "kapbox-base", 0o755),
)
OUR_UNITS = ("kapbox-base@.service", "kaptyorka-egress@.socket", "kaptyorka-egress@.service",
             "kaptyorka-canary.service", "kaptyorka-relay.service", "kaptyorka-relay-box@.socket")


def version() -> str:
    try:
        m = re.search(r'__version__\s*=\s*"([0-9][0-9A-Za-z.+-]{0,30})"',
                      open(os.path.join(os.path.dirname(HERE), "__init__.py")).read())
        return m.group(1) if m else "0"
    except OSError:
        return kr.read_text(f"{kc.ETC}/version").strip() or "0"


def say(*a):
    kr.say(*a)


# ---------------------------------------------------------------------------- checks (9.2)

def preflight(person: str):
    """(reasons to stop, warnings)."""
    stop, warn = [], []
    r = kr.run(["systemctl", "--version"], check=False)
    m = re.match(r"systemd (\d+)", r.stdout)
    if not m or int(m.group(1)) < 254:
        stop.append("systemd 254 or newer is needed (OpenFile=, systemd-run --expand-environment)")
    if not os.path.exists("/sys/fs/cgroup/cgroup.controllers"):
        stop.append("cgroup v2 is needed")
    rel = os.uname().release.split(".")
    if (int(rel[0]), int(re.sub(r"\D.*", "", rel[1]) or 0)) < (5, 10):
        stop.append("Linux 5.10 or newer is needed (nosymfollow)")
    if sys.version_info < (3, 10):
        stop.append("Python 3.10 or newer is needed")
    for tool in ("ip", "nsenter", "ss", "setpriv", "useradd", "visudo", "mountpoint"):
        if not shutil.which(tool, path=kr.CLEAN_ENV["PATH"]):
            stop.append(f"the program {tool} is missing")
    mode = kr.read_text(kc.MODE_FILE).strip()
    if mode and mode != "personal":
        stop.append(f"this computer was installed as {mode}; the mode is fixed at install")
    try:
        pw = pwd.getpwnam(person)
    except KeyError:
        return [f"there is no user {person}"], warn
    if pw.pw_uid < 1000 or pw.pw_dir != f"/home/{person}" or os.path.realpath(pw.pw_dir) != pw.pw_dir:
        stop.append(f"{person}'s home must really be /home/{person}")
    elif (os.stat(pw.pw_dir).st_mode & 0o777) not in (0o700, 0o750):
        stop.append(f"{person}'s home is open to others: run  chmod 700 {pw.pw_dir}")
    old = [p for p in ("/.kaptyorka", "/.local/bin/kaptyorka", "/.local/share/pipx/venvs/kaptyorka",
                       "/.config/systemd/user/kaptyorka.service", "/.config/systemd/user/kaptyorka-bridge.service")
           if os.path.lexists(pw.pw_dir + p)]
    for rc in (".bashrc", ".zshrc", ".bash_aliases", ".profile", ".config/fish/config.fish"):
        text = _read_nofollow(f"{pw.pw_dir}/{rc}")
        if re.search(r"^\s*(alias\s+(claude|codex|opencode|cursor-agent|git)=|(function\s+)?(claude|codex|opencode|cursor-agent)\s*\(\))",
                     text, re.M):
            stop.append(f"{pw.pw_dir}/{rc} defines its own claude/codex/opencode/cursor-agent; it would win "
                        "over the launcher: remove it first")
        if "kaptyorka" in text:
            old.append(f"~/{rc} mentions kaptyorka")
    if old:
        stop.append("today's kaptyorka is still in this account (" + ", ".join(old[:4]) +
                    "): run  kaptyorka leave  and  kaptyorka remove  first")
    for g in ("docker", "sudo", "lxd", "adm", "disk", "wheel"):
        try:
            if person in grp.getgrnam(g).gr_mem:
                warn.append(f"your account is in group {g}, so it can become root. The box still protects "
                            "you from kaptyorka, because no kaptyorka code runs as you.")
        except KeyError:
            pass
    for p in ("/etc/claude-code/managed-settings.json", "/etc/codex/requirements.toml", "/etc/opencode"):
        if os.path.exists(p):
            warn.append(f"{p} exists; it applies to the box too. Left alone.")
    return stop, warn


def _read_nofollow(path: str) -> str:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return ""
    with os.fdopen(fd, "rb") as f:
        return f.read(1 << 20).decode("utf-8", "replace")


# ---------------------------------------------------------------------------- install

def copy(src: str, dst: str, mode: int) -> None:
    with open(src, "rb") as f:
        kr.write_file(dst, f.read(), mode)


def install_code(ver: str) -> None:
    kr.mkdir("/usr/local/libexec", 0o755)
    kr.mkdir(kc.LIBEXEC, 0o755)
    for src, name, mode in PROGRAMS:
        copy(os.path.join(HERE, src), f"{kc.LIBEXEC}/{name}", mode)
    copy(os.path.join(FILES, "hubchat"), "/usr/local/bin/hubchat", 0o755)
    kr.mkdir(kc.OPT, 0o755)
    for part, files in (("egress", {"kap_egress.py": "egress.py"}), ("box", {"kap_proxybridge.py": "proxybridge.py"}),
                        ("relay", {os.path.join(RELAY_SRC, "relay.py"): "relay.py"})):
        kr.mkdir(f"{kc.OPT}/{part}", 0o755)
        kr.mkdir(f"{kc.OPT}/{part}/{ver}", 0o755)
        for src, name in files.items():
            copy(os.path.join(HERE, src), f"{kc.OPT}/{part}/{ver}/{name}", 0o644)
        cur = f"{kc.OPT}/{part}/current"
        tmp = cur + ".new"
        if os.path.lexists(tmp):
            os.unlink(tmp)
        os.symlink(ver, tmp)
        os.replace(tmp, cur)
    kr.mkdir(kc.TOOLS_DIR, 0o755)
    kr.mkdir(kc.TOOLS_DIR + "/bin", 0o755)


def install_tools(src: str) -> None:
    """Copy a prepared tools folder (bin/<tool> plus whatever they need) to /opt/kap-tools,
    root-owned and not writable by anyone else: the box cannot change its own tools (5.2)."""
    for dirpath, dirs, files in os.walk(src):
        rel = os.path.relpath(dirpath, src)
        dst = os.path.normpath(os.path.join(kc.TOOLS_DIR, rel))
        kr.mkdir(dst, 0o755)
        for n in files:
            s, d = os.path.join(dirpath, n), os.path.join(dst, n)
            if os.path.islink(s):
                if os.path.lexists(d):
                    os.unlink(d)
                os.symlink(os.readlink(s), d)
            else:
                copy(s, d, (os.stat(s).st_mode & 0o755) | 0o444)


def install_etc(ver: str) -> None:
    kr.mkdir(kc.ETC, 0o755)
    kr.write_file(f"{kc.ETC}/version", ver + "\n")
    for d in ("box", "box/claude", "box/codex", "box/opencode", "box/cursor"):
        kr.mkdir(f"{kc.ETC}/{d}", 0o755)
    for dirpath, dirs, files in os.walk(os.path.join(FILES, "box")):
        rel = os.path.relpath(dirpath, os.path.join(FILES, "box"))
        for n in files:
            copy(os.path.join(dirpath, n), os.path.normpath(f"{kc.BOXCONF}/{rel}/{n}"), 0o644)
    copy(os.path.join(FILES, "launcher.sh"), f"{kc.ETC}/launcher.sh", 0o644)
    copy(os.path.join(FILES, "launcher.fish"), f"{kc.ETC}/launcher.fish", 0o644)
    for d in (kc.PEOPLE, kc.BOXES):
        kr.mkdir(d, 0o755)
    kr.mkdir(kc.SECRET, 0o700)
    if not os.path.exists(kc.MODE_FILE):
        kr.write_file(kc.MODE_FILE, "personal\n", 0o444)


def install_units() -> None:
    for n in os.listdir(os.path.join(FILES, "units")):
        copy(os.path.join(FILES, "units", n), f"{SYSTEMD}/{n}", 0o644)
    for n in ("kaptyorka-relay.service", "kaptyorka-relay-box@.socket"):
        copy(os.path.join(RELAY_SRC, "units", n), f"{SYSTEMD}/{n}", 0o644)
    copy(os.path.join(FILES, "kaptyorka-tmpfiles.conf"), "/etc/tmpfiles.d/kaptyorka.conf", 0o644)
    kr.systemctl("daemon-reload")
    kr.run(["systemd-tmpfiles", "--create", "/etc/tmpfiles.d/kaptyorka.conf"], check=False)
    kr.systemctl("enable", "--now", "kaptyorka-canary.service")


def check_relay_values(a) -> dict:
    """Every outside value, checked before anything is written (9.2, I-1)."""
    try:
        ip = str(ipaddress.ip_address(a["--hub-ip"]))
    except ValueError:
        raise Refuse("--hub-ip must be an IP address")
    port = a["--hub-port"]
    if not port.isdigit() or not 0 < int(port) < 65536:
        raise Refuse("--hub-port must be a port number")
    fp = a["--hub-sha256"].lower().replace(":", "")
    if not FP_RX.fullmatch(fp):
        raise Refuse("--hub-sha256 must be 64 hex characters")
    if not CNAME_RX.fullmatch(a["--name"]):
        raise Refuse("--name: letters, digits, '.', '_', '-', at most 32")
    tok = open(a["--device-token-file"]).read().strip()
    if not TOKEN_RX.fullmatch(tok):
        raise Refuse("the device token does not look like one")
    out = {"ip": ip, "port": int(port), "fp": fp, "name": a["--name"], "token": tok, "claude": ""}
    if a.get("--claude-token-file"):
        out["claude"] = open(a["--claude-token-file"]).read().strip()
        if not TOKEN_RX.fullmatch(out["claude"]):
            raise Refuse("the Claude token does not look like one (claude setup-token prints it)")
    if a.get("--tools-from") and not os.path.isdir(os.path.join(a["--tools-from"], "bin")):
        raise Refuse("--tools-from needs a folder with bin/<tool>")
    return out


def install_relay_conf(v: dict) -> None:
    kr.write_file(f"{kc.ETC}/relay.conf", f"hub_ip={v['ip']}\nhub_port={v['port']}\nhub_sha256={v['fp']}\n"
                                          f"name={v['name']}\nversion={version()}\nidle_exit=300\n", 0o644)
    kr.write_file(f"{kc.SECRET}/device-token", v["token"] + "\n", 0o600)
    if not os.path.exists(f"{kc.SECRET}/chat-key"):
        kr.write_file(f"{kc.SECRET}/chat-key", secrets.token_hex(32) + "\n", 0o600)
    d = f"{SYSTEMD}/kaptyorka-relay.service.d"
    kr.mkdir(d, 0o755)
    kr.write_file(f"{d}/hub.conf", f"[Service]\nIPAddressAllow={v['ip']}\n", 0o644)
    kr.systemctl("daemon-reload")


def install_hooks() -> None:
    hook = open(os.path.join(FILES, "shell-hook.sh")).read()
    kr.write_file("/etc/profile.d/kaptyorka.sh", hook, 0o644)
    targets = ["/etc/bash.bashrc"] + (["/etc/zsh/zshenv"] if os.path.isdir("/etc/zsh") else [])
    for t in targets:
        text = kr.read_text(t)
        if HOOK_BEGIN not in text:
            kr.write_file(t, text + ("" if text.endswith("\n") or not text else "\n") + hook, 0o644)
    if os.path.isdir("/etc/fish/conf.d"):
        kr.write_file("/etc/fish/conf.d/kaptyorka.fish", f"{HOOK_BEGIN}\nsource {kc.ETC}/launcher.fish\n{HOOK_END}\n")


def install_person(person: str, v: dict) -> None:
    kr.mkdir(f"{kc.PEOPLE}/{person}", 0o755)
    if not os.path.exists(f"{kc.PEOPLE}/{person}/folders.json"):
        kr.save_folders(person, {})
    kr.mkdir(f"{kc.SECRET}/{person}", 0o700)
    if v["claude"]:
        kr.write_file(f"{kc.SECRET}/{person}/claude-oauth-token", v["claude"] + "\n", 0o600)
    elif sys.stdin.isatty() and not os.path.exists(f"{kc.SECRET}/{person}/claude-oauth-token"):
        import getpass
        tok = getpass.getpass("Paste the token from `claude setup-token` (run it as yourself), or Enter to skip: ").strip()
        if tok:
            if not TOKEN_RX.fullmatch(tok):
                raise Refuse("that does not look like a token")
            kr.write_file(f"{kc.SECRET}/{person}/claude-oauth-token", tok + "\n", 0o600)
    kr.ensure_box(person, person, kr.read_text(f"{kc.BOXCONF}/egress-base"))
    sudoers = (f"# kaptyorka (secure design v2, 4.2): {person} may start team chats and use hubchat. Nothing else.\n"
               f"{person} ALL=(root) NOPASSWD: {kc.ENTER}, {kc.HUBCHAT_ROOT}\n")
    tmp = f"/etc/sudoers.d/.kaptyorka-{person}.new"
    kr.write_file(tmp, sudoers, 0o440)
    kr.run(["visudo", "-cf", tmp])
    os.replace(tmp, f"/etc/sudoers.d/kaptyorka-{person}")


def install(a) -> int:
    person = a.get("--person") or os.environ.get("SUDO_USER", "")
    kc.check_person(person)
    if not a.get("--personal"):
        raise Refuse("say --personal (a team machine is installed with pipx install kaptyorka and kaptyorka join)")
    for k in ("--hub-ip", "--hub-port", "--hub-sha256", "--name", "--device-token-file"):
        if not a.get(k):
            raise Refuse(f"{k} is needed")
    rt = a.get("--runtime-max") or "7d"
    if not kc.RUNTIME_RX.fullmatch(rt):
        raise Refuse("--runtime-max: like 7d, 12h, 90min or infinity")
    try:
        v = check_relay_values(a)
    except OSError as e:
        raise Refuse(f"cannot read a token file: {e.strerror}")
    stop, warn = preflight(person)
    for w in warn:
        say("warning: " + w)
    if stop:
        for s in stop:
            print("kaptyorka-secure-setup: " + s, file=sys.stderr)
        return 2
    ver = version()
    say(f"Installing kaptyorka {ver} for {person}, personal-laptop mode ...")
    install_code(ver)
    if a.get("--tools-from"):
        install_tools(a["--tools-from"])
    install_etc(ver)
    kr.write_file(kc.LIMITS_FILE, f"runtime_max={rt}\n", 0o644)
    install_units()
    install_relay_conf(v)
    install_hooks()
    install_person(person, v)
    problems = kr.outside_check(person, [person])
    if problems:
        say("Self-check: PROBLEMS", *[f"  {p}" for p in problems])
        return 3
    kr.ensure_box_running(person)
    r = kr.run(["systemd-run", "--quiet", "--collect", "--pipe", "--wait", "--expand-environment=no",
                f"--working-directory={kc.view_root(person)}", *kr.props_args(person, person), "--",
                kc.WALLCHECK, "--verbose", "--box", person, "--person", person,
                "--addrs", ",".join(kr.host_addrs()), "--", "/bin/true"], check=False)
    if r.returncode != 0:
        say("Self-check (inside the box): PROBLEMS", "  " + (r.stderr or r.stdout).strip()[-400:])
        return 3
    say("Self-check: all walls in place. " + r.stderr.strip())
    pub = kr.read_text(f"{kc.box_home(person)}/.ssh/id_ed25519.pub").strip()
    say("", "Done. Log out and in once, so your account joins its new group.",
        "Next:  hubchat add <folder>   (or: hubchat add <git URL>)",
        *(["The box's own git SSH key (add it to your git account as an extra key):", "  " + pub] if pub else []))
    return 0


# ---------------------------------------------------------------------------- remove (9.6)

def strip_hook(path: str) -> None:
    text = kr.read_text(path)
    if HOOK_BEGIN not in text:
        return
    out, skip = [], False
    for line in text.splitlines(keepends=True):
        if line.startswith(HOOK_BEGIN):
            skip = True
        elif skip and line.startswith(HOOK_END):
            skip = False
            continue
        if not skip:
            out.append(line)
    kr.write_file(path, "".join(out), 0o644)


def remove(a) -> int:
    if os.geteuid() != 0:
        raise Refuse("run it with sudo")
    people = sorted(p for p in os.listdir(kc.PEOPLE) if kc.PERSON_RX.fullmatch(p)) if os.path.isdir(kc.PEOPLE) else []
    if a.get("--person"):
        people = [kc.check_person(a["--person"])]
    delete_home = bool(a.get("--delete-box-home"))
    if not delete_home and not a.get("--yes") and sys.stdin.isatty():
        delete_home = input("Delete the box homes too (team memory, the box's sign-ins and SSH key)? [y/N] ") \
            .strip().lower() in ("y", "yes")
    hc = _hubchat()
    failed = []
    for person in people:
        say(f"Removing the team box of {person} ...")
        try:
            folders = kr.load_folders(person)
        except (Refuse, ValueError, KeyError):
            folders = {}
        for name in sorted(folders):
            try:
                hc.remove(person, name)
            except (Refuse, OSError) as e:
                failed.append(f"{name}: {e}")
        for box in sorted(kr.boxes_of(person), key=lambda b: b == person):   # separate boxes first
            kr.drop_box(person, box, delete_home)
        for p in (f"/etc/sudoers.d/kaptyorka-{person}",):
            if os.path.lexists(p):
                os.unlink(p)
        for p in (f"{kc.PEOPLE}/{person}", f"{kc.SECRET}/{person}"):
            shutil.rmtree(p, ignore_errors=True)
        try:
            os.rmdir(kc.view_root(person))
        except OSError:
            pass
    left = [p for p in os.listdir(kc.PEOPLE)] if os.path.isdir(kc.PEOPLE) else []
    if not left:
        kr.systemctl("disable", "--now", "kaptyorka-canary.service", check=False)
        kr.systemctl("stop", "kaptyorka-relay.service", check=False)
        for n in OUR_UNITS:
            if os.path.lexists(f"{SYSTEMD}/{n}"):
                os.unlink(f"{SYSTEMD}/{n}")
        shutil.rmtree(f"{SYSTEMD}/kaptyorka-relay.service.d", ignore_errors=True)
        for p in ("/etc/tmpfiles.d/kaptyorka.conf", "/etc/profile.d/kaptyorka.sh", "/etc/fish/conf.d/kaptyorka.fish",
                  "/usr/local/bin/hubchat", *kc.CANARY_FILES):
            if os.path.lexists(p):
                os.unlink(p)
        for sl in ("kapbox.slice", "system-kapbox\\x2dbase.slice", "system-kaptyorka\\x2degress.slice",
                   "system-kaptyorka\\x2drelay\\x2dbox.slice"):   # parents of the units above, now empty
            kr.systemctl("stop", sl, check=False)
        kr.systemctl("daemon-reload", check=False)
        kr.systemctl("reset-failed", check=False)
        for t in ("/etc/bash.bashrc", "/etc/zsh/zshenv"):
            strip_hook(t)
        for p in (kc.ETC, kc.OPT, kc.TOOLS_DIR, kc.LIBEXEC, kc.RUNBOX, "/run/kaptyorka"):
            shutil.rmtree(p, ignore_errors=True)
        for p in (kc.VIEW, kc.TEAM) if delete_home else (kc.VIEW,):
            if p == kc.TEAM:
                shutil.rmtree(p, ignore_errors=True)
            else:
                try:
                    os.rmdir(p)
                except OSError:
                    pass
    say("Removed." if not failed else "Removed, except:", *[f"  {f}" for f in failed],
        *([] if delete_home else [f"The box homes were kept in {kc.TEAM}; delete them with sudo when you are sure."]),
        "Now revoke what the box used: the Claude token at claude.ai (Settings, Claude Code), the box's",
        "Codex, Cursor and OpenCode sessions in each vendor's account page, the box's SSH key in your git",
        "account, and ask an admin to remove this computer on the hub page.")
    return 1 if failed else 0


def _hubchat():
    if __package__:
        from . import kap_hubchat
    else:
        import kap_hubchat
    return kap_hubchat


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
        if pos == ["install"]:
            return install(a)
        if pos == ["remove"]:
            return remove(a)
        print(__doc__.split("\n\n")[1])
        return 2
    except Refuse as e:
        print(f"kaptyorka-secure-setup: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
