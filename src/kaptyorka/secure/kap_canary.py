"""kaptyorka-canary (secure design v2, 4.6): things a box must never reach, kept alive so the
self-check can prove each wall. `enter` first checks every canary answers from outside (a dead
canary would let the inside check pass for the wrong reason); wallcheck then checks the box
cannot reach any of them.

Holds: a SysV shared memory segment with a fixed key, an abstract Unix socket, TCP listeners on
127.0.0.1 and on the wildcard address, and itself as a process with a marker on its command
line. The canary files are made by tmpfiles.d (and again by enter when they are missing).
A caller that hangs up at once must not kill it (slice 0: send errors are ignored).

Runs as a DynamicUser; installed as /usr/local/libexec/kaptyorka/canary. Standard library only.
"""
import ctypes
import socket
import sys
import threading

MARK = b"KAPTYORKA-WALL-CANARY"
SHM_KEY = 0x4B415031
ABSTRACT = b"\0kaptyorka-wall-canary"
TCP_LO, TCP_ANY = 47991, 47992


def hold_shm() -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    libc.shmat.restype = ctypes.c_void_p
    i = libc.shmget(SHM_KEY, 4096, 0o1644)     # IPC_CREAT | 0644
    if i < 0:
        if libc.shmget(SHM_KEY, 0, 0) >= 0:      # still there from before: it serves the same purpose
            return
        raise SystemExit("canary: shmget failed")
    a = libc.shmat(i, None, 0)
    ctypes.memmove(a, MARK, len(MARK))


def serve(fam: int, addr) -> socket.socket:
    s = socket.socket(fam, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(addr)
    s.listen(16)

    def loop():
        while True:
            try:
                c, _ = s.accept()
            except OSError:
                continue
            try:
                c.settimeout(2)
                c.sendall(MARK + b"\n")
            except OSError:
                pass
            finally:
                c.close()
    threading.Thread(target=loop, daemon=True).start()
    return s


def main(argv) -> int:
    hold_shm()
    serve(socket.AF_UNIX, ABSTRACT)
    serve(socket.AF_INET, ("127.0.0.1", TCP_LO))
    serve(socket.AF_INET, ("0.0.0.0", TCP_ANY))
    threading.Event().wait()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
