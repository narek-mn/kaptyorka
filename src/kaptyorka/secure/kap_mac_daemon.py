#!/usr/bin/python3 -I
"""kaptyorka's LaunchDaemons on macOS (secure design v2, 10.1; TEST VERSION). launchd starts each
as root; root makes what only root may make (the doors, the credentials, the listening ports),
then hands over to the service's own user inside a Seatbelt profile, keeping the same process (so
the systemd-style LISTEN_* names still fit). The services are the Linux ones, unchanged.

  kap_mac_daemon.py pf                 at boot: kaptyorka's pf anchor loaded, pf on
  kap_mac_daemon.py canary MARK        the canaries the self-check proves alive (as nobody)
  kap_mac_daemon.py relay              relay.py as _kaptyorka, one door per box
  kap_mac_daemon.py egress BOX         egress.py (the forwarder) as _kapproxy on 127.0.0.1:<port>

Installed as /usr/local/libexec/kaptyorka/kap_mac_daemon.py. Standard library only.
"""
import ctypes
import fcntl
import grp
import os
import pwd
import socket
import sys
import threading

if __package__:
    from . import kap_mac as km, kapcommon as kc, kaproot as kr
else:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import kap_mac as km
    import kapcommon as kc
    import kaproot as kr

ENV = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "LANG": "en_US.UTF-8"}


def say(msg: str) -> None:
    print(kc.term(msg), flush=True)


def hand_over(socks, names, user: str, profile: str, argv, extra_env=None) -> None:
    """socks on descriptors 3, 4, ...; the user's own ids; then sandbox-exec with the profile."""
    high = [fcntl.fcntl(s.fileno(), fcntl.F_DUPFD, 64) for s in socks]
    for s in socks:
        s.close()
    for i, fd in enumerate(high):
        os.dup2(fd, 3 + i)
        os.close(fd)
    env = dict(ENV, LISTEN_PID=str(os.getpid()), LISTEN_FDS=str(len(high)), LISTEN_FDNAMES=":".join(names),
               **(extra_env or {}))
    pw = pwd.getpwnam(user)
    os.setgroups([pw.pw_gid])
    os.setgid(pw.pw_gid)
    os.setuid(pw.pw_uid)
    if os.getuid() == 0 or os.geteuid() == 0:
        raise SystemExit("kap_mac_daemon: could not drop root")
    os.execve("/usr/bin/sandbox-exec", ["sandbox-exec", "-f", profile, *argv], env)


def profile(name: str, writable=(), denied=()) -> str:
    path = f"{kc.ETC}/{name}.sb"
    text = km.service_profile(list(writable), list(denied))
    if kr.read_text(path) != text:
        kr.write_file(path, text, 0o644)
    return path


def relay() -> int:
    py = km.python()
    pw = pwd.getpwnam(km.RELAY_USER)
    kr.mkdir(km.RUN, 0o755)
    kr.mkdir(km.RELAY_DIR, 0o755)
    socks, names = [], []
    for box in km.boxes():
        path = km.relay_door(box)
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.bind(path)
        os.chown(path, 0, grp.getgrnam(kc.box_user(box)).gr_gid)
        os.chmod(path, 0o660)
        s.listen(16)
        socks.append(s)
        names.append(f"box-{box}")
    cred = f"{km.RUN}/relay-cred"
    kr.mkdir(cred, 0o700, pw.pw_uid, pw.pw_gid)
    for n in ("device-token", "chat-key"):
        kr.write_file(f"{cred}/{n}", kr.read_text(f"{kc.SECRET}/{n}"), 0o400, pw.pw_uid, pw.pw_gid)
    say(f"relay: doors for {', '.join(km.boxes()) or 'nobody'}; handing over to {km.RELAY_USER}")
    hand_over(socks, names, km.RELAY_USER, profile("relay"),
              [py, "-I", f"{kc.OPT}/relay/current/relay.py", f"{kc.ETC}/relay.conf"],
              {"CREDENTIALS_DIRECTORY": cred})
    return 1


def egress(box: str) -> int:
    kc.check_box(box)
    py = km.python()
    port = km.proxy_port(box)
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("127.0.0.1", port))
    s.listen(64)
    say(f"egress: the forwarder of {box} on 127.0.0.1:{port}; handing over to {km.PROXY_USER}")
    hand_over([s], ["proxy"], km.PROXY_USER, profile("egress"),
              [py, "-I", f"{kc.OPT}/egress/current/egress.py", box, f"{kc.BOXES}/{box}/egress-allow"])
    return 1


def canary(mark: str) -> int:
    """The canaries (07 4.6 and 10.2): TCP on 127.0.0.1 and on every address, a Unix socket every
    user may connect to, a SysV segment, and this process with the marker in its arguments."""
    if mark != kc.CANARY_PROC:
        raise SystemExit("kap_mac_daemon: canary needs its marker")
    kr.mkdir(km.RUN, 0o755)
    socks = []
    for fam, addr in ((socket.AF_INET, ("127.0.0.1", km.CANARY_TCP_LO)), (socket.AF_INET, ("0.0.0.0", km.CANARY_TCP_ANY))):
        s = socket.socket(fam, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(addr)
        s.listen(16)
        socks.append(s)
    try:
        os.unlink(km.CANARY_SOCK)
    except FileNotFoundError:
        pass
    u = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    u.bind(km.CANARY_SOCK)
    os.chmod(km.CANARY_SOCK, 0o666)
    u.listen(16)
    socks.append(u)
    libc = ctypes.CDLL(None, use_errno=True)
    libc.shmat.restype = ctypes.c_void_p
    i = libc.shmget(km.CANARY_SHM_KEY, 4096, 0o1644)
    if i >= 0:
        a = libc.shmat(i, None, 0)
        if a not in (None, ctypes.c_void_p(-1).value):
            ctypes.memmove(a, kc.CANARY_MARK, len(kc.CANARY_MARK))
    kr.write_file(km.CANARY_PID, f"{os.getpid()}\n", 0o644)
    pw = pwd.getpwnam("nobody")
    os.setgroups([pw.pw_gid])
    os.setgid(pw.pw_gid)
    os.setuid(pw.pw_uid)

    def loop(s):
        while True:
            try:
                c, _ = s.accept()
            except OSError:
                continue
            try:
                c.settimeout(2)
                c.sendall(kc.CANARY_MARK + b"\n")
            except OSError:
                pass
            finally:
                c.close()
    for s in socks:
        threading.Thread(target=loop, args=(s,), daemon=True).start()
    say("canary: up")
    threading.Event().wait()
    return 0


def pf() -> int:
    kr.mkdir(km.RUN, 0o755)
    try:
        text = km.pf_text()
    except (KeyError, kc.Refuse, ValueError) as e:
        say(f"pf: kaptyorka's rules cannot be made ({e}); the self-check refuses team chats until repair")
        return 1
    kr.write_file(km.PF_FILE, text, 0o644)
    km.pf_load()
    say("pf: " + (km.pf_problem() or "kaptyorka's anchor loaded, pf on"))
    return 0


def main(argv) -> int:
    if os.geteuid() != 0:
        raise SystemExit("kap_mac_daemon: runs as root, from launchd")
    os.umask(0o077)
    if argv == ["relay"]:
        return relay()
    if len(argv) == 2 and argv[0] == "egress":
        return egress(argv[1])
    if len(argv) == 2 and argv[0] == "canary":
        return canary(argv[1])
    if argv == ["pf"]:
        return pf()
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
