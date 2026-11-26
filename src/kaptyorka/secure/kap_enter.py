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
8. What the box prints reaches the person's terminal only through tty-filter (4.8), its own
   unit: at a live terminal the box's side is a pty made here (the person's keys go in as
   they are, the output comes back filtered; a resize is passed on); otherwise each of stdout
   and stderr that is a terminal gets a filter of its own. When the chat ends, pending input
   is thrown away (tcflush) before the shell gets the terminal back.

Nothing comes from the hub. Installed as /usr/local/libexec/kaptyorka/enter.
"""
import fcntl
import os
import secrets
import signal
import subprocess
import sys
import termios
import time

if __package__:
    from . import kapcommon as kc, kaproot as kr
else:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import kapcommon as kc
    import kaproot as kr


def notices(person: str, box: str, name: str) -> None:
    """Warnings that do not stop the start, at every start: git's ownership check switched off,
    and the files that can run code in the person's own tools or editor which changed since the
    folder became a team folder (7.1, 7.2; red team round 3, finding 1: at any depth, whoever
    owns the folder they are in). The covers in her view are the wall; this is so she knows."""
    try:
        folders = kr.load_folders(person)
        shorts = [f["shortcut"] for f in folders.values() if f.get("shortcut")]
        for w in kc.safe_directory_warnings(kr.git_safe_lines(person), shorts):
            print(kc.term("enter: " + w), file=sys.stderr)
        if name == kc.MEMORY:
            return
        fd = kr.open_dir(f"{kc.box_projects(box)}/{name}")
        try:
            changed = kr.trap_files(fd, kr.read_since(box, name), build=False)
        finally:
            os.close(fd)
        if changed:
            print("enter: team chats may have changed files that run code when your own tools or editor open "
                  "this folder: " + kr.trap_notice(changed) + ". Your view of the folder covers the ones at its "
                  "top; look before you open a subfolder with your own tools.", file=sys.stderr)
    except (kc.Refuse, OSError, KeyError, ValueError):
        pass


def repair(person: str, box: str, name: str) -> None:
    """Before the self-check: the covers in the person's view of this folder, and the relay's copy
    of the box's sign-ins (red team round 3, findings 1 and 2). kaptyorka-guard does both as soon
    as something changes; here they are certain before a chat starts."""
    if name != kc.MEMORY:
        try:
            kr.hide_view(person, name, box)
        except (kc.Refuse, OSError):
            pass
    try:
        kr.write_box_secrets(person, box)
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
        repair(person, box, name)
        problems = kr.outside_check(person, boxes)
        if problems:
            print("enter: self-check found a problem:", file=sys.stderr)
            for p in problems:
                print(kc.term(f"  - {p}"), file=sys.stderr)
            print("enter: a wall is missing, so the team chat was not started.", file=sys.stderr)
            print("Next: restart this computer and try again. If it stays, send these lines to your team admin.",
                  file=sys.stderr)
            return 3
        for ln in kr.old_login_hint(person):
            print(kc.term(ln), file=sys.stderr)
        notices(person, box, name)
        kr.ensure_box_running(box)
        kr.start_helper(person, box)
        tty = os.isatty(0) and os.isatty(1)
        cmd = build(person, box, tool, args, cwd, tty, os.environ.get("TERM", ""))
    except kc.Refuse as e:
        print(kc.term(f"enter: {e}"), file=sys.stderr)
        return 2
    env = dict(kr.CLEAN_ENV)
    if tty and kc.TERM_RX.fullmatch(os.environ.get("TERM", "")):
        env["TERM"] = os.environ["TERM"]
    for s in (signal.SIGINT, signal.SIGQUIT):
        signal.signal(s, signal.SIG_IGN)   # systemd-run and the box get them; enter only waits
    try:
        return run_tty(cmd, env) if tty else run_pipe(cmd, env)
    except kc.Refuse as e:
        print(kc.term(f"enter: {e}"), file=sys.stderr)
        return 2


def filter_cmd(mode: str) -> list:
    cmd = ["systemd-run", "--quiet", "--collect", "--pipe", "--wait", "--expand-environment=no",
           f"--unit=kaptyorka-ttyfilter-{secrets.token_hex(4)}"]
    for p in kc.tty_filter_props(kr.runtime_max()):
        cmd += ["-p", p]
    return cmd + ["--", "/usr/bin/python3", "-I", kc.TTY_FILTER, mode]


def unit_of(cmd: list) -> str:
    return next(a.split("=", 1)[1] for a in cmd if a.startswith("--unit="))


def _ctty():
    fcntl.ioctl(0, termios.TIOCSCTTY, 0)


def _winsize(src: int, dst: int) -> None:
    try:
        fcntl.ioctl(dst, termios.TIOCSWINSZ, fcntl.ioctl(src, termios.TIOCGWINSZ, b"\0" * 8))
    except OSError:
        pass


def _stop(unit: str) -> None:
    kr.run(["systemctl", "stop", "--no-block", unit + ".service"], check=False)


def run_tty(cmd: list, env: dict) -> int:
    """systemd-run --pty on a pty made here; the person's terminal only behind tty-filter."""
    import pty
    import tty as ttymod
    saved = termios.tcgetattr(0)
    master, slave = pty.openpty()
    _winsize(0, slave)
    person = os.open(os.ttyname(0), os.O_RDWR | os.O_NOCTTY)
    try:
        filt = subprocess.Popen(filter_cmd("pty"), stdin=person, stdout=master, stderr=subprocess.DEVNULL,
                                env=dict(kr.CLEAN_ENV))
    finally:
        os.close(person)
    signal.signal(signal.SIGWINCH, lambda *_: _winsize(0, master))
    box = None
    try:
        ttymod.setraw(0)   # keys go to the box as they are; the box's own pty does the rest
        box = subprocess.Popen(cmd, env=env, stdin=slave, stdout=slave, stderr=slave, start_new_session=True,
                               preexec_fn=_ctty)
        os.close(slave)
        slave = -1
        while box.poll() is None:
            if filt.poll() is not None:     # no filter, no output: the chat is stopped
                _stop(unit_of(cmd))
                box.wait(30)
                raise kc.Refuse("the terminal filter stopped, so the team chat was stopped too")
            time.sleep(0.1)
        try:
            filt.wait(5)
        except subprocess.TimeoutExpired:
            filt.terminate()
        return box.returncode
    finally:
        if slave >= 0:
            os.close(slave)
        if box is not None and box.poll() is None:
            _stop(unit_of(cmd))
        if filt.poll() is None:
            try:
                filt.wait(5)
            except subprocess.TimeoutExpired:
                filt.kill()
        os.close(master)
        signal.signal(signal.SIGWINCH, signal.SIG_DFL)
        try:
            termios.tcsetattr(0, termios.TCSAFLUSH, saved)
            termios.tcflush(0, termios.TCIFLUSH)   # nothing the box left in the input reaches the shell
        except termios.error:
            pass


def run_pipe(cmd: list, env: dict) -> int:
    """--pipe --wait (D8): each of stdout and stderr that is a terminal gets its own filter."""
    outs, filters = {1: 1, 2: 2}, []
    try:
        for fd in (1, 2):
            if os.isatty(fd):
                r, w = os.pipe()
                filters.append(subprocess.Popen(filter_cmd("pipe"), stdin=r, stdout=fd, stderr=subprocess.DEVNULL,
                                                env=dict(kr.CLEAN_ENV)))
                os.close(r)
                outs[fd] = w
        box = subprocess.Popen(cmd, env=env, stdout=outs[1], stderr=outs[2])
    finally:
        for fd in (1, 2):
            if outs[fd] != fd:
                os.close(outs[fd])
    rc = box.wait()
    for f in filters:
        try:
            f.wait(10)
        except subprocess.TimeoutExpired:
            f.kill()
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
