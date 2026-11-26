"""kapcommon: the pure part shared by the root programs of a personal laptop (secure design v2,
sections 4 and 7): names, paths, the decision "is this a team folder", the box's walls as one
list, the tool arguments `enter` lets through, git URLs, the forwarder's allow list, and the
git config that `hubchat remove` keeps.

Nothing here touches the system: no file writes, no programs, no network. Root actions are in
kaproot.py. Python standard library only; installed as /usr/local/libexec/kaptyorka/kapcommon.py
and imported from that fixed, root-owned folder. Every value from a person, a file or a folder
name is checked here against a strict pattern before root uses it. Nothing comes from the hub.
"""
import ipaddress
import json
import re
from typing import Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------- fixed places (4.2)

LIBEXEC = "/usr/local/libexec/kaptyorka"
ETC = "/etc/kaptyorka"
PEOPLE = ETC + "/people"          # people/<person>/folders.json  (written only by hubchat-root)
BOXES = ETC + "/boxes"            # boxes/<box>/passwd, group, claude-settings.json, egress-allow
BOXCONF = ETC + "/box"            # tool configs shared by every box; no secret inside
SECRET = ETC + "/secret"          # root only: device-token, chat-key, <person>/claude-oauth-token
MODE_FILE = ETC + "/mode"
LIMITS_FILE = ETC + "/box.conf"
TEAM = "/var/lib/kap-team"        # TEAM/kap-<box>/{home,memory,projects}
VIEW = "/srv/team"                # VIEW/<person>/<folder>: the person's view, one mount each
RUNBOX = "/run/kapbox"            # RUNBOX/<box>/{tmp,vartmp,run,pub}
RELAY_DOORS = "/run/kaptyorka/relay"
OPT = "/opt/kaptyorka"
BOXCODE = OPT + "/box/current"
TOOLS_DIR = "/opt/kap-tools"
WALLCHECK = LIBEXEC + "/wallcheck"
BOX_EXEC = LIBEXEC + "/box-exec"
ENTER = LIBEXEC + "/enter"
HUBCHAT_ROOT = LIBEXEC + "/hubchat-root"
MEMORY = "_memory"

TOOLS = ("claude", "opencode", "codex", "cursor-agent", "git")

# The canaries (4.6): one service holds them, enter proves them alive, wallcheck proves them gone.
CANARY_MARK = b"KAPTYORKA-WALL-CANARY"
# Fixed canary files sit in folders only root can write (the top of the disk, /home, /run/kaptyorka),
# so nobody can put a file or a link there first. /tmp and /dev/shm are open to every user: there
# each canary gets a fresh name nobody can guess (made with O_EXCL), recorded in CANARY_STATE.
CANARY_FILES = ("/.kaptyorka-wall-canary", "/home/.kaptyorka-wall-canary", "/run/kaptyorka/wall-canary")
CANARY_SHARED_DIRS = ("/tmp", "/dev/shm")
CANARY_STATE = "/run/kaptyorka/wall-canaries"
CANARY_SHARED_RX = re.compile(r"/(tmp|dev/shm)/kaptyorka-wall-canary-[0-9a-f]{16}")
CANARY_SHM_KEY = 0x4B415031
CANARY_ABSTRACT = b"\0kaptyorka-wall-canary"
CANARY_TCP_LO = 47991
CANARY_TCP_ANY = 47992
CANARY_PROC = "KAPTYORKA_WALL_CANARY_PROC"

# Files of a team folder that can run code when the person opens the folder (7.2), at any depth.
# TOOL_DIRS and TOOL_FILES: what an AI tool or an editor of the person's loads by itself when it
# starts in the folder (OpenCode runs .opencode/plugin/*.js and opencode.json's plugins with no
# prompt: red team round 3, finding 1). Everything inside a TOOL_DIR counts, at any depth.
TOOL_DIRS = (".opencode", ".claude", ".codex", ".cursor", ".vscode", ".idea", ".zed", ".devcontainer")
TOOL_FILES = ("opencode.json", "opencode.jsonc", ".mcp.json", ".envrc", ".nvim.lua", ".nvimrc", ".exrc",
              ".dir-locals.el")
# what a build or a version manager may run (listed by hubchat list and remove, not at every start)
BUILD_FILES = ("Makefile", "makefile", "GNUmakefile", "package.json", ".mise.toml", "mise.toml",
               ".tool-versions", ".nvmrc", ".python-version", ".gitattributes")
TRAP_MAX = 500                 # names reported at most
TRAP_BUDGET = 200000           # entries looked at at most (a huge tree is cut, never hangs enter)
TRAP_DEPTH = 32
# In the person's view of a team folder (/srv/team/<person>/<name>), the TOOL_DIRS and
# TOOL_FILES at the top and .git/hooks are covered: a folder by an empty private tmpfs of the
# person's own (her tool may write its scratch there; it never reaches the box), a file by an
# empty or "{}" read-only file. The box keeps the real ones. kaptyorka-guard redoes it the moment
# the box makes, renames or removes one (inotify).
HIDE_GIT = ".git/hooks"
HIDE_FILE = LIBEXEC + "/hide-empty"
HIDE_JSON = LIBEXEC + "/hide-empty.json"
HIDE_TMPFS = "kaptyorka-hide"
SHIMS = LIBEXEC + "/shims"     # first on the person's PATH: claude, opencode, codex, cursor-agent

# A box's tool sign-ins (red team round 3, finding 2): their current values are given to the
# relay, which refuses any outgoing text that holds one, raw or encoded.
SIGNIN_FILES = ("state/codex/auth.json", "state/opencode/data/opencode/auth.json", ".cursor/auth.json",
                ".config/cursor/auth.json", ".ssh/id_ed25519")
BOX_SECRETS = "/run/kaptyorka/box-secrets"   # <box>: root:kaprelay-secrets 0640, read by the relay
RELAY_GROUP = "kaprelay-secrets"             # the relay's only extra group; nobody is in it
SECRETS_KEEP = 64                            # values kept per box (old ones too: a copy taken before a refresh)
_ISO_TIME = re.compile(r"\d{4}-\d\d-\d\d[T ][0-9:.]+(Z|[+-]\d\d:?\d\d)?")


def secret_like(v) -> bool:
    """A value from a sign-in file that is worth guarding: long, no spaces, letters and digits,
    not a time stamp, a URL or a path."""
    if not isinstance(v, str) or not 16 <= len(v) <= 8192 or any(c.isspace() for c in v):
        return False
    if _ISO_TIME.fullmatch(v) or v.startswith(("http://", "https://", "/")) or "@" in v and "." in v.split("@")[-1]:
        return False
    kinds = sum((any(c.islower() for c in v), any(c.isupper() for c in v), any(c.isdigit() for c in v)))
    return kinds >= 2 and len(set(v)) >= 10


def signin_values(text: str) -> List[str]:
    """The secret-like values of one sign-in file: every string in it when it is JSON, else every
    word (an SSH key's base64 body is one value)."""
    out: List[str] = []
    try:
        data = json.loads(text)
    except ValueError:
        data = None
    if data is not None:
        stack = [data]
        while stack:
            x = stack.pop()
            if isinstance(x, dict):
                stack.extend(x.values())
            elif isinstance(x, list):
                stack.extend(x)
            elif secret_like(x):
                out.append(x)
    elif "PRIVATE KEY-----" in text:
        body = "".join(ln.strip() for ln in text.splitlines() if ln.strip() and "-----" not in ln)
        if body:
            out.append(body[:8192])
    else:
        out = [w for w in text.split() if secret_like(w)]
    return list(dict.fromkeys(out))

PERSON_RX = re.compile(r"[a-z_][a-z0-9_-]{0,30}")
NAME_RX = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._-]{0,63}")
BOX_RX = re.compile(r"[a-z_][a-z0-9_-]{0,27}")          # kap-<box> stays within 32 characters
RUNTIME_RX = re.compile(r"[1-9][0-9]{0,6}(s|min|h|d)?|infinity")
TERM_RX = re.compile(r"[A-Za-z0-9._+-]{1,64}")
TOKEN_RX = re.compile(r"[A-Za-z0-9_-]{20,400}")     # a token from `claude setup-token`

# How to end a Claude setup-token. Anthropic's docs (code.claude.com/docs/en/authentication,
# "Generate a long-lived token", read 2026-11-26) say it lasts one year and name no command or
# page that revokes it; `claude setup-token` only makes new ones. The claude.ai page below is
# where people report finding it; the docs do not confirm it.
REVOKE_CLAUDE_TOKEN = (
    "To end the old Claude token: sign in at claude.ai, open Settings, then Claude Code, and remove",
    "the token there if it is listed. Anthropic's documentation names no revoke command for setup-tokens,",
    "so if it is not listed, ask Anthropic support; otherwise it works until about a year after it was made.",
)


class Refuse(Exception):
    """A plain one-line reason. The programs print it and stop."""


# ---------------------------------------------------------------------------- names

def check_person(name: str) -> str:
    if not isinstance(name, str) or not PERSON_RX.fullmatch(name):
        raise Refuse("not a valid user name")
    return name


def check_name(name: str, allow_memory: bool = False) -> str:
    """A team folder's name: letters, digits, '.', '_', '-'; not starting with '.' or '-'."""
    if allow_memory and name == MEMORY:
        return name
    if not isinstance(name, str) or not NAME_RX.fullmatch(name) or name == MEMORY or name.endswith(".lock"):
        raise Refuse(f"{shown(name)} is not a usable team folder name (letters, digits, '.', '_', '-')")
    return name


def box_id(person: str, separate: Optional[str] = None) -> str:
    """The box of a person: '<person>', or '<person>-<folder>' for a --separate box."""
    check_person(person)
    if separate is None:
        return person
    b = (person + "-" + separate).lower()
    if not BOX_RX.fullmatch(b):
        raise Refuse(f"{shown(separate)} cannot name a separate box: use a short lower-case name")
    return b


def check_box(box: str) -> str:
    if not isinstance(box, str) or not BOX_RX.fullmatch(box):
        raise Refuse("not a valid box name")
    return box


def box_user(box: str) -> str:
    return "kap-" + check_box(box)


def share_group(box: str) -> str:
    return "kapshare-" + check_box(box)


def box_root(box: str) -> str:
    return f"{TEAM}/kap-{check_box(box)}"


def box_home(box: str) -> str:
    return box_root(box) + "/home"


def box_projects(box: str) -> str:
    return box_root(box) + "/projects"


def box_memory(box: str) -> str:
    return box_root(box) + "/memory"


def view_root(person: str) -> str:
    return f"{VIEW}/{check_person(person)}"


def view_path(person: str, name: str) -> str:
    return f"{view_root(person)}/{check_name(name, allow_memory=True)}"


def term(s) -> str:
    """One line that is safe at the person's terminal: every character that is not printable
    (escape and control characters, line breaks, tabs, direction overrides and other format
    characters) becomes '?', so no name or text can move the cursor, set the title or the
    clipboard, or start a forged line (red team round 2, finding 1)."""
    return "".join(c if c.isprintable() else "?" for c in str(s))


def shown(s, most: int = 80) -> str:
    """An untrusted value (a file name, an argument) for a message: printable, short, quoted."""
    return repr(term(s)[:most])


def unit_escape(s: str) -> str:
    """systemd-escape of one string: '/' becomes '-', anything but [A-Za-z0-9:_.] (and a leading
    '.') becomes \\xNN. So 'proj-a' gives 'proj\\x2da'."""
    out = []
    for i, ch in enumerate(s):
        if ch == "/":
            out.append("-")
        elif ch.isascii() and (ch.isalnum() or ch in ":_.") and not (i == 0 and ch == "."):
            out.append(ch)
        else:
            out.extend(f"\\x{b:02x}" for b in ch.encode())
    return "".join(out)


def unit_escape_path(path: str) -> str:
    """systemd-escape --path: the name of a .mount unit for this mount point."""
    p = "/".join(x for x in path.split("/") if x)
    return unit_escape(p) if p else "-"


def view_mount_unit(person: str, name: str) -> str:
    return unit_escape_path(view_path(person, name)) + ".mount"


def mount_unit_text(person: str, name: str, what: str) -> str:
    """The person's view of one team folder (4.10): a bind with nosymfollow,noexec,nosuid,nodev,
    so box-made symlinks are not followed and box-made programs do not run for the person, and
    private, so the covers kaproot.hide_view mounts in it never show in the box's own copy."""
    where = view_path(person, name)
    for p in (what, where):
        if not p.startswith("/") or any(c in p for c in " \t\n\\%$\"'") or "/../" in p + "/":
            raise Refuse("bad mount path")
    return (f"# Written by hubchat-root. {person}'s view of the team folder {name}.\n"
            "[Unit]\n"
            f"Description=kaptyorka: {person}'s view of team folder {name}\n"
            "[Mount]\n"
            f"What={what}\nWhere={where}\nType=none\n"
            "Options=bind,nosymfollow,noexec,nosuid,nodev,private\n"
            "[Install]\nWantedBy=local-fs.target\n")


def slice_name(box: str) -> str:
    """One flat slice per box (a '-' in a slice name would nest it under another box)."""
    return "kapbox-" + unit_escape(check_box(box)) + ".slice"


# ---------------------------------------------------------------------------- the team-folder decision (4.7)

def team_folder(real: str, person: str) -> Optional[Tuple[str, str]]:
    """(folder name, path inside it) when the real path `real` is /srv/team/<person>/<name> or
    below; otherwise None. A folder is a team folder only by where it really is; only root can
    put a folder there. No list from the hub is asked."""
    check_person(person)
    if not isinstance(real, str) or not real.startswith("/") or "\0" in real:
        return None
    base = f"{VIEW}/{person}/"
    if not real.startswith(base):
        return None
    name, _, sub = real[len(base):].partition("/")
    if name != MEMORY and not NAME_RX.fullmatch(name):
        return None
    if sub and any(p in ("", ".", "..") for p in sub.rstrip("/").split("/")):
        return None
    return name, sub.rstrip("/")


def under(path: str, top: str) -> bool:
    """path is top or below it (plain string test on normalised absolute paths)."""
    return path == top or path.startswith(top.rstrip("/") + "/")


# ---------------------------------------------------------------------------- folders.json

def parse_folders(text: str) -> Dict[str, dict]:
    """The person's team folders, by name. Strict: an entry that does not fit is an error, so a
    damaged file stops `enter` instead of being guessed at."""
    data = json.loads(text) if text.strip() else {"folders": []}
    out: Dict[str, dict] = {}
    for e in data.get("folders", []):
        name = check_name(e["name"])
        box = check_box(e["box"])
        short = e.get("shortcut", "")
        if short and (not isinstance(short, str) or not short.startswith("/") or "\0" in short
                      or "/../" in short + "/" or len(short) > 4096):
            raise Refuse(f"folders.json: bad shortcut for {name}")
        source = e.get("source", "")
        if source and parse_git_url(source)[0] != source:
            raise Refuse(f"folders.json: bad source for {name}")
        out[name] = {"name": name, "box": box, "shortcut": short, "source": source,
                     "separate": bool(e.get("separate", False))}
    return out


def dump_folders(folders: Dict[str, dict]) -> str:
    rows = [folders[k] for k in sorted(folders)]
    return json.dumps({"folders": rows}, indent=1, sort_keys=True) + "\n"


# ---------------------------------------------------------------------------- the walls (4.4)

LOCKED_FILES = (  # (source in /etc/kaptyorka/box, place in the box home): read-only file binds (5.2)
    ("claude/user-settings.json", "state/claude/settings.json"),
    ("codex/config.toml", "state/codex/config.toml"),
    ("codex/hooks.json", "state/codex/hooks.json"),
    ("opencode/opencode.json", "state/opencode/cfg/opencode/opencode.json"),
    ("cursor/mcp.json", ".cursor/mcp.json"),
    ("cursor/hooks.json", ".cursor/hooks.json"),
    ("cursor/sandbox.json", ".cursor/sandbox.json"),
    ("cursor/cli-config.json", ".cursor/cli-config.json"),
)
# OpenCode's global config folder must stay writable (D5), so the folders in it that would load
# code or agents into every later chat are root-owned, empty and each its own read-only mount
# (a root-owned folder alone could be renamed away; B-9)
OPENCODE_LOCKED_DIRS = ("agent", "agents", "plugin", "plugins", "command", "commands", "mode", "modes",
                        "tool", "tools", "skill", "skills")
OPENCODE_CFG = "state/opencode/cfg/opencode"
STATE_DIRS = ("state/claude", "state/codex", "state/opencode", "state/ssh", ".cursor", ".cache")
# /sys: ProtectKernelTunables= mounts the real /sys again over a TemporaryFileSystem=/sys
# (measured, systemd 255), so each of its standard folders gets its own empty tmpfs instead. The
# box keeps only the CPU list (the C library counts processors from it).
SYS_HIDDEN = ("block", "bus", "class", "dev", "devices", "firmware", "hypervisor", "kernel", "module", "power")


def box_props(person: str, box: str, runtime_max: str = "7d") -> List[str]:
    """The settings of every box unit (each chat, the helper, each git call), one per line:
    design v2 4.4 with the slice-0 changes D1 (no ProtectControlGroups=), D3 (@pkey), D4 (own
    exec-able tmpfs for /tmp), D5 (OpenCode config folder writable, its file locked), D10 (a
    writable ~/.cache). The same list for all, so the walls are the same everywhere."""
    check_person(person)
    check_box(box)
    if not RUNTIME_RX.fullmatch(runtime_max):
        raise Refuse("bad runtime limit")
    u, g, home = box_user(box), share_group(box), box_home(box)
    run = f"{RUNBOX}/{box}"
    locked = " ".join(f"{BOXCONF}/{src}:{home}/{dst}" for src, dst in LOCKED_FILES)
    return [
        f"User={u}", f"Group={u}", f"SupplementaryGroups={g}", f"Slice={slice_name(box)}", "UMask=0007",
        # files: an empty root, then named folders added. Measured on systemd 255: ProtectSystem=
        # (and DynamicUser=, which implies it) makes systemd drop TemporaryFileSystem=/ without a
        # word, so neither may ever be added here. wallcheck (W1, W2) proves the real view each start.
        "TemporaryFileSystem=/:ro",
        "BindReadOnlyPaths=/usr /bin /sbin /lib -/lib64 /etc/ssl -/etc/ca-certificates -/etc/alternatives "
        "/etc/ld.so.cache -/etc/localtime",
        "TemporaryFileSystem=/usr/local:ro", f"BindReadOnlyPaths={LIBEXEC}",   # not the computer's own /usr/local
        "TemporaryFileSystem=" + " ".join(f"/sys/{d}:ro" for d in SYS_HIDDEN),
        "BindReadOnlyPaths=/sys/devices/system/cpu",
        f"BindReadOnlyPaths={BOXES}/{box}/passwd:/etc/passwd {BOXES}/{box}/group:/etc/group "
        f"{BOXCONF}/nsswitch.conf:/etc/nsswitch.conf {BOXCONF}/hosts:/etc/hosts",
        f"BindReadOnlyPaths={TOOLS_DIR} {OPT}/box {BOXCONF} {BOXES}/{box}",
        f"BindPaths={home}",
        f"ReadOnlyPaths={home}",
        "ReadWritePaths=" + " ".join(f"{home}/{d}" for d in STATE_DIRS),
        f"BindReadOnlyPaths={locked}",
        "ReadOnlyPaths=" + " ".join(f"-{home}/{OPENCODE_CFG}/{d}" for d in OPENCODE_LOCKED_DIRS),
        # the tools' own managed settings, which outrank every user and project file (slice 5):
        # OpenCode's system folder, Codex's requirements
        f"BindReadOnlyPaths={BOXCONF}/opencode/etc:/etc/opencode {BOXCONF}/codex/requirements.toml:"
        "/etc/codex/requirements.toml",
        f"BindPaths={box_projects(box)}:{VIEW}/{person}",
        f"BindPaths={box_memory(box)}:{VIEW}/{person}/{MEMORY}",
        f"BindPaths={run}/tmp:/tmp {run}/vartmp:/var/tmp {run}/run:/run/kapbox",
        f"BindPaths={RELAY_DOORS}/{box}.sock {run}/pub/egress.sock:/run/egress.sock",
        # memory, devices, processes
        "TemporaryFileSystem=/dev/shm:mode=1777", "PrivateIPC=yes", "PrivateDevices=yes",
        # ProcSubset=pid: the box sees only process folders in /proc, not the computer's memory,
        # CPU counters, uptime or interrupts (red team round 1, B-17). Codex once stopped at its
        # first screen with it (its own pid-managed app-server needs the boot time from /proc/stat);
        # box-exec now opens Codex with --remote on the chat's own app-server, which never asks.
        "ProtectProc=invisible", "ProcSubset=pid", "TemporaryFileSystem=/sys/fs/cgroup:ro", "KeyringMode=private",
        # network: own namespace with only lo; the relay and the forwarder are socket files
        f"NetworkNamespacePath=/run/netns/kapbox-{box}",
        "RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6 AF_NETLINK",
        # rights
        "NoNewPrivileges=yes", "CapabilityBoundingSet=", "AmbientCapabilities=", "RestrictSUIDSGID=yes",
        "ProtectKernelTunables=yes", "ProtectKernelModules=yes", "ProtectKernelLogs=yes",
        "ProtectClock=yes", "ProtectHostname=yes", "LockPersonality=yes", "SystemCallArchitectures=native",
        "SystemCallFilter=@system-service", "SystemCallFilter=@pkey",
        "SystemCallFilter=~@debug @module @reboot @swap @raw-io @cpu-emulation @obsolete @clock",
        "SystemCallFilter=~process_vm_readv process_vm_writev kcmp pidfd_getfd",
        "RestrictNamespaces=yes", "LimitCORE=0",
        # limits and a hard end for the whole tree
        "TasksMax=4096", "MemoryMax=75%", "MemorySwapMax=0", "KillMode=control-group",
        "TimeoutStopSec=5", f"RuntimeMaxSec={runtime_max}",
        "Environment=PATH=/opt/kap-tools/bin:/usr/local/bin:/usr/bin:/bin LANG=C.UTF-8",
        "Environment=HTTPS_PROXY=http://127.0.0.1:3128 HTTP_PROXY=http://127.0.0.1:3128 "
        "ALL_PROXY=http://127.0.0.1:3128 NO_PROXY=",
    ]


def parse_limits(text: str) -> str:
    """runtime_max from /etc/kaptyorka/box.conf (root-owned); default 7d."""
    for line in text.splitlines():
        k, _, v = line.partition("=")
        if k.strip() == "runtime_max":
            v = v.strip()
            if not RUNTIME_RX.fullmatch(v):
                raise Refuse("box.conf: bad runtime_max")
            return v
    return "7d"


# ---------------------------------------------------------------------------- tool arguments (4.7 step 4)

MODEL_RX = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/\[\]@-]{0,99}")
ID_RX = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}")


def _choice(*xs):
    return re.compile("|".join(re.escape(x) for x in xs))


# per tool: plain flags, flags with a value (value pattern), flags with an optional id,
# and first words that are subcommands the box must not run (sign-in, servers, settings).
ARGS = {
    "claude": {
        "flags": {"-c", "--continue", "-p", "--print", "--verbose", "-v", "--version", "-h", "--help",
                  "--fork-session"},
        "valued": {"--model": MODEL_RX, "--output-format": _choice("text", "json", "stream-json"),
                   "--input-format": _choice("text", "stream-json"),
                   "--permission-mode": _choice("default", "acceptEdits", "plan")},
        "optional": {"-r": ID_RX, "--resume": ID_RX},
        "refused_sub": {"mcp", "plugin", "plugins", "config", "install", "update", "upgrade",
                        "setup-token", "login", "logout", "migrate-installer", "remote-control"},
    },
    "codex": {
        "flags": {"-h", "--help", "-V", "--version", "--last"},
        "valued": {"-m": MODEL_RX, "--model": MODEL_RX},
        "optional": {},
        "refused_sub": {"login", "logout", "mcp", "mcp-server", "app-server", "proto", "apply", "a",
                        "cloud", "sandbox", "debug", "responses-api-proxy", "features", "completion"},
    },
    "opencode": {
        "flags": {"-c", "--continue", "-h", "--help", "-v", "--version"},
        "valued": {"-s": ID_RX, "--session": ID_RX, "-m": MODEL_RX, "--model": MODEL_RX,
                   "--agent": ID_RX, "--format": _choice("default", "json")},
        "optional": {},
        "refused_sub": {"auth", "serve", "web", "mcp", "upgrade", "uninstall", "acp", "github",
                        "debug", "import", "attach", "plugin"},
    },
    "cursor-agent": {
        "flags": {"-p", "--print", "-h", "--help", "-v", "--version"},
        "valued": {"--model": MODEL_RX, "--output-format": _choice("text", "json", "stream-json")},
        "optional": {"--resume": ID_RX},
        "refused_sub": {"login", "logout", "mcp", "update", "upgrade", "install-shell-integration",
                        "uninstall-shell-integration"},
    },
}


def filter_args(tool: str, args: List[str]) -> List[str]:
    """The tool's arguments when every flag is on the tool's short list; otherwise Refuse with
    one line. Prompts (plain words) pass as they are. git's arguments pass as they are: git runs
    inside the box. Values are never changed, only checked."""
    if tool not in TOOLS:
        raise Refuse(f"{shown(tool)} is not offered in team folders")
    if tool == "git":
        return list(args)
    spec = ARGS[tool]
    out: List[str] = []
    i, first_word, rest_plain = 0, True, False
    while i < len(args):
        a = args[i]
        if rest_plain or a == "-" or not a.startswith("-"):
            if first_word and a in spec["refused_sub"]:
                raise Refuse(f"{tool} {a} is not offered in team folders")
            first_word = False
            out.append(a)
            i += 1
            continue
        if a == "--":
            rest_plain = True
            out.append(a)
            i += 1
            continue
        flag, eq, val = a.partition("=")
        if flag in spec["flags"] and not eq:
            out.append(a)
            i += 1
        elif flag in spec["valued"]:
            if not eq:
                if i + 1 >= len(args):
                    raise Refuse(f"{tool} {flag} needs a value")
                val = args[i + 1]
            if not spec["valued"][flag].fullmatch(val):
                raise Refuse(f"{tool} {flag}: value {shown(val)} is not offered")
            out.extend([a] if eq else [a, val])
            i += 1 if eq else 2
        elif flag in spec["optional"]:
            if eq:
                if not spec["optional"][flag].fullmatch(val):
                    raise Refuse(f"{tool} {flag}: value {shown(val)} is not offered")
                out.append(a)
                i += 1
            elif i + 1 < len(args) and spec["optional"][flag].fullmatch(args[i + 1]):
                out.extend([a, args[i + 1]])
                i += 2
            else:
                out.append(a)
                i += 1
        else:
            raise Refuse(f"{tool} {flag} is not offered in team folders (it could load settings, "
                         "plugins or folders from outside the box)")
    return out


# ---------------------------------------------------------------------------- git URLs (4.9)

_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
# a host NAME: dotted labels, the last one not all digits (so never an IP address)
HOST_RX = re.compile(rf"(?=.{{1,253}}$){_LABEL}(?:\.{_LABEL})*\.(?![0-9]+$){_LABEL}")
_PART = r"[A-Za-z0-9_][A-Za-z0-9._~-]{0,99}"
GPATH_RX = re.compile(rf"{_PART}(?:/{_PART}){{0,9}}")
GIT_URL_RX = [
    ("https", re.compile(rf"https://(?P<host>[a-z0-9.-]+)(?::(?P<port>443))?/(?P<path>[^?#\s]+)")),
    ("ssh", re.compile(rf"ssh://git@(?P<host>[a-z0-9.-]+)(?::(?P<port>22))?/(?P<path>[^?#\s]+)")),
    ("scp", re.compile(rf"git@(?P<host>[a-z0-9.-]+):(?P<path>[^?#\s]+)")),
]


def parse_git_url(url: str) -> Tuple[str, str, int, str]:
    """(url, host, port, default folder name) for https://host/path, ssh://git@host/path or
    git@host:path, and nothing else: no ext::, file://, local paths, options, user names,
    credentials, IP addresses, '..' or other ports. Refuse otherwise."""
    if not isinstance(url, str) or len(url) > 400 or url.startswith("-"):
        raise Refuse("not an allowed git URL")
    for kind, rx in GIT_URL_RX:
        m = rx.fullmatch(url)
        if not m:
            continue
        host, path = m.group("host"), m.group("path")
        if not HOST_RX.fullmatch(host):
            break
        try:
            ipaddress.ip_address(host)
            break  # names only: the forwarder's allow list is by name
        except ValueError:
            pass
        if not GPATH_RX.fullmatch(path):
            break
        port = 443 if kind == "https" else 22
        name = path.rsplit("/", 1)[-1]
        if name.endswith(".git"):
            name = name[:-4]
        return url, host, port, name
    raise Refuse("not an allowed git URL (use https://host/path, ssh://git@host/path or git@host:path)")


def looks_like_url(s: str) -> bool:
    return "://" in s or bool(re.match(r"[A-Za-z0-9._-]+@[A-Za-z0-9.-]+:", s))


# ---------------------------------------------------------------------------- the forwarder's allow list (4.5)

def parse_allow(text: str) -> Dict[str, set]:
    """host -> allowed ports. Lines: 'host' (port 443) or 'host:22'; '#' starts a comment.
    Lines that do not fit are ignored (the file is root-owned; a bad line allows nothing)."""
    out: Dict[str, set] = {}
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip().lower()
        if not line:
            continue
        host, _, port = line.partition(":")
        if not HOST_RX.fullmatch(host) or port not in ("", "443", "22"):
            continue
        out.setdefault(host, set()).add(int(port) if port else 443)
    return out


def allow_line(host: str, port: int) -> str:
    if not HOST_RX.fullmatch(host) or port not in (443, 22):
        raise Refuse("not an allowed host")
    return host if port == 443 else f"{host}:22"


def allow_add(text: str, host: str, port: int) -> Tuple[str, bool]:
    """(new text, whether it changed)."""
    if port in parse_allow(text).get(host, set()):
        return text, False
    if text and not text.endswith("\n"):
        text += "\n"
    return text + allow_line(host, port) + "  # hubchat add\n", True


def allow_remove(text: str, host: str, port: int) -> str:
    want = allow_line(host, port)
    keep = [ln for ln in text.splitlines() if ln.split("#", 1)[0].strip().lower() != want]
    return "\n".join(keep) + ("\n" if keep else "")


# ---------------------------------------------------------------------------- git made safe (4.9, 7.1)

_GBOOL = re.compile(r"true|false|yes|no|on|off|1|0")
_GKEEP = {  # (section, key) -> value pattern; a section with a subsection is marked by '*'
    ("core", "repositoryformatversion"): re.compile(r"[01]"),
    ("core", "filemode"): _GBOOL, ("core", "bare"): _GBOOL, ("core", "ignorecase"): _GBOOL,
    ("core", "logallrefupdates"): re.compile(r"true|false|always"),
    ("core", "symlinks"): _GBOOL, ("core", "precomposeunicode"): _GBOOL,
    ("extensions", "objectformat"): re.compile(r"sha1|sha256"),
    ("extensions", "refstorage"): re.compile(r"files|reftable"),
    ("remote*", "url"): None, ("remote*", "pushurl"): None,
    ("remote*", "fetch"): re.compile(r"\+?[A-Za-z0-9/_.*:-]{1,200}"),
    ("branch*", "remote"): re.compile(r"[A-Za-z0-9._-]{1,100}"),
    ("branch*", "merge"): re.compile(r"refs/[A-Za-z0-9/_.-]{1,200}"),
    ("branch*", "rebase"): re.compile(r"true|false|merges|interactive"),
}
_SUBNAME = re.compile(r"[A-Za-z0-9._/-]{1,100}")


def safe_git_config(text: str) -> Tuple[str, List[str]]:
    """A repository config with only plain keys: format version, remotes (safe URLs only),
    branch tracking. Everything else (fsmonitor, hooksPath, sshCommand, pager, aliases,
    includes, filters, credential helpers, uploadpack ...) is dropped. Returns (new text, names
    of dropped keys). Parsed here by a small strict reader, never by git (root never runs git on
    box-written data); lines it does not understand are dropped."""
    keep: List[Tuple[str, Optional[str], str, str]] = []
    dropped: List[str] = []
    section, sub = None, None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line[0] in "#;":
            continue
        m = re.fullmatch(r'\[\s*([A-Za-z0-9.-]+)\s*(?:"([^"\\]*)")?\s*\]\s*(?:[#;].*)?', line)
        if m:
            section, sub = m.group(1).lower(), m.group(2)
            if "." in section:   # [section.sub] old form: not kept
                section, sub = section + "?", None
            continue
        if line.startswith("["):
            section, sub = None, None
            dropped.append(line[:60])
            continue
        m = re.fullmatch(r'([A-Za-z][A-Za-z0-9-]*)\s*(?:=\s*(.*))?', line)
        if not m or section is None:
            dropped.append(line[:60])
            continue
        key, val = m.group(1).lower(), (m.group(2) or "true").strip()
        name = f"{section}.{sub + '.' if sub else ''}{key}"
        if val.endswith("\\") or re.search(r'\s[#;]', val):
            dropped.append(name)
            continue
        if len(val) >= 2 and val[0] == val[-1] == '"' and "\\" not in val and '"' not in val[1:-1]:
            val = val[1:-1]
        elif '"' in val or "\\" in val:
            dropped.append(name)
            continue
        rule_key = (section + ("*" if sub is not None else ""), key)
        if rule_key not in _GKEEP or (sub is not None and not _SUBNAME.fullmatch(sub)):
            dropped.append(name)
            continue
        rx = _GKEEP[rule_key]
        if rx is None:
            try:
                parse_git_url(val)
            except Refuse:
                dropped.append(name)
                continue
        elif not rx.fullmatch(val):
            dropped.append(name)
            continue
        keep.append((section, sub, key, val))
    out: List[str] = []
    last = None
    for section, sub, key, val in keep:
        head = (section, sub)
        if head != last:
            out.append(f'[{section}]' if sub is None else f'[{section} "{sub}"]')
            last = head
        out.append(f"\t{key} = {val}")
    return ("\n".join(out) + "\n") if out else "", dropped


# ---------------------------------------------------------------------------- hubchat add checks (4.9)

SECRET_NAMES = re.compile(
    r"\.env(\..+)?|id_(rsa|dsa|ecdsa|ed25519)(_sk)?|.+\.(pem|key|p12|pfx|kdbx|ovpn)|\.npmrc|\.netrc"
    r"|\.pypirc|\.git-credentials|credentials(\.json)?|\.credentials\.json|auth\.json|secrets?\.(json|ya?ml|toml)"
    r"|.*\.tfstate|kubeconfig|\.dockercfg|config\.json\.bak")
SYNC_NAMES = {"dropbox", "nextcloud", "owncloud", "onedrive", "google drive", "googledrive",
              "pcloud", "pclouddrive", "mega", "megasync", "sync", "icloud drive", "box sync", "seafile"}
SYNC_MARKERS = (".dropbox", ".dropbox.cache", ".stfolder", ".sync", ".nextcloudsync.log",
                ".owncloudsync.log", ".sync_journal.db", ".seafile-data")


def is_secret_name(name: str) -> bool:
    return bool(SECRET_NAMES.fullmatch(name)) and not name.endswith(".pub")


# What a secret looks like inside a file, whatever its name (red team round 1, finding 2: a hard
# link to ~/.ssh/id_ed25519 named "hardlink-to-key" passed the name check). Only the first
# CONTENT_SAMPLE bytes of a file are read; a file over CONTENT_MAX_SIZE or with a NUL byte in the
# sample is not looked into.
CONTENT_SAMPLE = 4096
CONTENT_MAX_SIZE = 1 << 20
SECRET_CONTENT = (
    (re.compile(rb"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----"), "a private key"),
    (re.compile(rb"sk-ant-[A-Za-z0-9_-]{16,}"), "an Anthropic key or token (sk-ant-)"),
    (re.compile(rb"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{30,}|\bgithub_pat_[A-Za-z0-9_]{30,}"), "a GitHub token"),
    (re.compile(rb"\bglpat-[A-Za-z0-9_-]{20,}"), "a GitLab token (glpat-)"),
    (re.compile(rb"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), "an AWS access key (AKIA)"),
    (re.compile(rb"\bxox[abposr]-[A-Za-z0-9-]{10,}"), "a Slack token (xox)"),
    (re.compile(rb"\bAIza[0-9A-Za-z_-]{35}"), "a Google API key (AIza)"),
    (re.compile(rb"\bhf_[A-Za-z0-9]{30,}"), "a Hugging Face token (hf_)"),
    (re.compile(rb"\bnpm_[A-Za-z0-9]{36}\b"), "an npm token (npm_)"),
    (re.compile(rb'"(?:accessToken|refreshToken|access_token|refresh_token|oauth[A-Za-z_]*|password|'
                rb'client_secret|private_key|privateKey|clientSecret)"\s*:\s*"[^"\s]{6,}"', re.I),
     "credentials in JSON (a token, password or client secret)"),
    # upper-case names only, as environment files write them (code's max_tokens = ... is not one)
    (re.compile(rb"(?m)^[ \t]*(?:export[ \t]+)?[A-Z_][A-Z0-9_]*(?:SECRET|TOKEN|PASSWORD|PASSWD|API_?KEY|"
                rb"PRIVATE_?KEY|ACCESS_?KEY|CREDENTIALS?)[A-Z0-9_]*[ \t]*=[ \t]*[\"']?[^\s\"'#$]{8,}"),
     "a KEY=secret line (.env style)"),
)


def secret_in_content(sample: bytes) -> Optional[str]:
    """The reason a file's first bytes look like a secret, or None. Binary-looking samples
    (a NUL byte) are not looked into."""
    if b"\0" in sample:
        return None
    for rx, reason in SECRET_CONTENT:
        if rx.search(sample):
            return reason
    return None


def in_synced_folder(parts: List[str]) -> Optional[str]:
    """parts: the path components from the home down to the folder. A component named like a
    sync tool's folder."""
    for p in parts:
        if p.lower() in SYNC_NAMES or p.lower().startswith(("dropbox (", "nextcloud-", "onedrive -")):
            return p
    return None


# ---------------------------------------------------------------------------- safe.directory (7.1)

def safe_directory_warnings(lines: List[Tuple[str, str]], shortcuts: List[str]) -> List[str]:
    """lines: (origin file, value) of every safe.directory entry the person's git sees.
    A warning for each entry that switches git's ownership check off for a team folder."""
    out = []
    for origin, value in lines:
        v = value.strip()
        if v.endswith("*"):        # '*' (everything) or '<dir>/*' (everything below, git 2.46+)
            top = v[:-1].rstrip("/") or "/"
            bad = any(under(t, top) or under(top, t) for t in (VIEW, TEAM)) or any(under(s, top) for s in shortcuts)
        else:
            p = v.rstrip("/") or "/"
            bad = under(p, VIEW) or under(p, TEAM) or p in shortcuts
        if bad:
            out.append(f"WARNING: git's ownership check is switched off for team folders by "
                       f"'safe.directory = {v}' in {origin}. Team chats could then run code as you "
                       f"through git. Remove that line.")
    return out
