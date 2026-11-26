#!/usr/bin/python3 -I
"""kaptyorka-secure-setup (secure design v2, 9.1, 9.2, 9.6): install and remove the personal-laptop
mode. Root, run once by the person: sudo python3 -I kap_setup.py install --personal '<invite link>'

  install --personal '<https invite link>' [--name NAME] [--person NAME] [--claude-token-file FILE]
          [--runtime-max 7d] [--tools-from DIR]
  remove [--person NAME] [--delete-box-home] [--yes]

install: checks the computer; redeems the personal invite over TLS pinned to the link's own
fingerprint (the device token comes back; the hub's address is the one actually connected to);
makes the box user and groups; writes the files of 4.2 (root programs, launcher, box configs,
units); installs relay, forwarder and box code; makes the namespace, relay door, forwarder,
canary; stores the Claude token root-only; makes the box's SSH key; adds the launcher to the
system shell files; runs the self-check. The mode is fixed here (/etc/kaptyorka/mode = personal)
and never changes afterwards.

For tests only, instead of the link: --hub-ip IP --hub-port N --hub-sha256 HEX
--device-token-file FILE (a token redeemed some other way). Not in the usage text.

remove: hands every team folder back (made safe, as hubchat remove), stops and deletes every
unit, file, user and group it made, the namespace and the launcher lines.

Every outside value is checked against a strict pattern before root uses it. From the hub only
the device token is taken (a strict pattern); nothing else it answers reaches a file or a unit.
The tools' own sign-ins are slice 5.
"""
import grp
import hashlib
import hmac
import http.client
import ipaddress
import json
import os
import pwd
import re
import secrets
import shutil
import ssl
import subprocess
import sys

if __package__:
    from . import kapcommon as kc, kaproot as kr
else:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import kapcommon as kc
    import kaproot as kr

Refuse = kc.Refuse
USAGE = """Install or remove kaptyorka's team box on a personal laptop (run with sudo).

Install, with the personal invite link your team admin sent you (keep the quotes):
  sudo python3 -I kap_setup.py install --personal 'https://HUB:PORT/i/CODE#sha256=FINGERPRINT'
  It asks for a name for this computer on the hub and for the token from `claude setup-token`
  (run that as yourself first). Or give them: --name THIS-COMPUTER --claude-token-file FILE.
  More: --person NAME (default: you), --runtime-max 7d (longest a team chat runs).

Remove (every team folder goes back to you first):
  sudo /usr/local/libexec/kaptyorka/kaptyorka-secure-setup remove [--delete-box-home] [--yes]"""
HERE = os.path.dirname(os.path.abspath(__file__))
FILES = os.path.join(HERE, "files")
RELAY_SRC = os.path.join(os.path.dirname(HERE), "relay")
SYSTEMD = kr.SYSTEMD
HOOK_BEGIN, HOOK_END = "# >>> kaptyorka >>>", "# <<< kaptyorka <<<"
FP_RX = re.compile(r"[0-9a-f]{64}")
CNAME_RX = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._-]{0,31}")   # also within the relay's name pattern
TOKEN_RX = kc.TOKEN_RX
PROGRAMS = (  # (source in this folder, name in /usr/local/libexec/kaptyorka, mode)
    ("kap_enter.py", "enter", 0o755), ("kap_hubchat.py", "hubchat-root", 0o755),
    ("kap_hubchat.py", "kap_hubchat.py", 0o644), ("kap_wallcheck.py", "wallcheck", 0o755),
    ("kap_box_exec.py", "box-exec", 0o755), ("kap_canary.py", "canary", 0o755),
    ("kap_addrwatch.py", "addrwatch", 0o755), ("kap_guard.py", "guard", 0o755),
    ("kap_setup.py", "kaptyorka-secure-setup", 0o755), ("kapcommon.py", "kapcommon.py", 0o644),
    ("kaproot.py", "kaproot.py", 0o644), ("files/launch", "launch", 0o755),
    ("files/kapbox-base", "kapbox-base", 0o755),
)
OUR_UNITS = ("kapbox-base@.service", "kaptyorka-egress@.socket", "kaptyorka-egress@.service",
             "kaptyorka-canary.service", "kaptyorka-relay.service", "kaptyorka-relay-box@.socket",
             "kaptyorka-addrwatch.service", "kaptyorka-guard.service")


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
    # what covers a tool file in a person's view (kaproot.hide_view), and the shims first on her PATH
    kr.write_file(kc.HIDE_FILE, "", 0o444)
    kr.write_file(kc.HIDE_JSON, "{}\n", 0o444)
    kr.mkdir(kc.SHIMS, 0o755)
    for tool in ("claude", "opencode", "codex", "cursor-agent"):
        kr.write_file(f"{kc.SHIMS}/{tool}", f"#!/bin/sh\n# kaptyorka: {tool} through the launcher (a team folder: the box)\n"
                                            f"exec {kc.LIBEXEC}/launch {tool} \"$@\"\n", 0o755)
    kr.mkdir(kc.OPT, 0o755)
    for part, files in (("egress", {"kap_egress.py": "egress.py"}), ("box", {"kap_proxybridge.py": "proxybridge.py",
                                                                         "kap_helper.py": "helper.py",
                                                                         "kap_fence.py": "fence.py",
                                                                         "kap_toolwire.py": "toolwire.py"}),
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
    install_roots()


# The root folders of the relay and the forwarder (their units' RootDirectory=): root-owned and
# empty but for the places their binds go. systemd makes any other mount point it needs.
ROOTS = {
    "relay-root": ("bin dev etc/alternatives etc/kaptyorka lib lib64 opt/kaptyorka/relay proc run/kaptyorka/box-secrets "
                   "sbin sys tmp usr var/tmp", "etc/passwd etc/ld.so.cache etc/kaptyorka/relay.conf"),
    "egress-root": ("bin dev etc/alternatives etc/kaptyorka/allow lib lib64 opt/kaptyorka/egress proc run sbin sys tmp "
                    "usr var/tmp", "etc/ld.so.cache etc/hosts etc/resolv.conf etc/nsswitch.conf etc/gai.conf "
                                   "etc/host.conf"),
}


def install_roots() -> None:
    for root, (dirs, files) in ROOTS.items():
        top = f"{kc.OPT}/{root}"
        kr.mkdir(top, 0o755)
        for d in dirs.split():
            p = top
            for part in d.split("/"):
                p += "/" + part
                kr.mkdir(p, 0o755)
        for f in files.split():
            if not os.path.lexists(f"{top}/{f}"):
                kr.write_file(f"{top}/{f}", "", 0o644)


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
    claude_execute_only()


def claude_execute_only() -> None:
    """The claude program execute-only (0711): the kernel then makes claude non-dumpable, so the
    commands it runs (same box user) cannot read its token from /proc/<pid>/environ or its memory
    (red team round 1, finding 1; measured with Claude Code 2.1.332). box-exec refuses to hand the
    token to a claude it can read."""
    p = os.path.realpath(f"{kc.TOOLS_DIR}/bin/claude")
    if kc.under(p, kc.TOOLS_DIR) and os.path.isfile(p) and not os.path.islink(p):
        os.chmod(p, 0o711)


def install_etc(ver: str) -> None:
    kr.mkdir(kc.ETC, 0o755)
    kr.write_file(f"{kc.ETC}/version", ver + "\n")
    for d in ("box", "box/claude", "box/codex", "box/opencode", "box/opencode/etc", "box/opencode/etc/plugins",
              "box/cursor"):
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
    try:
        grp.getgrnam(kc.RELAY_GROUP)
    except KeyError:     # the relay's group for the box sign-in values (kaproot.write_box_secrets)
        kr.run(["groupadd", "--system", kc.RELAY_GROUP])
    for n in os.listdir(os.path.join(FILES, "units")):
        copy(os.path.join(FILES, "units", n), f"{SYSTEMD}/{n}", 0o644)
    for n in ("kaptyorka-relay.service", "kaptyorka-relay-box@.socket"):
        copy(os.path.join(RELAY_SRC, "units", n), f"{SYSTEMD}/{n}", 0o644)
    copy(os.path.join(FILES, "kaptyorka-tmpfiles.conf"), "/etc/tmpfiles.d/kaptyorka.conf", 0o644)
    kr.systemctl("daemon-reload")
    kr.run(["systemd-tmpfiles", "--create", "/etc/tmpfiles.d/kaptyorka.conf"], check=False)
    kr.systemctl("enable", "--now", "kaptyorka-canary.service")
    kr.systemctl("enable", "kaptyorka-addrwatch.service", "kaptyorka-guard.service")
    # a new release runs its new code
    kr.systemctl("restart", "kaptyorka-addrwatch.service", "kaptyorka-guard.service", check=False)


def check_local_values(a) -> dict:
    """The values given on this computer (name, Claude token, tools), checked before anything
    is sent or written (9.2, I-1)."""
    if not CNAME_RX.fullmatch(a.get("--name") or ""):
        raise Refuse("--name: letters, digits, '.', '_', '-', at most 32, not starting with '.' or '-'")
    out = {"name": a["--name"], "claude": ""}
    if a.get("--claude-token-file"):
        out["claude"] = open(a["--claude-token-file"]).read().strip()
        if not TOKEN_RX.fullmatch(out["claude"]):
            raise Refuse("the Claude token does not look like one (claude setup-token prints it)")
    if a.get("--tools-from") and not os.path.isdir(os.path.join(a["--tools-from"], "bin")):
        raise Refuse("--tools-from needs a folder with bin/<tool>")
    return out


def check_relay_values(a) -> dict:
    """Tests only: the hub's values and a device token file given by hand, checked like the
    invite's (9.2, I-1)."""
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
    out = check_local_values(a)
    tok = open(a["--device-token-file"]).read().strip()
    if not TOKEN_RX.fullmatch(tok):
        raise Refuse("the device token does not look like one")
    return {"ip": ip, "port": int(port), "fp": fp, "token": tok, **out}


# ---------------------------------------------------------------------------- the invite (9.2, 9.5)

ASK_ADMIN = "ask your team admin for a new personal link (kaptyorka invite <you> --personal)"
UNCHANGED = " Nothing was changed on this computer."
PLAIN_HTTP = ("this link starts with http://, and a personal laptop joins only over an encrypted "
              "connection: ask your team admin to switch it on (kaptyorka tls on, then tls only) and "
              "send you a new link." + UNCHANGED)
INVITE_RX = re.compile(r"https://(\[[0-9A-Fa-f:.]{2,45}\]|[A-Za-z0-9][A-Za-z0-9.-]{0,252})(?::([0-9]{1,5}))?"
                       r"/i/([A-Za-z0-9_-]{1,64})/?")
HOST_RX = re.compile(r"[a-z0-9][a-z0-9.-]{0,252}")
MAX_ANSWER = 64 * 1024


def parse_invite(link: str) -> dict:
    """The invite link's parts, each checked: {"host", "port", "code", "fp"}. The fingerprint
    comes only from the link (9.5): without one, no connection is made."""
    link = (link or "").strip()
    if re.match(r"http://", link, re.I):
        raise Refuse(PLAIN_HTTP)
    base, hashmark, frag = link.partition("#")
    m = INVITE_RX.fullmatch(base)
    if not m:
        raise Refuse("that is not an invite link (expected 'https://<hub>:<port>/i/<code>#sha256=<fingerprint>'; "
                     "copy the whole link, in quotes)." + UNCHANGED)
    fm = re.fullmatch(r"sha256=([0-9A-Fa-f]{64})", frag)
    if not fm:
        raise Refuse("the link has no fingerprint at its end (#sha256= and 64 letters and digits): copy the "
                     "whole link, in quotes, or " + ASK_ADMIN + "." + UNCHANGED)
    host, port, code = m.group(1).lower(), m.group(2), m.group(3)
    if host.startswith("["):
        try:
            if ipaddress.ip_address(host[1:-1]).version != 6:
                raise ValueError
        except ValueError:
            raise Refuse("the link's hub address is not an address." + UNCHANGED)
    elif not HOST_RX.fullmatch(host) or ".." in host:
        raise Refuse("the link's hub name is not a name." + UNCHANGED)
    port = int(port) if port else 443
    if not 0 < port < 65536:
        raise Refuse("the link's port is not a port number." + UNCHANGED)
    return {"host": host, "port": port, "code": code, "fp": fm.group(1).lower()}


class CertMismatch(OSError):
    pass


class PinnedConnection(http.client.HTTPSConnection):
    """https that accepts the hub only by its certificate's SHA-256 fingerprint, checked before
    any byte of the request goes out (the relay's own way, relay.PinnedConnection)."""

    def __init__(self, host: str, port: int, pin: str, timeout: float):
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE  # the fingerprint decides
        super().__init__(host, port, timeout=timeout, context=ctx)
        self._pin = pin

    def connect(self):
        super().connect()
        der = self.sock.getpeercert(binary_form=True)
        if not der or not hmac.compare_digest(hashlib.sha256(der).hexdigest(), self._pin):
            self.sock.close()
            raise CertMismatch()


def hub_refusal(status: int, said: str) -> str:
    """One plain sentence and a next step for a hub that did not take the invite."""
    said = said.strip()
    if status == 400 and said in ("invite already used",):
        return "this link was already used (a link works once): " + ASK_ADMIN + "." + UNCHANGED
    if status == 400 and said in ("no such invite", "invite expired"):
        return "this link is not valid any more (cancelled, or older than 7 days): " + ASK_ADMIN + "." + UNCHANGED
    if status == 400 and ("not for a personal laptop" in said or "joins as a team machine" in said):
        return ("this link is for a team machine (kaptyorka join), not for a personal laptop: "
                + ASK_ADMIN + "." + UNCHANGED)
    if status == 429:
        return "the hub had too many wrong tries with this link: wait an hour and run this again." + UNCHANGED
    words = kc.term(said.split("\n", 1)[0])[:120]
    return (f"the hub refused this link ({status}{': ' + words if words else ''}): " + ASK_ADMIN + "."
            + UNCHANGED)


def redeem(inv: dict, name: str, timeout: float = 20) -> dict:
    """Use the personal invite over the pinned connection: {"ip", "port", "fp", "token"}. The
    address is the one this connection reached (the relay has no DNS); of the hub's answer only
    the device token is taken, and only when it fits its pattern and the answer says personal."""
    conn = PinnedConnection(inv["host"], inv["port"], inv["fp"], timeout)
    where = f"{inv['host']}:{inv['port']}"
    try:
        try:
            conn.connect()
        except CertMismatch:
            raise Refuse(f"the hub at {where} is not the one in the link (its fingerprint differs), so nothing "
                         "was sent: check that you copied the whole link, or ask your team admin whether the "
                         "hub's certificate changed and for a new link." + UNCHANGED)
        except (OSError, ssl.SSLError):
            raise Refuse(f"cannot reach the hub at {where} over an encrypted connection: check that this "
                         "computer is online (and on the team's network or VPN), then run this again." + UNCHANGED)
        try:
            peer = ipaddress.ip_address(conn.sock.getpeername()[0])
        except (OSError, ValueError, IndexError):
            raise Refuse("cannot tell which address the hub has." + UNCHANGED)
        if peer.version == 6 and peer.ipv4_mapped:
            peer = peer.ipv4_mapped
        if peer.is_link_local or peer.is_multicast or peer.is_unspecified:
            raise Refuse(f"the hub answered from {peer}, which this computer cannot keep using: "
                         "ask your team admin for a link with the hub's usual address." + UNCHANGED)
        body = json.dumps({"device_name": name, "mode": "personal"})
        try:
            conn.request("POST", f"/i/{inv['code']}", body, {"Content-Type": "application/json"})
            r = conn.getresponse()
            raw = r.read(MAX_ANSWER + 1)
        except (OSError, ssl.SSLError, http.client.HTTPException):
            raise Refuse(f"the hub at {where} stopped answering: run this again in a minute." + UNCHANGED)
    finally:
        conn.close()
    if len(raw) > MAX_ANSWER:
        raise Refuse("the hub's answer is too long; it is not a kaptyorka hub's: ask your team admin." + UNCHANGED)
    if r.status != 200:
        raise Refuse(hub_refusal(r.status, raw.decode("utf-8", "replace")))
    try:
        got = json.loads(raw.decode("utf-8"))
    except (ValueError, RecursionError):
        got = None
    if not isinstance(got, dict) or got.get("mode") != "personal" or not isinstance(got.get("token"), str) \
            or not TOKEN_RX.fullmatch(got["token"]):
        raise Refuse("the hub's answer is not a personal laptop's key, so it was not used: "
                     + ASK_ADMIN + "." + UNCHANGED)
    return {"ip": str(peer), "port": inv["port"], "fp": inv["fp"], "token": got["token"]}


def ask_name(a) -> None:
    if a.get("--name") or not sys.stdin.isatty():
        return
    a["--name"] = input("A name for this computer on the hub, such as laptop-2 (not your user or host "
                        "name): ").strip()


def ask_claude_token(person: str, v: dict) -> None:
    """Asked before the invite is used, so stopping here costs no link."""
    if v["claude"] or not sys.stdin.isatty() or os.path.exists(f"{kc.SECRET}/{person}/claude-oauth-token"):
        return
    import getpass
    tok = getpass.getpass("Paste the token from `claude setup-token` (run it as yourself), or Enter to skip: ").strip()
    if tok and not TOKEN_RX.fullmatch(tok):
        raise Refuse("that does not look like a token")
    v["claude"] = tok


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
        strip_hook(t)            # an older release's lines make way for this one's
        text = kr.read_text(t)
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
    kr.ensure_box(person, person, kr.read_text(f"{kc.BOXCONF}/egress-base"))
    sudoers = (f"# kaptyorka (secure design v2, 4.2): {person} may start team chats and use hubchat. Nothing else.\n"
               f"{person} ALL=(root) NOPASSWD: {kc.ENTER}, {kc.HUBCHAT_ROOT}\n")
    tmp = f"/etc/sudoers.d/.kaptyorka-{person}.new"
    kr.write_file(tmp, sudoers, 0o440)
    kr.run(["visudo", "-cf", tmp])
    os.replace(tmp, f"/etc/sudoers.d/kaptyorka-{person}")


BY_HAND = ("--hub-ip", "--hub-port", "--hub-sha256", "--device-token-file")   # tests only


def install(a) -> int:
    person = a.get("--person") or os.environ.get("SUDO_USER", "")
    kc.check_person(person)
    if not a.get("--personal"):
        raise Refuse("say --personal (a team machine is installed with pipx install kaptyorka and kaptyorka join)")
    link = a.get("link")
    by_hand = [k for k in BY_HAND if a.get(k)]
    if link and by_hand:
        raise Refuse("give the invite link alone: " + " ".join(by_hand) + " are for tests")
    if not link and len(by_hand) < len(BY_HAND):
        raise Refuse("give the personal invite link your team admin sent you, in quotes:  sudo python3 -I "
                     "kap_setup.py install --personal 'https://<hub>:<port>/i/<code>#sha256=<fingerprint>'")
    rt = a.get("--runtime-max") or "7d"
    if not kc.RUNTIME_RX.fullmatch(rt):
        raise Refuse("--runtime-max: like 7d, 12h, 90min or infinity")
    # every value is checked, and the computer too, before the link is used: a refusal costs no link
    inv = parse_invite(link) if link else None
    if inv:
        ask_name(a)
        if not a.get("--name"):
            raise Refuse("add --name THIS-COMPUTER: the name this computer gets on the hub (not your user or "
                         "host name), such as laptop-2." + UNCHANGED)
    try:
        v = check_local_values(a) if inv else check_relay_values(a)
    except OSError as e:
        raise Refuse(f"cannot read a token file: {e.strerror}")
    stop, warn = preflight(person)
    for w in warn:
        say("warning: " + w)
    if stop:
        for s in stop:
            print(kc.term("kaptyorka-secure-setup: " + s), file=sys.stderr)
        if inv:
            say("Nothing was changed on this computer, and the link was not used: fix the above and run this again.")
        return 2
    ask_claude_token(person, v)
    if inv:
        say(f"Joining the hub at {inv['host']}:{inv['port']} (fingerprint {inv['fp'][:16]}...) as {v['name']} ...")
        v.update(redeem(inv, v["name"]))
        say(f"Joined: this computer is {v['name']} on the hub (address {v['ip']}).")
    ver = version()
    say(f"Installing kaptyorka {ver} for {person}, personal-laptop mode ...")
    install_code(ver)
    if a.get("--tools-from"):
        install_tools(a["--tools-from"])
    claude_execute_only()
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
                "--addrs", ",".join(kr.host_addrs()), "--canaries", ",".join(kr.shared_canaries()),
                "--", "/bin/true"], check=False)
    if r.returncode != 0:
        say("Self-check (inside the box): PROBLEMS", *["  " + ln for ln in (r.stderr or r.stdout).strip()[-400:].splitlines()])
        return 3
    say("Self-check: all walls in place. " + " ".join(r.stderr.split()))
    pub = kr.read_text(f"{kc.box_home(person)}/.ssh/id_ed25519.pub").strip()
    say("", f"Done. This computer is {v['name']} on the hub; team chats reach it only through the box.",
        "Next steps:",
        "  1. Log out and in once (or restart the computer). Until then your own programs get",
        "     \"Permission denied\" in team folders.",
        "  2. Make a folder a team folder:   hubchat add ~/path/to/folder",
        "     or get a repository as one:    hubchat add <git URL>",
        "  3. cd into it and run claude, codex, opencode or cursor-agent as usual.",
        *([] if v["claude"] else ["  Claude in the box has no token yet: run  claude setup-token  as yourself, then",
                                  "  hubchat token-rotate  and paste it."]),
        *([f"  Delete the token file now: rm {kc.shown(a['--claude-token-file'], 200)}"]
          if a.get("--claude-token-file") else []),
        *(["For private repositories: add the box's own SSH key to your git account (as an extra key):",
           "  " + pub] if pub else []))
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
                hc.remove(person, name, uninstall=True)
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
        kr.systemctl("disable", "--now", "kaptyorka-canary.service", "kaptyorka-addrwatch.service",
                     "kaptyorka-guard.service", check=False)
        kr.systemctl("stop", "kaptyorka-relay.service", check=False)
        for n in OUR_UNITS:
            if os.path.lexists(f"{SYSTEMD}/{n}"):
                os.unlink(f"{SYSTEMD}/{n}")
        shutil.rmtree(f"{SYSTEMD}/kaptyorka-relay.service.d", ignore_errors=True)
        for p in ("/etc/tmpfiles.d/kaptyorka.conf", "/etc/profile.d/kaptyorka.sh", "/etc/fish/conf.d/kaptyorka.fish",
                  "/usr/local/bin/hubchat", *kc.CANARY_FILES):
            if os.path.lexists(p):
                os.unlink(p)
        for p in kr.read_text(kc.CANARY_STATE).split():   # ours only: a name in the list, root's file
            if kc.CANARY_SHARED_RX.fullmatch(p) and kr.canary_ok(p):
                os.unlink(p)
        for sl in ("kapbox.slice", "system-kapbox\\x2dbase.slice", "system-kaptyorka\\x2degress.slice",
                   "system-kaptyorka\\x2drelay\\x2dbox.slice"):   # parents of the units above, now empty
            kr.systemctl("stop", sl, check=False)
        kr.systemctl("daemon-reload", check=False)
        kr.systemctl("reset-failed", check=False)
        for t in ("/etc/bash.bashrc", "/etc/zsh/zshenv"):
            strip_hook(t)
        kr.run(["groupdel", kc.RELAY_GROUP], check=False)
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
        "Open a new terminal (or log out and in): then claude, codex, opencode and cursor-agent are",
        "your own again everywhere.",
        *([] if delete_home else ["The box homes (team memory, the box's sign-ins) were kept. To delete them:",
                                  f"  sudo rm -rf {kc.TEAM}"]),
        "Last, end what the box used:",
        "  - the box's Codex, Cursor and OpenCode sign-ins: on each vendor's account page",
        "  - the box's SSH key: in your git account",
        "  - this computer on the hub: ask your team admin",
        *kc.REVOKE_CLAUDE_TOKEN)
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
        if pos[:1] == ["install"] and len(pos) <= 2:
            if len(pos) == 2:
                a["link"] = pos[1]
            return install(a)
        if pos == ["remove"]:
            return remove(a)
        print(USAGE)
        return 2
    except BrokenPipeError:   # `... | head`: the reader left, nothing to say
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())   # no second error at exit
        return 0
    except Refuse as e:
        print(kc.term(f"kaptyorka-secure-setup: {e}"), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
