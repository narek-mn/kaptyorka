import os
import sys
from pathlib import Path
from typing import List, Optional

from kaptyorka import http, paths, service


def main(hub: str, token: str, name: str, adapters: Optional[list] = None,
         install=service.install, only: Optional[List[str]] = None) -> int:
    hub = hub.rstrip("/")
    try:
        status, _ = http.request("GET", hub + "/api/feed", token)
    except http.HubUnreachable:
        print(f"kaptyorka hub unreachable at {hub}")
        return 1
    if status == 401:
        print("wrong token")
        return 1
    if status != 200:
        print(f"kaptyorka hub at {hub} answered {status}")
        return 1
    paths.save_config({"hub": hub, "token": token, "machine": name})
    if adapters is None:
        from kaptyorka.adapters import all_adapters
        adapters = all_adapters(print)
    exe = str(Path(sys.argv[0]).resolve())
    done = [a for a in adapters if a.installed() and (not only or a.tool in only)]
    for a in done:
        for line in a.configure(exe):
            print(f"{a.tool}: {line}")
    rc = 0
    if os.environ.get("KAPTYORKA_SKIP_SERVICE") != "1":
        try:
            for line in install("kaptyorka-bridge", [exe, "bridge"]):
                print(line)
        except service.ServiceError as e:
            print(f"bridge service failed: {e}", file=sys.stderr)
            rc = 1
    notes = [a.restart_note for a in done if a.restart_note]
    for n in notes:
        print(f"restart: {n}")
    if not notes:
        print("done, nothing to restart")
    return rc
