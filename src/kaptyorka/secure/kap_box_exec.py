#!/usr/bin/python3 -I
"""box-exec (secure design v2, 4.7 and 5.2): the last step inside the box before the tool. It
reads the Claude token from the file systemd opened for it (OpenFile=, fd name claude-token),
closes that file, hands the token to claude on a descriptor (never in an environment), sets each
tool's own folders, and replaces itself with the tool from
/opt/kap-tools. Runs as the box user, after wallcheck. Installed as
/usr/local/libexec/kaptyorka/box-exec. Standard library only.

Usage: box-exec <claude|opencode|codex|cursor-agent|git> args...
"""
import os
import pwd
import socket
import sys

TOOLS_BIN = "/opt/kap-tools/bin"
TOKEN_FD = 3
BOXES = "/etc/kaptyorka/boxes"
BOXCONF = "/etc/kaptyorka/box"


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


def plan(tool: str, args, env, home: str, box: str, token: str):
    """(program, argv, env) for one tool. Pure, for the tests."""
    env = dict(env)
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
        env["CODEX_HOME"] = f"{home}/state/codex"
        prog = f"{TOOLS_BIN}/codex"
        argv = [prog, *args]
    elif tool == "opencode":
        s = f"{home}/state/opencode"
        env.update(XDG_CONFIG_HOME=f"{s}/cfg", XDG_DATA_HOME=f"{s}/data", XDG_STATE_HOME=f"{s}/state",
                   XDG_CACHE_HOME=f"{s}/cache", OPENCODE_DISABLE_AUTOUPDATE="1")
        prog = f"{TOOLS_BIN}/opencode"
        argv = [prog, *args]
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
    prog, args, env = plan(argv[0], argv[1:], env, env.get("HOME", ""), me[4:], token)
    if not os.path.exists(prog):
        raise SystemExit(f"box-exec: {argv[0]} is not installed in the box ({prog})")
    if token and argv[0] == "claude":
        why = token_readable_problem(prog)
        if why:
            raise SystemExit(f"box-exec: {why}, so team chats could read its Claude token; not started. "
                             "Run the installer again (it makes the program execute-only).")
        hand_token(token)
    os.execve(prog, args, env)


if __name__ == "__main__":
    main(sys.argv[1:])
