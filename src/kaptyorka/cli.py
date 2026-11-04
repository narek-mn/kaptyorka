import argparse
import os
import secrets
import select
import socket
import sys
from pathlib import Path

from kaptyorka import __version__, http, paths, service

def _token() -> str:
    p = paths.home() / "hub-token"
    if not p.exists():
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(secrets.token_urlsafe(24) + "\n")
    return p.read_text().strip()


def _outbound_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"


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
    try:
        lines = service.install("kaptyorka-hub", [exe, "serve", "--foreground", "--port", str(args.port)])
    except service.ServiceError as e:
        print(e, file=sys.stderr)
        return 1
    for line in lines:
        print(line, file=sys.stderr)
    url = f"http://{args.host or _outbound_ip()}:{args.port}"
    print(f"hub:   {url}\ntoken: {token}\npage:  {url}/#t={token}")
    return 0


def _who(args) -> int:
    cfg = paths.load_config()
    if not cfg.get("hub") or not cfg.get("token"):
        print("not joined: run kaptyorka join")
        return 1
    try:
        status, feed = http.request("GET", cfg["hub"] + "/api/feed", cfg["token"])
    except http.HubUnreachable:
        print(f"kaptyorka hub unreachable at {cfg['hub']}")
        return 1
    if status != 200:
        print(f"kaptyorka hub at {cfg['hub']} answered {status}")
        return 1
    rows = [("NAME", "STATUS", "ACTIVITY", "LAST SEEN")] + [
        (a["name"], a["status"], a.get("activity") or "", f"{a['last_seen_s']}s ago")
        for a in feed["agents"]]
    w = [max(len(r[i]) for r in rows) for i in range(3)]
    for r in rows:
        print("  ".join(c.ljust(n) for c, n in zip(r, w)) + "  " + r[3])
    return 0


def _mcp(args) -> int:
    from kaptyorka import shim

    cfg = paths.load_config()
    if not cfg.get("hub") or not cfg.get("token"):
        print("not joined: run kaptyorka join", file=sys.stderr)
        return 1
    shim.run(cfg, args.workspace)
    return 0


def _bridge(args) -> int:
    from kaptyorka.adapters import all_adapters
    from kaptyorka.bridge import Bridge, log

    cfg = paths.load_config()
    if not cfg.get("hub") or not cfg.get("token") or not cfg.get("machine"):
        print("not joined: run kaptyorka join", file=sys.stderr)
        return 1
    Bridge(cfg, [a for a in all_adapters(log) if a.installed()], log).run()
    return 0


def _hook(args) -> int:
    from kaptyorka import hook

    stdin = ""
    try:  # a tool that keeps the pipe open must not block the hook
        if not sys.stdin.isatty() and select.select([sys.stdin], [], [], 0.5)[0]:
            stdin = sys.stdin.read()
    except (OSError, ValueError):
        pass
    return hook.main(args.tool, args.event, stdin)


def _join(args) -> int:
    from kaptyorka import join

    return join.main(args.hub, args.token, args.name, only=args.only)


def entry() -> None:
    p = argparse.ArgumentParser(prog="kaptyorka", description="A team hub for coding agents")
    p.add_argument("--version", action="version", version=f"kaptyorka {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve", help="run the hub (installs a user service)")
    s.add_argument("--port", type=int, default=8765)
    s.add_argument("--host", help="address shown to the team (default: this machine's IP)")
    s.add_argument("--foreground", action="store_true")
    s.set_defaults(run=_serve)
    sub.add_parser("who", help="list agents online").set_defaults(run=_who)
    m = sub.add_parser("mcp", help="MCP server for one agent session (started by the tool)")
    m.add_argument("--workspace", help="project folder, for tools that do not set the cwd")
    m.set_defaults(run=_mcp)
    sub.add_parser("bridge", help="push messages into this machine's sessions").set_defaults(run=_bridge)
    h = sub.add_parser("hook", help="print the hub's brief for a tool hook (started by the tool)")
    h.add_argument("tool")
    h.add_argument("event", help="start or prompt")
    h.set_defaults(run=_hook)
    j = sub.add_parser("join", help="connect this machine's agents to the hub")
    j.add_argument("--hub", required=True)
    j.add_argument("--token", required=True)
    j.add_argument("--name", default=socket.gethostname().split(".")[0])
    j.add_argument("--only", action="append", help="configure only this tool (repeatable)")
    j.set_defaults(run=_join)
    args = p.parse_args()
    sys.exit(args.run(args))
