#!/usr/bin/python3 -I
"""wallcheck (secure design v2, 4.6): the inside half of the self-check. It runs as the box user,
in the very unit that will run the tool, tries each wall once (W1 to W15) and then replaces
itself with the tool. Any miss: one line naming the missing wall, exit 1, the tool never starts.

Usage: wallcheck [--verbose] --box BOX --person NAME [--addrs a,b] [--canaries f,g] -- command args...
Installed as /usr/local/libexec/kaptyorka/wallcheck. Standard library only, no other kaptyorka
file, so it reads the same inside every box.
"""
import ctypes
import errno
import glob
import grp
import json
import os
import pwd
import re
import signal
import socket
import sys
import time

# The same constants as kapcommon (the canary service holds these; enter proves them alive).
CANARY_FILES = ("/.kaptyorka-wall-canary", "/home/.kaptyorka-wall-canary", "/run/kaptyorka/wall-canary")
CANARY_SHARED_RX = re.compile(r"/(tmp|dev/shm)/kaptyorka-wall-canary-[0-9a-f]{16}")
CANARY_SHM_KEY = 0x4B415031
CANARY_ABSTRACT = b"\0kaptyorka-wall-canary"
CANARY_TCP_LO, CANARY_TCP_ANY = 47991, 47992
CANARY_PROC = b"KAPTYORKA_WALL_CANARY_PROC"
BOX_RX = re.compile(r"[a-z_][a-z0-9_-]{0,27}")
PERSON_RX = re.compile(r"[a-z_][a-z0-9_-]{0,30}")
ENV_OK = {"LANG", "PATH", "USER", "LOGNAME", "HOME", "SHELL", "INVOCATION_ID", "TERM", "SYSTEMD_EXEC_PID",
          "MEMORY_PRESSURE_WATCH", "MEMORY_PRESSURE_WRITE", "HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "NO_PROXY",
          "LISTEN_FDS", "LISTEN_PID", "LISTEN_FDNAMES", "PWD", "JOURNAL_STREAM", "MANAGERPID"}
SYS_HIDDEN = ("block", "bus", "class", "dev", "devices", "firmware", "hypervisor", "kernel", "module", "power")
LOCKED = ("state/claude/settings.json", "state/codex/config.toml", "state/codex/hooks.json",
          "state/opencode/cfg/opencode/opencode.json", ".cursor/mcp.json", ".cursor/hooks.json", ".cursor/sandbox.json")


def fail(wall: str, what: str) -> None:
    print(f"wallcheck: wall {wall} is missing: {what}. The tool was not started.", file=sys.stderr, flush=True)
    sys.exit(1)


def en(e: OSError) -> str:
    return errno.errorcode.get(e.errno, str(e.errno))


def parse(argv):
    a, opts = list(argv), {"verbose": False, "addrs": [], "canaries": [], "box": None, "person": None}
    while a and a[0] != "--":
        x = a.pop(0)
        if x == "--verbose":
            opts["verbose"] = True
        elif x in ("--addrs", "--canaries", "--box", "--person") and a:
            v = a.pop(0)
            opts[x[2:]] = [s for s in v.split(",") if s] if x in ("--addrs", "--canaries") else v
        else:
            break
    if not a or a[0] != "--" or len(a) < 2 or not opts["box"] or not opts["person"] \
            or not BOX_RX.fullmatch(opts["box"]) or not PERSON_RX.fullmatch(opts["person"]) \
            or not all(CANARY_SHARED_RX.fullmatch(c) for c in opts["canaries"]):
        print("wallcheck: usage: wallcheck [--verbose] --box BOX --person NAME [--addrs a,b] [--canaries f,g] "
              "-- command",
              file=sys.stderr)
        sys.exit(2)
    return opts, a[1:]


def relay_answers(path: str) -> str:
    """'' when the relay's door answers this box (an unknown call gets the relay's own refusal,
    without asking the hub); else what went wrong."""
    s = socket.socket(socket.AF_UNIX)
    s.settimeout(3)
    try:
        s.connect(path)
        s.sendall(b'{"call": "wallcheck", "args": {}}\n')
        data = b""
        while not data.endswith(b"\n") and len(data) < 4096:
            got = s.recv(4096)
            if not got:
                break
            data += got
    except OSError as e:
        return f"relay door: {e}"
    finally:
        s.close()
    try:
        ans = json.loads(data.decode())
    except ValueError:
        return f"relay answered {data[:40]!r}"
    if ans.get("ok") is False and "unknown call" in str(ans.get("error", "")):
        return ""
    return f"relay answered {str(ans)[:60]}"


def forwarder_answers(path: str) -> str:
    s = socket.socket(socket.AF_UNIX)
    s.settimeout(3)
    try:
        s.connect(path)
        s.sendall(b"CONNECT wallcheck.invalid:443 HTTP/1.1\r\nHost: wallcheck.invalid:443\r\n\r\n")
        r = s.recv(200)
    except OSError as e:
        return f"forwarder door: {e}"
    finally:
        s.close()
    return "" if r.startswith(b"HTTP/1.1 403") else f"forwarder answered {r[:40]!r}"


def view(box: str, person: str):
    """What the box may see, folder by folder: (folder, entries that must be there, entries that
    may be there). Anything else in these folders means a wall that systemd dropped or that was
    never set (TemporaryFileSystem=/ is ignored without a word next to ProtectSystem=)."""
    u = "kap-" + box
    return [
        ("/", {"bin", "dev", "etc", "lib", "opt", "proc", "run", "sbin", "srv", "sys", "tmp", "usr", "var"}, {"lib64"}),
        ("/etc", {"group", "hosts", "kaptyorka", "ld.so.cache", "nsswitch.conf", "passwd", "ssl"},
         {"alternatives", "ca-certificates", "localtime"}),
        ("/etc/kaptyorka", {"box", "boxes"}, set()),
        ("/etc/kaptyorka/boxes", {box}, set()),
        ("/opt", {"kap-tools", "kaptyorka"}, set()),
        ("/opt/kaptyorka", {"box"}, set()),
        ("/var", {"lib", "tmp"}, set()),
        ("/var/lib", {"kap-team"}, set()),
        ("/var/lib/kap-team", {u}, set()),
        ("/var/lib/kap-team/" + u, {"home"}, set()),
        ("/srv", {"team"}, set()),
        ("/srv/team", {person}, set()),
        ("/run", {"egress.sock", "kapbox", "kaptyorka"}, {"systemd"}),
        ("/run/kaptyorka", {"relay"}, set()),
        ("/run/kaptyorka/relay", {box + ".sock"}, set()),
        ("/run/systemd", set(), {"incoming"}),
        ("/usr/local", {"libexec"}, set()),
        ("/usr/local/libexec", {"kaptyorka"}, set()),
        ("/sys", set(), set(SYS_HIDDEN) | {"fs"}),
        ("/sys/devices", {"system"}, set()),
        ("/sys/devices/system", {"cpu"}, set()),
    ] + [("/sys/" + d, set(), set()) for d in SYS_HIDDEN if d != "devices"]


def view_problem(spec) -> str:
    """'' when every folder shows exactly what spec allows; else the first difference."""
    for d, need, may in spec:
        try:
            seen = set(os.listdir(d))
        except OSError as e:
            return f"{d} cannot be listed ({en(e)})"
        extra, missing = seen - need - may, need - seen
        if extra:
            return f"{d} shows {', '.join(sorted(extra)[:8])} (not on the box's list)"
        if missing:
            return f"{d} lacks {', '.join(sorted(missing)[:8])}"
    return ""


def main(argv) -> None:
    t0 = time.monotonic()
    opts, cmd = parse(argv)
    box, person = opts["box"], opts["person"]
    user, share = "kap-" + box, "kapshare-" + box
    home = os.environ.get("HOME", "")
    # W1 the canary files (the fixed ones, and the ones with fresh names in /tmp and /dev/shm)
    for f in CANARY_FILES + tuple(opts["canaries"]):
        try:
            open(f, "rb").close()
        except FileNotFoundError:
            continue
        except OSError as e:
            fail("W1", f"{f} gives {en(e)}, not 'No such file or directory'")
        fail("W1", f"{f} is readable")
    # W2 personal and system places, and the whole view: each folder holds exactly the box's list
    for d in ("/home", "/root", "/run/user", "/mnt", "/media", "/var/log", "/data"):
        if os.path.lexists(d):
            fail("W2", f"{d} exists")
    why = view_problem(view(box, person))
    if why:
        fail("W2", why)
    # W3 the canary SysV segment
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.shmget(CANARY_SHM_KEY, 0, 0) != -1:
        fail("W3", "the canary SysV shared memory segment is visible")
    # W4 the abstract canary socket
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.connect(CANARY_ABSTRACT)
        fail("W4", "connected to @kaptyorka-wall-canary")
    except ConnectionRefusedError:
        pass
    except OSError as e:
        fail("W4", f"the abstract connect gave {en(e)}")
    finally:
        s.close()
    # W5 TCP canaries on loopback and on the laptop's own addresses
    for host, port in [("127.0.0.1", CANARY_TCP_LO), ("127.0.0.1", CANARY_TCP_ANY)] + \
                      [(a, CANARY_TCP_ANY) for a in opts["addrs"]]:
        s = socket.socket(socket.AF_INET6 if ":" in host else socket.AF_INET)
        s.settimeout(1)
        try:
            s.connect((host, port))
            fail("W5", f"{host}:{port} answered")
        except socket.timeout:
            fail("W5", f"{host}:{port} timed out instead of 'refused' or 'no route'")
        except OSError as e:
            if e.errno not in (errno.ECONNREFUSED, errno.ENETUNREACH, errno.EHOSTUNREACH, errno.EADDRNOTAVAIL):
                fail("W5", f"{host}:{port} gave {en(e)}")
        finally:
            s.close()
    # W6 network interfaces
    names = [n for _, n in socket.if_nameindex()]
    if names != ["lo"]:
        fail("W6", f"network interfaces {names}")
    # W7 processes: only our own, never the canary
    me = os.getuid()
    for p in glob.glob("/proc/[0-9]*"):
        try:
            uid = os.stat(p).st_uid
            with open(p + "/cmdline", "rb") as f:
                cl = f.read(4096)
        except OSError:
            continue
        if CANARY_PROC in cl:
            fail("W7", "the canary process is visible")
        if uid != me:
            fail("W7", f"a process of uid {uid} is visible")
    # W8 rights
    st = dict(ln.split(":", 1) for ln in open("/proc/self/status").read().splitlines() if ":" in ln)
    for k in ("CapInh", "CapPrm", "CapEff", "CapBnd", "CapAmb"):
        if int(st[k].strip(), 16) != 0:
            fail("W8", f"{k} is {st[k].strip()}")
    if st["NoNewPrivs"].strip() != "1":
        fail("W8", "NoNewPrivs is not 1")
    if st["Seccomp"].strip() != "2":
        fail("W8", "no system call filter")
    # other settings install seccomp filters too, so also prove the box's own list: a child calls
    # process_vm_readv, which SystemCallFilter= answers by killing it with SIGSYS
    nr = {"x86_64": 310, "aarch64": 270}.get(os.uname().machine)
    if nr:
        pid = os.fork()
        if pid == 0:
            libc.prctl(4, 0, 0, 0, 0)   # PR_SET_DUMPABLE 0: no core dump (a pipe core handler ignores LimitCORE)
            libc.syscall(nr, os.getpid(), 0, 0, 0, 0, 0)
            os._exit(0)
        _, status = os.waitpid(pid, 0)
        if not (os.WIFSIGNALED(status) and os.WTERMSIG(status) == signal.SIGSYS):
            fail("W8", "the system call filter lets process_vm_readv through")
    # W9 the cgroup tree
    try:
        ents = os.listdir("/sys/fs/cgroup")
    except FileNotFoundError:
        ents = []
    if ents:
        fail("W9", f"/sys/fs/cgroup shows {len(ents)} entries")
    # W10 system sockets
    for p in ("/run/dbus/system_bus_socket", "/run/docker.sock", "/var/run/docker.sock", "/run/systemd/private",
              "/run/snapd.socket", "/run/cups/cups.sock", "/run/containerd/containerd.sock", "/run/podman"):
        if os.path.lexists(p):
            fail("W10", f"{p} exists")
    # W11 read-only places: EROFS where plain rights alone would allow the write
    for d, need_rofs in (("/usr", False), ("/etc", False), ("/opt/kap-tools", False), ("/etc/kaptyorka/box", False),
                         (home, True), (home + "/state", True), (home + "/.ssh", True)):
        fn = os.path.join(d, ".wallcheck-write-test")
        try:
            fd = os.open(fn, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except OSError as e:
            if e.errno == errno.EROFS or (e.errno == errno.EACCES and not need_rofs):
                continue
            fail("W11", f"writing in {d} gave {en(e)}, not 'Read-only file system'")
        os.close(fd)
        os.unlink(fn)
        fail("W11", f"{d} is writable")
    for rel in LOCKED:   # each locked file is its own read-only mount (a root-owned file alone is not enough)
        f = os.path.join(home, rel)
        try:
            ro = os.statvfs(f).f_flag & os.ST_RDONLY
        except OSError as e:
            fail("W11", f"{f} gave {en(e)}")
        if not ro:
            fail("W11", f"{f} is not on a read-only mount")
    # W12 who: the box user, groups exactly as allowed
    try:
        if pwd.getpwuid(os.getuid()).pw_name != user:
            fail("W12", f"running as {pwd.getpwuid(os.getuid()).pw_name}")
        want = {grp.getgrnam(user).gr_gid, grp.getgrnam(share).gr_gid}
    except KeyError as e:
        fail("W12", f"unknown user or group {e}")
    have = set(os.getgroups()) | {os.getgid()}
    if have != want:
        fail("W12", f"groups {sorted(have)}, allowed {sorted(want)}")
    # W13 devices
    for p in ["/dev/mem", "/dev/kmem", "/dev/port", "/dev/fuse", "/dev/kmsg", "/dev/dri", "/dev/kvm", "/dev/input",
              "/dev/snd", "/dev/video0", "/dev/tpm0"] + glob.glob("/dev/sd*") + glob.glob("/dev/nvme*") + \
             glob.glob("/dev/vd*") + glob.glob("/dev/loop*") + glob.glob("/dev/mapper"):
        if os.path.lexists(p):
            fail("W13", f"{p} exists")
    # W14 environment: names only, values never printed
    extra = sorted(k for k in os.environ if k not in ENV_OK)
    if extra:
        fail("W14", "unexpected environment names " + ",".join(extra))
    # W15 the two doors answer (so a box is not cut off for nothing)
    why = relay_answers(f"/run/kaptyorka/relay/{box}.sock") or forwarder_answers("/run/egress.sock")
    if why:
        fail("W15", why)
    if opts["verbose"]:
        print(f"wallcheck ok: 15 walls in {(time.monotonic() - t0) * 1000:.0f} ms", file=sys.stderr, flush=True)
    os.execv(cmd[0], cmd)


if __name__ == "__main__":
    main(sys.argv[1:])
