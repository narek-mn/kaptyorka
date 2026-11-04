import argparse
import os
import secrets
import select
import socket
import subprocess
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
        uvicorn.run(build_app(store, token), host="0.0.0.0", port=args.port, log_level="warning",
                    timeout_graceful_shutdown=3)  # open agent streams must not hold up a stop
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
    rows = [("NAME", "LABEL", "STATUS", "ACTIVITY", "LAST SEEN")] + [
        (a["name"], a.get("label") or "", a["status"], a.get("activity") or "",
         f"{a['last_seen_s']}s ago")
        for a in feed["agents"]]
    w = [max(len(r[i]) for r in rows) for i in range(4)]
    for r in rows:
        print("  ".join(c.ljust(n) for c, n in zip(r, w)) + "  " + r[4])
    return 0


def _stop(args) -> int:
    try:
        for unit in ("kaptyorka-bridge", "kaptyorka-hub"):
            for line in service.stop(unit):
                print(line)
    except service.ServiceError as e:
        print(e, file=sys.stderr)
        return 1
    return 0


def _change(verb: str):
    def run(args) -> int:
        for unit in ("kaptyorka-bridge", "kaptyorka-hub"):
            try:
                for line in getattr(service, verb)(unit):
                    print(line)
            except service.ServiceError as e:
                print(f"{unit}: {e}", file=sys.stderr)
                return 1
        return 0
    return run


def _update(args, run=subprocess.run) -> int:
    venvs = Path(os.environ.get("PIPX_HOME") or Path.home() / ".local/share/pipx") / "venvs" / "kaptyorka"
    if Path(sys.prefix).resolve() != venvs.resolve():
        print("kaptyorka was not installed with pipx; update it the way you installed it", file=sys.stderr)
        return 1
    try:
        r = run(["pipx", "reinstall", "kaptyorka"], capture_output=True, text=True)
        new = run([str(venvs / "bin/kaptyorka"), "--version"], capture_output=True, text=True) if r.returncode == 0 else r
    except FileNotFoundError:
        print("pipx not found", file=sys.stderr)
        return 1
    if r.returncode != 0:
        print((r.stderr or r.stdout).strip() or "pipx reinstall failed", file=sys.stderr)
        return 1
    print(f"kaptyorka {__version__} -> {new.stdout.strip().removeprefix('kaptyorka ')}")
    return _change("restart")(args)


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
    from kaptyorka.bridge import Bridge, joined_adapters, log

    cfg = paths.load_config()
    if not cfg.get("hub") or not cfg.get("token") or not cfg.get("machine"):
        print("not joined: run kaptyorka join", file=sys.stderr)
        return 1
    adapters = [a for a in all_adapters(log) if a.installed()]
    Bridge(cfg, joined_adapters(cfg, adapters), log).run()
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
    sub.add_parser("stop", help="stop the bridge (and hub) and keep them off after a reboot; "
                   "kaptyorka join / serve start them again").set_defaults(run=_stop)
    sub.add_parser("start", help="start the bridge (and hub) installed here, after kaptyorka stop"
                   ).set_defaults(run=_change("start"))
    sub.add_parser("restart", help="restart the bridge (and hub) installed here, so new code or "
                   "settings take effect (use after updating)").set_defaults(run=_change("restart"))
    sub.add_parser("update", help="update kaptyorka (pipx) to the latest version and restart "
                   "the services").set_defaults(run=_update)
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
