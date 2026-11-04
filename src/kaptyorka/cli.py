import argparse
import os
import secrets
import socket
import sys
from pathlib import Path

from kaptyorka import __version__, http, paths, service

COMMANDS = ["join", "mcp", "hook", "bridge"]


def _stub(args) -> int:
    print("not implemented")
    return 2


def _token() -> str:
    p = paths.home() / "hub-token"
    if not p.exists():
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(secrets.token_urlsafe(24) + "\n")
    return p.read_text().strip()


def _serve(args) -> int:
    token = _token()
    if args.foreground:
        import uvicorn

        from kaptyorka.hub import build_app
        from kaptyorka.store import Store

        store = Store(paths.home() / "hub.db")
        uvicorn.run(build_app(store, token), host="0.0.0.0", port=args.port, log_level="warning")
        return 0
    exe = str(Path(sys.argv[0]).resolve())
    for line in service.install("kaptyorka-hub", [exe, "serve", "--foreground", "--port", str(args.port)]):
        print(line, file=sys.stderr)
    url = f"http://{socket.gethostname()}:{args.port}"
    print(f"hub:   {url}\ntoken: {token}\npage:  {url}/#t={token}")
    return 0


def _who(args) -> int:
    cfg = paths.load_config()
    if not cfg.get("hub") or not cfg.get("token"):
        print("not joined: run kaptyorka join")
        return 1
    try:
        _, feed = http.request("GET", cfg["hub"] + "/api/feed", cfg["token"])
    except http.HubUnreachable:
        print(f"kaptyorka hub unreachable at {cfg['hub']}")
        return 1
    rows = [("NAME", "STATUS", "ACTIVITY", "LAST SEEN")] + [
        (a["name"], a["status"], a.get("activity") or "", f"{a['last_seen_s']}s ago")
        for a in feed["agents"]]
    w = [max(len(r[i]) for r in rows) for i in range(3)]
    for r in rows:
        print("  ".join(c.ljust(n) for c, n in zip(r, w)) + "  " + r[3])
    return 0


def entry() -> None:
    p = argparse.ArgumentParser(prog="kaptyorka", description="A team hub for coding agents")
    p.add_argument("--version", action="version", version=f"kaptyorka {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve", help="run the hub (installs a user service)")
    s.add_argument("--port", type=int, default=8765)
    s.add_argument("--foreground", action="store_true")
    s.set_defaults(run=_serve)
    sub.add_parser("who", help="list agents online").set_defaults(run=_who)
    for name in COMMANDS:
        sub.add_parser(name, help=f"{name} (not implemented yet)").set_defaults(run=_stub)
    args = p.parse_args()
    sys.exit(args.run(args))
