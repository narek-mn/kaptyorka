#!/usr/bin/python3 -I
"""enter (secure design v2, 4.7): start a tool in the box. Root, reached only through the sudo
rule from the launcher:  sudo -n /usr/local/libexec/kaptyorka/enter <tool> args...

1. The person is the caller's user id from sudo.
2. The tool is one of claude, opencode, codex, cursor-agent, git.
3. The real current folder is /srv/team/<person>/<name> or below, and <name> is a team folder
   of this person (folders.json, written only by hubchat-root).
4. Tool flags pass through a short allow list; git's arguments pass as they are (git runs in
   the box).
5. The outside half of the self-check; any problem stops the start.
6. The box's units and its helper are started if they are not running.
7. The tool starts in a transient unit with the box's walls, behind wallcheck (the inside half):
   --pty at a live terminal, --pipe --wait otherwise (D8).

Nothing comes from the hub. Installed as /usr/local/libexec/kaptyorka/enter.
"""
import os
import secrets
import signal
import subprocess
import sys
import termios

if __package__:
    from . import kapcommon as kc, kaproot as kr
else:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import kapcommon as kc
    import kaproot as kr


def notices(person: str, box: str, name: str) -> None:
    """Warnings that do not stop the start: git's ownership check switched off, and files that
    can run code which the box changed since the last start (7.1, 7.2)."""
    try:
        folders = kr.load_folders(person)
        shorts = [f["shortcut"] for f in folders.values() if f.get("shortcut")]
        for w in kc.safe_directory_warnings(kr.git_safe_lines(person), shorts):
            print("enter: " + w, file=sys.stderr)
        if name == kc.MEMORY:
            return
        stamp = f"{kc.box_root(box)}/.seen-{name}"
        try:
            since = os.lstat(stamp).st_mtime
        except FileNotFoundError:
            since = 0.0
        fd = kr.open_dir(f"{kc.box_projects(box)}/{name}")
        try:
            changed = kr.trap_files(fd, kr.uid_of(kc.box_user(box)), since)
        finally:
            os.close(fd)
        if changed:
            print("enter: team chats changed files that can run code when you open this folder yourself: "
                  + ", ".join(changed[:12]) + ". Look before you trust them in an editor.", file=sys.stderr)
        kr.write_file(stamp, "", 0o600)
    except (kc.Refuse, OSError, KeyError, ValueError):
        pass


def build(person: str, box: str, tool: str, args, cwd: str, tty: bool, term: str) -> list:
    unit = f"kapbox-{box}-chat-{tool}-{secrets.token_hex(4)}"
    cmd = ["systemd-run", "--quiet", "--collect", "--expand-environment=no", f"--unit={unit}",
           f"--working-directory={cwd}"]
    cmd += ["--pty"] if tty else ["--pipe", "--wait"]
    cmd += kr.props_args(person, box)
    token = f"{kc.SECRET}/{person}/claude-oauth-token"
    if tool == "claude" and os.path.isfile(token):
        cmd += ["-p", f"OpenFile={token}:claude-token:read-only"]
    if tty and kc.TERM_RX.fullmatch(term or ""):
        cmd += ["-p", f"Environment=TERM={term}"]
    cmd += ["--", kc.WALLCHECK, "--box", box, "--person", person, "--addrs", ",".join(kr.host_addrs()),
            "--canaries", ",".join(kr.shared_canaries()), "--", kc.BOX_EXEC, tool, *args]
    return cmd


def main(argv) -> int:
    try:
        person = kr.caller_person()
        if not argv:
            raise kc.Refuse("usage: enter <claude|opencode|codex|cursor-agent|git> args...")
        tool = argv[0]
        args = kc.filter_args(tool, argv[1:])
        try:
            cwd = os.getcwd()
        except OSError:
            raise kc.Refuse("the current folder is gone")
        hit = kc.team_folder(cwd, person)
        if not hit:
            raise kc.Refuse(f"{kc.shown(cwd)} is not a team folder (run hubchat list)")
        name = hit[0]
        folders = kr.load_folders(person)
        if name == kc.MEMORY:
            box = person
        elif name in folders:
            box = folders[name]["box"]
        else:
            raise kc.Refuse(f"{name} is not one of your team folders (run hubchat list)")
        if kc.view_path(person, name) not in kr.mount_flags():
            raise kc.Refuse(f"the team folder {name} is not mounted (run hubchat list)")
        boxes = sorted({box, person})
        problems = kr.outside_check(person, boxes)
        if problems:
            for p in problems:
                print(f"enter: self-check: {p}", file=sys.stderr)
            print("enter: a wall is missing, so the team chat was not started.", file=sys.stderr)
            return 3
        notices(person, box, name)
        kr.ensure_box_running(box)
        kr.start_helper(person, box)
        tty = os.isatty(0) and os.isatty(1)
        cmd = build(person, box, tool, args, cwd, tty, os.environ.get("TERM", ""))
    except kc.Refuse as e:
        print(f"enter: {e}", file=sys.stderr)
        return 2
    env = dict(kr.CLEAN_ENV)
    if tty and kc.TERM_RX.fullmatch(os.environ.get("TERM", "")):
        env["TERM"] = os.environ["TERM"]
    p = subprocess.Popen(cmd, env=env)
    for s in (signal.SIGINT, signal.SIGQUIT):
        signal.signal(s, signal.SIG_IGN)   # systemd-run and the box get them; enter only waits
    rc = p.wait()
    if tty:
        try:
            termios.tcflush(0, termios.TCIFLUSH)   # nothing the box left in the input reaches the shell
        except termios.error:
            pass
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
