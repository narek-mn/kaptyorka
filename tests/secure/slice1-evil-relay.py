# A malicious relay release (attacker R): installed as /opt/kaptyorka/relay/current/relay.py and
# started by the REAL kaptyorka-relay.service. It tries R-4 to R-12 (and R-11b, R-12b) from inside
# and prints one RESULT line per attempt. Test aid for slice1.sh only.
import ctypes, errno, glob, os, signal, socket, subprocess, sys
if "--viewcheck" in sys.argv: sys.exit(0)   # the unit's ExecStartPre=: the attack runs once, in ExecStart=
KT = "/opt/kaptyorka/relay/evil/kt/"        # the test's values (its /etc/kaptest is outside the relay's root)
CAN = "CAN" + "ARY"
def R(i, ok, exp, act):
    print("RESULT\t%s\t%s\t%s\t%s" % (i, "PASS" if ok else "FAIL", exp, str(act).replace("\n", " ").replace("\t", " ")[:350]), flush=True)
def tryread(p):
    try:
        with open(p, "rb") as f: d = f.read(200000)
        return ("READ " + d[:80].decode(errors="replace")), (CAN.encode() in d)
    except Exception as e: return type(e).__name__ + ": " + str(e), False
def tryls(p):
    try: return os.listdir(p)
    except Exception as e: return type(e).__name__ + ": " + str(e)
R("R-0 who am I", os.getuid() != 0, "a dynamic user, not root", "uid=%d gid=%d groups=%s" % (os.getuid(), os.getgid(), os.getgroups()))
cred = os.environ.get("CREDENTIALS_DIRECTORY", "")
R("R-2 own credentials only", sorted(os.listdir(cred)) == ["chat-key", "device-token"] if cred else False, "device-token and chat-key", tryls(cred) if cred else "none")
# R-4 files
for p in ["/home/alice/personal/diary.txt", "/home/alice/.ssh/id_ed25519", "/home/alice/.claude/.credentials.json",
          "/home/alice/.claude/projects/-home-alice-personal/chat.jsonl", "/tmp/alice-notes.txt", "/var/tmp/alice-notes.txt",
          "/dev/shm/alice-shm", "/etc/kaptyorka/secret/device-token", "/etc/kaptyorka/secret/chat-key",
          "/etc/kaptyorka/secret/alice/claude-oauth-token", "/var/lib/kap-team/kap-alice/home/box-canary.txt",
          "/var/lib/kap-team/kap-alice/projects/proj-a/team-canary.txt", "/var/log/kaptest-hub.log", "/root/slice1.sh"]:
    a, leak = tryread(p); R("R-4 open " + p, not leak and not a.startswith("READ"), "ENOENT/EACCES", a)
for d in ["/home", "/root", "/run/user", "/srv", "/mnt", "/media", "/var/lib/kap-team", "/dev/shm", "/tmp", "/run/kapbox", "/etc/kaptyorka/secret"]:
    l = tryls(d); R("R-4 ls " + d, not (isinstance(l, list) and l), "empty or refused", l)
for p in ["/data/canary.txt", "/kaptest-extra/secret.txt", "/usr/local/kaptest-extra.txt", "/opt/alice-canary.txt",
          "/.kaptyorka-wall-canary", "/run/kaptyorka/wall-canary"]:
    a, leak = tryread(p); R("R-4c open " + p, not leak and not a.startswith("READ"), "ENOENT", a)
top = set(os.listdir("/"))
R("R-4c the relay's whole root", top <= {"bin", "dev", "etc", "lib", "lib64", "opt", "proc", "root", "run", "sbin", "sys", "tmp", "usr", "var"},
  "only its own list (no extra folder of the laptop)", " ".join(sorted(top)))
R("R-4c /etc, /opt, /usr/local, /sys/devices", set(os.listdir("/etc")) <= {"alternatives", "kaptyorka", "ld.so.cache", "passwd"}
  and os.listdir("/opt") == ["kaptyorka"] and os.listdir("/usr/local") == [] and os.listdir("/sys/devices") == [],
  "only the relay's files; empty /usr/local and /sys", "%s | %s | %s | %s" % (tryls("/etc"), tryls("/opt"), tryls("/usr/local"), tryls("/sys/devices")[:5]))
found = []
for root, dirs, files in os.walk("/", onerror=None):
    if root.startswith(("/proc", "/sys", "/dev", "/usr")): dirs[:] = []; continue
    for f in files:
        if f.startswith("id_") or f in ("chat.jsonl", ".credentials.json", "diary.txt", "claude-oauth-token", "box-canary.txt"): found.append(os.path.join(root, f))
R("R-4 find /", not found, "no key, chat or canary file found", found)
# R-5 processes, cgroups
pid = open(KT + "alice-sleep.pid").read().strip()
pids = [p for p in os.listdir("/proc") if p.isdigit()]
R("R-5 ls /proc", all(os.stat("/proc/" + p).st_uid == os.getuid() for p in pids), "only own processes", "%d pids: %s" % (len(pids), pids[:10]))
for f in ("cmdline", "environ", "status"):
    a, leak = tryread("/proc/%s/%s" % (pid, f)); R("R-5 /proc/<alice>/" + f, not leak and not a.startswith("READ"), "ENOENT", a)
cg = tryls("/sys/fs/cgroup")
R("R-5 cgroup tree (D1: no ProtectControlGroups=yes)", cg == [] or not isinstance(cg, list), "empty", cg if not isinstance(cg, list) else "%d entries %s" % (len(cg), cg[:3]))
try: os.kill(int(pid), 0); R("R-15 kill(alice,0)", False, "ESRCH/EPERM", "allowed")
except Exception as e: R("R-15 kill(alice,0)", True, "ESRCH/EPERM", repr(e))
# R-6 rights
st = {l.split(":")[0]: l.split(":")[1].strip() for l in open("/proc/self/status") if l.startswith(("Cap", "NoNewPrivs", "Seccomp"))}
R("R-6 capabilities", all(st[k] == "0000000000000000" for k in ("CapInh", "CapPrm", "CapEff", "CapBnd", "CapAmb")) and st["NoNewPrivs"] == "1",
  "all cap sets 0, NoNewPrivs 1", st)
for cmd in (["sudo", "-n", "id", "-u"], ["su", "-c", "id -u", "root"], ["/usr/bin/newgrp", "docker"]):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=5, stdin=subprocess.DEVNULL)
        R("R-6 " + " ".join(cmd), p.returncode != 0, "fails", "rc=%d %s" % (p.returncode, (p.stdout + p.stderr).strip()[:200]))
    except Exception as e: R("R-6 " + " ".join(cmd), True, "fails", repr(e))
# R-7 unix sockets: it may not make any (its doors come from systemd)
try: socket.socket(socket.AF_UNIX, socket.SOCK_STREAM); R("R-7 socket(AF_UNIX)", False, "EAFNOSUPPORT", "created")
except OSError as e: R("R-7 socket(AF_UNIX)", e.errno == errno.EAFNOSUPPORT, "EAFNOSUPPORT", repr(e))
for fam, n in ((socket.AF_NETLINK, "AF_NETLINK"), (socket.AF_PACKET, "AF_PACKET")):
    try: socket.socket(fam, socket.SOCK_RAW, 0); R("R-7 socket(%s)" % n, False, "EAFNOSUPPORT", "created")
    except OSError as e: R("R-7 socket(%s)" % n, True, "EAFNOSUPPORT", repr(e))
# R-8 / R-9 network
def tcp(ip, port, fam=socket.AF_INET):
    s = socket.socket(fam, socket.SOCK_STREAM); s.settimeout(3)
    try: s.connect((ip, port))
    except OSError as e: s.close(); return repr(e)
    try: d = s.recv(200); return "CONNECTED " + d[:60].decode(errors="replace")
    except OSError: return "CONNECTED (no banner)"
    finally: s.close()
eth = open(KT + "eth").read().strip()
for ip, port in [("127.0.0.1", 9222), (eth, 9223), ("10.77.0.2", 443)]:
    a = tcp(ip, port); R("R-8/9 tcp %s:%d" % (ip, port), not a.startswith("CONNECTED") and CAN not in a, "EPERM / blocked", a)
a = tcp("::1", 9222, socket.AF_INET6); R("R-8 tcp [::1]:9222", not a.startswith("CONNECTED"), "EPERM / blocked", a)
a = tcp("10.77.0.1", 22); print("RESULT\tR-9 tcp hub IP, other port 22\tINFO\tinfo: IPAddressAllow is per address, any port\t" + a, flush=True)
a = tcp("10.77.0.1", 8443); R("R-9 tcp hub 10.77.0.1:8443 (allowed)", a.startswith("CONNECTED"), "connects", a)
try:
    u = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); u.sendto(b"\x12\x34\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00\x01x\x00\x00\x01\x00\x01", ("10.77.0.2", 53))
    R("R-9 udp dns to 10.77.0.2:53", False, "EPERM", "sent")
except OSError as e: R("R-9 udp dns to 10.77.0.2:53", True, "EPERM", repr(e))
try: socket.getaddrinfo("example.invalid", 443); R("R-9 getaddrinfo", False, "fails", "resolved")
except Exception as e: R("R-9 getaddrinfo", True, "fails", repr(e))
# R-10 writes
for p in ["/etc/kaptest-evil", "/usr/kaptest-evil", "/opt/kaptyorka/relay/kaptest-evil", "/etc/systemd/system/kaptyorka-relay.service",
          "/opt/kaptyorka/relay/current/relay.py", "/etc/kaptyorka/relay.conf", "/run/kaptyorka/relay/evil.sock"]:
    try: open(p, "a").close(); R("R-10 write " + p, False, "EROFS/EACCES", "written")
    except OSError as e: R("R-10 write " + p, e.errno in (errno.EROFS, errno.EACCES, errno.ENOENT), "EROFS/EACCES", repr(e))
# R-11, R-11b system calls, each in a child (the filter kills the caller with SIGSYS)
L = ctypes.CDLL(None, use_errno=True)
def sc(name, nr, *args):
    rd, wr = os.pipe(); pid_ = os.fork()
    if pid_ == 0:
        os.close(rd)
        r = L.syscall(nr, *[ctypes.c_long(a) if isinstance(a, int) else a for a in args]); e = ctypes.get_errno()
        os.write(wr, ("ret=%d errno=%s" % (r, errno.errorcode.get(e, e))).encode()); os._exit(0 if (r == -1 and e in (errno.EPERM, errno.EACCES, errno.ENOSYS)) else 3)
    os.close(wr); out = os.read(rd, 200).decode(); os.close(rd); _, stt = os.waitpid(pid_, 0)
    if os.WIFSIGNALED(stt): R(name, os.WTERMSIG(stt) == signal.SIGSYS, "refused (SIGSYS or EPERM)", "killed by " + signal.Signals(os.WTERMSIG(stt)).name)
    else: R(name, os.WEXITSTATUS(stt) == 0, "refused (SIGSYS or EPERM)", out)
addr = int(open(KT + "alice-addr").read().strip(), 16)
buf = ctypes.create_string_buffer(64); iov = (ctypes.c_void_p * 2)(ctypes.addressof(buf), 64); riov = (ctypes.c_void_p * 2)(addr, 64)
sc("R-11b process_vm_readv(alice, real stack address)", 310, int(pid), iov, 1, riov, 1, 0)
sc("R-11 process_vm_writev(alice)", 311, int(pid), iov, 1, riov, 1, 0)
sc("R-11 ptrace(PTRACE_ATTACH, alice)", 101, 16, int(pid), 0, 0)
sc("R-11 kcmp", 312, os.getpid(), int(pid), 0, 0, 0)
sc("R-11 pidfd_getfd", 438, 0, 0, 0)
sc("R-11 unshare(CLONE_NEWUSER)", 272, 0x10000000)
sc("R-11 mount(tmpfs)", 165, b"none", b"/tmp", b"tmpfs", 0, 0)
sc("R-11 bpf(BPF_PROG_LOAD)", 321, 5, 0, 0)
sc("R-11 init_module", 175, 0, 0, b"")
sc("R-11 perf_event_open", 298, 0, 0, -1, -1, 0)
sc("R-11 kexec_load", 246, 0, 0, 0, 0)
sc("R-11 setns", 308, 0, 0)
sc("R-11 userfaultfd", 323, 0)
# R-4b SysV shared memory of alice (PrivateIPC=yes)
try:
    L.shmat.restype = ctypes.c_void_p; i = L.shmget(0x7777, 0, 0)
    R("R-4 SysV shm of alice", i < 0, "not visible", "shmget %d" % i)
except Exception as e: R("R-4 SysV shm of alice", True, "not visible", repr(e))
# R-12 limits (memory last: OOMPolicy=stop may stop the unit)
p = subprocess.run([sys.executable, "-I", "-c", "import os,time\nn=0\ntry:\n  while n<1000:\n    if os.fork()==0:\n      time.sleep(20); os._exit(0)\n    n+=1\nexcept OSError as e: print('stopped at',n,repr(e))\nelse: print('forked',n)"],
                   capture_output=True, text=True, timeout=40)
R("R-12 fork 1000", "stopped at" in p.stdout, "stops near TasksMax=64", p.stdout.strip())
subprocess.run(["pkill", "-9", "-f", "time.sleep(20)"], capture_output=True)
print("ATTACK-DONE", flush=True)
p = subprocess.run([sys.executable, "-I", "-c", "b=b'x'*(2*1024**3); print('allocated', len(b))"], capture_output=True, text=True, timeout=40)
R("R-12b alloc 2GB child", p.returncode != 0 and "allocated" not in p.stdout, "killed at MemoryMax=200M (MemorySwapMax=0)", "rc=%d %s %s" % (p.returncode, p.stdout.strip(), p.stderr.strip()[-120:]))
