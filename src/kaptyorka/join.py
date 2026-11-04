import os
import sys
from pathlib import Path
from typing import List, Optional

from kaptyorka import http, paths, service


def main(hub: str, token: str, name: str, adapters: Optional[list] = None,
         install=service.install, only: Optional[List[str]] = None) -> int:
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
    if only:
        cfg["tools"] = sorted(set(only))  # the bridge drives only these
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
    if os.environ.get("KAPTYORKA_SKIP_SERVICE") != "1":
        try:
            for line in install("kaptyorka-bridge", [exe, "bridge"]):
                print(f"bridge: {line}")
        except service.ServiceError as e:
            print(f"bridge service failed: {e}", file=sys.stderr)
            rc = 1
    if not done:
        print(", ".join(f"{t} is not installed" for t in only) if only
              else "no supported agent tool found", file=sys.stderr)
        return 1
    notes = [a.restart_note for a in ok if a.restart_note]
    for n in notes:
        print(f"restart: {n}")
    if not notes and ok:
        print("done, nothing to restart")
    return rc
