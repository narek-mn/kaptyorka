#!/usr/bin/python3 -I
"""box-exec (secure design v2, 4.7 and 5.2): the last step inside the box before the tool. It
reads the Claude token from the file systemd opened for it (OpenFile=, fd name claude-token),
closes that file, sets each tool's own folders, and replaces itself with the tool from
/opt/kap-tools. Runs as the box user, after wallcheck. Installed as
/usr/local/libexec/kaptyorka/box-exec. Standard library only.

Usage: box-exec <claude|opencode|codex|cursor-agent|git> args...
"""
import os
import pwd
import sys

TOOLS_BIN = "/opt/kap-tools/bin"
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
        env.update(CLAUDE_CONFIG_DIR=f"{home}/state/claude", DISABLE_AUTOUPDATER="1",
                   CLAUDE_CODE_SUBPROCESS_ENV_SCRUB="1")
        if token:
            env["CLAUDE_CODE_OAUTH_TOKEN"] = token
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
    os.execve(prog, args, env)


if __name__ == "__main__":
    main(sys.argv[1:])
