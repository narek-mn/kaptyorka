"""`kaptyorka hook <tool> <event>`: print the hub's brief for this session. Silent on any failure."""
import json
from typing import Callable, Dict

from kaptyorka import http, paths
from kaptyorka.identity import identify

TIMEOUT_S = 3

# (event, brief) -> what the tool expects on stdout
FORMATTERS: Dict[str, Callable[[str, str], str]] = {
    "claude-code": lambda event, brief: brief,
    "opencode": lambda event, brief: brief,  # the plugin pushes it as a system line
}


def main(tool: str, event: str, stdin: str = "") -> int:
    try:
        cfg = paths.load_config()
        fmt = FORMATTERS.get(tool)
        if not fmt or not cfg.get("hub") or not cfg.get("token"):
            return 0
        workspace = None
        if tool == "cursor":
            workspace = (json.loads(stdin or "{}").get("workspace_roots") or [None])[0]
        ident = identify(cfg.get("machine", ""), workspace=workspace)
        if ident is None:
            return 0
        status, brief = http.request("GET", f"{cfg['hub']}/api/brief?event={event}", cfg["token"],
                                     headers=ident.headers(), timeout=TIMEOUT_S)
        if status == 200 and isinstance(brief, str) and brief:
            print(fmt(event, brief))
    except Exception:
        pass  # a hook must never disturb the tool
    return 0
