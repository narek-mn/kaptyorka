import json
import subprocess
from types import SimpleNamespace

from kaptyorka import facts, paths
from kaptyorka.adapters.base import Session


class Run:
    def __init__(self, answers):
        self.answers, self.calls = answers, []

    def __call__(self, cmd, **kw):
        self.calls.append((cmd, kw))
        for key, out in self.answers.items():
            if key in " ".join(cmd):
                if isinstance(out, Exception):
                    raise out
                return SimpleNamespace(returncode=out[0], stdout=out[1])
        return SimpleNamespace(returncode=1, stdout="")


GIT = {"rev-parse": (0, "main\n"), "status": (0, " M a.py\n"), "log": (0, "fix thing\n")}


def setup_function():
    facts._cache.clear()


def test_git_reads_branch_dirty_subject_with_timeout():
    run = Run(GIT)
    assert facts.git("/w/p", run) == {"branch": "main", "dirty": True, "subject": "fix thing"}
    assert all(c[0][:3] == ["git", "-C", "/w/p"] and c[1]["timeout"] == 2 for c in run.calls)


def test_git_clean_repo():
    run = Run({**GIT, "status": (0, "")})
    assert facts.git("/w/p", run)["dirty"] is False


def test_git_not_a_repo_or_timeout_is_none():
    assert facts.git("/w/a", Run({"rev-parse": (128, "")})) is None
    assert facts.git("/w/b", Run({"rev-parse": subprocess.TimeoutExpired("git", 2)})) is None


def test_git_cached_for_30_seconds(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(facts.time, "monotonic", lambda: now[0])
    run = Run(GIT)
    facts.git("/w/p", run)
    n = len(run.calls)
    now[0] += 29
    facts.git("/w/p", run)
    assert len(run.calls) == n
    now[0] += 2
    facts.git("/w/p", run)
    assert len(run.calls) > n


def adapter(tool, installed=True):
    return SimpleNamespace(tool=tool, installed=lambda: installed)


def test_machine_facts(monkeypatch):
    import kaptyorka

    monkeypatch.setattr(facts.shutil, "which", lambda n: f"/bin/{n}" if n != "opencode" else None)
    monkeypatch.setattr(facts.getpass, "getuser", lambda: "alice")
    monkeypatch.setattr(facts.os, "getloadavg", lambda: (1.5, 1, 1))
    monkeypatch.setattr(facts.os, "cpu_count", lambda: None)
    run = Run({"--version": (0, "2.1.3 (Claude Code)\nmore\n")})
    m = facts.machine([adapter("claude-code"), adapter("opencode")], run)
    assert m["os_user"] == "alice" and m["tmux"] is True and m["load"] == 1.5
    assert m["cpus"] == 1 and m["version"] == kaptyorka.__version__
    assert m["tools"] == {"claude-code": "2.1.3 (Claude Code)", "opencode": ""}
    assert m["hooks_ok"] == {"claude-code": True, "opencode": True}
    assert run.calls[0][0] == ["/bin/claude", "--version"] and run.calls[0][1]["timeout"] == 5


def test_load_zero_on_error_and_version_cached(monkeypatch):
    def boom():
        raise OSError

    now = [0.0]
    monkeypatch.setattr(facts.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(facts.os, "getloadavg", boom)
    monkeypatch.setattr(facts.shutil, "which", lambda n: "/bin/" + n)
    run = Run({"--version": (0, "1.0\n")})
    assert facts.machine([adapter("codex")], run)["load"] == 0.0
    now[0] = 599
    facts.machine([adapter("codex")], run)
    assert len(run.calls) == 1
    now[0] = 601
    facts.machine([adapter("codex")], run)
    assert len(run.calls) == 2


def test_folders_learn_and_remember(home):
    (home / "proj-a").mkdir()
    s = [Session("claude:1", "claude-code", "proj-b", cwd="/w/proj-b")]
    out = facts.folders(["proj-a", "proj-b", "proj-c"], s, home)
    assert out == {"proj-a": str(home / "proj-a"), "proj-b": "/w/proj-b"}
    saved = paths.home() / "folders.json"
    assert json.loads(saved.read_text()) == {"proj-b": "/w/proj-b"}
    # the session ended: still known
    assert facts.folders(["proj-b"], [], home) == {"proj-b": "/w/proj-b"}
    # a learned path beats the home folder
    s = [Session("claude:2", "claude-code", "proj-a", cwd="/x/proj-a")]
    assert facts.folders(["proj-a"], s, home) == {"proj-a": "/x/proj-a"}


def test_folders_tolerate_broken_file_and_write_only_on_change(home):
    saved = paths.home() / "folders.json"
    saved.write_text("{nope")
    s = [Session("claude:1", "claude-code", "p", cwd="/w/p")]
    assert facts.folders(["p"], s, home) == {"p": "/w/p"}
    mtime = saved.stat().st_mtime_ns
    facts.folders(["p"], s, home)
    assert saved.stat().st_mtime_ns == mtime
