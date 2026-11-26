#!/usr/bin/python3 -I
"""addrwatch (secure design v2, 4.5, second wall): when this computer's own addresses change,
add the new ones to IPAddressDeny= of every box's forwarder unit and of each running box slice,
with `systemctl set-property --runtime`. The forwarder's code refuses this computer's addresses
at every connection; this keeps its unit refusing them too, so a bad forwarder release cannot
reach a server of the person's on 0.0.0.0 through an address the computer got after the chat
started (slice 2, E-2c).

Root, from kaptyorka-addrwatch.service. It listens for address changes on a netlink socket and
also looks every RESYNC seconds (a missed message, a new box). It only ever adds: an address
that goes away stays denied until the unit next starts, which is harmless. Restart-safe: it
reads what each unit already denies and adds only what is missing. Every systemctl call has a
time limit. Installed as /usr/local/libexec/kaptyorka/addrwatch. Standard library only.
"""
import ipaddress
import os
import select
import socket
import subprocess
import sys
import time
from typing import Dict, List

if __package__:
    from . import kapcommon as kc, kaproot as kr
else:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import kapcommon as kc
    import kaproot as kr

RESYNC = 30.0
SETTLE = 0.3                          # an address change comes as several messages
RTMGRP_IPV4_IFADDR, RTMGRP_IPV6_IFADDR = 0x10, 0x100


def log(msg: str) -> None:
    print(f"addrwatch: {msg}", flush=True)


def units() -> List[str]:
    """The forwarder of every box (loaded or not: the runtime setting waits for its next start)
    and every box slice that is running."""
    out = []
    try:
        names = sorted(os.listdir(kc.BOXES))
    except FileNotFoundError:
        return out
    for b in names:
        if kc.BOX_RX.fullmatch(b):
            out.append(f"kaptyorka-egress@{b}.service")
            if kr.is_active(kc.slice_name(b)):
                out.append(kc.slice_name(b))
    return out


def denied(unit: str) -> set:
    """The networks a unit denies now (an address is a /32 or /128 network)."""
    r = kr.systemctl("show", "-p", "IPAddressDeny", "--value", unit, check=False)
    got = set()
    for item in r.stdout.split():
        try:
            got.add(ipaddress.ip_network(item, strict=False))
        except ValueError:
            continue
    return got


def missing(want: List[str], have: set) -> List[str]:
    """The addresses no denied network covers yet (10.77.0.9 is covered by 10.0.0.0/8)."""
    return [a for a in want if not any(ipaddress.ip_address(a) in net for net in have)]


def sync(cache: Dict[str, set]) -> int:
    """Add this computer's addresses to every unit that lacks one. Returns how many units changed."""
    addrs = kr.host_addrs()
    changed = 0
    for u in units():
        have = cache.get(u)
        if have is None or missing(addrs, have):
            have = denied(u)          # ask systemd before changing: restart-safe, nothing added twice
        add = missing(addrs, have)
        if add:
            r = kr.systemctl("set-property", "--runtime", u, "IPAddressDeny=" + " ".join(add), check=False)
            if r.returncode == 0:
                log(f"{u}: now also denies {' '.join(add)}")
                have = have | {ipaddress.ip_network(a) for a in add}
                changed += 1
            else:
                log(f"{u}: could not add {' '.join(add)}: {(r.stderr or r.stdout).strip()[:200]}")
        cache[u] = have
    return changed


def listener() -> socket.socket:
    s = socket.socket(socket.AF_NETLINK, socket.SOCK_RAW | socket.SOCK_CLOEXEC, 0)   # NETLINK_ROUTE
    s.bind((0, RTMGRP_IPV4_IFADDR | RTMGRP_IPV6_IFADDR))
    s.setblocking(False)
    return s


def drain(s: socket.socket) -> None:
    while True:
        try:
            if not s.recv(65536):
                return
        except BlockingIOError:
            return


def main(argv) -> int:
    if os.geteuid() != 0:
        log("runs as root (kaptyorka-addrwatch.service)")
        return 2
    once = "--once" in argv
    cache: Dict[str, set] = {}
    s = None if once else listener()
    log(f"started; this computer's addresses: {' '.join(kr.host_addrs()) or 'none'}")
    try:
        sync(cache)
    except (OSError, kc.Refuse, subprocess.SubprocessError) as e:
        log(f"sync failed: {e}")
    if once:
        return 0
    while True:
        ready, _, _ = select.select([s], [], [], RESYNC)
        if ready:
            time.sleep(SETTLE)
            drain(s)
            log(f"addresses changed: {' '.join(kr.host_addrs()) or 'none'}")
        else:
            cache.clear()             # every RESYNC seconds: ask systemd again
        try:
            sync(cache)
        except (OSError, kc.Refuse, subprocess.SubprocessError) as e:
            log(f"sync failed: {e}")


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
