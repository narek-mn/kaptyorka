"""Team skills: written to ~/.claude/skills/team-<name>/SKILL.md, removed when the hub drops them."""
import json
import re
import shutil
from pathlib import Path
from typing import Callable, List

from kaptyorka import paths
from kaptyorka.adapters.base import write_atomic

MARKER = "<!-- kaptyorka: team skill, replaced on change; edit it in the team's rules folder -->"
PREFIX = "team-"
STATE = "skills.json"
DESC_MAX = 200
SAFE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")


def _split(text: str):
    """(frontmatter lines, body) of a text that may start with a --- block."""
    lines = text.lstrip("﻿").split("\n")
    if lines and lines[0].strip() == "---":
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                return lines[1:i], "\n".join(lines[i + 1:]).lstrip("\n")
    return [], text


def render(name: str, text: str) -> str:
    front, body = _split(text)
    desc = ""
    for line in front:
        m = re.match(r"description:\s*(.*)", line)
        if m:
            desc = m.group(1).strip().strip("\"'")
            break
    if not desc:
        desc = next((x.strip().lstrip("# ").strip() for x in body.split("\n")
                     if x.strip().lstrip("# ").strip()), name)
    desc = " ".join(desc.split())[:DESC_MAX]
    return f"---\nname: {json.dumps(PREFIX + name)}\ndescription: {json.dumps(desc)}\n---\n{MARKER}\n\n{body}"


def _ours(folder: Path) -> bool:
    try:
        return MARKER in (folder / "SKILL.md").read_text()
    except (OSError, ValueError):
        return False


def _remove(folder: Path, log: Callable) -> List[str]:
    if folder.is_symlink() or (folder / "SKILL.md").is_symlink():
        log(f"left alone, symlink: {folder}")
        return []
    if not _ours(folder):
        log(f"left alone, not ours: {folder}")
        return []
    shutil.rmtree(folder)
    return [f"removed team skill {folder}"]


def _root(home: Path) -> Path:
    """The skills folder with a symlinked ~/.claude or ~/.claude/skills followed once."""
    return (home / ".claude" / "skills").resolve()


def _folders(home: Path) -> List[Path]:
    root = _root(home)
    return sorted(p for p in root.glob(PREFIX + "*") if p.is_dir() or p.is_symlink()) if root.is_dir() else []


def sync(home: Path, version: str, items: List[dict], log: Callable) -> List[str]:
    root = _root(home)
    lines: List[str] = []
    names = []
    for it in items:
        name = it.get("name") if isinstance(it, dict) else None
        if not isinstance(name, str) or not SAFE_NAME.match(name):
            log(f"skipped skill with unsafe name: {name!r}")
            continue
        names.append(name)
        folder = root / (PREFIX + name)
        target = folder / "SKILL.md"
        if folder.is_symlink() or target.is_symlink():
            log(f"left alone, symlink: {folder}")
            continue
        if folder.exists() and not _ours(folder):
            log(f"left alone, not ours: {folder}")
            continue
        text = render(name, str(it.get("text", "")))
        try:
            if target.read_text() == text:
                continue
        except (OSError, ValueError):
            pass
        write_atomic(target, text)
        lines.append(f"wrote team skill {target}")
    keep = {PREFIX + n for n in names}
    for folder in _folders(home):
        if folder.name not in keep:
            lines += _remove(folder, log)
    write_atomic(paths.home() / STATE, json.dumps({"version": version, "names": names}))
    return lines


def applied_version(home_kaptyorka: Path) -> str:
    try:
        v = json.loads((home_kaptyorka / STATE).read_text()).get("version")
    except (OSError, ValueError, AttributeError):
        return ""
    return v if isinstance(v, str) else ""


def remove_all(home: Path, log: Callable) -> List[str]:
    lines: List[str] = []
    for folder in _folders(home):
        lines += _remove(folder, log)
    state = paths.home() / STATE
    if state.exists():
        state.unlink()
    return lines
