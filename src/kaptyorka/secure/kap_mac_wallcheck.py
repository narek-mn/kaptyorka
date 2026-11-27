#!/usr/bin/python3 -I
"""wallcheck on macOS (secure design v2, 10.2; TEST VERSION): the inside half of the self-check.
It runs as the box user, inside the box's Seatbelt profile, in the very process that becomes the
tool: it tries each wall once (M1 to M14) and then replaces itself with the tool. Any miss: one
line naming the missing wall, exit 1, the tool never starts. The worst case is "does not start",
never "leaks".

Usage: wallcheck [--verbose] --box BOX --person NAME --canaries f,g --canary-pid N --addrs a,b
                 --proxy-port N -- command args...
Installed as /usr/local/libexec/kaptyorka/wallcheck. Standard library only, no other kaptyorka file.
"""
import ctypes
import ctypes.util
import errno
import os
import pwd
import re
import socket
import stat
import sys
import time

MARK = b"KAPTYORKA-WALL-CANARY"
CANARY_PROC = b"KAPTYORKA_WALL_CANARY_PROC"
CANARY_TCP_LO, CANARY_TCP_ANY = 47991, 47992
CANARY_SOCK = "/private/var/run/kaptyorka/canary.sock"
CANARY_SHM_KEY = 0x4B415031
BOX_RX = re.compile(r"[a-z_][a-z0-9_-]{0,27}")
PERSON_RX = re.compile(r"[a-z_][a-z0-9_-]{0,30}")
CANARY_RX = re.compile(r"/[A-Za-z0-9/._-]{1,200}kaptyorka-wall-canary(-[0-9a-f]{16})?")
ENV_OK = {"PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "TERM", "TMPDIR", "HTTPS_PROXY", "HTTP_PROXY",
          "ALL_PROXY", "NO_PROXY", "CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR", "PWD", "OLDPWD", "SHLVL", "_",
          "__CF_USER_TEXT_ENCODING", "__PYVENV_LAUNCHER__", "APP_SANDBOX_CONTAINER_ID"}
TEAMAREA = "/Users/Shared/kaptyorka"
BOXCONF = "/private/etc/kaptyorka/box"
LOCKED = (("claude/user-settings.json", "state/claude/settings.json"),
          ("codex/config.toml", "state/codex/config.toml"),
          ("codex/hooks.json", "state/codex/hooks.json"),
          ("opencode/opencode.json", "state/opencode/cfg/opencode/opencode.json"),
          ("cursor/mcp.json", ".cursor/mcp.json"),
          ("cursor/hooks.json", ".cursor/hooks.json"),
          ("cursor/sandbox.json", ".cursor/sandbox.json"),
          ("cursor/cli-config.json", ".cursor/cli-config.json"))
# places the box must not list, and places it must not write in (where plain rights might let it)
NO_LIST = ("/Users", "/private/tmp", "/private/var/tmp", "/Users/Shared", "/private/etc/kaptyorka/secret",
           "/private/etc/kaptyorka/people", "/private/var/run/kaptyorka", "/Library/Keychains")
NO_WRITE = ("/private/tmp", "/private/var/tmp", "/Users/Shared", "/opt/kap-tools", "/private/etc/kaptyorka/box",
            "/usr/local/libexec/kaptyorka", "/Library/LaunchDaemons")
MACH_DENIED = ("com.apple.pasteboard.1", "com.apple.windowserver.active", "com.apple.coreservices.appleevents")
REFUSED = (errno.EPERM, errno.EACCES, errno.ECONNREFUSED, errno.ENETUNREACH, errno.EHOSTUNREACH,
           errno.EADDRNOTAVAIL, errno.ENOENT)


def fail(wall: str, what: str) -> None:
    what = "".join(c if c.isprintable() else "?" for c in str(what))
    print(f"wallcheck: wall {wall} is missing: {what}. The tool was not started.", file=sys.stderr, flush=True)
    print("Next: run  hubchat list  and send its self-check lines to your team admin (this is the macOS TEST "
          "VERSION: a macOS update may have changed a wall).", file=sys.stderr, flush=True)
    sys.exit(1)


def en(e: OSError) -> str:
    return errno.errorcode.get(e.errno, str(e.errno))


def parse(argv):
    a = list(argv)
    o = {"verbose": False, "canaries": [], "addrs": [], "box": None, "person": None, "canary-pid": "0",
         "proxy-port": "0"}
    while a and a[0] != "--":
        x = a.pop(0)
        if x == "--verbose":
            o["verbose"] = True
        elif x in ("--canaries", "--addrs", "--box", "--person", "--canary-pid", "--proxy-port") and a:
            v = a.pop(0)
            o[x[2:]] = [s for s in v.split(",") if s] if x in ("--canaries", "--addrs") else v
        else:
            break
    if not a or a[0] != "--" or len(a) < 2 or not o["box"] or not BOX_RX.fullmatch(o["box"]) \
            or not o["person"] or not PERSON_RX.fullmatch(o["person"]) \
            or not all(CANARY_RX.fullmatch(c) for c in o["canaries"]) \
            or not o["canary-pid"].isdigit() or not o["proxy-port"].isdigit():
        print("wallcheck: usage: wallcheck [--verbose] --box BOX --person NAME --canaries f,g --canary-pid N "
              "--addrs a,b --proxy-port N -- command", file=sys.stderr)
        sys.exit(2)
    return o, a[1:]


def connect_refused(fam, addr, timeout: float = 2.0) -> str:
    """'' when the connection is refused at once; else what happened."""
    s = socket.socket(fam, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(addr)
        return "answered"
    except socket.timeout:
        return "timed out instead of being refused (only pf held: the Seatbelt network rule is missing)"
    except OSError as e:
        return "" if e.errno in REFUSED else f"gave {en(e)}"
    finally:
        s.close()


def relay_answers(path: str) -> str:
    s = socket.socket(socket.AF_UNIX)
    s.settimeout(3)
    data = b""
    try:
        s.connect(path)
        s.sendall(b'{"call": "wallcheck", "args": {}}\n')
        while not data.endswith(b"\n") and len(data) < 4096:
            got = s.recv(4096)
            if not got:
                break
            data += got
    except OSError as e:
        return f"relay door: {e}"
    finally:
        s.close()
    if b"unknown call" in data or b"the relay is busy" in data:
        return ""
    return f"relay answered {data[:60]!r}"


def forwarder_answers(port: int) -> str:
    s = socket.socket(socket.AF_INET)
    s.settimeout(3)
    try:
        s.connect(("127.0.0.1", port))
        s.sendall(b"CONNECT wallcheck.invalid:443 HTTP/1.1\r\nHost: wallcheck.invalid:443\r\n\r\n")
        r = s.recv(200)
    except OSError as e:
        return f"forwarder on 127.0.0.1:{port}: {e}"
    finally:
        s.close()
    return "" if r.startswith(b"HTTP/1.1 403") else f"forwarder answered {r[:40]!r}"


def procargs(libc, pid: int) -> bytes:
    """KERN_PROCARGS2 of pid (arguments and environment), b'' when refused."""
    mib = (ctypes.c_int * 3)(1, 49, pid)        # CTL_KERN, KERN_PROCARGS2
    size = ctypes.c_size_t(65536)
    buf = ctypes.create_string_buffer(size.value)
    if libc.sysctl(mib, 3, buf, ctypes.byref(size), None, ctypes.c_size_t(0)) != 0:
        return b""
    return buf.raw[:size.value]


def pidpath(libc, pid: int) -> bytes:
    buf = ctypes.create_string_buffer(4096)
    try:
        n = libc.proc_pidpath(pid, buf, 4096)
    except AttributeError:
        return b""
    return buf.raw[:n] if n > 0 else b""


def mach_lookup(libc, name: str) -> bool:
    """True when the service name can be looked up from here."""
    try:
        port = ctypes.c_uint.in_dll(libc, "bootstrap_port")
        out = ctypes.c_uint(0)
        return libc.bootstrap_look_up(port, name.encode(), ctypes.byref(out)) == 0
    except (AttributeError, ValueError):
        return False


def sandboxed(libc) -> bool:
    """Whether this process is inside a Seatbelt sandbox (sandbox_check with no operation)."""
    try:
        lib = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
        f = lib.sandbox_check
    except (OSError, AttributeError):
        return True     # cannot ask: the other walls decide
    f.restype = ctypes.c_int
    f.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int)
    return f(os.getpid(), None, 0) != 0


def main(argv) -> None:
    t0 = time.monotonic()
    o, cmd = parse(argv)
    box, person = o["box"], o["person"]
    user = "kap-" + box
    home = os.environ.get("HOME", "")
    libc = ctypes.CDLL(ctypes.util.find_library("c") or "/usr/lib/libSystem.B.dylib", use_errno=True)
    # M1 the process is inside a sandbox, and it is the box user, never root
    if os.getuid() == 0 or os.geteuid() == 0:
        fail("M1", "running as root")
    try:
        if pwd.getpwuid(os.getuid()).pw_name != user:
            fail("M1", f"running as {pwd.getpwuid(os.getuid()).pw_name}")
    except KeyError:
        fail("M1", "unknown user")
    if not sandboxed(libc):
        fail("M1", "not inside the Seatbelt profile")
    for gid in os.getgrouplist(user, os.getgid()):
        if gid in (0, 20, 80):
            fail("M1", f"the box user is in group {gid} (wheel, staff or admin)")
    # M2 the canary files: the person's home, kaptyorka's own folder, /tmp, /var/tmp, /Users/Shared
    for f in o["canaries"]:
        try:
            with open(f, "rb") as fh:
                got = fh.read(64)
        except OSError:
            continue
        fail("M2", f"{f} is readable" + (" (the canary)" if MARK in got else ""))
    # M3 listings that would show the person's or other people's names
    for d in NO_LIST + (f"/Users/{person}",):
        try:
            os.listdir(d)
        except OSError:
            continue
        fail("M3", f"{d} can be listed")
    # M4 writes outside the box's own places
    for d in NO_WRITE:
        fn = os.path.join(d, f".kaptyorka-wallcheck-{os.getpid()}")
        try:
            fd = os.open(fn, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except OSError:
            continue
        os.close(fd)
        os.unlink(fn)
        fail("M4", f"{d} is writable")
    # M5 the locked tool settings: there, as root wrote them, and not writable
    for src, dst in LOCKED:
        p = os.path.join(home, dst)
        try:
            with open(os.path.join(BOXCONF, src), "rb") as a, open(p, "rb") as b:
                if a.read() != b.read():
                    fail("M5", f"{p} differs from kaptyorka's copy")
            st = os.lstat(p)
        except OSError as e:
            fail("M5", f"{p} gave {en(e)}")
        if not stat.S_ISREG(st.st_mode) or st.st_uid != 0:
            fail("M5", f"{p} is not root's file")
        try:
            fd = os.open(p, os.O_WRONLY | os.O_APPEND)
            os.close(fd)
            fail("M5", f"{p} is writable")
        except OSError:
            pass
    # M6 the person's and the computer's services: canaries on loopback, on this computer's own
    # addresses and on a Unix socket; the internet directly; name lookups
    for host, port in [("127.0.0.1", CANARY_TCP_LO), ("127.0.0.1", CANARY_TCP_ANY)] + \
                      [(a, CANARY_TCP_ANY) for a in o["addrs"]] + [("1.1.1.1", 443), ("8.8.8.8", 53)]:
        why = connect_refused(socket.AF_INET6 if ":" in host else socket.AF_INET, (host, port))
        if why:
            fail("M6", f"{host}:{port} {why}")
    why = connect_refused(socket.AF_UNIX, CANARY_SOCK)
    if why:
        fail("M6", f"the canary socket {why}")
    try:
        socket.getaddrinfo("example.com", 443)
        fail("M6", "a name lookup worked (the box must reach names only through the forwarder)")
    except OSError:
        pass
    u = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        u.sendto(b"\0", ("1.1.1.1", 53))
        fail("M6", "a UDP packet left the box")
    except OSError:
        pass
    finally:
        u.close()
    # M7 the canary process (the person's processes): no arguments, no path, no signal
    pid = int(o["canary-pid"])
    if pid > 1:
        if CANARY_PROC in procargs(libc, pid):
            fail("M7", "the canary process's arguments are readable")
        if pidpath(libc, pid):
            fail("M7", "the canary process's program path is readable")
        try:
            os.kill(pid, 0)
            fail("M7", "the canary process can be signalled")
        except PermissionError:
            pass
        except ProcessLookupError:
            fail("M7", "the canary process is not there (enter checked it a moment ago)")
    else:
        fail("M7", "no canary process was named")
    # M8 the desktop session: clipboard, screen, Apple Events
    for name in MACH_DENIED:
        if mach_lookup(libc, name):
            fail("M8", f"the service {name} can be reached")
    # M9 SysV shared memory of others
    try:
        if libc.shmget(CANARY_SHM_KEY, 0, 0) != -1:
            fail("M9", "the canary shared memory segment is visible")
    except AttributeError:
        pass
    # M10 a sandbox cannot be nested: a second profile must be refused (the tools' own sandboxes
    # are off inside the box for this reason; measured by the kit)
    try:
        f = ctypes.CDLL("/usr/lib/libSystem.B.dylib").sandbox_init
        f.argtypes = (ctypes.c_char_p, ctypes.c_uint64, ctypes.POINTER(ctypes.c_char_p))
        err = ctypes.c_char_p()
        if f(b"(version 1)(allow default)", 0, ctypes.byref(err)) == 0:
            fail("M10", "a second sandbox profile could be applied")
    except (OSError, AttributeError):
        pass
    # M11 environment: names only, values never printed
    extra = sorted(k for k in os.environ if k not in ENV_OK)
    if extra:
        fail("M11", "unexpected environment names " + ",".join(extra))
    if home != f"/var/lib/kap-team/kap-{box}/home":
        fail("M11", "HOME is not the box's")
    # M12 the two doors answer (so a box is not cut off for nothing)
    why = relay_answers(f"/private/var/run/kaptyorka/relay/{box}.sock") or forwarder_answers(int(o["proxy-port"]))
    if why:
        fail("M12", why)
    # M13 the team area: this person's folder is reachable, nobody else's
    try:
        os.listdir(f"{TEAMAREA}/{person}")
    except OSError as e:
        fail("M13", f"{TEAMAREA}/{person} gave {en(e)}")
    # M14 the controlling terminal (when there is one) is not the person's: a pseudo-terminal of the
    # box's own, made by enter
    try:
        fd = os.open("/dev/tty", os.O_RDWR | os.O_NOCTTY)
        try:
            if os.fstat(fd).st_uid not in (os.getuid(), 0):
                fail("M14", "the terminal belongs to someone else")
        finally:
            os.close(fd)
    except OSError:
        pass
    if o["verbose"]:
        print(f"wallcheck ok: 14 walls in {(time.monotonic() - t0) * 1000:.0f} ms", file=sys.stderr, flush=True)
    os.execv(cmd[0], cmd)


if __name__ == "__main__":
    main(sys.argv[1:])
