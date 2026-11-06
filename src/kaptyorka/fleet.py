import re
import shlex
import subprocess
from pathlib import Path
from typing import Callable, List, Optional

PATH_FIX = 'export PATH="$HOME/.local/bin:$PATH"; '
TOOL_NAMES = {"claude": "claude-code", "codex": "codex", "opencode": "opencode"}


def read_key(key_file: Path) -> str:
    """The key file's one NAME=value line, with a newline; ValueError (no secret in it) otherwise."""
    lines = Path(key_file).read_text().splitlines()
    if len(lines) != 1 or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.+", lines[0]):
        raise ValueError(f"{key_file}: expected one NAME=value line")
    return lines[0] + "\n"


def _ssh(target: str, command: str) -> List[str]:
    # the remote command is a shell line by necessity: every value in it is shlex-quoted
    return ["ssh", "-o", "BatchMode=yes", "--", target, command]


def _first_line(text: str) -> str:
    lines = (text or "").strip().splitlines()
    return lines[0].strip() if lines else "no error text"


def add(targets: List[str], person: str, key_file: Optional[Path], hub_url: str,
        invite: Callable[[str], str], run=subprocess.run) -> List[str]:
    """Set up kaptyorka on each target over ssh; one result line per target.
    The API key (one NAME=value line) goes only on the stdin of the join step."""
    key_line = read_key(key_file) if key_file else None  # before any ssh
    lines = []
    for target in targets:
        name = target
        if target.startswith("-") or re.search(r"\s", target):
            lines.append(f"{target}: not a user@host")
            continue
        try:
            url = invite(person)
        except RuntimeError as e:
            e.lines = lines
            raise
        join_cmd = f"kaptyorka join {shlex.quote(url)} --name {shlex.quote(name)}"
        if key_line:
            join_cmd += " --env-stdin"
        steps = [
            ("pipx", "pipx --version || (python3 -m pip install --user pipx && python3 -m pipx ensurepath)", None),
            ("install", PATH_FIX + "pipx install kaptyorka || pipx upgrade kaptyorka", None),
            ("join", PATH_FIX + join_cmd, key_line),
        ]
        by_hand = (f"cat {shlex.quote(str(key_file))} | ssh -- {shlex.quote(target)} "
                   f"{shlex.quote(PATH_FIX + join_cmd)}") if key_line else None
        failed = None
        for step, command, stdin in steps:
            kw = {"input": stdin} if stdin else {}
            r = run(_ssh(target, command), capture_output=True, text=True, **kw)
            if r.returncode != 0:
                failed = f"{target}: failed at {step}: {_first_line(r.stderr)}; run by hand: {by_hand if step == 'join' and by_hand else command}"
                break
        if failed:
            lines.append(failed)
            continue
        r = run(_ssh(target, PATH_FIX + "which claude opencode codex"), capture_output=True, text=True)
        found = [TOOL_NAMES[Path(p.strip()).name] for p in (r.stdout or "").splitlines()
                 if Path(p.strip()).name in TOOL_NAMES]
        lines.append(f"{target}: joined as {name}; tools: {', '.join(sorted(found)) or 'none'}")
    return lines


def list(machines: List[dict]) -> List[str]:  # noqa: A001 (the brief names it fleet.list)
    out = []
    for m in machines:
        for d in m.get("devices", []):
            state = "online" if d.get("online") else "offline"
            out.append(f"{m['host']} {d.get('os_user') or '':<12} {d.get('person') or '':<10} "
                       f"{state} {d.get('version') or ''}")
    return out
