"""`kaptyorka doctor`: check this machine's setup, one line per check; --fix repairs the mechanical ones."""
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional

from kaptyorka import http, paths, service
from kaptyorka.adapters.base import is_our_hook

BRIDGE = "kaptyorka-bridge"
HUB = "kaptyorka-hub"


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""
    fix: Optional[str] = None


def _json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _has_hooks(hooks: object, tool: str, events: tuple, nested: bool) -> bool:
    """Every (hook event, our command name) in events has one of our commands in hooks."""
    if not isinstance(hooks, dict):
        return False
    for event, name in events:
        entries = hooks.get(event)
        if not isinstance(entries, list):
            return False
        found = False
        for e in entries:
            if not isinstance(e, dict):
                continue
            cmds = [h.get("command", "") for h in e.get("hooks", []) if isinstance(h, dict)] \
                if nested and isinstance(e.get("hooks"), list) else [] if nested else [e.get("command", "")]
            found = found or any(is_our_hook(c, tool, (name,)) for c in cmds)
        if not found:
            return False
    return True


def _mcp_entry(servers: object) -> bool:
    return isinstance(servers, dict) and isinstance(servers.get("kaptyorka"), dict)


def tool_configured(adapter, home: Path) -> bool:
    """Read-only mirror of `configure`: our MCP entry and, where the tool has them, our hooks."""
    tool = adapter.tool
    if tool == "claude-code":
        hooks = _json(home / ".claude" / "settings.json").get("hooks")
        return (_mcp_entry(_json(home / ".claude.json").get("mcpServers"))
                and _has_hooks(hooks, tool, (("SessionStart", "start"), ("UserPromptSubmit", "prompt")), True))
    if tool == "opencode":
        conf = home / ".config" / "opencode"
        return (_mcp_entry(_json(conf / "opencode.json").get("mcp"))
                and (conf / "plugins" / "kaptyorka.js").exists())
    if tool == "codex":
        from kaptyorka.adapters.codex import EVENTS, codex_home

        conf = codex_home(home)
        try:
            toml = (conf / "config.toml").read_text()
        except OSError:
            return False
        return ("[mcp_servers.kaptyorka]" in toml
                and _has_hooks(_json(conf / "hooks.json").get("hooks"), tool, EVENTS, True))
    if tool == "cursor":
        return (_mcp_entry(_json(home / ".cursor" / "mcp.json").get("mcpServers"))
                and _has_hooks(_json(home / ".cursor" / "hooks.json").get("hooks"), tool,
                               (("stop", "stop"), ("postToolUse", "tool")), False))
    return False


def _tmux(run: Callable, platform: str) -> Check:
    install = "brew install tmux" if platform == "darwin" else "sudo apt install -y tmux"
    try:
        ok = run(["tmux", "-V"], capture_output=True, text=True).returncode == 0
    except OSError:
        ok = False
    return Check("tmux", ok, "" if ok else "not installed", None if ok else install)


def _service(name: str, label: str, run: Callable, platform: str) -> Check:
    active = service.is_active(name, run=run, platform=platform)
    if active:
        return Check(label, True)
    if platform == "darwin":
        restart = f"launchctl kickstart -k gui/{os.getuid()}/io.kaptyorka.{name}"
    else:
        restart = f"systemctl --user restart {name}"
    if active is None:
        return Check(label, False, "not installed", "kaptyorka join" if name == BRIDGE else "kaptyorka start")
    return Check(label, False, "stopped", restart)


def checks(cfg: dict, adapters: list, home: Path, run: Callable = subprocess.run,
           platform: str = sys.platform) -> List[Check]:
    out: List[Check] = []
    joined = bool(cfg.get("hub") and cfg.get("token"))
    out.append(Check("config", joined, "" if joined else "not joined", None if joined else "kaptyorka join"))
    reach = Check("hub reachable", False, "not joined", "kaptyorka join")
    token = Check("token valid", False, "not joined", "kaptyorka join")
    if joined:
        try:
            status, _ = http.request("GET", cfg["hub"] + "/api/me", cfg["token"], timeout=5)
        except http.HubUnreachable:
            reach = Check("hub reachable", False, f"no answer from {cfg['hub']}", "kaptyorka join")
            token = Check("token valid", False, "hub unreachable", "kaptyorka join")
        else:
            reach = Check("hub reachable", True)
            token = Check("token valid", True) if status == 200 else \
                Check("token valid", False, f"hub answered {status}", "kaptyorka join")
    out += [reach, token, _service(BRIDGE, "bridge service", run, platform)]
    if (paths.home() / "hub.json").exists():
        out.append(_service(HUB, "hub service", run, platform))
    out.append(_tmux(run, platform))
    for a in adapters:
        if a.installed():
            ok = tool_configured(a, home)
            out.append(Check(f"tool: {a.tool}", ok, "" if ok else "not wired to kaptyorka",
                             None if ok else "kaptyorka join"))
    return out


def _show(results: List[Check]) -> None:
    for c in results:
        if c.ok:
            print(f"ok   {c.name}")
        else:
            print(f"FAIL {c.name}: {c.detail}" + (f" -> {c.fix}" if c.fix else ""))


def run(cfg: dict, fix: bool = False, adapters: Optional[list] = None, home: Optional[Path] = None,
        run: Callable = subprocess.run, platform: str = sys.platform, exe: Optional[str] = None) -> int:
    if adapters is None:
        from kaptyorka.adapters import all_adapters
        adapters = all_adapters(print)
    home = home or Path.home()
    results = checks(cfg, adapters, home, run, platform)
    if fix and not all(c.ok for c in results):
        exe = exe or str(Path(sys.argv[0]).resolve())
        failing = {c.name: c for c in results if not c.ok}
        for a in adapters:
            if f"tool: {a.tool}" in failing:
                for line in a.configure(exe):
                    print(line)
        if "bridge service" in failing and failing["bridge service"].detail == "stopped":
            try:
                for line in service.restart(BRIDGE, run=run, platform=platform):
                    print(line)
            except service.ServiceError as e:
                print(f"{BRIDGE}: {e}", file=sys.stderr)
        results = checks(cfg, adapters, home, run, platform)
    _show(results)
    return 0 if all(c.ok for c in results) else 1
