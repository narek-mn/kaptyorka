"""Start and stop worker sessions for signed orders: folder, worktree, command line, detach."""
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable, Iterable, List, Optional, Tuple

from kaptyorka import facts, paths

NAME = re.compile(r"^[A-Za-z0-9._-]{1,80}$")
# what tmux and claude say when the session to stop is not there
GONE = ("can't find session", "no server running", "can't find pane", "No job matching")
NO_SERVER = ("error connecting to", "No such file or directory")  # newer tmux, no server
CLAUDE_WAIT_S = 60  # `claude --bg` returns once the session runs
TMUX_WAIT_S = 10  # `tmux new-session -d` returns at once
# `claude --bg` ignores --session-id and prints the short id that `claude stop` takes (never
# the name): the first 8 hex digits of the session id the session file shows
BACKGROUNDED = re.compile(r"backgrounded · ([0-9a-f]{8})\b")


class LaunchError(Exception):
    """A worker could not be started or stopped; the text goes to the hub."""


def _plain(value: str, what: str) -> str:
    """A value that goes on a command line on its own: never one an option parser would take."""
    if not isinstance(value, str) or not value or value.startswith("-"):
        raise LaunchError(f"bad {what}")
    return value


class Launcher:
    def __init__(self, cfg: dict, home: Path, run=subprocess.run, popen=subprocess.Popen,
                 platform: str = sys.platform, log: Callable[[str], None] = print):
        self.cfg, self.home, self.run, self.popen = cfg, Path(home), run, popen
        self.platform, self.log = platform, log

    def _name(self, order: dict) -> str:
        name = order.get("worker")
        if not isinstance(name, str) or not NAME.match(name) or name.startswith("-"):
            raise LaunchError("bad worker name")
        return name

    @staticmethod
    def occupied(folder: str, sessions: Iterable) -> bool:
        """A live session of this account already works in the folder."""
        folder = folder.rstrip("/")
        return any((getattr(s, "cwd", "") or "").rstrip("/") == folder for s in sessions)

    def folder_for(self, order: dict, sessions: Iterable = ()) -> Tuple[str, bool]:
        if order.get("manager") is True:  # only the manager's own folder; never a worktree
            folder = order.get("folder")
            want = paths.home() / paths.MANAGER_DIR
            if not isinstance(folder, str) or not os.path.isabs(folder) \
                    or not os.path.isdir(folder) or Path(folder).resolve() != want.resolve():
                raise LaunchError(f"no manager folder {folder!r} on this machine"
                                  f" (it must be {want})")
            return folder.rstrip("/") or folder, False
        project = str(order.get("project") or "")
        learned = facts._learned().get(project)
        tried: List[str] = []
        for p in (order.get("folder"), learned, str(self.home / project) if project else None):
            if p and isinstance(p, str) and p not in tried:
                tried.append(p)
        folder = next((p for p in tried if os.path.isdir(p)), None)
        if folder is None:
            raise LaunchError(f"no folder for {project} on this machine"
                              f" (looked in {', '.join(tried)})")
        folder = folder.rstrip("/") or folder
        if not order.get("worktree") and not self.occupied(folder, sessions):
            return folder, False
        name = self._name(order)
        n = 1
        while os.path.exists(f"{folder}--{n}"):
            n += 1
        path = f"{folder}--{n}"
        try:
            r = self.run(["git", "-C", folder, "worktree", "add", "-b", f"work/{name}", path],
                         capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise LaunchError(f"git worktree add failed: {e}")
        if r.returncode != 0:
            raise LaunchError((r.stderr or r.stdout or "").strip()
                              or f"git worktree add failed in {folder}")
        return path, True

    def command(self, order: dict, folder: str) -> List[str]:
        """The tool's argv; the first message is one element, never a shell string.
        Never any permission flag: the worker asks like any session."""
        name, tool = self._name(order), order.get("tool")
        first = _plain(order.get("first_message"), "first message")
        resume = order.get("resume")
        if resume is not None:
            resume = _plain(resume, "resume id")
        if tool == "claude-code":
            return ["claude", "--bg", "-n", name, *(["--resume", resume] if resume else []),
                    first]
        tmux = ["tmux", "new-session", "-d", "-s", name, "-c", folder]
        if tool == "codex":
            return [*tmux, "codex", *(["resume", resume] if resume else []), first]
        if tool == "opencode":
            return [*tmux, "opencode", *(["--session", resume] if resume else []),
                    "--prompt", first]
        raise LaunchError(f"no worker support for {tool}")

    def detach(self, argv: List[str], folder: str, name: str) -> str:
        """Start argv outside the bridge's process group and service, output to its log.
        Returns what it printed."""
        logs = paths.home() / "workers"
        logs.mkdir(mode=0o700, parents=True, exist_ok=True)
        wait = CLAUDE_WAIT_S if argv[0] == "claude" else TMUX_WAIT_S
        kw = {}
        if self.platform.startswith("linux") and shutil.which("systemd-run"):
            argv = ["systemd-run", "--user", "--scope", "--quiet", "--unit",
                    f"kaptyorka-worker-{name}", *argv]
        else:
            kw["start_new_session"] = True
        log_path = logs / f"{name}.log"
        with open(log_path, "ab") as out:
            start = out.tell()
            try:
                proc = self.popen(argv, cwd=folder, env=os.environ, stdin=subprocess.DEVNULL,
                                  stdout=out, stderr=subprocess.STDOUT, **kw)
            except OSError as e:
                raise LaunchError(f"could not start {argv[0]}: {e}")
            try:
                code = proc.wait(timeout=wait)
            except subprocess.TimeoutExpired:
                proc.kill()
                raise LaunchError(f"{argv[0]} did not return within {wait} s;"
                                  f" see {log_path}")
        with open(log_path, "rb") as f:
            f.seek(start)
            output = f.read().decode(errors="replace")
        if "Workspace not trusted" in output:
            raise LaunchError(f"Claude Code does not trust {folder}; open claude there once"
                              " and accept")
        if code != 0:
            raise LaunchError(output.strip() or f"{argv[0]} exited with {code}")
        return output

    def launch(self, order: dict, sessions: Iterable = ()) -> dict:
        name = self._name(order)
        folder, worktree = self.folder_for(order, sessions)
        argv = self.command(order, folder)
        output = self.detach(argv, folder, name)
        job = None
        if order.get("tool") == "claude-code":
            m = BACKGROUNDED.search(output)
            if m is None:
                raise LaunchError("claude --bg printed no session id; see"
                                  f" ~/.kaptyorka/workers/{name}.log")
            job = m.group(1)
        return {"key": None, "job": job, "folder": folder, "worktree": worktree,
                "needs_brief": False}

    @staticmethod
    def _job(order: dict) -> str:
        """The short id `claude stop` takes: from the launch, else from the linked key."""
        job = order.get("job")
        if not job:
            key = str(order.get("key") or "")
            job = key.split(":", 1)[1][:8] if key.startswith("claude:") else ""
        if not re.fullmatch(r"[0-9a-f]{8}", job):
            raise LaunchError("no Claude Code session id to stop")
        return job

    def close(self, order: dict) -> None:
        name = self._name(order)
        argv = (["claude", "stop", self._job(order)] if order.get("tool") == "claude-code"
                else ["tmux", "kill-session", "-t", f"={name}"])  # `=`: this name, not a prefix
        try:
            r = self.run(argv, capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise LaunchError(f"{argv[0]} failed: {e}")
        out = ((r.stderr or "") + (r.stdout or "")).strip()
        if r.returncode != 0 and (any(g in out for g in GONE)
                                  or all(g in out for g in NO_SERVER)):
            self.log(f"worker {name} was already gone: {out}")
            return  # closing a session that is not there is done
        if r.returncode != 0:
            raise LaunchError(out or f"{' '.join(argv)} exited with {r.returncode}")

    def find_key(self, order: dict, sessions: Iterable) -> Optional[str]:
        if order.get("tool") == "claude-code":
            key, job = order.get("key"), order.get("job")
            for s in sessions:
                if (key and s.key == key) or (job and s.key.startswith(f"claude:{job}")):
                    return s.key
            return None
        folder = str(order.get("folder") or "").rstrip("/")
        for s in sessions:
            if s.tool == order.get("tool") and (s.cwd or "").rstrip("/") == folder:
                return s.key
        return None
