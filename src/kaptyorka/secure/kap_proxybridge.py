"""proxybridge (secure design v2, 4.5): inside the box, the only way out to the internet.

  proxybridge.py --idle SECONDS     listen on 127.0.0.1:3128 in the box's own network namespace and
                                    pass each connection to the forwarder's socket /run/egress.sock
                                    (HTTPS_PROXY=http://127.0.0.1:3128 for every box unit). Exits
                                    after SECONDS with no connection.
  proxybridge.py --stdio HOST PORT  ssh's ProxyCommand: ask the forwarder for HOST:PORT, then pass
                                    stdin and stdout through.

Runs as the box user inside the walls; it decides nothing (the forwarder outside does).
Installed as /opt/kaptyorka/box/<version>/proxybridge.py. Standard library only.
"""
import os
import re
import socket
import sys
import threading
import time

EGRESS = "/run/egress.sock"
HOST_RX = re.compile(r"[A-Za-z0-9.-]{1,253}|\[[0-9A-Fa-f:.]{2,45}\]")


def pump(a, b, done=None) -> None:
    try:
        while True:
            d = a.recv(65536)
            if not d:
                break
            b.sendall(d)
    except OSError:
        pass
    finally:
        for x in (a, b):
            try:
                x.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        if done:
            done()


def serve(idle: float, port: int = 3128, ls=None, stop=None, state=None) -> None:
    """ls: a listening socket already open (the helper's, from its socket unit); else bind port.
    stop: an Event; once set, no new connection is taken (they wait in the socket's queue for the
    next helper) while the open ones go on. state["open"]: how many pump ends are still open."""
    if ls is None:
        ls = socket.socket()
        ls.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        ls.bind(("127.0.0.1", port))
        ls.listen(64)
    ls.settimeout(0.5)
    state = state if state is not None else {}
    state.update({"open": 0, "last": time.monotonic()})
    lock = threading.Lock()

    def closed():
        with lock:
            state["open"] -= 1
            state["last"] = time.monotonic()

    while not (stop and stop.is_set()):
        try:
            c, _ = ls.accept()
        except socket.timeout:
            with lock:
                if state["open"] <= 0 and time.monotonic() - state["last"] > idle:
                    return
            continue
        try:
            u = socket.socket(socket.AF_UNIX)
            u.connect(EGRESS)
        except OSError:
            c.close()
            continue
        with lock:
            state["open"] += 2
            state["last"] = time.monotonic()
        threading.Thread(target=pump, args=(c, u, closed), daemon=True).start()
        threading.Thread(target=pump, args=(u, c, closed), daemon=True).start()


def stdio(host: str, port: str) -> int:
    if not HOST_RX.fullmatch(host) or not port.isdigit():
        print("proxybridge: bad host or port", file=sys.stderr)
        return 2
    u = socket.socket(socket.AF_UNIX)
    u.connect(EGRESS)
    u.sendall(f"CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n\r\n".encode())
    head = b""
    while b"\r\n\r\n" not in head and len(head) < 8192:
        got = u.recv(1)
        if not got:
            break
        head += got
    if not head.startswith(b"HTTP/1.1 200"):
        print("proxybridge: the forwarder refused: " + head.split(b"\r\n\r\n")[0].decode("latin1", "replace")
              .replace("\r\n", "; ")[:200], file=sys.stderr)
        return 1

    def up():
        try:
            while True:
                d = os.read(0, 65536)
                if not d:
                    break
                u.sendall(d)
        except OSError:
            pass
        try:
            u.shutdown(socket.SHUT_WR)
        except OSError:
            pass
    threading.Thread(target=up, daemon=True).start()
    try:
        while True:
            d = u.recv(65536)
            if not d:
                break
            os.write(1, d)
    except OSError:
        pass
    return 0


def main(argv) -> int:
    if len(argv) == 3 and argv[0] == "--stdio":
        return stdio(argv[1], argv[2])
    if len(argv) == 2 and argv[0] == "--idle" and argv[1].isdigit():
        serve(float(argv[1]))
        return 0
    print("usage: proxybridge.py --idle SECONDS | --stdio HOST PORT", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
