#!/usr/bin/python3 -I
"""enter on macOS (secure design v2, 4.7 and 10; TEST VERSION): start a tool in the box. Root,
reached only through the sudo rule from the launcher:  sudo -n /usr/local/libexec/kaptyorka/enter <tool> args...

1. The person is the caller's user id from sudo; the tool one of claude, codex, cursor-agent, git
   (OpenCode is not offered on macOS yet: its window needs a loopback server of its own).
2. The real current folder is /Users/Shared/kaptyorka/<person>/<name> or below, and <name> is a
   team folder of this person (folders.json, written only by hubchat-root).
3. Tool flags pass through the same short allow list as on Linux.
4. Root puts right what a reboot or a network change may undo (pf's anchor, a daemon not yet
   started), then the outside half of the self-check; any problem stops the start.
5. The tool starts as the box user, on a pseudo-terminal made here (so it can never type into the
   person's terminal), inside the box's Seatbelt profile, behind wallcheck (the inside half: the
   canaries, the network, the processes, the clipboard). Its output reaches the person's
   terminal only through tty-filter, which runs as nobody in a profile of its own.
6. The chat ends at the box's time limit at the latest; then pending input is thrown away.

Installed as /usr/local/libexec/kaptyorka/enter on macOS.
"""
import os
import pwd
import signal
import subprocess
import sys
import termios
import time

if __package__:
    from . import kap_enter as ke, kap_mac as km, kap_update, kapcommon as kc, kaproot as kr
else:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import kap_enter as ke
    import kap_mac as km
    import kap_update
    import kapcommon as kc
    import kaproot as kr

NOT_YET = {"opencode": "OpenCode is not offered in team folders on macOS yet (TEST VERSION): its window runs a "
                       "server of its own on a loopback port, and the box's profile gives the box no loopback "
                       "port but its forwarder's. Use claude, codex or cursor-agent here."}


def notices(person: str, box: str, name: str) -> None:
    """Warnings that do not stop the start: files that can run code in the person's own tools,
    which changed since the folder became a team folder. On macOS nothing covers them in the
    person's view (no such view exists): this is how she knows."""
    if name == kc.MEMORY:
        return
    try:
        fd = kr.open_dir(km.team_path(person, name))
        try:
            changed = kr.trap_files(fd, kr.read_since(box, name))
        finally:
            os.close(fd)
        if changed:
            print(kc.term("enter: team chats changed files that run code when your own tools, editor, git or a "
                          "build open this folder: " + kr.trap_notice(changed) + ". On macOS nothing hides them "
                          "from your own programs: do not open this folder with them before you read those files."),
                  file=sys.stderr)
    except (kc.Refuse, OSError, KeyError, ValueError):
        pass


def filter_cmd(mode: str) -> list:
    py = km.python()
    path = f"{kc.ETC}/filter.sb"
    text = km.filter_profile(km.py_prefix(py))
    if kr.read_text(path) != text:
        kr.write_file(path, text, 0o644)
    return ["/usr/bin/sandbox-exec", "-f", path, py, "-I", km.TTY_FILTER, mode]


def nobody() -> dict:
    pw = pwd.getpwnam("nobody")
    return {"user": pw.pw_uid, "group": pw.pw_gid, "extra_groups": []}


def _ctty():
    import fcntl
    fcntl.ioctl(0, termios.TIOCSCTTY, 0)


def stop(p: subprocess.Popen) -> None:
    km.stop_chat({"pid": p.pid})


def run_tty(person: str, box: str, inner: list, cwd: str, tool: str, deadline: float) -> int:
    import pty
    import tty as ttymod
    saved = termios.tcgetattr(0)
    master, slave = pty.openpty()
    ke._winsize(0, slave)
    uid, gid, _ = km.ids(box)
    try:
        os.chown(os.ttyname(slave), uid, gid)
        os.chmod(os.ttyname(slave), 0o620)
    except OSError:
        pass
    me = os.open(os.ttyname(0), os.O_RDWR | os.O_NOCTTY)
    try:
        filt = subprocess.Popen(filter_cmd("pty"), stdin=me, stdout=master, stderr=subprocess.DEVNULL,
                                env=dict(kr.CLEAN_ENV), close_fds=True, **nobody())
    finally:
        os.close(me)
    signal.signal(signal.SIGWINCH, lambda *_: ke._winsize(0, master))
    p = None
    try:
        ttymod.setraw(0)
        p = km.popen_box(person, box, inner, cwd, claude=tool == "claude", term=os.environ.get("TERM", ""),
                         stdin=slave, stdout=slave, stderr=slave, start_new_session=True, preexec_fn=_ctty)
        km.record_chat(p.pid, person, box, tool, cwd)
        os.close(slave)
        slave = -1
        while p.poll() is None:
            if filt.poll() is not None:
                stop(p)
                raise kc.Refuse("the terminal filter stopped, so the team chat was stopped too")
            if deadline and time.monotonic() > deadline:
                stop(p)
                break
            time.sleep(0.1)
        try:
            filt.wait(5)
        except subprocess.TimeoutExpired:
            filt.terminate()
        return p.returncode if p.returncode is not None else 1
    finally:
        if slave >= 0:
            os.close(slave)
        if p is not None:
            if p.poll() is None:
                stop(p)
            km.end_session(p.pid)
            km.drop_chat(p.pid)
        if filt.poll() is None:
            try:
                filt.wait(5)
            except subprocess.TimeoutExpired:
                filt.kill()
                filt.wait()
        os.close(master)
        signal.signal(signal.SIGWINCH, signal.SIG_DFL)
        ke.reset_terminal(1, filt.returncode == 0)
        try:
            termios.tcsetattr(0, termios.TCSAFLUSH, saved)
            termios.tcflush(0, termios.TCIFLUSH)
        except termios.error:
            pass


def run_pipe(person: str, box: str, inner: list, cwd: str, tool: str, deadline: float) -> int:
    outs, filters = {1: 1, 2: 2}, []
    p = None
    try:
        for fd in (1, 2):
            if os.isatty(fd):
                r, w = os.pipe()
                filters.append(subprocess.Popen(filter_cmd("pipe"), stdin=r, stdout=fd, stderr=subprocess.DEVNULL,
                                                env=dict(kr.CLEAN_ENV), close_fds=True, **nobody()))
                os.close(r)
                outs[fd] = w
        p = km.popen_box(person, box, inner, cwd, claude=tool == "claude", stdin=subprocess.DEVNULL if os.isatty(0)
                         else 0, stdout=outs[1], stderr=outs[2], start_new_session=True)
        km.record_chat(p.pid, person, box, tool, cwd)
    finally:
        for fd in (1, 2):
            if outs[fd] != fd:
                os.close(outs[fd])
    try:
        while p.poll() is None:
            if deadline and time.monotonic() > deadline:
                stop(p)
                break
            time.sleep(0.1)
    finally:
        if p.poll() is None:
            stop(p)
        km.end_session(p.pid)
        km.drop_chat(p.pid)
    clean = True
    for f in filters:
        try:
            f.wait(10)
        except subprocess.TimeoutExpired:
            f.kill()
            f.wait()
        clean = clean and f.returncode == 0
    for fd in (1, 2):
        if os.isatty(fd):
            ke.reset_terminal(fd, clean)
            break
    return p.returncode if p.returncode is not None else 1


def main(argv) -> int:
    try:
        person = kr.caller_person()
        if kc.unfinished_install():
            raise kc.Refuse(kc.unfinished_install())
        ke.update_lock()
        if not argv:
            raise kc.Refuse("usage: enter <claude|codex|cursor-agent|git> args...")
        tool = argv[0]
        args = kc.filter_args(tool, argv[1:])
        if tool in NOT_YET:
            raise kc.Refuse(NOT_YET[tool])
        try:
            cwd = os.getcwd()
        except OSError:
            raise kc.Refuse("the current folder is gone")
        hit = km.team_folder(cwd, person)
        if not hit:
            raise kc.Refuse(f"{kc.shown(cwd)} is not a team folder (run hubchat list)")
        name, box = hit[0], person
        if name != kc.MEMORY and name not in kr.load_folders(person):
            raise kc.Refuse(f"{name} is not one of your team folders (run hubchat list)")
        km.repair_before_start(person, box)
        problems = km.outside_check(person, box)
        if problems:
            print("enter: self-check found a problem:", file=sys.stderr)
            for p in problems:
                print(kc.term(f"  - {p}"), file=sys.stderr)
            print("enter: a wall is missing, so the team chat was not started.", file=sys.stderr)
            print(f"Next: run  sudo {kc.INSTALLER} repair  and try again. If it stays, send these lines to your "
                  "team admin (macOS TEST VERSION).", file=sys.stderr)
            return 3
        notices(person, box, name)
        if kap_update.waiting():
            print(kc.term(f"enter: kaptyorka {kap_update.waiting()} is downloaded and verified; it installs by itself "
                          "once no team chat runs."), file=sys.stderr)
        try:
            km.start_helper(person, box)
        except (kc.Refuse, OSError, KeyError) as e:
            print(kc.term(f"enter: the box helper did not start ({e}); the chat runs without hub reports"),
                  file=sys.stderr)
        inner = [km.python(), "-I", km.BOX_EXEC, tool, *args]
        rt = km.runtime_max_s()
        deadline = time.monotonic() + rt if rt else 0.0
        tty = os.isatty(0) and os.isatty(1)
    except kc.Refuse as e:
        print(kc.term(f"enter: {e}"), file=sys.stderr)
        return 2
    for s in (signal.SIGINT, signal.SIGQUIT):
        signal.signal(s, signal.SIG_IGN)
    try:
        return (run_tty if tty else run_pipe)(person, box, inner, cwd, tool, deadline)
    except kc.Refuse as e:
        print(kc.term(f"enter: {e}"), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
