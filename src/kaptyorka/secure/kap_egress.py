"""The forwarder (secure design v2, 4.5): the box's only way to the internet.

A small HTTP CONNECT proxy on the socket systemd passes in (kaptyorka-egress@<box>.socket,
/run/kapbox/<box>/pub/egress.sock, 0660 root:kap-<box>). It never sees inside TLS. A connection
is allowed only when the host NAME is on /etc/kaptyorka/boxes/<box>/egress-allow with that port
(443, or 22 for a listed git host). It resolves the name itself and refuses loopback, private,
link-local, multicast and this laptop's own addresses. Its unit also denies those ranges
(IPAddressDeny=), so a bug here is not enough.

Runs as a DynamicUser, Python standard library only. Installed as
/opt/kaptyorka/egress/<version>/egress.py; started as `egress.py <box>`. It exits when idle;
the socket starts it again.
"""
import ipaddress
import os
import re
import socket
import sys
import threading
import time

BOX_RX = re.compile(r"[a-z_][a-z0-9_-]{0,27}")
_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
# a host NAME: dotted labels, the last one not all digits (so never an IP address)
HOST_RX = re.compile(rf"(?=.{{1,253}}$){_LABEL}(?:\.{_LABEL})*\.(?![0-9]+$){_LABEL}")
BAD = [ipaddress.ip_network(n) for n in (
    "0.0.0.0/8 10.0.0.0/8 100.64.0.0/10 127.0.0.0/8 169.254.0.0/16 172.16.0.0/12 192.0.0.0/24 192.168.0.0/16 "
    "198.18.0.0/15 224.0.0.0/4 240.0.0.0/4 ::/128 ::1/128 ::ffff:0:0/96 64:ff9b::/96 fc00::/7 fe80::/10 ff00::/8"
).split()]
MAX_HEAD = 8192
MAX_CONNS = 64
IDLE_EXIT = 600


def parse_allow(text: str):
    """host -> ports. 'host' means 443, 'host:22' allows ssh for a git host. Bad lines allow nothing."""
    out = {}
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip().lower()
        if not line:
            continue
        host, _, port = line.partition(":")
        if HOST_RX.fullmatch(host) and port in ("", "443", "22"):
            out.setdefault(host, set()).add(int(port) if port else 443)
    return out


def own_addresses():
    """Every address of this laptop (all interfaces), from the kernel's tables."""
    s, prev = set(), None
    try:
        for line in open("/proc/net/fib_trie"):
            t = line.strip()
            if t.startswith("|--"):
                prev = t[3:].strip()
            elif "/32 host LOCAL" in t and prev:
                s.add(ipaddress.ip_address(prev))
    except OSError:
        pass
    try:
        for line in open("/proc/net/if_inet6"):
            h = line.split()[0]
            s.add(ipaddress.ip_address(":".join(h[i:i + 4] for i in range(0, 32, 4))))
    except OSError:
        pass
    return s


def refused_address(ip) -> str:
    if any(ip in n for n in BAD if n.version == ip.version):
        return f"address {ip} is local or private"
    if ip in own_addresses():
        return f"address {ip} is this computer"
    return ""


def parse_connect(head: bytes):
    """(host, port) from 'CONNECT host:port HTTP/1.x'; raises ValueError."""
    line = head.split(b"\r\n", 1)[0].decode("latin1")
    p = line.split()
    if len(p) != 3 or p[0] != "CONNECT" or not p[2].startswith("HTTP/1."):
        raise ValueError("only CONNECT")
    host, _, port = p[1].rpartition(":")
    if not port.isdigit() or not host:
        raise ValueError("bad port")
    host = host.lower()
    if host.startswith("["):
        raise ValueError("names only")
    if not HOST_RX.fullmatch(host):
        raise ValueError("names only")
    return host, int(port)


def decide(host: str, port: int, allow) -> str:
    if port not in allow.get(host, set()):
        return "host or port not on the allow list"
    return ""


class Forwarder:
    def __init__(self, allow_file: str):
        self.allow_file = allow_file
        self.busy = 0
        self.last = time.monotonic()
        self.lock = threading.Lock()

    def allow(self):
        try:
            with open(self.allow_file, encoding="utf-8") as f:
                return parse_allow(f.read(1 << 20))
        except OSError:
            return {}

    def handle(self, c: socket.socket) -> None:
        u = None
        try:
            c.settimeout(10)
            head = b""
            while b"\r\n\r\n" not in head:
                got = c.recv(1024)
                if not got:
                    return
                head += got
                if len(head) > MAX_HEAD:
                    return self.no(c, "request too long")
            try:
                host, port = parse_connect(head)
            except ValueError as e:
                return self.no(c, str(e))
            why = decide(host, port, self.allow())
            if why:
                return self.no(c, why)
            try:
                infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
            except OSError:
                return self.no(c, "name does not resolve")
            last = "no address"
            for fam, _, _, _, sa in infos:
                ip = ipaddress.ip_address(sa[0].split("%")[0])
                why = refused_address(ip)
                if why:
                    return self.no(c, why)   # one bad address refuses the name (no DNS games)
            for fam, _, _, _, sa in infos:
                try:
                    u = socket.socket(fam, socket.SOCK_STREAM)
                    u.settimeout(8)
                    u.connect(sa)
                    break
                except OSError as e:
                    last = str(e)
                    u.close()
                    u = None
            if u is None:
                c.sendall(f"HTTP/1.1 502 Bad Gateway\r\nX-Error: {last[:100]}\r\n\r\n".encode())
                return
            c.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
            rest = head.split(b"\r\n\r\n", 1)[1]
            if rest:
                u.sendall(rest)
            c.settimeout(None)
            u.settimeout(None)
            t = threading.Thread(target=pump, args=(c, u), daemon=True)
            t.start()
            pump(u, c)
            t.join()
        except OSError:
            pass
        finally:
            for x in (c, u):
                if x is not None:
                    try:
                        x.close()
                    except OSError:
                        pass

    @staticmethod
    def no(c: socket.socket, why: str) -> None:
        try:
            c.sendall(f"HTTP/1.1 403 Forbidden\r\nX-Refused: {why}\r\n\r\n".encode())
        except OSError:
            pass   # a caller that hangs up at once changes nothing

    def serve(self, ls: socket.socket) -> None:
        ls.settimeout(5)
        slots = threading.BoundedSemaphore(MAX_CONNS)

        def run(c):
            try:
                self.handle(c)
            finally:
                with self.lock:
                    self.busy -= 1
                    self.last = time.monotonic()
                slots.release()

        while True:
            try:
                c, _ = ls.accept()
            except socket.timeout:
                with self.lock:
                    if self.busy == 0 and time.monotonic() - self.last > IDLE_EXIT:
                        return
                continue
            if not slots.acquire(blocking=False):
                c.close()
                continue
            with self.lock:
                self.busy += 1
            threading.Thread(target=run, args=(c,), daemon=True).start()


def pump(a, b) -> None:
    try:
        while True:
            d = a.recv(65536)
            if not d:
                break
            b.sendall(d)
    except OSError:
        pass
    finally:
        try:
            b.shutdown(socket.SHUT_WR)
        except OSError:
            pass


# What this program may see (its unit's RootDirectory= and binds). ExecStartPre= runs
# `egress.py --viewcheck` inside the same sandbox: anything more in these folders means a setting
# systemd dropped without a word, and the egress does not start.
SYS_HIDDEN = ("block", "bus", "class", "dev", "devices", "firmware", "hypervisor", "kernel", "module", "power")
VIEW = [("/", {"bin", "dev", "etc", "lib", "lib64", "opt", "proc", "root", "run", "sbin", "sys", "tmp", "usr", "var"}),
        ("/etc", {"alternatives", "gai.conf", "host.conf", "hosts", "kaptyorka", "ld.so.cache",
                  "nsswitch.conf", "resolv.conf"}),
        ("/etc/kaptyorka", {"allow"}),
        ("/opt", {"kaptyorka"}), ("/opt/kaptyorka", {"egress"}), ("/var", {"tmp"}),
        ("/run", {"credentials", "host", "systemd"}), ("/usr/local", set()), ("/sys", set(SYS_HIDDEN) | {"fs"}),
        ("/sys/fs/cgroup", set())] + [("/sys/" + d, set()) for d in SYS_HIDDEN]


def view_problem(view) -> str:
    """'' when no folder in view shows more than it may; else the first extra."""
    for d, may in view:
        try:
            extra = set(os.listdir(d)) - may
        except FileNotFoundError:
            continue
        except OSError as e:
            return f"{d} cannot be listed ({e.strerror})"
        if extra:
            return f"{d} shows {', '.join(sorted(extra)[:8])}"
    return ""


def main(argv) -> int:
    if argv[1:2] == ["--viewcheck"]:
        why = view_problem(VIEW)
        if why:
            print(f"egress: its sandbox is not as set: {why}. The forwarder was not started.", file=sys.stderr)
            return 1
        return 0
    if len(argv) < 2 or not BOX_RX.fullmatch(argv[1]) or (len(argv) > 2 and not argv[2].startswith("/")):
        print("egress: usage: egress.py <box> [allow-file]", file=sys.stderr)
        return 2
    if os.environ.get("LISTEN_PID") != str(os.getpid()) or os.environ.get("LISTEN_FDS") != "1":
        print("egress: start it through kaptyorka-egress@<box>.socket", file=sys.stderr)
        return 2
    fwd = Forwarder(argv[2] if len(argv) > 2 else f"/etc/kaptyorka/boxes/{argv[1]}/egress-allow")
    fwd.serve(socket.socket(fileno=3))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
