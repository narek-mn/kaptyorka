import argparse
import getpass
import json
import os
import re
import secrets
import shutil
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


MANAGER_NOTE = ("manager: starts by itself through this machine's bridge; if its first launch "
                "fails with \"does not trust\", run claude once in ~/.kaptyorka/manager and accept the prompt")


def _apply_manager(store, args) -> None:
    """Switch the manager off or on from the flags; logged only when it changes.
    Neither flag (a plain service start) leaves the stored switch alone."""
    from kaptyorka.manager import ManagerStore

    off, on = getattr(args, "no_manager", False), getattr(args, "manager", False)
    if not (off or on):
        return
    mm = ManagerStore(store)
    if mm.enabled() != on:
        mm.set_enabled(on, "hub")
    if on:
        mm.unblock()  # the admin has made Claude Code trust the folder


def _serve(args) -> int:
    token = _token()
    if args.foreground:
        import uvicorn

        from kaptyorka.hub import build_app
        from kaptyorka.retention import prune_loop
        from kaptyorka.store import Store

        store = Store(paths.home() / "hub.db", legacy_token=token)
        _apply_manager(store, args)
        stop = threading.Event()
        threading.Thread(target=prune_loop, args=(store, stop), daemon=True).start()
        uvicorn.run(build_app(store, manager_url=getattr(args, "manager_url", None)
                                  or f"http://127.0.0.1:{args.port}"),
                    host="0.0.0.0", port=args.port, log_level="warning",
                    timeout_graceful_shutdown=3)  # open agent streams must not hold up a stop
        return 0
    exe = str(Path(sys.argv[0]).resolve())
    try:
        cmd = [exe, "serve", "--foreground", "--port", str(args.port)]
        if getattr(args, "no_manager", False):
            cmd.append("--no-manager")
        lines = service.install("kaptyorka-hub", cmd)
    except service.ServiceError as e:
        print(e, file=sys.stderr)
        return 1
    for line in lines:
        print(line, file=sys.stderr)
    url = f"http://{args.host or _outbound_ip()}:{args.port}"
    admin = _admin_device(token)
    from kaptyorka.store import Store

    store = Store(paths.home() / "hub.db", legacy_token=token)
    try:
        _apply_manager(store, args)
    finally:
        store.close()
    cfg = paths.load_config()
    if not cfg.get("hub") or not cfg.get("token"):  # this machine joins as the admin
        paths.save_config({"hub": url, "token": admin, "machine": _default_name()})
    print(f"hub:   {url}\npage:  {url}/#t={admin}")
    print(f"\njoin this machine: kaptyorka join --hub {url} --token {admin}"
          "\ninvite teammates: kaptyorka invite <name>")
    if not getattr(args, "no_manager", False):
        print(MANAGER_NOTE)
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
    args.no_manager = args.no_manager or (not args.manager and saved.get("manager") is False)
    rc = _serve(args)
    if rc == 0:
        saved_file.write_text(json.dumps({"port": args.port, "host": args.host,
                                          "manager": not args.no_manager}))
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


def _rules_call(method: str, path: str, body):
    """Call the hub for a rules push; the body on 200, else None after printing why."""
    got = _admin_call(method, path, body)
    if got is None:
        return None
    if got[0] == 403:
        print("only the admin's device may push rules", file=sys.stderr)
        return None
    if got[0] != 200:
        print(got[1] if isinstance(got[1], str) else f"hub answered {got[0]}", file=sys.stderr)
        return None
    return got[1]


def _read_team_file(path: Path):
    """The file's text, or None after printing why it cannot be read."""
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        print(f"cannot read {path}: not UTF-8 text", file=sys.stderr)
    except OSError as e:
        print(f"cannot read {path}: {e.strerror or e}", file=sys.stderr)
    return None


def _rules_push(args) -> int:
    from kaptyorka import rules
    from kaptyorka.store import KaptyorkaError

    root = Path(args.dir)
    rules_file = root / "RULES.md"
    if not rules_file.is_file():
        print(f"no RULES.md in {root}")
        return 1
    text = _read_team_file(rules_file)
    if text is None:
        return 1
    if len(text) > rules.MAX_TEXT:
        print(f"{rules_file} is too long: {len(text)} characters, limit {rules.MAX_TEXT}", file=sys.stderr)
        return 1
    skills_dir = root / "skills"
    skill_files = sorted(f for f in skills_dir.glob("*.md") if not f.is_dir()) if skills_dir.is_dir() else []
    skills = {}
    for f in skill_files:  # everything is read and checked before anything is uploaded
        try:
            name = rules.check_name(f.stem)
        except KaptyorkaError as e:
            print(f"{f.name}: {e}", file=sys.stderr)
            return 1
        body = _read_team_file(f)
        if body is None:
            return 1
        if len(body) > rules.MAX_TEXT:
            print(f"{f} is too long: {len(body)} characters, limit {rules.MAX_TEXT}", file=sys.stderr)
            return 1
        skills[name] = body
    if not args.force:  # rules accepted or edited on the page live only on the hub
        cur = _rules_call("GET", "/api/rules", None)
        if cur is None:
            return 1
        have = {line.strip() for line in text.splitlines()}
        missing = [line.strip() for line in str(cur.get("text") or "").splitlines()
                   if line.strip() and line.strip() not in have]
        if missing:
            print("the hub has rules not in RULES.md (edited on the page or accepted proposals):",
                  file=sys.stderr)
            for line in missing:
                print("  " + line, file=sys.stderr)
            print("add them to RULES.md or push with --force", file=sys.stderr)
            return 1
    sent = _rules_call("POST", "/api/rules", {"text": text})
    if sent is None:
        return 1
    got = _admin_call("PUT", "/api/skills", {"skills": skills})
    if got is None:
        return 1
    if got[0] != 200:
        answer = got[1] if isinstance(got[1], str) else f"hub answered {got[0]}"
        print(f"skills upload failed ({got[0]}): {answer}; the rules were already pushed as v{sent['version']}",
              file=sys.stderr)
        return 1
    done = got[1]
    print(f"team rules v{sent['version']}; skills: {len(done['added'])} added, "
          f"{len(done['changed'])} changed, {len(done['removed'])} removed")
    if not skill_files and done["removed"]:  # no skills/ folder: the push replaced the whole set
        print(f"no skills/ folder in {root}: all team skills removed")
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


def _admin_json(method: str, path: str, body=None):
    """One admin call; its answer on 2xx, else None after printing the hub's sentence."""
    got = _admin_call(method, path, body)
    if got is None:
        return None
    if got[0] not in (200, 204):
        print(got[1] if isinstance(got[1], str) and got[1] else f"hub answered {got[0]}",
              file=sys.stderr)
        return None
    return got[1] if got[1] is not None else {}


def _confirm(question: str, yes: bool) -> bool:
    if yes:
        return True
    try:
        answer = input(f"{question} [y/N] ")
    except EOFError:
        answer = ""
    if answer.strip().lower() in ("y", "yes"):
        return True
    print("nothing changed")
    return False


def _people_remove(args) -> int:
    if not _confirm(f"Remove {args.name}? Their devices stop working at once.", args.yes):
        return 1
    done = _admin_json("DELETE", f"/api/people/{quote(args.name, safe='')}")
    if done is None:
        return 1
    print(f"removed {args.name}: {done['devices']} devices stopped, {done['workers']} workers"
          f" closed, {done['grants']} grants and {done['requests']} requests dropped")
    return 0


def _people_role(args) -> int:
    return _admin_do("POST", f"/api/people/{quote(args.name, safe='')}/role",
                     {"role": args.role}, f"{args.name} is now a {args.role}")


def _invites(args) -> int:
    if args.cancel:
        return _admin_do("DELETE", f"/api/invites/{quote(args.cancel, safe='')}", None,
                         "invite cancelled")
    rows = _admin_json("GET", "/api/invites")
    if rows is None:
        return 1
    if not rows:
        print("no open invites")
        return 0
    now = time.time()
    table = [("PERSON", "EXPIRES IN", "CODE")]
    for r in rows:
        table.append((r["person"], f"{max(0, round((r['expires'] - now) / 3600))}h", r["code"]))
    w = [max(len(r[i]) for r in table) for i in range(2)]
    for r in table:
        print("  ".join(c.ljust(n) for c, n in zip(r, w)) + "  " + r[2])
    return 0


def _project_edit(args) -> int:
    return _admin_do("POST", f"/api/projects/{quote(args.name, safe='')}",
                     {"description": args.description}, f"project {args.name} updated")


def _project_remove(args) -> int:
    if not _confirm(f"Remove the project {args.name}? Everyone loses access; its notes stay.",
                    args.yes):
        return 1
    done = _admin_json("DELETE", f"/api/projects/{quote(args.name, safe='')}")
    if done is None:
        return 1
    print(f"removed the project {args.name}: {done['grants']} grants dropped; its notes stay")
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

    try:
        extra = {"env": join.read_env(sys.stdin.read())} if args.env_stdin else {}
    except ValueError as e:
        print(e, file=sys.stderr)
        return 1
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
    except (RuntimeError, OSError, ValueError) as e:
        for line in getattr(e, "lines", []):
            print(line)
        print(e, file=sys.stderr)
        return 1
    for line in lines:
        print(line)
    return 1 if any(": failed at " in l or l.endswith(": not a user@host") for l in lines) else 0


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


def _backup(args) -> int:
    from kaptyorka import backup

    home = paths.home()
    if args.nightly:
        try:
            backup._check_dest(args.dest)
            if not shutil.which("gpg"):
                print("gpg not found: nightly backups will be unencrypted")
            for line in service.install_timer("kaptyorka-backup",
                                              [str(Path(sys.argv[0]).resolve()), "backup", args.dest]):
                print(line)
            # made now, not at the first night's run, so its copy can be kept from the start
            print(f"backup passphrase in {backup._passfile(home)}, keep a copy")
        except (ValueError, OSError, service.ServiceError) as e:
            print(e, file=sys.stderr)
            return 1
        return 0
    return backup.run_backup(home, args.dest, subprocess.run)


def _restore(args) -> int:
    from kaptyorka import backup

    return backup.restore(paths.home(), Path(args.file), subprocess.run)


def _doctor(args) -> int:
    from kaptyorka import doctor

    return doctor.run(paths.load_config(), fix=args.fix)


def entry() -> None:
    p = argparse.ArgumentParser(prog="kaptyorka", description="A team hub for coding agents")
    p.add_argument("--version", action="version", version=f"kaptyorka {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True, metavar="{start,join,leave,stop,update,remove,invite,people,"
                            "project,grant,revoke,machine,who,doctor,fleet,backup,restore}")
    hide = argparse.SUPPRESS  # subparsers are hidden by giving them no help text
    s = sub.add_parser("start", help="run the hub on this machine (again, with the same settings)")
    s.add_argument("--port", type=int, help="hub port (default 8765)")
    s.add_argument("--host", help="address shown to the team (default: this machine's IP)")
    m = s.add_mutually_exclusive_group()
    m.add_argument("--no-manager", action="store_true", help="do not keep a manager session")
    m.add_argument("--manager", action="store_true", help="keep a manager session (the default)")
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
    v.add_argument("--no-manager", action="store_true")
    v.add_argument("--manager-url")
    v.set_defaults(run=_serve)
    i = sub.add_parser("invite")
    i.add_argument("person")
    i.set_defaults(run=_invite)
    pp = sub.add_parser("people")
    pp.set_defaults(run=_people)
    ps = pp.add_subparsers(dest="people_cmd")
    pr = ps.add_parser("remove", help="take a person off the team")
    pr.add_argument("name")
    pr.add_argument("--yes", action="store_true", help="do not ask first")
    pr.set_defaults(run=_people_remove)
    pl = ps.add_parser("role", help="make a person a member or a manager")
    pl.add_argument("name")
    pl.add_argument("role", choices=("member", "manager"))
    pl.set_defaults(run=_people_role)
    iv = sub.add_parser("invites")
    iv.add_argument("--cancel", metavar="code")
    iv.set_defaults(run=_invites)
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
    pe = pj.add_parser("edit")
    pe.add_argument("name")
    pe.add_argument("--description", required=True)
    pe.set_defaults(run=_project_edit)
    px = pj.add_parser("remove")
    px.add_argument("name")
    px.add_argument("--yes", action="store_true", help="do not ask first")
    px.set_defaults(run=_project_remove)
    mc = sub.add_parser("machine").add_subparsers(dest="machine_cmd", required=True)
    mo = mc.add_parser("owner")
    mo.add_argument("host")
    mo.add_argument("person")
    mo.set_defaults(run=_machine_owner)
    sub.add_parser("who").set_defaults(run=_who)
    d = sub.add_parser("doctor", help="check this machine's setup; --fix repairs what it can")
    d.add_argument("--fix", action="store_true", help="re-wire failing tools and restart a stopped bridge")
    d.set_defaults(run=_doctor)
    bk = sub.add_parser("backup", help="send a copy of the hub's data to user@host:path (scp)")
    bk.add_argument("dest", metavar="user@host:path")
    bk.add_argument("--nightly", action="store_true", help="do it every day at 03:00")
    bk.set_defaults(run=_backup)
    rs = sub.add_parser("restore", help="put a backup file back (stop the hub first)")
    rs.add_argument("file")
    rs.set_defaults(run=_restore)
    fl = sub.add_parser("fleet")
    fs = fl.add_subparsers(dest="fleet_cmd", required=True)
    fa = fs.add_parser("add")
    fa.add_argument("targets", nargs="+", metavar="user@host")
    fa.add_argument("--person", required=True)
    fa.add_argument("--key-from", help="file with one NAME=value line, sent to the bridge's environment")
    fa.set_defaults(run=_fleet_add)
    fs.add_parser("list").set_defaults(run=_fleet_list)
    rl = sub.add_parser("rules").add_subparsers(dest="rules_cmd", required=True)
    rp = rl.add_parser("push", help="upload a team rules folder: RULES.md and skills/*.md")
    rp.add_argument("dir", metavar="dir")
    rp.add_argument("--force", action="store_true", help="replace the hub's rules even if it has lines not in RULES.md")
    rp.set_defaults(run=_rules_push)
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
