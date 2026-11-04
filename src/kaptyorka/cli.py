import argparse
import json
import os
import re
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


def _installed_hub_port() -> dict:
    """The port of a hub unit installed by a version that did not write hub.json."""
    for unit in (Path.home() / ".config/systemd/user/kaptyorka-hub.service",
                 Path.home() / "Library/LaunchAgents/io.kaptyorka.kaptyorka-hub.plist"):
        try:
            m = re.search(r"--port(?:</string>)?\s*(?:<string>)?(\d+)", unit.read_text())
        except OSError:
            continue
        if m:
            return {"port": int(m.group(1))}
    return {}


def _start(args) -> int:
    """Run the hub here; with no flags, again with the port and host used last time."""
    saved_file = paths.home() / "hub.json"
    try:
        saved = json.loads(saved_file.read_text())
    except (OSError, ValueError):
        saved = _installed_hub_port()
    args.port = args.port or saved.get("port", 8765)
    args.host = args.host or saved.get("host")
    args.foreground = False
    rc = _serve(args)
    if rc == 0:
        saved_file.write_text(json.dumps({"port": args.port, "host": args.host}))
    return rc


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


def _restart(args) -> int:
    for unit in ("kaptyorka-bridge", "kaptyorka-hub"):
        try:
            for line in service.restart(unit):
                print(line)
        except service.ServiceError as e:
            print(f"{unit}: {e}", file=sys.stderr)
            return 1
    return 0


def _remove(args) -> int:
    from kaptyorka import remove

    return remove.main()


def _update(args, run=subprocess.run) -> int:
    venv = Path(sys.prefix)
    if not (venv / "pipx_metadata.json").exists():  # pipx writes this into every venv it makes
        print("kaptyorka was not installed with pipx; update it the way you installed it", file=sys.stderr)
        return 1
    try:
        r = run(["pipx", "reinstall", "kaptyorka"], capture_output=True, text=True)
        new = run([str(venv / "bin/kaptyorka"), "--version"], capture_output=True, text=True) if r.returncode == 0 else r
    except FileNotFoundError:
        print("pipx not found", file=sys.stderr)
        return 1
    if r.returncode != 0:
        print((r.stderr or r.stdout).strip() or "pipx reinstall failed", file=sys.stderr)
        return 1
    print(f"kaptyorka {__version__} -> {new.stdout.strip().removeprefix('kaptyorka ')}")
    return _restart(args)


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

    if not args.hub and not args.token:  # again, from the saved settings: just the bridge
        if not paths.load_config().get("hub"):
            print("not joined yet: run kaptyorka join --hub <address> --token <token>", file=sys.stderr)
            return 1
        return 0 if join.install_bridge(str(Path(sys.argv[0]).resolve())) else 1
    if not (args.hub and args.token):
        print("join needs both --hub and --token", file=sys.stderr)
        return 2
    return join.main(args.hub, args.token, args.name, only=args.only)


def entry() -> None:
    p = argparse.ArgumentParser(prog="kaptyorka", description="A team hub for coding agents")
    p.add_argument("--version", action="version", version=f"kaptyorka {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True, metavar="{start,join,stop,update,remove}")
    hide = argparse.SUPPRESS  # subparsers are hidden by giving them no help text
    s = sub.add_parser("start", help="run the hub on this machine (again, with the same settings)")
    s.add_argument("--port", type=int, help="hub port (default 8765)")
    s.add_argument("--host", help="address shown to the team (default: this machine's IP)")
    s.set_defaults(run=_start)
    j = sub.add_parser("join", help="connect this machine's agents to the hub (again, with no flags)")
    j.add_argument("--hub", help="hub address")
    j.add_argument("--token", help="hub token")
    j.add_argument("--name", default=socket.gethostname().split(".")[0], help="this machine's name")
    j.add_argument("--only", action="append", help=hide)
    j.set_defaults(run=_join)
    sub.add_parser("stop", help="stop the bridge (and hub) and keep them off after a reboot; "
                   "start / join bring them back").set_defaults(run=_stop)
    sub.add_parser("update", help="update kaptyorka (pipx) to the latest version and restart "
                   "the services").set_defaults(run=_update)
    sub.add_parser("remove", help="remove kaptyorka from this machine completely").set_defaults(run=_remove)
    # called by service units and tool configs, not by people
    v = sub.add_parser("serve")
    v.add_argument("--port", type=int, default=8765)
    v.add_argument("--host")
    v.add_argument("--foreground", action="store_true")
    v.set_defaults(run=_serve)
    sub.add_parser("who").set_defaults(run=_who)
    m = sub.add_parser("mcp")
    m.add_argument("--workspace")
    m.set_defaults(run=_mcp)
    sub.add_parser("bridge").set_defaults(run=_bridge)
    h = sub.add_parser("hook")
    h.add_argument("tool")
    h.add_argument("event")
    h.set_defaults(run=_hook)
    args = p.parse_args()
    sys.exit(args.run(args))
