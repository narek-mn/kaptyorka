"""The AI tools inside the box (secure design v2, 9.2, 9.4, 5.2): installed into /opt/kap-tools by
each vendor's own official installer, never copied from the person's home.

The vendor installer runs in a throw-away systemd unit: a fresh user of its own (DynamicUser=, so
it shares nothing with the person or the box), no home of anyone, its own empty state folder as
HOME, and a network that reaches only that vendor's hosts (IPAddressDeny=any plus their addresses,
looked up by root just before, and a hosts file with only those names: there is no other name
lookup). Root never runs vendor code: it reads the result (the program, its folder and the
version it printed inside the unit), checks it and copies it, root-owned and read-only for
everyone else, to /opt/kap-tools/<tool>/<version>; /opt/kap-tools/bin/<tool> points there.
The version of each tool is recorded in /etc/kaptyorka/tools.json (root-owned); the box runs
exactly that one and never updates it itself (each tool's auto-update is off in its box config).
`hubchat update-tools` (root through the person's sudo rule) installs a newer one on request.

Vendor docs for each method:
  claude        https://code.claude.com/docs/en/setup  "curl -fsSL https://claude.ai/install.sh | bash",
                a specific version as its first argument ("Install a specific version")
  codex         https://github.com/openai/codex (README, "Installing and running Codex CLI")
                "curl -fsSL https://chatgpt.com/codex/install.sh | sh"; CODEX_RELEASE for a version,
                CODEX_NON_INTERACTIVE=1 to skip prompts (the script's own usage text)
  opencode      https://opencode.ai/docs (Install) "curl -fsSL https://opencode.ai/install | bash",
                --version <v> and --no-modify-path (the script's usage text); OpenCode 2 from
                https://opencode.ai/v2/install (the same script options)
  cursor-agent  https://cursor.com/docs/cli/installation  "curl https://cursor.com/install -fsS | bash"
                (no version option: it installs the version the script names)
"""
import grp
import hashlib
import json
import os
import re
import secrets
import shutil
import socket
import stat
import sys
import time

if __package__:
    from . import kapcommon as kc, kaproot as kr
else:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import kapcommon as kc
    import kaproot as kr

Refuse = kc.Refuse
NAMES = ("claude", "codex", "opencode", "cursor-agent")
RECORD = kc.ETC + "/tools.json"
LOCK = "/run/kaptyorka-tools.lock"
STAGE_TOP = "/var/lib"       # systemd's StateDirectory= place; a DynamicUser's is in STAGE_TOP/private
RUNDIR = "/run/kaptyorka"
VER_RX = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.]{1,30})?")
SPEC_RX = re.compile(r"(claude|codex|opencode|cursor-agent)(?:=([0-9]+(?:\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.]{1,30})?)?))?")
GOT_RX = re.compile(r"(?<![0-9.])([0-9]+\.[0-9]+(?:\.[0-9]+)?(?:[-+][0-9A-Za-z.]{1,40})?)")
MAX_BYTES = 1536 << 20       # one tool's folder at most
MAX_FILES = 20000
UNIT_LIMIT = 600             # seconds one vendor installer may run
PROBE_S = 8                  # seconds a vendor host has to answer before the installs start
GH = ("github.com", "api.github.com", "objects.githubusercontent.com", "release-assets.githubusercontent.com")

# tool: (script URL, hosts it reaches, the program it leaves in HOME, copy its folder or the file)
VENDOR = {
    "claude": ("https://claude.ai/install.sh", ("claude.ai", "downloads.claude.ai"),
               ".local/bin/claude", "file"),
    "codex": ("https://chatgpt.com/codex/install.sh", ("chatgpt.com", "releases.openai.com") + GH,
              ".local/bin/codex", "dir"),
    "opencode": ("https://opencode.ai/install", ("opencode.ai", "raw.githubusercontent.com", "registry.npmjs.org") + GH,
                 ".opencode/bin/opencode", "file"),
    "opencode2": ("https://opencode.ai/v2/install", ("opencode.ai", "raw.githubusercontent.com", "registry.npmjs.org"),
                  ".opencode/bin/opencode", "file"),
    "cursor-agent": ("https://cursor.com/install", ("cursor.com", "downloads.cursor.com"),
                     ".local/bin/cursor-agent", "dir"),
}

# The script the unit runs: fixed text; the tool, URL and version come in as checked variables.
SCRIPT = r'''set -eu
cd "$HOME"
curl -fsSL --proto '=https' --tlsv1.2 --max-time 120 -o install.sh "$KAP_URL"
case "$KAP_TOOL" in
  claude) bash install.sh ${KAP_VERSION:+"$KAP_VERSION"} ;;
  codex) CODEX_NON_INTERACTIVE=1 CODEX_RELEASE="${KAP_VERSION:-latest}" sh install.sh ;;
  opencode|opencode2) bash install.sh --no-modify-path ${KAP_VERSION:+--version "$KAP_VERSION"} ;;
  cursor-agent) bash install.sh ;;
esac
"$HOME/$KAP_PROGRAM" --version > "$HOME/.kap-version" 2>&1 || true
'''

# Where a person's own copy of a tool usually is: looked at by name only (never run, never read)
PERSON_BIN = (".local/bin", ".opencode/bin", "bin", ".npm-global/bin", ".bun/bin", ".yarn/bin", ".cargo/bin")
SYSTEM_BIN = ("/usr/local/bin", "/usr/bin", "/bin", "/snap/bin", "/opt/homebrew/bin", "/home/linuxbrew/.linuxbrew/bin")


def parse_specs(text: str):
    """'claude,codex=0.172.0' -> {"claude": "", "codex": "0.172.0"}; 'none' -> {}. Strict."""
    text = (text or "").strip()
    if text in ("none", ""):
        return {}
    out = {}
    for part in text.replace(" ", ",").split(","):
        if not part:
            continue
        m = SPEC_RX.fullmatch(part)
        if not m:
            raise Refuse(f"{kc.shown(part, 40)}: a tool is claude, codex, opencode or cursor-agent, with =VERSION "
                         "(like codex=0.172.0, or opencode=2 for OpenCode 2) if you want one version")
        if m.group(2) and m.group(1) != "opencode" and not VER_RX.fullmatch(m.group(2)):
            raise Refuse(f"{kc.shown(part, 40)}: a version looks like 1.2.3")
        out[m.group(1)] = m.group(2) or ""
    return out


def detect(home: str):
    """The tools the person already has, by name only: an entry with that name in the usual
    folders of their own PATH (never run, never read, links not followed)."""
    dirs = [os.path.join(home, d) for d in PERSON_BIN] + list(SYSTEM_BIN)
    found = []
    for t in NAMES:
        if any(os.path.lexists(os.path.join(d, t)) for d in dirs if not kc.under(d, kc.LIBEXEC)):
            found.append(t)
    return found


def vendor_of(tool: str, ver: str):
    """(key in VENDOR, version to ask for): OpenCode 2 has its own installer."""
    if tool == "opencode" and ver.split(".")[0].isdigit() and int(ver.split(".")[0]) >= 2:
        return "opencode2", ("" if ver.isdigit() else ver)
    return tool, ver


def addresses(hosts):
    """{host: [addresses]} as root resolves them now: the unit's only way to each name."""
    out = {}
    for h in hosts:
        try:
            got = socket.getaddrinfo(h, 443, type=socket.SOCK_STREAM)
        except OSError:
            continue
        ips = []
        for fam, _, _, _, sa in got:
            ip = sa[0]
            if ip not in ips and fam in (socket.AF_INET, socket.AF_INET6):
                ips.append(ip)
        if ips:
            out[h] = ips
    return out


def hosts_text(addrs) -> str:
    lines = ["127.0.0.1 localhost", "::1 localhost"]
    for h in sorted(addrs):
        for ip in addrs[h]:
            lines.append(f"{ip} {h}")
    return "\n".join(lines) + "\n"


def unit_cmd(unit: str, key: str, tool: str, ver: str, stage: str, hostsfile: str, addrs) -> list:
    url, _, program, _ = VENDOR[key]
    ips = " ".join(sorted({ip for v in addrs.values() for ip in v}))
    cmd = ["systemd-run", "--quiet", "--collect", "--wait", "--pipe", f"--unit={unit}", "--expand-environment=no"]
    props = [
        "DynamicUser=yes", f"StateDirectory={os.path.basename(stage)}", f"WorkingDirectory=/var/lib/{os.path.basename(stage)}",
        f"Environment=HOME=/var/lib/{os.path.basename(stage)}", "Environment=PATH=/usr/bin:/bin",
        "Environment=LANG=C.UTF-8", f"Environment=KAP_TOOL={key}", f"Environment=KAP_URL={url}",
        f"Environment=KAP_VERSION={ver}", f"Environment=KAP_PROGRAM={program}",
        "ProtectSystem=strict", "ProtectHome=yes", "PrivateTmp=yes", "PrivateDevices=yes", "PrivateIPC=yes",
        "ProtectProc=invisible", "ProcSubset=pid", "ProtectKernelTunables=yes", "ProtectKernelModules=yes",
        "ProtectKernelLogs=yes", "ProtectClock=yes", "ProtectHostname=yes", "ProtectControlGroups=yes",
        "NoNewPrivileges=yes", "CapabilityBoundingSet=", "AmbientCapabilities=", "RestrictSUIDSGID=yes",
        "RestrictNamespaces=yes", "LockPersonality=yes", "RestrictRealtime=yes", "SystemCallArchitectures=native",
        "SystemCallFilter=@system-service", "SystemCallFilter=~@privileged @mount @debug @module @reboot @swap",
        "RestrictAddressFamilies=AF_INET AF_INET6", "TemporaryFileSystem=/dev/shm", "IPAddressDeny=any", f"IPAddressAllow={ips}",
        f"BindReadOnlyPaths={hostsfile}:/etc/hosts",
        "InaccessiblePaths=-/etc/kaptyorka -/var/lib/kap-team -/srv/team -/run/kapbox -/run/kaptyorka -/opt/kaptyorka "
        "-/opt/kap-tools -/root -/etc/sudoers.d -/etc/ssh -/var/lib/kaptyorka",
        "UMask=0022", "MemoryMax=3G", "MemorySwapMax=0", "TasksMax=256", "CPUQuota=200%",
        f"RuntimeMaxSec={UNIT_LIMIT}", "KillMode=control-group", "TimeoutStopSec=5", "LimitCORE=0",
    ]
    for p in props:
        cmd += ["-p", p]
    return cmd + ["--", "/bin/bash", "-c", SCRIPT]


def _inside(path: str, top: str) -> bool:
    return path == top or path.startswith(top.rstrip("/") + "/")


def collect(stage: str, key: str):
    """(source path to copy, path of the program inside it, version): from the unit's state
    folder, every path checked to stay inside it."""
    _, _, program, how = VENDOR[key]
    real = os.path.realpath(os.path.join(stage, program))
    if not _inside(real, stage) or not os.path.isfile(real) or os.path.islink(real):
        raise Refuse(f"the {key} installer left no program at ~/{program}")
    got = GOT_RX.search(kr.read_text(os.path.join(stage, ".kap-version"), 4096))
    if not got:
        raise Refuse(f"the {key} program did not say its version")
    ver = got.group(1)[:40]
    if how == "file":
        return real, os.path.basename(real), ver
    top = os.path.dirname(real)
    if top == stage:
        raise Refuse(f"the {key} installer put its program straight in its home")
    return top, os.path.relpath(real, top), ver


def copy_tree(src: str, dst: str) -> None:
    """Root-owned copy, modes masked to rwxr-xr-x (no set-id bits); a link is kept only when it
    stays inside the copy; anything else (devices, pipes) is left out. Every file must belong to
    whoever owns src (the installer's own user): a hard link to someone else's file is refused."""
    total = files = 0
    owner = os.lstat(src).st_uid
    if os.path.isfile(src):
        _copy_file(src, os.path.join(dst, os.path.basename(src)), owner)
        return
    for dirpath, dirs, names in os.walk(src):
        rel = os.path.relpath(dirpath, src)
        here = os.path.normpath(os.path.join(dst, rel))
        kr.mkdir(here, 0o755)
        for n in sorted(dirs + names):
            s, d = os.path.join(dirpath, n), os.path.join(here, n)
            st = os.lstat(s)
            if stat.S_ISLNK(st.st_mode):
                target = os.readlink(s)
                if os.path.isabs(target) or not _inside(os.path.realpath(s), os.path.realpath(src)):
                    raise Refuse(f"the installer left a link that points outside its folder: {kc.shown(n, 60)}")
                os.symlink(target, d)
            elif stat.S_ISREG(st.st_mode):
                files += 1
                total += st.st_size
                if files > MAX_FILES or total > MAX_BYTES:
                    raise Refuse("the tool's folder is bigger than any tool should be")
                _copy_file(s, d, owner)


def _copy_file(s: str, d: str, owner: int) -> None:
    fd = os.open(s, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    st = os.fstat(fd)
    if not stat.S_ISREG(st.st_mode) or st.st_uid != owner:
        os.close(fd)
        raise Refuse(f"the installer left a file that is not its own: {kc.shown(os.path.basename(s), 60)}")
    mode = st.st_mode
    with os.fdopen(fd, "rb") as f:
        tmp = d + ".kapnew"
        with open(tmp, "wb") as out:
            shutil.copyfileobj(f, out, 1 << 20)
        if os.geteuid() == 0:
            os.chown(tmp, 0, 0)
        os.chmod(tmp, (mode & 0o755) | 0o444)
        os.replace(tmp, d)


def load_record():
    try:
        got = json.loads(kr.read_text(RECORD) or "{}")
    except ValueError:
        got = {}
    return {k: v for k, v in got.items() if k in NAMES and isinstance(v, dict)} if isinstance(got, dict) else {}


def save_record(rec) -> None:
    kr.write_file(RECORD, json.dumps(rec, indent=1, sort_keys=True) + "\n", 0o644)


def point(tool: str, target_rel: str) -> None:
    """/opt/kap-tools/bin/<tool> -> ../<tool>/<version>/<program>, swapped in one rename."""
    link = f"{kc.TOOLS_DIR}/bin/{tool}"
    tmp = link + ".kapnew"
    if os.path.lexists(tmp):
        os.unlink(tmp)
    os.symlink(target_rel, tmp)
    os.replace(tmp, link)


def prune(tool: str, keep) -> None:
    """Old versions go, but the one before stays: a chat that runs it keeps its files."""
    top = f"{kc.TOOLS_DIR}/{tool}"
    try:
        names = sorted(os.listdir(top), key=lambda n: os.stat(os.path.join(top, n)).st_mtime)
    except OSError:
        return
    for n in names[:-2]:
        if n not in keep:
            shutil.rmtree(os.path.join(top, n), ignore_errors=True)


def unreachable(specs) -> str:
    """'' when each tool's vendor answers (a TCP connection to its first host's port 443 within
    PROBE_S seconds); else that host's name. Before the long installs: an offline computer fails at
    once instead of waiting out each installer's time limit (red team round 5)."""
    for tool in [t for t in NAMES if t in specs]:
        host = VENDOR[vendor_of(tool, specs[tool])[0]][1][0]
        try:
            socket.create_connection((host, 443), timeout=PROBE_S).close()
        except OSError:
            return host
    return ""


def install_one(tool: str, ver: str, say=kr.say) -> str:
    """One tool from its vendor's installer into /opt/kap-tools; returns the version installed."""
    key, ask = vendor_of(tool, ver)
    url, hosts, program, how = VENDOR[key]
    if unreachable({tool: ver}):
        raise Refuse(f"cannot reach {hosts[0]}, where {tool} comes from: is this computer online?")
    addrs = addresses(hosts)
    if not addrs:
        raise Refuse(f"cannot look up {hosts[0]}: is this computer online?")
    name = f"kaptyorka-tool-stage-{tool}"
    stage = f"{STAGE_TOP}/private/{name}"
    shutil.rmtree(stage, ignore_errors=True)
    if os.path.islink(f"{STAGE_TOP}/{name}"):
        os.unlink(f"{STAGE_TOP}/{name}")
    kr.mkdir(RUNDIR, 0o755)
    hostsfile = f"{RUNDIR}/tool-hosts-{tool}"
    kr.write_file(hostsfile, hosts_text(addrs), 0o644)
    unit = f"kaptyorka-tool-install-{tool}-{secrets.token_hex(3)}"
    say(f"Installing {tool}{' ' + ver if ver else ''} into the box with its vendor's installer ({url}) ...")
    try:
        r = kr.run(unit_cmd(unit, key, tool, ask, f"/var/lib/{name}", hostsfile, addrs), check=False,
                   timeout=UNIT_LIMIT + 30)
        if r.returncode != 0:
            tail = " ".join((r.stderr or r.stdout).strip().splitlines()[-3:])[-300:]
            raise Refuse(f"{tool}: its installer failed ({kc.term(tail) or 'exit ' + str(r.returncode)})")
        src, inner, got = collect(stage, key)
        dst = f"{kc.TOOLS_DIR}/{tool}/{got}"
        kr.mkdir(kc.TOOLS_DIR, 0o755)
        kr.mkdir(f"{kc.TOOLS_DIR}/bin", 0o755)
        kr.mkdir(f"{kc.TOOLS_DIR}/{tool}", 0o755)
        shutil.rmtree(dst, ignore_errors=True)
        kr.mkdir(dst, 0o755)
        try:
            copy_tree(src, dst)
        except (Refuse, OSError):
            shutil.rmtree(dst, ignore_errors=True)    # no half copy is left
            raise
        prog = os.path.join(dst, inner)
        with open(prog, "rb") as f:
            digest = hashlib.sha256(f.read()).hexdigest()
        point(tool, f"../{tool}/{got}/{inner}")
        if tool == "claude":
            os.chmod(prog, 0o711)   # execute-only: the token's guard (kap_setup.claude_execute_only)
        rec = load_record()
        rec[tool] = {"version": got, "program": f"{tool}/{got}/{inner}", "sha256": digest, "from": url,
                     "installed": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        save_record(rec)
        prune(tool, {got})
        say(f"  {tool} {got} is in the box ({kc.TOOLS_DIR}/bin/{tool}).")
        return got
    finally:
        shutil.rmtree(stage, ignore_errors=True)
        for p in (f"{STAGE_TOP}/{name}", hostsfile):
            if os.path.lexists(p):
                os.unlink(p)


def install(specs, say=kr.say):
    """Each tool of specs ({tool: version or ""}), one at a time; (done, failed)."""
    import fcntl
    fd = os.open(LOCK, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise Refuse("the box's tools are being installed already: wait for that to finish")
        done, failed = {}, {}
        for tool in [t for t in NAMES if t in specs]:
            try:
                done[tool] = install_one(tool, specs[tool], say)
            except (Refuse, OSError) as e:
                failed[tool] = str(e)
                say(f"  {tool}: not installed: {e}")
        return done, failed
    finally:
        os.close(fd)


def installed():
    """{tool: version} from the record, only while its program is really there."""
    out = {}
    for t, v in load_record().items():
        if os.path.exists(f"{kc.TOOLS_DIR}/bin/{t}"):
            out[t] = v.get("version", "?")
    return out
