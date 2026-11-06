import getpass
import os
import re
import socket
import sys
from pathlib import Path
from typing import List, Optional, Tuple

from kaptyorka import http, paths, service


def install_bridge(exe: str, install=None) -> bool:
    """Install and start the bridge service; False (after printing why) if that failed."""
    if os.environ.get("KAPTYORKA_SKIP_SERVICE") == "1":
        return True
    try:
        for line in (install or service.install)("kaptyorka-bridge", [exe, "bridge"],
                                                 env={"PATH": os.environ.get("PATH", "")}):
            print(f"bridge: {line}")
    except service.ServiceError as e:
        print(f"bridge service failed: {e}", file=sys.stderr)
        return False
    return True


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
        print(f"kaptyorka hub unreachable at {base}", file=sys.stderr)
        return None
    if status != 200 or not isinstance(body, dict):
        print(f"invite failed: {body if isinstance(body, str) else status}", file=sys.stderr)
        return None
    return body.get("hub") or base, body["token"], body["secret"], str(body.get("person") or "")


def main(hub: str, token: str, name: str, adapters: Optional[list] = None,
         install=service.install, only: Optional[List[str]] = None,
         secret: Optional[str] = None, person: Optional[str] = None) -> int:
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
        print(f"kaptyorka hub unreachable at {hub}", file=sys.stderr)
        return 1
    if status == 401:
        print("wrong token", file=sys.stderr)
        return 1
    if status != 200:
        print(f"kaptyorka hub at {hub} answered {status}", file=sys.stderr)
        return 1
    exe = str(Path(sys.argv[0]).resolve())
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
    if not install_bridge(exe, install):
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
