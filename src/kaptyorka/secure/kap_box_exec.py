#!/usr/bin/python3 -I
"""box-exec (secure design v2, 4.7 and 5.2): the last step inside the box before the tool. It
reads the Claude token from the file systemd opened for it (OpenFile=, fd name claude-token),
closes that file, hands the token to claude on a descriptor (never in an environment), sets each
tool's own folders, and replaces itself with the tool from
/opt/kap-tools, keeping its pid (KAP_CHAT_PID names it to the helper's hooks). For a Codex
window it first starts that chat's own app-server as a child; for an OpenCode 1 window it picks
the port of the window's own server; for an OpenCode 2 window it makes the session in the box's
service first (slice 5). Runs as the box user, after wallcheck. Installed as
/usr/local/libexec/kaptyorka/box-exec. Standard library only.

Usage: box-exec <claude|opencode|codex|cursor-agent|git> args...
"""
import base64
import json
import os
import pwd
import re
import secrets
import socket
import string
import subprocess
import sys
import time
import urllib.error
import urllib.request
from typing import Optional

TOOLS_BIN = "/opt/kap-tools/bin"
TOKEN_FD = 3
BOXES = "/etc/kaptyorka/boxes"
BOXCONF = "/etc/kaptyorka/box"
VIEW = "/srv/team"
CHATS = "/run/kapbox/chats"          # the helper's chat records (kap_helper.STATE)
CODEX_RUN = "/run/kapbox/codex"      # one private app-server socket per Codex chat
# macOS (TEST VERSION), as a box user: the real places, the box's own runtime folder (kap_mac.box_run)
if sys.platform == "darwin" and pwd.getpwuid(os.getuid()).pw_name.startswith("kap-"):
    VIEW = "/Users/Shared/kaptyorka"
    _RUN = f"/private/var/run/kapbox/{pwd.getpwuid(os.getuid()).pw_name[4:]}"
    CHATS, CODEX_RUN = _RUN + "/chats", _RUN + "/codex"
# Codex subcommands that are not its terminal UI; anything else starts the TUI
CODEX_SUBS = {"exec", "e", "review", "login", "logout", "mcp", "mcp-server", "app-server", "completion",
              "sandbox", "debug", "apply", "a", "resume", "fork", "cloud", "features", "help", "update",
              "remote-control", "proto", "responses-api-proxy"}
HELP_FLAGS = {"-h", "--help", "-v", "-V", "--version"}


def take_token(env) -> str:
    """The token from the passed-in file, if there is one. Every passed descriptor is closed."""
    n = int(env.get("LISTEN_FDS", "0") or 0) if env.get("LISTEN_PID") == str(os.getpid()) else 0
    names = env.get("LISTEN_FDNAMES", "").split(":")
    token = ""
    for i in range(n):
        fd = 3 + i
        try:
            if i < len(names) and names[i] == "claude-token":
                token = os.read(fd, 4096).decode("ascii", "replace").strip()
        finally:
            try:
                os.close(fd)
            except OSError:
                pass
    for k in ("LISTEN_FDS", "LISTEN_PID", "LISTEN_FDNAMES"):
        env.pop(k, None)
    return token


def tui(tool: str, args) -> bool:
    """Whether these arguments start the tool's terminal UI (where a chat lives), not a
    subcommand or --help/--version."""
    if any(a in HELP_FLAGS for a in args):
        return False
    first = next((a for a in args if not a.startswith("-")), None)
    if tool == "codex":
        return first is None or first not in CODEX_SUBS
    if tool == "opencode":
        return first is None or os.path.isdir(first)
    return False


def plan(tool: str, args, env, home: str, box: str, token: str, pid: int = 0, oc: Optional[dict] = None):
    """(program, argv, env) for one tool. Pure, for the tests. pid is box-exec's own process,
    which the tool keeps through exec: KAP_CHAT_PID tells the helper's hooks which process is the
    chat (to close it from the page). oc: for OpenCode, {"major", "port", "session"} as main found."""
    env = dict(env)
    if tool != "git" and pid > 1:
        env["KAP_CHAT_PID"] = str(pid)
    if tool == "claude":
        # No CLAUDE_CODE_SUBPROCESS_ENV_SCRUB=1: Claude Code 2.1.332 then refuses to start without
        # bubblewrap ("bubblewrap is required for subprocess env scrubbing and isolation", measured
        # in slice 2), and bubblewrap does not nest in the box (D9). The token is in no environment
        # anyway (hand_token), and the box's environment holds no other secret.
        env.update(CLAUDE_CONFIG_DIR=f"{home}/state/claude", DISABLE_AUTOUPDATER="1")
        if token:
            # not in the environment: on a descriptor (see hand_token)
            env["CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR"] = str(TOKEN_FD)
        prog = f"{TOOLS_BIN}/claude"
        argv = [prog, "--settings", f"{BOXES}/{box}/claude-settings.json",
                "--mcp-config", f"{BOXCONF}/claude/mcp.json", "--strict-mcp-config", *args]
    elif tool == "codex":
        # Codex's limits are in /etc/codex/requirements.toml (the box's own, read-only): managed
        # hooks only, allowed sandbox modes and approval policies. A terminal UI talks to its
        # own app-server (codex_server), so the helper can reach this one chat.
        env["CODEX_HOME"] = f"{home}/state/codex"
        prog = f"{TOOLS_BIN}/codex"
        argv = [prog, *args]
        if pid > 1 and tui("codex", args):
            sock = codex_socket(pid)
            env["KAP_CODEX_SOCKET"] = sock
            argv = [prog, "--remote", f"unix://{sock}", *args]
    elif tool == "opencode":
        env.update(oc_env(home))
        # An OpenCode window talks HTTP to its own server (V1 --port) or the box's service (V2) on
        # 127.0.0.1, and it honours HTTP_PROXY: without this the box's proxy gets those calls and
        # the forwarder refuses them (403, measured in slice 5). Loopback is the box's own
        # network namespace, so nothing outside the box becomes reachable.
        env["NO_PROXY"] = env["no_proxy"] = "127.0.0.1,localhost,::1"
        prog = f"{TOOLS_BIN}/opencode"
        argv = [prog, *args]
        oc = oc or {}
        if oc.get("major") == 1 and oc.get("port"):
            # an OpenCode 1 window runs its own server only with --port; on the box's own lo
            env["KAP_OPENCODE_URL"] = f"http://127.0.0.1:{oc['port']}"
            argv = [prog, "--port", str(oc["port"]), "--hostname", "127.0.0.1", *args]
        elif oc.get("major") == 2 and oc.get("session"):
            argv = [prog, "-s", oc["session"], *args]   # made in the box's service first (main)
    elif tool == "cursor-agent":
        env.update(AGENT_CLI_CREDENTIAL_STORE="file", XDG_CACHE_HOME=f"{home}/.cache")
        prog = f"{TOOLS_BIN}/cursor-agent"
        argv = [prog, *args]
    elif tool == "git":
        env["GIT_TERMINAL_PROMPT"] = "0"
        prog = "/usr/bin/git"
        argv = [prog, *args]
    else:
        raise SystemExit("box-exec: unknown tool")
    return prog, argv, env


def oc_env(home: str) -> dict:
    s = f"{home}/state/opencode"   # the same folders as the helper's (kap_toolwire.oc_env)
    return {"XDG_CONFIG_HOME": f"{s}/cfg", "XDG_DATA_HOME": f"{s}/data", "XDG_STATE_HOME": f"{s}/state",
            "XDG_CACHE_HOME": f"{s}/cache", "OPENCODE_DISABLE_AUTOUPDATE": "1"}


def codex_socket(pid: int) -> str:
    return f"{CODEX_RUN}/{pid}/app.sock"


def codex_server(prog: str, env: dict, sock: str, wait: float = 20) -> int:
    """Start this chat's own Codex app-server (`codex app-server --listen unix://PATH`,
    learn.chatgpt.com/docs/app-server) as a child: it stays in the chat's unit, so its cgroup
    ends it with the chat (KillMode=control-group), and the parent's end signals it too. Its
    folder is the box user's, mode 0700 (Codex refuses a socket folder others could change).
    Returns its pid once the socket answers."""
    d = os.path.dirname(sock)
    os.makedirs(CODEX_RUN, mode=0o700, exist_ok=True)
    os.makedirs(d, mode=0o700, exist_ok=True)
    os.chmod(d, 0o700)
    logdir = f"{env.get('CODEX_HOME', '/tmp')}/log"
    os.makedirs(logdir, mode=0o700, exist_ok=True)
    out = os.open(f"{logdir}/kaptyorka-app-server.log", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    child = os.fork()
    if child == 0:
        try:
            import ctypes
            if sys.platform != "darwin":   # macOS has no PDEATHSIG: the chat's process group ends it
                ctypes.CDLL(None).prctl(1, 15, 0, 0, 0)   # PR_SET_PDEATHSIG: SIGTERM when the chat ends
            os.setpgid(0, 0)   # not in the terminal's group: a ctrl-c is the chat's, not its
            null = os.open(os.devnull, os.O_RDONLY)
            os.dup2(null, 0)
            os.dup2(out, 1)
            os.dup2(out, 2)
            os.execve(prog, [prog, "app-server", "--listen", f"unix://{sock}"], env)
        finally:
            os._exit(127)
    os.close(out)
    end = time.monotonic() + wait
    while time.monotonic() < end:
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                s.settimeout(1)
                s.connect(sock)
                return child
        except OSError:
            pass
        if os.waitpid(child, os.WNOHANG)[0]:
            break
        time.sleep(0.2)
    raise SystemExit("box-exec: Codex's app-server for this chat did not start (see state/codex/log)")


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def oc_major(prog: str, env: dict) -> int:
    try:
        r = subprocess.run([prog, "--version"], capture_output=True, text=True, timeout=20, env=env)
    except (OSError, subprocess.SubprocessError):
        return 0
    m = re.search(r"(\d+)\.\d+", r.stdout + r.stderr)
    return int(m.group(1)) if m else 0


def oc_new_session(env: dict, cwd: str, wait: float = 20) -> str:
    """OpenCode 2: make this window's session in the box's own service first (POST /api/session
    with an id: "If a session with this ID already exists, it is returned unchanged",
    opencode.ai/docs/api), so the window opens it (-s) and its chat record names its process.
    "" when the service does not answer (the window then makes its own; no close from the page)."""
    path = f"{env['XDG_STATE_HOME']}/opencode/service.json"
    sid = "ses_" + secrets.token_hex(6) + "".join(secrets.choice(string.ascii_letters + string.digits)
                                                    for _ in range(14))
    end = time.monotonic() + wait
    while time.monotonic() < end:
        try:
            with open(path) as f:
                info = json.load(f)
            url = str(info["url"]).rstrip("/")
            if not url.startswith("http://127.0.0.1:"):
                return ""
            auth = base64.b64encode(f"opencode:{info.get('password') or ''}".encode()).decode()
            req = urllib.request.Request(url + "/api/session", method="POST",
                                         data=json.dumps({"id": sid, "location": {"directory": cwd}}).encode(),
                                         headers={"Authorization": f"Basic {auth}", "Content-Type": "application/json"})
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(req, timeout=5) as r:
                if r.status == 200:
                    return sid
        except (OSError, ValueError, KeyError, TypeError, urllib.error.URLError):
            pass
        time.sleep(0.5)
    return ""


def write_chat(chat: str, tool: str, pid: int, cwd: str, extra=None) -> None:
    """A chat record for the helper (kap_helper.write_record's shape), for a chat whose hooks
    run where its own process is not known (an OpenCode 2 window: its plugin runs in the
    service)."""
    parts = cwd[len(VIEW) + 1:].split("/") if cwd.startswith(VIEW + "/") else []
    if len(parts) < 2:
        return
    try:
        with open(f"/proc/{pid}/stat") as f:
            pstart = f.read().rsplit(")", 1)[1].split()[19]
    except (OSError, IndexError):
        pstart = None
    now = time.time()
    rec = {"chat": chat, "tool": tool, "pid": pid, "pstart": pstart, "cwd": cwd, "person": parts[0],
           "project": parts[1], "started": now, "last": now, "state": "idle", **(extra or {})}
    os.makedirs(CHATS, mode=0o700, exist_ok=True)
    name = re.sub(r"[^A-Za-z0-9._-]", "_", chat)[:150]
    tmp = f"{CHATS}/{name}.json.{pid}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(rec, f)
    os.replace(tmp, f"{CHATS}/{name}.json")


def token_readable_problem(prog: str) -> str:
    """Why the Claude token would be readable by the box's own commands, or "". The token is in
    claude's environment. The kernel makes a process non-dumpable when it runs a program its
    user may not read, and then /proc/<pid>/environ and /proc/<pid>/mem belong to root, so a
    command claude starts (the same box user) cannot read them (measured, red team round 1
    finding 1). The installer makes the claude program execute-only (0711) for that."""
    try:
        if os.access(os.path.realpath(prog), os.R_OK):
            return "the claude program can be read by the box"
    except OSError as e:
        return os.strerror(e.errno or 0)
    return ""


def hand_token(token: str, fd: int = TOKEN_FD) -> None:
    """Put the token on descriptor fd for claude: one end of a socket pair, the token and a newline
    already written into it, the other end closed. Claude Code reads a socket named by
    CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR up to the newline, closes it and drops the variable
    from its own environment (read in Claude Code 2.1.332; not in its documentation), so the token
    is in no environment: not claude's /proc/<pid>/environ, not the commands it starts. A command
    that got the descriptor anyway would find nothing left to read."""
    mine, theirs = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        mine.sendall(token.encode("ascii") + b"\n")
        mine.shutdown(socket.SHUT_WR)
    finally:
        mine.close()
    os.dup2(theirs.fileno(), fd, inheritable=True)
    theirs.close()


def main(argv) -> None:
    if not argv:
        raise SystemExit("box-exec: usage: box-exec <tool> args...")
    env = dict(os.environ)
    token = take_token(env)
    me = pwd.getpwuid(os.getuid()).pw_name
    if not me.startswith("kap-"):
        raise SystemExit("box-exec: runs only as a box user")
    tool, home, pid = argv[0], env.get("HOME", ""), os.getpid()
    oc = None
    if tool == "opencode" and tui("opencode", argv[1:]):
        oenv = dict(env, **oc_env(home))
        major = oc_major(f"{TOOLS_BIN}/opencode", oenv)
        oc = {"major": major}
        if major == 1:
            oc["port"] = free_port()
        elif major == 2 and not any(a in ("-s", "--session", "-c", "--continue") or a.startswith("--session=")
                                    for a in argv[1:]):
            oc["session"] = oc_new_session(oenv, os.getcwd())
    prog, args, env = plan(tool, argv[1:], env, home, me[4:], token, pid, oc)
    if not os.path.exists(prog):
        raise SystemExit(f"box-exec: {argv[0]} is not installed in the box ({prog})")
    if env.get("KAP_CODEX_SOCKET"):
        codex_server(prog, env, env["KAP_CODEX_SOCKET"])
    if oc and oc.get("session"):
        write_chat(f"opencode:{oc['session']}", "opencode", pid, os.getcwd())
    if token and argv[0] == "claude":
        why = token_readable_problem(prog)
        if why:
            raise SystemExit(f"box-exec: {why}, so team chats could read its Claude token; not started. "
                             "Run the installer again (it makes the program execute-only).")
        hand_token(token)
    os.execve(prog, args, env)


if __name__ == "__main__":
    main(sys.argv[1:])
