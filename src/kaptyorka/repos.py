"""A session's project from its git repository, besides its folder's name.

A folder whose git remote is a registered project's repo counts as that project, whatever the
folder is called (a worktree or branch copy such as proj-a-fix-login). Matched on this machine
only: the hub sends a fingerprint of each registered repo (sha256 of its normalized URL), never
asks for local remotes, and a folder that matches none stays personal exactly as before.

The remotes are read from the git config file directly (no git process): `.git` is a folder,
or a file `gitdir: <path>` for a linked worktree (its `commondir` names the main repository)
and for a submodule (its own config under .git/modules). Parsed configs are cached per file
and read again only when the file's mtime changes.
"""
import hashlib
import os
import re
from typing import Dict, List, Optional, Set, Tuple
from urllib.parse import urlsplit

_SCP = re.compile(r"^(?:[^@/]+@)?([^:/]+):(?!//)(.+)$")  # git@host:group/repo.git
_cache: Dict[str, Tuple[float, List[str]]] = {}  # config path -> (mtime, remote urls)


def normalize(url: str) -> str:
    """host/path in lower case, with no scheme, user, port, trailing .git or slashes:
    http://gitserver:8000/group/proj-a.git, ssh://git@gitserver:2222/group/proj-a and
    git@gitserver:group/proj-a.git all give gitserver/group/proj-a. "" for no URL."""
    url = (url or "").strip()
    if not url:
        return ""
    if "://" in url:
        try:
            parts = urlsplit(url)
            host, path = parts.hostname or "", parts.path
        except ValueError:
            return ""
    else:
        m = _SCP.match(url)
        if not m:
            return ""  # a local path: no team repo
        host, path = m.group(1), m.group(2)
    path = path.strip("/")
    if path.lower().endswith(".git"):
        path = path[:-4].rstrip("/")
    if not host or not path:
        return ""
    return f"{host}/{path}".lower()


def fingerprint(url: str) -> str:
    """What the hub sends for a registered repo; "" for none."""
    n = normalize(url)
    return hashlib.sha256(n.encode()).hexdigest() if n else ""


def _git_dir(cwd: str) -> Optional[str]:
    """The git dir of the work tree cwd lies in (followed through a `.git` file), or None."""
    d = os.path.abspath(cwd)
    while True:
        dot = os.path.join(d, ".git")
        if os.path.isdir(dot):
            return dot
        if os.path.isfile(dot):
            try:
                with open(dot) as f:
                    line = f.readline().strip()
            except OSError:
                return None
            if not line.startswith("gitdir:"):
                return None
            target = line[len("gitdir:"):].strip()
            return os.path.normpath(os.path.join(d, target))
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent


def _config_path(git_dir: str) -> str:
    """A linked worktree's config is its main repository's (named in `commondir`)."""
    try:
        with open(os.path.join(git_dir, "commondir")) as f:
            common = f.read().strip()
        if common:
            git_dir = os.path.normpath(os.path.join(git_dir, common))
    except OSError:
        pass
    return os.path.join(git_dir, "config")


def _parse(text: str) -> List[str]:
    """The url of every [remote "..."] section."""
    urls, in_remote = [], False
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line[0] in "#;":
            continue
        if line.startswith("["):
            in_remote = line.lower().startswith("[remote ")
            continue
        if in_remote:
            key, sep, value = line.partition("=")
            if sep and key.strip().lower() == "url":
                value = value.strip()
                if len(value) >= 2 and value[0] == value[-1] == '"':
                    value = value[1:-1]
                urls.append(value)
    return urls


def remotes(cwd: str) -> List[str]:
    """The remote URLs of the repository cwd lies in; [] when none (or not a repository)."""
    if not cwd:
        return []
    git_dir = _git_dir(cwd)
    if git_dir is None:
        return []
    path = _config_path(git_dir)
    try:
        mtime = os.stat(path).st_mtime
    except OSError:
        return []
    hit = _cache.get(path)
    if hit and hit[0] == mtime:
        return hit[1]
    try:
        with open(path, errors="replace") as f:
            urls = _parse(f.read())
    except OSError:
        return []
    _cache[path] = (mtime, urls)
    return urls


def match(cwd: str, repos: Dict[str, str]) -> Optional[str]:
    """The registered project whose repo is one of cwd's remotes; None when none is.
    repos: {fingerprint: project}, as the hub sent it."""
    if not repos or not cwd:
        return None
    found: Set[str] = {repos[f] for f in (fingerprint(u) for u in remotes(cwd)) if f in repos}
    return found.pop() if len(found) == 1 else None  # two projects: not sure, so neither


def resolve(project: str, cwd: str, team: Optional[Set[str]], repos: Dict[str, str]) -> str:
    """A session's project: its folder's name when that is a team project (as today, so a
    folder that reached the hub keeps its project), else the registered project its git remote
    names, else the folder's name (a personal folder stays personal)."""
    from kaptyorka import paths

    if team is None or paths.team_name(project) in team:
        return project
    found = match(cwd, repos)
    return found if found is not None and found in team else project
