import getpass
import os
import re
import socket
import sys
from pathlib import Path
from typing import List, Optional, Tuple

from kaptyorka import http, paths, service


def install_bridge(exe: str, install=None, extra_env: Optional[dict] = None) -> bool:
    """Install and start the bridge service; False (after printing why) if that failed."""
    if os.environ.get("KAPTYORKA_SKIP_SERVICE") == "1":
        return True
    try:
        for line in (install or service.install)("kaptyorka-bridge", [exe, "bridge"],
                                                 env={"PATH": os.environ.get("PATH", ""), **(extra_env or {})}):
            print(f"bridge: {line}")
    except service.ServiceError as e:
        print(f"bridge service failed: {e}", file=sys.stderr)
        return False
    return True


ENV_LINE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)=(.*)", re.S)


def read_env(text: str) -> dict:
    """NAME=value lines (from stdin) as a dict. Anything else is refused with ValueError;
    the message names the line number only, never the text (it may be a secret)."""
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    out = {}
    for n, line in enumerate(lines, 1):
        m = ENV_LINE.fullmatch(line)
        if not m:
            raise ValueError(f"bad --env-stdin line {n}")
        out[m.group(1)] = m.group(2)
    if not out:
        raise ValueError("bad --env-stdin line: nothing on stdin")
    return out


def redeem(url: str, name: str) -> Optional[Tuple[str, str, str, str]]:
    """Use an invite link: (hub, token, secret, person), or None after printing why it
    failed."""
    m = re.fullmatch(r"(https?://[^/\s]+)/i/([\w-]+)/?", url.strip())
    if not m:
        print("that is not an invite link (expected http://<hub>/i/<code>)", file=sys.stderr)
        return None
    base, code = m.groups()
    try:
        host = socket.gethostname().split(".")[0]
        try:
            os_user = getpass.getuser()
        except Exception:
            os_user = ""
        status, body = http.request("POST", f"{base}/i/{code}", "", {
            "device_name": name, "host": host, "os_user": os_user})
    except http.HubUnreachable:
        print(http.unreachable(base), file=sys.stderr)
        return None
    if status != 200 or not isinstance(body, dict):
        why = {"no such invite": "it was cancelled, or it ran out (links last 7 days)",
               "invite already used": "it was already used (a link works once)"}
        said = body if isinstance(body, str) else f"the hub answered {status}"
        print(f"this invite link did not work: {why.get(said, said)}; ask your admin for a new one",
              file=sys.stderr)
        return None
    return body.get("hub") or base, body["token"], body["secret"], str(body.get("person") or "")


def main(hub: str, token: str, name: str, adapters: Optional[list] = None,
         install=service.install, only: Optional[List[str]] = None,
         secret: Optional[str] = None, person: Optional[str] = None, env: Optional[dict] = None) -> int:
    hub = hub.strip()
    hub = (hub if "://" in hub else "http://" + hub).rstrip("/")
    token = token.strip().strip("'\"")
    if adapters is None:
        from kaptyorka.adapters import all_adapters
        adapters = all_adapters(print)
    valid = sorted(a.tool for a in adapters)
    for t in only or []:
        if t not in valid:
            print(f"unknown tool {t}; valid: {', '.join(valid)}", file=sys.stderr)
            return 2
    try:
        status, _ = http.request("GET", hub + "/api/feed", token)
    except http.HubUnreachable:
        print(http.unreachable(hub), file=sys.stderr)
        return 1
    if status == 401:
        print("the hub does not accept that token; check it with your admin", file=sys.stderr)
        return 1
    if status != 200:
        print(f"the hub at {hub} answered {status}, which is not what a kaptyorka hub says; check the address", file=sys.stderr)
        return 1
    exe = Path(sys.argv[0])
    # pipx's stable link survives a venv rebuild; any other path is resolved to the real file
    exe = str(exe if exe == Path.home() / ".local/bin/kaptyorka" else exe.resolve())
    done = [a for a in adapters if a.installed() and (not only or a.tool in only)]
    cfg = {"hub": hub, "token": token, "machine": name}
    saved = paths.load_config()
    # keep the signing secret saved for this same token (a retry after a failed join)
    secret = secret or (saved.get("secret") if saved.get("token") == token else None)
    if secret:
        cfg["secret"] = secret
    # the person this device belongs to, from the invite: kept like the secret
    person = person or (saved.get("person") if saved.get("token") == token else None)
    if person:
        cfg["person"] = person
    if only:
        # the bridge drives only these; a later --only adds to the earlier ones
        cfg["tools"] = sorted(set(only) | set(paths.load_config().get("tools", [])))
    paths.save_config(cfg)
    rc = 0
    ok = []
    for a in done:
        try:
            lines = a.configure(exe)
        except Exception as e:
            print(f"{a.tool}: failed: {e}", file=sys.stderr)
            rc = 1
            continue
        ok.append(a)
        for line in lines:
            print(f"{a.tool}: {line}")
    if not install_bridge(exe, install, env):
        rc = 1
    if not done:
        print(", ".join(f"{t} is not installed" for t in only) if only
              else "no supported agent tool found", file=sys.stderr)
        rc = 1
    notes = [a.restart_note for a in ok if a.restart_note]
    for n in notes:
        print(f"restart: {n}")
    if not notes and ok:
        print("done, nothing to restart")
    print(f"page: {hub}/#t={token}")
    return rc
