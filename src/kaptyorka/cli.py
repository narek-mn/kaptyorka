import argparse
import getpass
import json
import os
import re
import secrets
import select
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.parse import quote

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
        from kaptyorka.retention import prune_loop
        from kaptyorka.store import Store

        store = Store(paths.home() / "hub.db", legacy_token=token)
        stop = threading.Event()
        threading.Thread(target=prune_loop, args=(store, stop), daemon=True).start()
        uvicorn.run(build_app(store), host="0.0.0.0", port=args.port, log_level="warning",
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
    admin = _admin_device(token)
    cfg = paths.load_config()
    if not cfg.get("hub") or not cfg.get("token"):  # this machine joins as the admin
        paths.save_config({"hub": url, "token": admin, "machine": _default_name()})
    print(f"hub:   {url}\npage:  {url}/#t={admin}")
    print(f"\njoin this machine: kaptyorka join --hub {url} --token {admin}"
          "\ninvite teammates: kaptyorka invite <name>")
    return 0


def _admin_device(legacy_token: str) -> str:
    """The admin's own device token (device `hub`), made on the first start and kept in
    ~/.kaptyorka/admin-token, so the admin never depends on the shared token."""
    from kaptyorka.store import Store

    p = paths.home() / "admin-token"
    store = Store(paths.home() / "hub.db", legacy_token=legacy_token)
    try:
        saved = p.read_text().strip() if p.exists() else ""
        got = store.authenticate(saved) if saved else None
        if got is not None and got.is_admin() and not got.legacy:
            return saved
        admin = next(x for x in store.people() if x["role"] == "admin")
        token = store.device_add(admin["id"], "hub")
    finally:
        store.close()
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(token + "\n")
    return token


def _default_name() -> str:
    host = socket.gethostname().split(".")[0]
    try:
        return f"{getpass.getuser()}@{host}"
    except Exception:
        return host


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


def _admin_call(method: str, path: str, body=None):
    """Call the hub with the saved config; (status, body), or None after printing why not."""
    cfg = paths.load_config()
    if not cfg.get("hub") or not cfg.get("token"):
        print("not joined: run kaptyorka join", file=sys.stderr)
        return None
    try:
        return http.request(method, cfg["hub"] + path, cfg["token"], body)
    except http.HubUnreachable:
        print(f"kaptyorka hub unreachable at {cfg['hub']}", file=sys.stderr)
        return None


def _invite(args) -> int:
    got = _admin_call("POST", "/api/people", {"name": args.person})
    if got is None:
        return 1
    if got[0] not in (200, 409):  # 409: the person already exists
        print(got[1] if isinstance(got[1], str) else f"hub answered {got[0]}", file=sys.stderr)
        return 1
    got = _admin_call("POST", "/api/invites", {"person": args.person})
    if got is None:
        return 1
    if got[0] != 200:
        print(got[1] if isinstance(got[1], str) else f"hub answered {got[0]}", file=sys.stderr)
        return 1
    print(got[1]["url"])
    return 0


def _people(args) -> int:
    got = _admin_call("GET", "/api/people")
    if got is None:
        return 1
    if got[0] != 200:
        print(got[1] if isinstance(got[1], str) else f"hub answered {got[0]}", file=sys.stderr)
        return 1
    now = time.time()
    rows = [("NAME", "ROLE", "DEVICES", "LAST SEEN")]
    for p in got[1]:
        used = [d["last_used"] for d in p["devices"] if d.get("last_used")]
        rows.append((p["name"], p["role"], str(len(p["devices"])),
                     f"{round(now - max(used))}s ago" if used else "never"))
    w = [max(len(r[i]) for r in rows) for i in range(3)]
    for r in rows:
        print("  ".join(c.ljust(n) for c, n in zip(r, w)) + "  " + r[3])
    return 0


def _admin_do(method: str, path: str, body: dict, done: str) -> int:
    """One admin call that answers 204: print `done`, or the hub's message and exit 1."""
    got = _admin_call(method, path, body)
    if got is None:
        return 1
    if got[0] not in (200, 204):
        print(got[1] if isinstance(got[1], str) else f"hub answered {got[0]}", file=sys.stderr)
        return 1
    print(done)
    return 0


GRANT_SHAPES = "use <project>:<role> (read, write or manage) or machine:<host>"


def _grant(args) -> int:
    if args.target.startswith("machine:"):
        host = args.target[len("machine:"):]
        return _admin_do("POST", f"/api/machines/{quote(host, safe='')}/grant",
                         {"person": args.person}, f"granted {args.person} {args.target}")
    project, _, role = args.target.rpartition(":")
    if not project or not role:
        print(GRANT_SHAPES, file=sys.stderr)
        return 1
    return _admin_do("POST", f"/api/projects/{quote(project, safe='')}/grant",
                     {"person": args.person, "role": role}, f"granted {args.person} {args.target}")


def _revoke(args) -> int:
    if args.target.startswith("machine:"):
        host = args.target[len("machine:"):]
        path = f"/api/machines/{quote(host, safe='')}/revoke"
    elif ":" in args.target:
        print("use <project> or machine:<host>; to change a project role, " + GRANT_SHAPES,
              file=sys.stderr)
        return 1
    else:
        path = f"/api/projects/{quote(args.target, safe='')}/revoke"
    return _admin_do("POST", path, {"person": args.person}, f"revoked {args.person} {args.target}")


def _machine_owner(args) -> int:
    return _admin_do("POST", f"/api/machines/{quote(args.host, safe='')}/owner",
                     {"person": args.person}, f"{args.host} owned by {args.person}")


def _project_add(args) -> int:
    return _admin_do("POST", "/api/projects",
                     {"name": args.name, "repo": args.repo, "description": args.desc},
                     f"project {args.name} added")


def _stop(args) -> int:
    try:
        for unit in ("kaptyorka-bridge", "kaptyorka-hub"):
            for line in service.stop(unit):
                print(line)
    except service.ServiceError as e:
        print(e, file=sys.stderr)
        return 1
    return 0


def _restart(was_active: dict, run) -> int:
    """Restart the units that ran before the update; leave stopped ones off."""
    for unit, active in was_active.items():
        if active is None:
            print(f"{unit}: not installed")
            continue
        if not active:
            print(f"{unit}: stopped, left off")
            continue
        try:
            for line in service.restart(unit, run=run):
                print(line)
        except service.ServiceError as e:
            print(f"{unit}: {e}", file=sys.stderr)
            return 1
    return 0


def _remove(args) -> int:
    from kaptyorka import remove

    return remove.main()


def _leave(args) -> int:
    from kaptyorka import remove

    return remove.leave()


def _update(args, run=subprocess.run) -> int:
    venv = Path(sys.prefix)
    if not (venv / "pipx_metadata.json").exists():  # pipx writes this into every venv it makes
        print("kaptyorka was not installed with pipx; update it the way you installed it", file=sys.stderr)
        return 1
    was_active = {unit: service.is_active(unit, run=run) for unit in ("kaptyorka-bridge", "kaptyorka-hub")}
    try:
        r = run(["pipx", "upgrade", "--pip-args=--no-cache-dir", "kaptyorka"], capture_output=True, text=True)
        new = run([str(venv / "bin/kaptyorka"), "--version"], capture_output=True, text=True) if r.returncode == 0 else r
    except FileNotFoundError:
        print("pipx not found", file=sys.stderr)
        return 1
    if r.returncode != 0:
        print((r.stderr or r.stdout).strip() or "pipx upgrade failed", file=sys.stderr)
        return 1
    print(f"kaptyorka {__version__} -> {new.stdout.strip().removeprefix('kaptyorka ')}")
    return _restart(was_active, run)


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

    extra = {"env": join.read_env(sys.stdin.read())} if args.env_stdin else {}
    if args.invite:
        got = join.redeem(args.invite, args.name)
        if got is None:
            return 1
        hub, token, secret, person = got
        # the invite is spent: keep the token so a plain `kaptyorka join` can retry
        cfg = {"hub": hub, "token": token, "machine": args.name, "secret": secret}
        if person:
            cfg["person"] = person
        paths.save_config(cfg)
        return join.main(hub, token, args.name, only=args.only, secret=secret, person=person,
                         **extra)
    if not args.hub and not args.token:  # no flags: the previous arguments, saved by the last join
        cfg = paths.load_config()
        if not cfg.get("hub") or not cfg.get("token"):
            print("not joined yet: run kaptyorka join --hub <address> --token <token>", file=sys.stderr)
            return 1
        return join.main(cfg["hub"], cfg["token"], cfg.get("machine") or args.name, only=cfg.get("tools"), **extra)
    if not (args.hub and args.token):
        print("join needs both --hub and --token", file=sys.stderr)
        return 2
    return join.main(args.hub, args.token, args.name, only=args.only, **extra)


def _fleet_add(args) -> int:
    from kaptyorka import fleet

    cfg = paths.load_config()

    def invite(person: str) -> str:
        got = _admin_call("POST", "/api/invites", {"person": person})
        if got is None or got[0] != 200:
            raise RuntimeError(got[1] if got and isinstance(got[1], str) else "could not make an invite")
        return got[1]["url"]

    try:
        lines = fleet.add(args.targets, args.person, Path(args.key_from) if args.key_from else None,
                          cfg.get("hub", ""), invite, run=subprocess.run)
    except (RuntimeError, OSError) as e:
        print(e, file=sys.stderr)
        return 1
    for line in lines:
        print(line)
    return 1 if any(": failed at " in l for l in lines) else 0


def _fleet_list(args) -> int:
    from kaptyorka import fleet

    got = _admin_call("GET", "/api/machines")
    if got is None:
        return 1
    if got[0] != 200:
        print(got[1] if isinstance(got[1], str) else f"hub answered {got[0]}", file=sys.stderr)
        return 1
    for line in fleet.list(got[1]):
        print(line)
    return 0


def _doctor(args) -> int:
    from kaptyorka import doctor

    return doctor.run(paths.load_config(), fix=args.fix)


def entry() -> None:
    p = argparse.ArgumentParser(prog="kaptyorka", description="A team hub for coding agents")
    p.add_argument("--version", action="version", version=f"kaptyorka {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True, metavar="{start,join,leave,stop,update,remove,invite,people,"
                            "project,grant,revoke,machine,who,doctor,fleet}")
    hide = argparse.SUPPRESS  # subparsers are hidden by giving them no help text
    s = sub.add_parser("start", help="run the hub on this machine (again, with the same settings)")
    s.add_argument("--port", type=int, help="hub port (default 8765)")
    s.add_argument("--host", help="address shown to the team (default: this machine's IP)")
    s.set_defaults(run=_start)
    j = sub.add_parser("join", help="connect this machine's agents to the hub (again, with no flags)")
    j.add_argument("invite", nargs="?", help="invite link from your admin")
    j.add_argument("--hub", help="hub address")
    j.add_argument("--token", help="hub token")
    j.add_argument("--name", default=_default_name(), help="this machine's name on the team (default: user@host)")
    j.add_argument("--only", action="append", help=hide)
    j.add_argument("--env-stdin", action="store_true", help=hide)
    j.set_defaults(run=_join)
    sub.add_parser("leave", help="leave the hub: undo join on this machine, keep kaptyorka "
                   "installed; join brings it back").set_defaults(run=_leave)
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
    i = sub.add_parser("invite")
    i.add_argument("person")
    i.set_defaults(run=_invite)
    sub.add_parser("people").set_defaults(run=_people)
    for name, run in (("grant", _grant), ("revoke", _revoke)):
        g = sub.add_parser(name)
        g.add_argument("person")
        g.add_argument("target", help="<project>:<role> or machine:<host> (revoke: <project>)")
        g.set_defaults(run=run)
    pj = sub.add_parser("project").add_subparsers(dest="project_cmd", required=True)
    pa = pj.add_parser("add")
    pa.add_argument("name")
    pa.add_argument("--repo", default="")
    pa.add_argument("--desc", default="")
    pa.set_defaults(run=_project_add)
    mc = sub.add_parser("machine").add_subparsers(dest="machine_cmd", required=True)
    mo = mc.add_parser("owner")
    mo.add_argument("host")
    mo.add_argument("person")
    mo.set_defaults(run=_machine_owner)
    sub.add_parser("who").set_defaults(run=_who)
    d = sub.add_parser("doctor", help="check this machine's setup; --fix repairs what it can")
    d.add_argument("--fix", action="store_true", help="re-wire failing tools and restart a stopped bridge")
    d.set_defaults(run=_doctor)
    fl = sub.add_parser("fleet")
    fs = fl.add_subparsers(dest="fleet_cmd", required=True)
    fa = fs.add_parser("add")
    fa.add_argument("targets", nargs="+", metavar="user@host")
    fa.add_argument("--person", required=True)
    fa.add_argument("--key-from", help="file with one NAME=value line, sent to the bridge's environment")
    fa.set_defaults(run=_fleet_add)
    fs.add_parser("list").set_defaults(run=_fleet_list)
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
