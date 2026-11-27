"""kap_mac: the macOS side of a personal laptop's box (secure design v2, section 10; the wall kit's
CI results in mac-kit/ci-results). TEST VERSION: not proven yet.

macOS has no systemd, no namespaces and no per-mount flags. The same picture is built from:
  - a hidden second user kap-<person> (dscl), its own group, not staff, no password, and a share
    group kapshare-<person> of exactly the person and the box;
  - the person's home at mode 700 (macOS makes it 755), checked at every start;
  - `enter` (root, through the person's sudo rule), which checks every wall from outside, then
    starts the tool as the box user, on a pseudo-terminal of its own, inside a Seatbelt profile
    (sandbox-exec): files, network, processes, the clipboard. The kit measured that the profile is
    REQUIRED: without it the box sees the person's process arguments (ps);
  - a pf anchor (com.apple/kaptyorka) with user rules: the box reaches only its forwarder's port
    on loopback, the relay only the hub, the forwarder only ports 443 and 22 of public addresses;
  - LaunchDaemons for the relay (_kaptyorka), each box's forwarder (_kapproxy), the canaries
    (nobody), pf at boot and kaptyorka's own signed updates.
Team folders live at one real place, /Users/Shared/kaptyorka/<person>/<name>, for the person and
the box alike: macOS has no mount that could give the person a view without symlinks and
programs (the honest gap; README and the overview's limits, [Mac]).

The pure parts (profile, pf rules, plists, sudoers text, paths) are tested on Linux
(tests/test_secure_mac.py); the rest only on a Mac (tests/mac-box/run.sh in CI).
Python standard library only; installed as /usr/local/libexec/kaptyorka/kap_mac.py.
"""
import fcntl
import ipaddress
import json
import os
import plistlib
import pwd
import grp
import re
import secrets
import signal
import socket
import stat
import subprocess
import sys
import time
from typing import Dict, List, Optional, Tuple

if __package__:
    from . import kapcommon as kc, kaproot as kr
else:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import kapcommon as kc
    import kaproot as kr

Refuse = kc.Refuse

TEST_NOTICE = ("macOS: TEST VERSION, not proven yet. Its walls rest on Seatbelt and pf, which Apple does not "
               "support for this use, and a macOS update can change them. Every team chat start checks them and "
               "refuses to start when one is missing.")

# ---------------------------------------------------------------------------- places
# The same as on Linux where macOS has the place; else the Mac's own. Seatbelt matches real paths,
# so each has its real form too (/etc, /var and /tmp are links into /private).
LIBEXEC, ETC, OPT, TOOLS_DIR = kc.LIBEXEC, kc.ETC, kc.OPT, kc.TOOLS_DIR
TEAMAREA = "/Users/Shared/kaptyorka"          # TEAMAREA/<person>/<name>: one real place for both
RUN = kc.RUN                                  # /private/var/run/kaptyorka, root 755: doors, canaries, chat records, locks
RUNBOX = kc.RUNBOX                            # RUNBOX/<box>: the box's own runtime folder (0700)
RELAY_DIR = RUN + "/relay"
BOX_SECRETS = RUN + "/box-secrets"
CHATS = RUN + "/chats"                        # root 0700: one record per running chat (pid.json)
LOGS = "/private/var/log/kaptyorka"
PYTHON_FILE = ETC + "/python"                 # the interpreter every root program and the box use
PROFILE_EXTRA = ETC + "/box-extra.sb"         # root-only local additions to the box profile
PF_FILE = ETC + "/pf.anchor"
PF_TOKEN = ETC + "/secret/pf.token"
ANCHOR = "com.apple/kaptyorka"
LAUNCHD = "/Library/LaunchDaemons"
LABEL = "com.kaptyorka"
RELAY_USER, PROXY_USER, TOOLS_USER = "_kaptyorka", "_kapproxy", "_kaptools"
SERVICE_USERS = (RELAY_USER, PROXY_USER, TOOLS_USER)
MARK = "kaptyorka (secure design v2): made by kaptyorka-secure-setup"   # RealName of every record we make
WALLCHECK = LIBEXEC + "/wallcheck"
BOX_EXEC = LIBEXEC + "/box-exec"
TTY_FILTER = LIBEXEC + "/tty-filter"
ENTER = kc.ENTER
HUBCHAT_ROOT = kc.HUBCHAT_ROOT
DAEMON = LIBEXEC + "/kap_mac_daemon.py"
PROXY_PORT_BASE = 47110                       # box forwarder ports: PROXY_PORT_BASE + n
# One loopback port per box for OpenCode: an OpenCode 1 window's own server (--port) or the box's
# OpenCode 2 service. Only the box may bind it (Seatbelt: localhost only, this one port) and only
# the box may reach it (pf: every other user's packets to it, and any other owner's answers from
# it, are dropped). Nothing else on loopback is the box's.
OPENCODE_PORT_BASE = 47310                    # OPENCODE_PORT_BASE + n, a range of its own
PORT_SPAN = 200
CANARY_TCP_LO, CANARY_TCP_ANY = kc.CANARY_TCP_LO, kc.CANARY_TCP_ANY
CANARY_SOCK = RUN + "/canary.sock"
CANARY_PID = RUN + "/canary.pid"
CANARY_STATE = RUN + "/wall-canaries"
CANARY_FIXED = ETC + "/kaptyorka-wall-canary"         # root's place the box must not read (a carve-out of /etc)
CANARY_HOME = ".kaptyorka-wall-canary"        # in the person's home
CANARY_SHARED_DIRS = ("/private/tmp", "/private/var/tmp", "/Users/Shared")
CANARY_SHARED_RX = re.compile(r"/(private/tmp|private/var/tmp|Users/Shared)/kaptyorka-wall-canary-[0-9a-f]{16}")
SUDOERS_DIR = "/private/etc/sudoers.d"
PF_CONF = "/private/etc/pf.conf"
SHELL_FILES = ("/private/etc/zshenv", "/private/etc/bashrc", "/private/etc/profile")   # the launcher hook
CANARY_SHM_KEY = kc.CANARY_SHM_KEY
MIN_MACOS = (14, 0)
ID_RANGE = (401, 499)                         # hidden accounts (below 500: not on the login window)
PY_RX = re.compile(r"/[A-Za-z0-9/._+-]{1,200}")
SB_SAFE = re.compile(r"[A-Za-z0-9/._+ -]{1,400}")
# mach services a tool needs (the kit's proven list) and the ones that reach the desktop session
MACH_ALLOW = ("com.apple.system.opendirectoryd.libinfo", "com.apple.logd", "com.apple.system.notification_center",
              "com.apple.trustd", "com.apple.SecurityServer", "com.apple.system.logger", "com.apple.cfprefsd.daemon",
              "com.apple.bsd.dirhelper")
MACH_DENY = ("com.apple.pasteboard.1", "com.apple.CARenderServer", "com.apple.windowserver.active",
             "com.apple.coreservices.appleevents")
# read-only places of the computer a tool needs (the kit's list, /opt and /Applications narrowed)
SYSTEM_READ = ("/usr", "/bin", "/sbin", "/System", "/Library", "/private/etc", "/private/var/db", "/dev")
# ... and the parts of them the box must not read, even where Unix rights would let it
SYSTEM_DENY = ("/private/etc/kaptyorka", "/Library/Keychains", "/Library/Preferences/SystemConfiguration",
               "/private/var/db/dslocal", "/private/var/db/sudo", "/private/etc/sudoers.d")


def real(path: str) -> str:
    """The real path Seatbelt sees: /etc, /var and /tmp are links into /private on macOS."""
    for top in ("/etc", "/var", "/tmp"):
        if path == top or path.startswith(top + "/"):
            return "/private" + path
    return path


def team_root(person: str) -> str:
    return f"{TEAMAREA}/{kc.check_person(person)}"


def team_path(person: str, name: str) -> str:
    return f"{team_root(person)}/{kc.check_name(name, allow_memory=True)}"


def team_folder(cwd: str, person: str) -> Optional[Tuple[str, str]]:
    """(name, path below it) when the real folder cwd is a team folder of this person, by place
    only: TEAMAREA/<person>/<name>, a root-owned folder nobody else can put a folder in."""
    kc.check_person(person)
    if not isinstance(cwd, str) or not cwd.startswith("/") or "\0" in cwd:
        return None
    base = team_root(person) + "/"
    if not cwd.startswith(base):
        return None
    name, _, sub = cwd[len(base):].partition("/")
    if name != kc.MEMORY and not kc.NAME_RX.fullmatch(name):
        return None
    if sub and any(p in ("", ".", "..") for p in sub.rstrip("/").split("/")):
        return None
    return name, sub.rstrip("/")


def box_run(box: str) -> str:
    return f"{RUNBOX}/{kc.check_box(box)}"


def box_tmp(box: str) -> str:
    return kc.box_root(box) + "/tmp"


def relay_door(box: str) -> str:
    return f"{RELAY_DIR}/{kc.check_box(box)}.sock"


def profile_path(box: str) -> str:
    return f"{kc.BOXES}/{kc.check_box(box)}/box.sb"


def label(name: str) -> str:
    return f"{LABEL}.{name}"


def plist_path(name: str) -> str:
    return f"{LAUNCHD}/{label(name)}.plist"


# ---------------------------------------------------------------------------- the Seatbelt profiles (pure)

def sb(path: str) -> str:
    """A path for a profile: absolute, plain characters only (no quote, backslash or line break
    can reach the profile's text)."""
    if not isinstance(path, str) or not path.startswith("/") or not SB_SAFE.fullmatch(path) or "/../" in path + "/":
        raise Refuse(f"not a path for the box profile: {kc.shown(path)}")
    return '"' + path + '"'


def box_profile(box: str, person: str, proxy_port: int, darwin_dir: str = "", extra: str = "",
                py_top: str = "", opencode_port: int = 0) -> str:
    """The box's Seatbelt profile (06 wall 6, 07 10.1; the kit's strict profile, measured on macOS
    15.8 and 26.7): deny by default; read the system's read-only places but not kaptyorka's own
    files, keychains and the account database; read and write only the team area, the box's
    state folders, its temp folders and runtime folder; the locked tool settings not even there;
    network only to the forwarder's port on loopback and the relay's door, and the box's own
    OpenCode port (bind on localhost, accept, connect: that one port only); no clipboard, screen
    or Apple Events; processes and signals only inside the same sandbox."""
    kc.check_box(box)
    kc.check_person(person)
    if not isinstance(proxy_port, int) or not 1024 < proxy_port < 65536:
        raise Refuse("bad forwarder port")
    if opencode_port and (not isinstance(opencode_port, int) or not 1024 < opencode_port < 65536
                          or opencode_port == proxy_port):
        raise Refuse("bad OpenCode port")
    home = real(kc.box_home(box))
    team = team_root(person)
    rw = [f"{home}/{d}" for d in kc.STATE_DIRS] + [real(box_tmp(box)), real(box_run(box)), team]
    if darwin_dir:
        rw.append(darwin_dir)
    locked = [f"{home}/{dst}" for _, dst in kc.LOCKED_FILES] + \
             [f"{home}/{kc.OPENCODE_CFG}/{d}" for d in kc.OPENCODE_LOCKED_DIRS]
    own = real(f"{kc.BOXES}/{box}")
    lines = [
        f";; kaptyorka box profile for kap-{box} (written by root at every start; TEST VERSION)",
        "(version 1)",
        "(deny default)",
        '(import "system.sb")',
        "(allow process-exec*)",
        "(allow process-fork)",
        "(allow sysctl-read)",
        "(allow file-read-metadata)",
        "(allow file-read* " + " ".join(f"(subpath {sb(p)})" for p in SYSTEM_READ + (OPT + "/box", TOOLS_DIR) +
                                                ((py_top,) if py_top else ())) + ")",
        "(deny file-read* " + " ".join(f"(subpath {sb(p)})" for p in SYSTEM_DENY) + ")",
        f"(allow file-read* (subpath {sb(real(kc.BOXCONF))}) (subpath {sb(own)}))",
        f"(allow file-read* (subpath {sb(home)}) (subpath {sb(real(kc.box_root(box)))}))",
        "(allow file-read* file-write* " + " ".join(f"(subpath {sb(p)})" for p in rw) + ")",
        "(deny file-write* " + " ".join(f"(literal {sb(p)})" for p in locked) + ")",
        '(allow file-write* (literal "/dev/null") (literal "/dev/dtracehelper") (literal "/dev/tty")'
        ' (regex #"^/dev/ttys[0-9]+$"))',
        "(allow pseudo-tty)",
        f'(allow network-outbound (remote ip "localhost:{proxy_port}"))',
        *([f'(allow network-bind network-inbound (local ip "localhost:{opencode_port}"))',
           f'(allow network-outbound (remote ip "localhost:{opencode_port}"))'] if opencode_port else []),
        f"(allow network-outbound (remote unix-socket (path-literal {sb(real(relay_door(box)))})))",
        f"(allow network-bind network-inbound (local unix-socket (subpath {sb(real(box_run(box)))})))",
        f"(allow network-outbound (remote unix-socket (subpath {sb(real(box_run(box)))})))",
        "(allow process-info* (target same-sandbox))",
        "(allow signal (target same-sandbox))",
        "(allow mach-lookup " + " ".join(f'(global-name "{n}")' for n in MACH_ALLOW) + ")",
        "(deny mach-lookup " + " ".join(f'(global-name "{n}")' for n in MACH_DENY) + ")",
    ]
    if extra.strip():
        lines += [";; local additions from " + PROFILE_EXTRA + " (root only; hubchat list warns)", extra.strip()]
    return "\n".join(lines) + "\n"


def service_profile(writable: List[str], readable_denied: List[str] = ()) -> str:
    """The relay's, the forwarder's and the tool installer's profile: everything but writes
    (except their own folders) and the people's files. Their main walls are their own user and pf;
    Seatbelt cannot name an IP address, so their network stays open here."""
    deny = ("/Users", "/private/tmp", "/private/var/tmp", real(kc.TEAM), real(kc.SECRET), real(RUNBOX)) + \
        tuple(readable_denied)
    return "\n".join([
        ";; kaptyorka service profile (TEST VERSION)",
        "(version 1)",
        "(allow default)",
        "(deny file-write*)",
        '(allow file-write* (literal "/dev/null") (literal "/dev/dtracehelper")' +
        "".join(f" (subpath {sb(p)})" for p in writable) + ")",
        "(deny file-read* " + " ".join(f"(subpath {sb(p)})" for p in deny) + ")",
        "(deny process-info*)",
        "(allow process-info* (target same-sandbox))",
        "(deny mach-lookup " + " ".join(f'(global-name "{n}")' for n in MACH_DENY) + ")",
    ]) + "\n"


def filter_profile(py_prefix: str) -> str:
    """tty-filter's profile: it reads its program and Python and writes only to the two devices it
    was handed; no network, no files, no processes."""
    return "\n".join([
        ";; kaptyorka tty-filter profile (TEST VERSION)",
        "(version 1)",
        "(deny default)",
        '(import "system.sb")',
        "(allow process-exec* (subpath " + sb(py_prefix) + ") (subpath \"/usr\") (subpath \"/System\"))",
        "(allow file-read* " + " ".join(f"(subpath {sb(p)})" for p in ("/usr", "/System", "/Library", py_prefix,
                                                                         "/private/etc", "/dev")) + ")",
        "(allow file-read-metadata)",
        "(allow sysctl-read)",
    ]) + "\n"


# ---------------------------------------------------------------------------- pf (pure)

PRIVATE4 = "10.0.0.0/8 172.16.0.0/12 192.168.0.0/16 169.254.0.0/16 100.64.0.0/10 127.0.0.0/8 0.0.0.0/8"
PRIVATE6 = "::1 fc00::/7 fe80::/10"


def pf_rules(boxes: Dict[int, int], relay_uid: int, proxy_uid: int, tools_uid: int, hub_ip: str, hub_port: int,
             opencode: Optional[Dict[int, int]] = None) -> str:
    """The anchor com.apple/kaptyorka (06 wall 3, option B; the kit's phase D): a second layer
    under Seatbelt. pf matches users only for TCP and UDP, and its block rules also catch loopback
    (measured), so each user's loopback way is passed first. boxes: {box uid: forwarder port};
    opencode: {box uid: its OpenCode port}: the box's own client and server pass, then every
    other packet to that port, and every other owner's answer from it, is dropped (another user
    of this Mac must not reach the box's OpenCode, nor pose as it by taking the port first)."""
    ip = ipaddress.ip_address(hub_ip)
    if not 0 < int(hub_port) < 65536:
        raise Refuse("bad hub port")
    for u in [relay_uid, proxy_uid, tools_uid, *boxes]:
        if not isinstance(u, int) or u <= 0:
            raise Refuse("bad user id for pf")
    opencode = opencode or {}
    ports = sorted(boxes.values())
    if any(u not in boxes or not 1024 < int(p) < 65536 or int(p) in ports for u, p in opencode.items()):
        raise Refuse("bad OpenCode port for pf")
    out = ["# kaptyorka (secure design v2, 10.1): written by root; TEST VERSION"]
    for uid, port in sorted(boxes.items()):
        out.append(f"pass out quick on lo0 proto tcp from any to 127.0.0.1 port {int(port)} user {uid}")
        if uid in opencode:
            oc = int(opencode[uid])
            out += [f"pass out quick on lo0 proto tcp from any to 127.0.0.1 port {oc} user {uid}",
                    f"pass out quick on lo0 proto tcp from 127.0.0.1 port {oc} to 127.0.0.1 user {uid}",
                    f"block drop out quick proto {{ tcp udp }} from any to {{ 127.0.0.1 ::1 }} port {oc}",
                    f"block drop out quick proto {{ tcp udp }} from {{ 127.0.0.1 ::1 }} port {oc} to any"]
        out.append(f"block drop out quick proto {{ tcp udp }} all user {uid}")
    out += [f"pass out quick proto tcp from any to {ip} port {int(hub_port)} user {relay_uid}",
            f"block drop out quick proto {{ tcp udp }} all user {relay_uid}"]
    if ports:
        out.append(f"pass out quick on lo0 proto tcp from 127.0.0.1 port {{ {' '.join(map(str, ports))} }} "
                   f"to any user {proxy_uid}")
    for uid in (proxy_uid, tools_uid):
        out += [f"block drop out quick inet proto {{ tcp udp }} from any to {{ {PRIVATE4} }} user {uid}",
                f"block drop out quick inet6 proto {{ tcp udp }} from any to {{ {PRIVATE6} }} user {uid}"]
    out += [f"pass out quick proto tcp from any to any port {{ 443 22 }} user {proxy_uid}",
            f"block drop out quick proto {{ tcp udp }} all user {proxy_uid}",
            f"pass out quick proto tcp from any to any port 443 user {tools_uid}",
            f"block drop out quick proto {{ tcp udp }} all user {tools_uid}"]
    return "\n".join(out) + "\n"


def pf_expected_uids(text: str) -> List[str]:
    """The users the anchor's loaded rules name ('user = N' in pfctl's listing; a name is turned
    into its number)."""
    out = set()
    for u in re.findall(r"user = (\S+)", text):
        if not u.isdigit():
            try:
                u = str(pwd.getpwnam(u).pw_uid)
            except KeyError:
                continue
        out.add(u)
    return sorted(out)


# ---------------------------------------------------------------------------- launchd, sudoers (pure)

def plist(name: str, args: List[str], keepalive: bool = False, interval: int = 0) -> bytes:
    d = {"Label": label(name), "ProgramArguments": list(args), "RunAtLoad": True, "ProcessType": "Background",
         "EnvironmentVariables": {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "LANG": "en_US.UTF-8"},
         "StandardOutPath": f"{LOGS}/{name}.log", "StandardErrorPath": f"{LOGS}/{name}.log", "Umask": 0o077}
    if keepalive:
        d["KeepAlive"] = True
        d["ThrottleInterval"] = 10
    if interval:
        d["StartInterval"] = int(interval)
    return plistlib.dumps(d, sort_keys=True)


def daemons(py: str, boxes: List[str]) -> Dict[str, bytes]:
    """name -> plist of every LaunchDaemon: pf at boot, the canaries, the relay, one forwarder per
    box, the hourly update check and the daily tool update (the systemd timers' places)."""
    if not PY_RX.fullmatch(py):
        raise Refuse("bad python path")
    out = {"pf": plist("pf", [py, "-I", DAEMON, "pf"]),
           "canary": plist("canary", [py, "-I", DAEMON, "canary", kc.CANARY_PROC], keepalive=True),
           "relay": plist("relay", [py, "-I", DAEMON, "relay"], keepalive=True),
           "update": plist("update", [py, "-I", LIBEXEC + "/kap_update.py", "run"], interval=3600),
           "tools-update": plist("tools-update", [py, "-I", LIBEXEC + "/kap_tools.py", "auto-update"],
                                 interval=86400)}
    for b in boxes:
        out[f"egress.{kc.check_box(b)}"] = plist(f"egress.{b}", [py, "-I", DAEMON, "egress", b], keepalive=True)
    return out


def sudoers_text(person: str) -> str:
    kc.check_person(person)
    return (f"# kaptyorka (secure design v2, 10.1; TEST VERSION): {person} may start team chats and use hubchat.\n"
            f"Defaults!{ENTER}, {HUBCHAT_ROOT} env_reset, !mail_badpass, !mail_no_perms\n"
            f"{person} ALL=(root) NOPASSWD: {ENTER}, {HUBCHAT_ROOT}\n")


def acl_entry(group: str) -> str:
    """The share group's ACL entry (macOS has no POSIX default ACL): read and write, inherited by
    every file and folder made later, so what the person makes stays editable by the box."""
    if not re.fullmatch(r"kapshare-[a-z_][a-z0-9_-]{0,27}", group):
        raise Refuse("bad share group")
    return (f"group:{group} allow read,write,append,execute,delete,delete_child,readattr,writeattr,"
            "readextattr,writeextattr,readsecurity,file_inherit,directory_inherit")


def parse_launchctl(text: str) -> dict:
    """state and pid from `launchctl print system/<label>`."""
    st = re.search(r"^\s*state = (\S+)", text, re.M)
    pid = re.search(r"^\s*pid = (\d+)", text, re.M)
    return {"state": st.group(1) if st else "", "pid": int(pid.group(1)) if pid else 0}


def parse_ifconfig(text: str) -> List[str]:
    """This computer's own addresses (not loopback, not link-local) from ifconfig."""
    out = []
    for m in re.finditer(r"^\s*inet6? ([0-9a-fA-F:.]+)(?:%\S+)?", text, re.M):
        try:
            ip = ipaddress.ip_address(m.group(1))
        except ValueError:
            continue
        if not ip.is_loopback and not ip.is_link_local and str(ip) not in out:
            out.append(str(ip))
    return out[:16]


def python_problem(py: str, version: Tuple[int, int], tree_problem) -> str:
    """'' when py may run root's programs: Python 3.10 or newer, and its whole install root-owned
    and writable only by root (a person-writable Python would be a way to root)."""
    if not PY_RX.fullmatch(py or ""):
        return "the Python path is not a plain path"
    if version < (3, 10):
        return f"Python {version[0]}.{version[1]} is too old: 3.10 or newer is needed"
    return tree_problem or ""


# ---------------------------------------------------------------------------- small root tools

def python() -> str:
    p = kr.read_text(PYTHON_FILE).strip()
    return p if PY_RX.fullmatch(p) else ""


def py_prefix(py: str) -> str:
    """The top of a Python install: .../Versions/3.x for a framework, else two levels up."""
    r = os.path.realpath(py)
    m = re.match(r"(.*/Python\.framework/Versions/[0-9.]+)/", r)
    return m.group(1) if m else os.path.dirname(os.path.dirname(r))


def run(cmd: List[str], check: bool = True, timeout: float = 60, **kw) -> subprocess.CompletedProcess:
    return kr.run(cmd, check=check, timeout=timeout, **kw)


def dscl(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return run(["/usr/bin/dscl", ".", *args], check=check)


def record_exists(kind: str, name: str) -> bool:
    return dscl("-read", f"/{kind}/{name}", "RecordName", check=False).returncode == 0


def record_ours(kind: str, name: str) -> bool:
    return MARK in dscl("-read", f"/{kind}/{name}", "RealName", check=False).stdout


def used_ids() -> set:
    used = set()
    for kind, attr in (("Users", "UniqueID"), ("Groups", "PrimaryGroupID")):
        for line in dscl("-list", f"/{kind}", attr, check=False).stdout.splitlines():
            parts = line.split()
            if parts and parts[-1].lstrip("-").isdigit():
                used.add(int(parts[-1]))
    return used


def free_id(used: set) -> int:
    for i in range(ID_RANGE[0], ID_RANGE[1] + 1):
        if i in used:
            continue
        try:
            pwd.getpwuid(i)
            continue
        except KeyError:
            pass
        try:
            grp.getgrgid(i)
            continue
        except KeyError:
            pass
        return i
    raise Refuse(f"no free hidden user id between {ID_RANGE[0]} and {ID_RANGE[1]}")


def make_group(name: str, gid: int) -> None:
    run(["/usr/sbin/dseditgroup", "-o", "create", "-i", str(gid), "-r", MARK, name])


def make_user(name: str, uid: int, gid: int, home: str, shell: str) -> None:
    for attr, val in (("RealName", MARK), ("UniqueID", str(uid)), ("PrimaryGroupID", str(gid)),
                      ("NFSHomeDirectory", home), ("UserShell", shell), ("Password", "*"), ("IsHidden", "1")):
        dscl("-create", f"/Users/{name}", attr, val)


def ensure_account(name: str, home: str, shell: str) -> Tuple[int, int]:
    """A hidden account with a group of its own (same id), no password, no login. Ours only: an
    account of the same name not made by kaptyorka is refused."""
    if record_exists("Users", name):
        if not record_ours("Users", name):
            raise Refuse(f"the account {name} already exists and was not made by kaptyorka")
        pw = pwd.getpwnam(name)
        if pw.pw_dir != home or pw.pw_shell != shell:   # an older install's values: this code's now
            dscl("-create", f"/Users/{name}", "NFSHomeDirectory", home)
            dscl("-create", f"/Users/{name}", "UserShell", shell)
            run(["/usr/bin/dscacheutil", "-flushcache"], check=False)
        return pw.pw_uid, pw.pw_gid
    if record_exists("Groups", name) and not record_ours("Groups", name):
        raise Refuse(f"the group {name} already exists and was not made by kaptyorka")
    used = used_ids()
    i = free_id(used)
    if not record_exists("Groups", name):
        make_group(name, i)
    gid = grp.getgrnam(name).gr_gid
    make_user(name, i, gid, home, shell)
    run(["/usr/bin/dscacheutil", "-flushcache"], check=False)
    return i, gid


def ensure_group(name: str) -> int:
    if record_exists("Groups", name):
        if not record_ours("Groups", name):
            raise Refuse(f"the group {name} already exists and was not made by kaptyorka")
    else:
        make_group(name, free_id(used_ids()))
        run(["/usr/bin/dscacheutil", "-flushcache"], check=False)
    return grp.getgrnam(name).gr_gid


def is_member(user: str, group: str) -> bool:
    r = run(["/usr/sbin/dseditgroup", "-o", "checkmember", "-m", user, group], check=False)
    return r.stdout.strip().startswith("yes")


def group_members(group: str) -> List[str]:
    out = dscl("-read", f"/Groups/{group}", "GroupMembership", check=False).stdout
    return sorted(out.split(":", 1)[1].split()) if ":" in out else []


def add_member(group: str, user: str) -> None:
    if user not in group_members(group):
        run(["/usr/sbin/dseditgroup", "-o", "edit", "-a", user, "-t", "user", group])


def delete_record(kind: str, name: str) -> None:
    if record_exists(kind, name) and record_ours(kind, name):
        dscl("-delete", f"/{kind}/{name}", check=False)


def fd_path(fd: int) -> str:
    """The real path of an open file or folder (macOS F_GETPATH)."""
    return fcntl.fcntl(fd, 50, b"\0" * 1024).split(b"\0", 1)[0].decode("utf-8", "surrogateescape")


def person_home(person: str) -> pwd.struct_passwd:
    pw = pwd.getpwnam(kc.check_person(person))
    if pw.pw_dir != f"/Users/{person}" or os.path.realpath(pw.pw_dir) != pw.pw_dir:
        raise Refuse(f"{person}'s home is not /Users/{person}")
    return pw


def host_addrs() -> List[str]:
    r = run(["/sbin/ifconfig"], check=False, timeout=10)
    return parse_ifconfig(r.stdout)


def launchctl(*args: str, check: bool = False) -> subprocess.CompletedProcess:
    return run(["/bin/launchctl", *args], check=check, timeout=60)


def daemon_state(name: str) -> dict:
    return parse_launchctl(launchctl("print", f"system/{label(name)}").stdout)


def load_daemon(name: str, data: bytes) -> None:
    """Write the plist and (re)load it: a changed plist is booted out first."""
    path = plist_path(name)
    old = b""
    try:
        with open(path, "rb") as f:
            old = f.read()
    except OSError:
        pass
    loaded = launchctl("print", f"system/{label(name)}").returncode == 0
    if old != data or not loaded:
        if loaded:
            launchctl("bootout", f"system/{label(name)}")
            time.sleep(0.5)
        kr.write_file(path, data, 0o644)
        r = launchctl("bootstrap", "system", path)
        if r.returncode != 0 and launchctl("print", f"system/{label(name)}").returncode != 0:
            raise Refuse(f"launchctl could not load {label(name)}: {(r.stderr or r.stdout).strip()[:200]}")


def unload_daemon(name: str) -> None:
    launchctl("bootout", f"system/{label(name)}")
    if os.path.lexists(plist_path(name)):
        os.unlink(plist_path(name))


def proc_uid(pid: int) -> int:
    """A process's user id, as an unsigned number (ps shows nobody as -2)."""
    t = run(["/bin/ps", "-o", "uid=", "-p", str(pid)], check=False, timeout=10).stdout.strip()
    return int(t) % (1 << 32) if t.lstrip("-").isdigit() else -1


def proc_args(pid: int) -> str:
    return run(["/bin/ps", "-ww", "-o", "args=", "-p", str(pid)], check=False, timeout=10).stdout.strip()


def runtime_max_s() -> int:
    v = kr.runtime_max()
    if v == "infinity":
        return 0
    m = re.fullmatch(r"(\d+)(s|min|h|d)?", v)
    return int(m.group(1)) * {"s": 1, None: 1, "min": 60, "h": 3600, "d": 86400}[m.group(2)] if m else 7 * 86400


# ---------------------------------------------------------------------------- boxes, ports, profiles

def boxes() -> List[str]:
    try:
        return sorted(b for b in os.listdir(kc.BOXES) if kc.BOX_RX.fullmatch(b))
    except OSError:
        return []


def opencode_port(box: str) -> int:
    """The box's OpenCode port (written at install, like proxy-port); 0 when there is none."""
    t = kr.read_text(f"{kc.BOXES}/{kc.check_box(box)}/opencode-port").strip()
    return int(t) if t.isdigit() and 1024 < int(t) < 65536 else 0


def proxy_port(box: str) -> int:
    t = kr.read_text(f"{kc.BOXES}/{kc.check_box(box)}/proxy-port").strip()
    if not t.isdigit() or not 1024 < int(t) < 65536:
        raise Refuse(f"the box {box} has no forwarder port (run the installer's repair)")
    return int(t)


def darwin_dir(box: str) -> str:
    """The box user's own macOS temp and cache folder (/private/var/folders/xx/yyy), as recorded at
    install; '' when it is missing or not the box's."""
    p = kr.read_text(f"{kc.BOXES}/{kc.check_box(box)}/darwin-dir").strip()
    if not re.fullmatch(r"/private/var/folders/[A-Za-z0-9_+-]{1,8}/[A-Za-z0-9_+-]{8,64}", p):
        return ""
    try:
        st = os.lstat(p)
        if not stat.S_ISDIR(st.st_mode) or st.st_uid != pwd.getpwnam(kc.box_user(box)).pw_uid:
            return ""
    except (OSError, KeyError):
        return ""
    return p


def write_profile(person: str, box: str) -> str:
    """The box profile, written anew from this code at every start (root-owned)."""
    text = box_profile(box, person, proxy_port(box), darwin_dir(box), kr.read_text(PROFILE_EXTRA),
                       py_prefix(python()) if python() else "", opencode_port(box))
    path = profile_path(box)
    if kr.read_text(path) != text:
        kr.write_file(path, text, 0o644)
    return path


def pf_text() -> str:
    conf = dict(ln.split("=", 1) for ln in kr.read_text(f"{ETC}/relay.conf").splitlines() if "=" in ln)
    bx, oc = {}, {}
    for b in boxes():
        try:
            uid = pwd.getpwnam(kc.box_user(b)).pw_uid
            bx[uid] = proxy_port(b)
        except (KeyError, Refuse):
            continue
        if opencode_port(b):
            oc[uid] = opencode_port(b)
    return pf_rules(bx, pwd.getpwnam(RELAY_USER).pw_uid, pwd.getpwnam(PROXY_USER).pw_uid,
                    pwd.getpwnam(TOOLS_USER).pw_uid, conf.get("hub_ip", "127.0.0.1"), int(conf.get("hub_port", "443")),
                    oc)


def pf_load() -> None:
    """The anchor from its file, and pf on (a counted enable: pfctl -E; its token is kept so remove
    gives it back with -X)."""
    run(["/sbin/pfctl", "-a", ANCHOR, "-f", PF_FILE], check=False, timeout=30)
    if not pf_enabled():
        r = run(["/sbin/pfctl", "-E"], check=False, timeout=30)
        m = re.search(r"Token : (\d+)", r.stdout + r.stderr)
        if m:
            kr.mkdir(kc.SECRET, 0o700)
            kr.write_file(PF_TOKEN, m.group(1) + "\n", 0o600)


def pf_enabled() -> bool:
    r = run(["/sbin/pfctl", "-s", "info"], check=False, timeout=30)
    return bool(re.search(r"^Status: Enabled", r.stdout, re.M))


def pf_problem() -> str:
    if not pf_enabled():
        return "pf (the second network wall) is not enabled"
    main = run(["/sbin/pfctl", "-s", "rules"], check=False, timeout=30).stdout
    if 'anchor "com.apple/*"' not in main:
        return "pf's main rules do not hold Apple's anchor com.apple/* (see /etc/pf.conf)"
    loaded = run(["/sbin/pfctl", "-a", ANCHOR, "-s", "rules"], check=False, timeout=30).stdout
    want = pf_expected_uids(kr.read_text(PF_FILE).replace("user ", "user = "))
    if not want or pf_expected_uids(loaded) != want:
        return f"pf's anchor {ANCHOR} does not hold kaptyorka's rules"
    return ""


# ---------------------------------------------------------------------------- canaries

def _shared_canaries(bad: List[str]) -> List[str]:
    """Fresh-named canaries in the places every user can write (made with O_EXCL, never a file
    someone made first), their names in CANARY_STATE (root's folder)."""
    old = [p for p in kr.read_text(CANARY_STATE).split() if CANARY_SHARED_RX.fullmatch(p)]
    out = []
    for d in CANARY_SHARED_DIRS:
        have = [p for p in old if os.path.dirname(p) == d and kr.canary_ok(p)]
        if have:
            out.append(have[0])
            continue
        try:
            st = os.lstat(d)
        except OSError as e:
            bad.append(f"canary folder {d}: {os.strerror(e.errno)}")
            continue
        if not stat.S_ISDIR(st.st_mode) or st.st_uid != 0 or not st.st_mode & stat.S_ISVTX:
            bad.append(f"{d} is not a root-owned folder with the sticky bit")
            continue
        p = f"{d}/kaptyorka-wall-canary-{secrets.token_hex(8)}"
        try:
            kr._make_canary(p)
            out.append(p)
        except OSError as e:
            bad.append(f"canary file in {d} cannot be made: {os.strerror(e.errno)}")
    if sorted(out) != sorted(old):
        kr.mkdir(RUN, 0o755)
        kr.write_file(CANARY_STATE, "".join(p + "\n" for p in out), 0o644)
    return out


def _home_canary(person: str, bad: List[str]) -> str:
    """A root-made canary in the person's home: proves the 700 home and the profile together."""
    try:
        hfd = kr.open_dir(person_home(person).pw_dir)
    except (Refuse, KeyError) as e:
        bad.append(str(e))
        return ""
    try:
        try:
            fd = os.open(CANARY_HOME, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=hfd)
            try:
                st = os.fstat(fd)
                if st.st_uid == 0 and stat.S_ISREG(st.st_mode) and kc.CANARY_MARK in os.read(fd, 64):
                    return f"{person_home(person).pw_dir}/{CANARY_HOME}"
            finally:
                os.close(fd)
        except FileNotFoundError:
            pass
        except OSError:
            pass
        try:
            os.unlink(CANARY_HOME, dir_fd=hfd)
        except FileNotFoundError:
            pass
        fd = os.open(CANARY_HOME, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644, dir_fd=hfd)
        try:
            os.write(fd, kc.CANARY_MARK + b"\n")
            os.fchmod(fd, 0o644)
        finally:
            os.close(fd)
        return f"{person_home(person).pw_dir}/{CANARY_HOME}"
    except OSError as e:
        bad.append(f"the canary in {person}'s home cannot be made: {os.strerror(e.errno)}")
        return ""
    finally:
        os.close(hfd)


def canaries(person: str, bad: List[str]) -> List[str]:
    """Every canary file the box must not read, alive (made again when missing)."""
    out = []
    p = kr._canary_file_ok(CANARY_FIXED)
    if p:
        bad.append(p)
    else:
        out.append(real(CANARY_FIXED))
    h = _home_canary(person, bad)
    if h:
        out.append(h)
    return out + _shared_canaries(bad)


def canary_pid() -> int:
    t = kr.read_text(CANARY_PID).strip()
    return int(t) if t.isdigit() else 0


def canary_alive_problems() -> List[str]:
    bad = []
    if kc.CANARY_MARK not in kr._recv(socket.AF_INET, ("127.0.0.1", CANARY_TCP_LO)):
        bad.append("the loopback canary does not answer (com.kaptyorka.canary)")
    if kc.CANARY_MARK not in kr._recv(socket.AF_UNIX, CANARY_SOCK):
        bad.append("the canary socket does not answer (com.kaptyorka.canary)")
    pid = canary_pid()
    if not pid or kc.CANARY_PROC not in proc_args(pid):
        bad.append("the canary process is not running (com.kaptyorka.canary)")
    try:
        import ctypes
        if ctypes.CDLL(None, use_errno=True).shmget(CANARY_SHM_KEY, 0, 0) < 0:
            bad.append("the canary shared memory segment is missing (com.kaptyorka.canary)")
    except (OSError, AttributeError):
        pass
    return bad


# ---------------------------------------------------------------------------- the outside half of the self-check

def tree_problem(top: str) -> Optional[str]:
    """kaproot's check, with macOS's own exception: /Library and /Users/Shared and their like may
    be group admin; never writable by others."""
    return kr._tree_problem(top)


PRIVILEGED = {0: "wheel", 20: "staff", 80: "admin"}


def privileged_groups(user: str, gid: int) -> List[str]:
    """The groups among wheel, staff and admin that user is in (as id -Gn sees it)."""
    try:
        have = set(os.getgrouplist(user, gid))
    except OSError:
        return ["(its groups cannot be read)"]
    return [PRIVILEGED[g] for g in sorted(have & set(PRIVILEGED))]


def account_problems(person: str, box: str) -> List[str]:
    bad = []
    u, g = kc.box_user(box), kc.share_group(box)
    try:
        pw = pwd.getpwnam(u)
    except KeyError:
        return [f"the box account {u} is missing (run the installer's repair)"]
    info = dscl("-read", f"/Users/{u}", check=False).stdout
    if "IsHidden: 1" not in info:
        bad.append(f"{u} is not hidden")
    if "AuthenticationAuthority:" in info:
        bad.append(f"{u} has a password or a login (AuthenticationAuthority)")
    bad += [f"{u} is in the group {n}; it must not be" for n in privileged_groups(u, pw.pw_gid)]
    if group_members(u):
        bad.append(f"the group {u} has members {group_members(u)}; it must have none")
    if group_members(g) != sorted({person, u}):
        bad.append(f"the group {g} has members {group_members(g)}; only {person} and {u}")
    if pw.pw_gid != grp.getgrnam(u).gr_gid:
        bad.append(f"{u}'s main group is not its own")
    for s in SERVICE_USERS:
        try:
            pwd.getpwnam(s)
        except KeyError:
            bad.append(f"the service account {s} is missing (run the installer's repair)")
            continue
        bad += [f"{s} is in the group {n}; it must not be" for n in privileged_groups(s, pwd.getpwnam(s).pw_gid)]
    return bad


def team_problems(person: str, box: str, folders: Dict[str, dict]) -> List[str]:
    bad = []
    try:
        buid, sgid = pwd.getpwnam(kc.box_user(box)).pw_uid, grp.getgrnam(kc.share_group(box)).gr_gid
    except KeyError:
        return []
    for p, uid, gid, mode in ((TEAMAREA, 0, None, 0o755), (team_root(person), 0, sgid, 0o750)):
        try:
            st = os.lstat(p)
        except OSError:
            bad.append(f"{p} is missing (run the installer's repair)")
            continue
        if not stat.S_ISDIR(st.st_mode) or st.st_uid != uid or (gid is not None and st.st_gid != gid) \
                or st.st_mode & 0o777 != mode:
            bad.append(f"{p} is not a root-owned folder with mode {mode:o}")
    for name in sorted(folders) + [kc.MEMORY]:
        p = team_path(person, name)
        try:
            st = os.lstat(p)
        except OSError:
            bad.append(f"the team folder {p} is missing")
            continue
        if not stat.S_ISDIR(st.st_mode) or st.st_uid != buid or st.st_gid != sgid or st.st_mode & 0o007:
            bad.append(f"the team folder {p} is not the box's (owner, group, no rights for others)")
    return bad


def daemon_problems(box: str) -> List[str]:
    bad = []
    for name, user in (("relay", RELAY_USER), (f"egress.{box}", PROXY_USER), ("canary", "nobody")):
        st = daemon_state(name)
        if st["state"] != "running" or not st["pid"]:
            bad.append(f"{label(name)} is not running")
            continue
        try:
            want = pwd.getpwnam(user).pw_uid % (1 << 32)
        except KeyError:
            want = -1
        # the starter runs as root a moment before it hands over to its user
        for _ in range(20):
            if proc_uid(daemon_state(name)["pid"] or st["pid"]) == want:
                break
            time.sleep(0.25)
        else:
            bad.append(f"{label(name)} does not run as {user}")
    return bad


def outside_check(person: str, box: str) -> List[str]:
    """The outside half of the self-check, as root, before a box process starts (07 10.2)."""
    bad: List[str] = []
    if kr.read_text(kc.MODE_FILE).strip() != "personal":
        bad.append(f"{kc.MODE_FILE} does not say personal")
    py = python()
    if not py:
        bad.append(f"{PYTHON_FILE} names no Python")
    else:
        p = tree_problem(py_prefix(py))
        if p:
            bad.append("the Python kaptyorka runs: " + p)
    canaries(person, bad)
    bad += canary_alive_problems()
    try:
        m = os.stat(person_home(person).pw_dir).st_mode & 0o777
        if m not in (0o700, 0o750):
            bad.append(f"{person}'s home has mode {m:o}; it must be 700 (chmod 700 /Users/{person})")
    except (Refuse, KeyError, OSError) as e:
        bad.append(str(e))
    bad += account_problems(person, box)
    for top in (LIBEXEC, ETC, OPT, TOOLS_DIR, f"{SUDOERS_DIR}/kaptyorka-{person}",
                *(plist_path(n) for n in ("pf", "canary", "relay", "update", "tools-update", f"egress.{box}"))):
        p = tree_problem(top)
        if p:
            bad.append(p)
    if kr.read_text(f"{SUDOERS_DIR}/kaptyorka-{person}") != sudoers_text(person):
        bad.append(f"/etc/sudoers.d/kaptyorka-{person} is not kaptyorka's rule")
    try:
        folders = kr.load_folders(person)
    except (Refuse, ValueError, KeyError) as e:
        folders = {}
        bad.append(f"folders.json: {e}")
    bad += team_problems(person, box, folders)
    p = pf_problem()
    if p:
        bad.append(p)
    bad += daemon_problems(box)
    return bad


def repair_before_start(person: str, box: str) -> None:
    """What root puts right by itself before the check (a reboot or a Wi-Fi change can drop the
    pf rules; launchd may not have started a daemon yet). Nothing here weakens a wall: whatever
    is still wrong afterwards stops the start."""
    if pf_problem():
        pf_load()
    for name in ("canary", "relay", f"egress.{box}"):
        if daemon_state(name)["state"] != "running" and os.path.exists(plist_path(name)):
            launchctl("bootstrap", "system", plist_path(name))
            launchctl("kickstart", f"system/{label(name)}")
    for _ in range(40):
        if not canary_alive_problems() and all(daemon_state(n)["state"] == "running"
                                               for n in ("relay", f"egress.{box}")):
            break
        time.sleep(0.25)
    try:
        lock_settings(box)
    except (Refuse, OSError) as e:
        print(kc.term(f"enter: the box's locked tool settings could not be put back: {e}"), file=sys.stderr)
    try:
        write_box_secrets(person, box)
    except (Refuse, OSError, KeyError, ValueError):
        pass


def write_locked(box: str, dst: str, data: bytes) -> None:
    """One locked tool settings file in the box home, root-owned: every folder on the way opened
    without following a link (the box owns some of them), the file swapped in by rename."""
    d, base = os.path.split(f"{kc.box_home(box)}/{dst}")
    dfd = kr.open_dir(d)
    try:
        tmp = f".kaplock-{secrets.token_hex(4)}"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=dfd)
        try:
            os.write(fd, data)
            os.fchown(fd, 0, 0)
            os.fchmod(fd, 0o644)
        finally:
            os.close(fd)
        os.rename(tmp, base, src_dir_fd=dfd, dst_dir_fd=dfd)
    finally:
        os.close(dfd)


def lock_settings(box: str) -> None:
    for src, dst in kc.LOCKED_FILES:
        want = kr.read_text(f"{kc.BOXCONF}/{src}").encode()
        p = f"{kc.box_home(box)}/{dst}"
        if not os.path.lexists(p) or kr.read_text(p).encode() != want or os.lstat(p).st_uid:
            write_locked(box, dst, want)


def write_box_secrets(person: str, box: str) -> int:
    try:
        gid = pwd.getpwnam(RELAY_USER).pw_gid
    except KeyError:
        gid = 0
    kr.mkdir(RUN, 0o755)
    return kr.write_box_secrets(person, box, where=BOX_SECRETS, gid=gid)


# ---------------------------------------------------------------------------- starting a box process

def box_env(person: str, box: str, term: str = "") -> Dict[str, str]:
    """The box process's whole environment: nothing of the person's."""
    u, port = kc.box_user(box), proxy_port(box)
    proxy = f"http://127.0.0.1:{port}"
    env = {"PATH": f"{TOOLS_DIR}/bin:/usr/bin:/bin:/usr/sbin:/sbin", "HOME": kc.box_home(box), "USER": u,
           "LOGNAME": u, "SHELL": "/bin/zsh", "LANG": "en_US.UTF-8", "TMPDIR": box_tmp(box) + "/",
           "HTTPS_PROXY": proxy, "HTTP_PROXY": proxy, "ALL_PROXY": proxy, "NO_PROXY": ""}
    if opencode_port(box):   # box-exec and the helper give it to OpenCode (kap_box_exec.oc_port)
        env["KAP_OPENCODE_PORT"] = str(opencode_port(box))
    if term and kc.TERM_RX.fullmatch(term):
        env["TERM"] = term
    return env


def box_argv(person: str, box: str, inner: List[str], verbose: bool = False) -> List[str]:
    """sandbox-exec with the box profile, then wallcheck (the inside half), then inner."""
    py = python()
    bad: List[str] = []
    files = canaries(person, bad)
    if bad:
        raise Refuse("; ".join(bad))
    return ["/usr/bin/sandbox-exec", "-f", write_profile(person, box), py, "-I", WALLCHECK,
            *(["--verbose"] if verbose else []), "--box", box, "--person", person,
            "--canaries", ",".join(files), "--canary-pid", str(canary_pid()), "--addrs", ",".join(host_addrs()),
            "--proxy-port", str(proxy_port(box)), "--opencode-port", str(opencode_port(box)), "--", *inner]


def ids(box: str) -> Tuple[int, int, int]:
    pw = pwd.getpwnam(kc.box_user(box))
    return pw.pw_uid, pw.pw_gid, grp.getgrnam(kc.share_group(box)).gr_gid


def token_fd(person: str) -> int:
    """The box's Claude token on one end of a socket pair, the token and a newline already in it
    (kap_box_exec.hand_token's way): claude reads it from the descriptor named by
    CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR. -1 when there is no token."""
    tok = kr.read_text(f"{kc.SECRET}/{kc.check_person(person)}/claude-oauth-token").strip()
    if not tok or not kc.TOKEN_RX.fullmatch(tok):
        return -1
    mine, theirs = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        mine.sendall(tok.encode("ascii") + b"\n")
        mine.shutdown(socket.SHUT_WR)
    finally:
        mine.close()
    fd = theirs.detach()
    os.set_inheritable(fd, True)
    return fd


def popen_box(person: str, box: str, inner: List[str], cwd: str, *, claude: bool = False, term: str = "",
              verbose: bool = False, **kw) -> subprocess.Popen:
    uid, gid, sgid = ids(box)
    env = box_env(person, box, term)
    fds = []
    if claude:
        fd = token_fd(person)
        if fd >= 0:
            fds.append(fd)
            env["CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR"] = str(fd)
    for d in (box_run(box), box_tmp(box)):
        os.makedirs(os.path.dirname(d), 0o755, exist_ok=True)
        kr.mkdir(d, 0o700, uid, gid)
    try:
        return subprocess.Popen(box_argv(person, box, inner, verbose), cwd=cwd, env=env, user=uid, group=gid,
                                extra_groups=[sgid], umask=0o007, pass_fds=tuple(fds), close_fds=True, **kw)
    finally:
        for fd in fds:
            os.close(fd)


# ---------------------------------------------------------------------------- chat records

def record_chat(pid: int, person: str, box: str, tool: str, cwd: str) -> None:
    kr.mkdir(RUN, 0o755)
    kr.mkdir(CHATS, 0o700)
    kr.write_file(f"{CHATS}/{int(pid)}.json", json.dumps({"pid": pid, "person": person, "box": box, "tool": tool,
                                                          "cwd": cwd, "started": time.time()}) + "\n", 0o600)


def drop_chat(pid: int) -> None:
    try:
        os.unlink(f"{CHATS}/{int(pid)}.json")
    except OSError:
        pass


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def chats(box: str = "") -> List[dict]:
    out = []
    try:
        names = os.listdir(CHATS)
    except OSError:
        return out
    for n in names:
        if not re.fullmatch(r"\d+\.json", n):
            continue
        try:
            rec = json.loads(kr.read_text(f"{CHATS}/{n}"))
        except ValueError:
            continue
        if not isinstance(rec, dict) or not alive(int(rec.get("pid", 0))):
            drop_chat(int(n[:-5]))
            continue
        if not box or rec.get("box") == box:
            out.append(rec)
    return out


def stop_chat(rec: dict) -> None:
    pid = int(rec["pid"])
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(pid, sig)
        except OSError:
            pass
        for _ in range(20):
            if not alive(pid):
                break
            time.sleep(0.1)
    end_session(pid)


def end_session(sid: int) -> None:
    """Every process left in a chat's session (the chat was its leader): nothing a chat started
    outlives it, as with systemd's KillMode=control-group. A process that made a session of its
    own stays (it is still the box user, inside the profile)."""
    if sid > 1:
        run(["/usr/bin/pkill", "-KILL", "-s", str(int(sid))], check=False, timeout=10)


def kill_box_procs_in(box: str, top: str) -> int:
    """Every process of the box user whose folder or open files are inside top (lsof)."""
    uid = pwd.getpwnam(kc.box_user(box)).pw_uid
    r = run(["/usr/sbin/lsof", "-nP", "-w", "-u", str(uid), "-Fpn"], check=False, timeout=60)
    pid, hit = 0, set()
    for line in r.stdout.splitlines():
        if line.startswith("p") and line[1:].isdigit():
            pid = int(line[1:])
        elif line.startswith("n") and pid and kc.under(line[1:], top):
            hit.add(pid)
    for p in hit:
        try:
            os.kill(p, signal.SIGKILL)
        except OSError:
            pass
    return len(hit)


# ---------------------------------------------------------------------------- the helper

def start_helper(person: str, box: str) -> None:
    """The box helper (5.1) as a background box process: same profile, same self-check; it ends
    itself ten minutes after the last chat."""
    pidf = f"{RUNBOX}/{kc.check_box(box)}.helper.pid"
    t = kr.read_text(pidf).strip()
    if t.isdigit() and alive(int(t)) and proc_uid(int(t)) == ids(box)[0]:
        return
    uid, gid, _ = ids(box)
    log = f"{kc.box_home(box)}/.cache/kaptyorka-helper.log"
    try:
        out = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
        os.fchown(out, uid, gid)
    except OSError:
        out = os.open(os.devnull, os.O_WRONLY)
    try:
        p = popen_box(person, box, [python(), "-I", kc.BOXCODE + "/helper.py", "run", "--idle", "600"],
                      team_root(person), claude=True, stdin=subprocess.DEVNULL, stdout=out, stderr=out,
                      start_new_session=True)
    finally:
        os.close(out)
    kr.write_file(pidf, f"{p.pid}\n", 0o644)
