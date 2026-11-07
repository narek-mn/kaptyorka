import json
import subprocess
import time
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
                return SimpleNamespace(returncode=out[0], stdout=out[1],
                                       stderr=out[2] if len(out) > 2 else "")
        return SimpleNamespace(returncode=1, stdout="", stderr="")


GIT = {"rev-parse": (0, "main\n"), "status": (0, " M a.py\n"), "log": (0, "fix thing\n")}


def setup_function():
    facts._cache.clear()


def test_git_reads_branch_dirty_subject_with_timeout():
    run = Run(GIT)
    got = facts.git("/w/p", run)
    read_at = got.pop("read_at")
    assert got == {"branch": "main", "dirty": True, "subject": "fix thing"}
    assert abs(read_at - time.time()) < 5  # when git ran, for the hub to drop stale resends
    assert all(c[0][:3] == ["git", "-C", "/w/p"] and 0 < c[1]["timeout"] <= 2 for c in run.calls)


def test_git_clean_repo():
    run = Run({**GIT, "status": (0, "")})
    assert facts.git("/w/p", run)["dirty"] is False


def test_git_not_a_repo_or_timeout_is_none():
    assert facts.git("/w/a", Run({"rev-parse": (128, "")})) is None
    assert facts.git("/w/b", Run({"rev-parse": subprocess.TimeoutExpired("git", 2)})) is None


def test_git_status_failure_leaves_dirty_unknown():
    run = Run({**GIT, "status": (1, "")})
    assert facts.git("/w/p", run)["dirty"] is None


def test_git_cached_for_30_seconds(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(facts.time, "monotonic", lambda: now[0])
    run = Run(GIT)
    first = facts.git("/w/p", run)["read_at"]
    n = len(run.calls)
    now[0] += 29
    assert facts.git("/w/p", run)["read_at"] == first  # a cached resend keeps its read time
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
    monkeypatch.setattr(facts.doctor, "tool_configured", lambda a, home: a.tool == "claude-code")
    run = Run({"--version": (0, "2.1.3 (Claude Code)\nmore\n")})
    m = facts.machine([adapter("claude-code"), adapter("opencode")], run)
    assert m["os_user"] == "alice" and m["tmux"] is True and m["load"] == 1.5
    assert m["cpus"] == 1 and m["version"] == kaptyorka.__version__
    assert m["tools"] == {"claude-code": "2.1.3 (Claude Code)", "opencode": ""}
    assert m["hooks_ok"] == {"claude-code": True, "opencode": False}
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


def test_folders_learn_and_remember(home, tmp_path):
    (home / "proj-a").mkdir()
    b, x = tmp_path / "w" / "proj-b", tmp_path / "x" / "proj-a"
    b.mkdir(parents=True)
    x.mkdir(parents=True)
    s = [Session("claude:1", "claude-code", "proj-b", cwd=str(b))]
    out = facts.folders(["proj-a", "proj-b", "proj-c"], s, home)
    assert out == {"proj-a": str(home / "proj-a"), "proj-b": str(b)}
    saved = paths.home() / "folders.json"
    assert json.loads(saved.read_text()) == {"proj-b": str(b)}
    # the session ended: still known
    assert facts.folders(["proj-b"], [], home) == {"proj-b": str(b)}
    # a learned path beats the home folder
    s = [Session("claude:2", "claude-code", "proj-a", cwd=str(x))]
    assert facts.folders(["proj-a"], s, home) == {"proj-a": str(x), "proj-b": str(b)}


def test_folders_report_unregistered_learned_folders(home, tmp_path):
    own = tmp_path / "w" / "proj-c"  # not in the hub's list: the person's own folder
    own.mkdir(parents=True)
    s = [Session("claude:1", "claude-code", "proj-c", cwd=str(own))]
    assert facts.folders([], s, home) == {"proj-c": str(own)}
    assert facts.folders([], [], home) == {"proj-c": str(own)}  # remembered


def test_folders_tolerate_broken_file_and_write_only_on_change(home, tmp_path):
    saved = paths.home() / "folders.json"
    saved.write_text("{nope")
    p = tmp_path / "p"
    p.mkdir()
    s = [Session("claude:1", "claude-code", "p", cwd=str(p))]
    assert facts.folders(["p"], s, home) == {"p": str(p)}
    mtime = saved.stat().st_mtime_ns
    facts.folders(["p"], s, home)
    assert saved.stat().st_mtime_ns == mtime


def test_folders_drop_vanished_learned_path(home):
    (home / "p").mkdir()
    gone = home / "gone-dir" / "p"
    gone.mkdir(parents=True)
    s = [Session("claude:1", "claude-code", "p", cwd=str(gone))]
    assert facts.folders(["p"], s, home) == {"p": str(gone)}
    gone.rmdir()
    assert facts.folders(["p"], [], home) == {"p": str(home / "p")}
    assert json.loads((paths.home() / "folders.json").read_text()) == {}


def test_folders_survive_unwritable_file(home, tmp_path, monkeypatch):
    def boom(*a):
        raise OSError("read-only")

    monkeypatch.setattr(facts, "write_atomic", boom)
    d = tmp_path / "p"
    d.mkdir()
    s = [Session("claude:1", "claude-code", "p", cwd=str(d))]
    assert facts.folders(["p"], s, home) == {"p": str(d)}


def _hooks(path, events, nested, tool):
    path.parent.mkdir(parents=True, exist_ok=True)
    cmd = lambda e: f"/bin/kaptyorka hook {tool} {e}"  # noqa: E731
    hooks = {ev: [{"hooks": [{"command": cmd(n)}]} if nested else {"command": cmd(n)}]
             for ev, n in events}
    path.write_text(json.dumps({"hooks": hooks}))


def test_harness_layers_per_tool(home, monkeypatch):
    monkeypatch.setattr(facts.doctor, "tool_configured", lambda a, h: a.tool == "claude-code")
    _hooks(home / ".claude" / "settings.json", [("SessionStart", "start")], True, "claude-code")
    _hooks(home / ".codex" / "hooks.json", [("SessionStart", "start")], True, "codex")
    ads = [adapter("claude-code"), adapter("codex"), adapter("cursor"), adapter("opencode"),
           adapter("gone", installed=False)]
    h = facts.harness(ads, home, True)
    assert h["claude-code"] == {"rules": True, "hooks": True, "skills": True}
    assert h["codex"] == {"rules": True, "hooks": False, "skills": None}
    assert h["cursor"] == {"rules": False, "hooks": False, "skills": True}
    assert h["opencode"]["rules"] is False and "gone" not in h
    plugin = home / ".config" / "opencode" / "plugins" / "kaptyorka.js"
    plugin.parent.mkdir(parents=True)
    plugin.write_text("x")
    assert facts.harness(ads, home, False)["opencode"] == {"rules": True, "hooks": False, "skills": False}


def _login_adapters(home):
    from kaptyorka.adapters.claude import ClaudeAdapter
    from kaptyorka.adapters.codex import CodexAdapter
    from kaptyorka.adapters.cursor import CursorAdapter
    from kaptyorka.adapters.opencode import OpenCodeAdapter

    real = [ClaudeAdapter(home=home), CodexAdapter(home=home), OpenCodeAdapter(home=home),
            CursorAdapter(home=home)]
    return [SimpleNamespace(tool=a.tool, installed=lambda: True, logged_in=a.logged_in)
            for a in real]


SECRET = '{"loggedIn": false, "email": "alice@example.com", "orgName": "proj-a"}'
OPENCODE_EMPTY = "\x1b[90m┌  Credentials ~/.local/share/opencode/auth.json\n│\n└  \x1b[1m0\x1b[0m credentials\x1b[0m\n"


def test_logins_from_tool_commands(monkeypatch, tmp_path):
    monkeypatch.setattr(facts.shutil, "which", lambda n: "/bin/" + n)
    run = Run({"auth status": (0, SECRET), "login status": (0, "Logged in using ChatGPT\n"),
               "auth list": (0, OPENCODE_EMPTY), "--version": (0, "1.0\n")})
    m = facts.machine(_login_adapters(tmp_path), run)
    assert m["logins"] == {"claude-code": False, "codex": True, "opencode": False, "cursor": None}
    login_calls = [c for c in run.calls if "--version" not in c[0]]
    assert [c[0] for c in login_calls] == [["/bin/claude", "auth", "status"],
                                           ["/bin/codex", "login", "status"],
                                           ["/bin/opencode", "auth", "list"]]
    assert all(c[1] == {"capture_output": True, "text": True, "timeout": 5} for c in login_calls)
    facts.machine(_login_adapters(tmp_path), run)  # cached for 10 minutes
    assert len([c for c in run.calls if "--version" not in c[0]]) == 3


def test_login_states_per_output(monkeypatch, tmp_path):
    monkeypatch.setattr(facts.shutil, "which", lambda n: "/bin/" + n)
    claude, codex, opencode, cursor = _login_adapters(tmp_path)
    assert claude.logged_in(Run({"auth": (0, '{"loggedIn": true}')})) is True
    assert claude.logged_in(Run({"auth": (0, "not json")})) is None
    assert claude.logged_in(Run({"auth": (0, '{"loggedIn": "yes"}')})) is None
    assert claude.logged_in(Run({"auth": (1, '{"loggedIn": true}')})) is None
    assert codex.logged_in(Run({"login": (0, "Not logged in\n")})) is False
    assert codex.logged_in(Run({"login": (1, "", "Not logged in\n")})) is False  # its real shape
    assert codex.logged_in(Run({"login": OSError()})) is None
    assert codex.logged_in(Run({"login": (0, "something else\n")})) is None
    assert opencode.logged_in(Run({"auth": (0, "└  2 credentials\n")})) is True
    assert opencode.logged_in(Run({"auth": (0, "no list here\n")})) is None
    assert opencode.logged_in(Run({"auth": OSError()})) is None
    monkeypatch.setattr(facts.shutil, "which", lambda n: None)
    assert claude.logged_in(Run({"auth": (0, '{"loggedIn": true}')})) is None


def test_login_check_timeout_is_unknown(monkeypatch, tmp_path):
    monkeypatch.setattr(facts.shutil, "which", lambda n: "/bin/" + n)
    run = Run({"auth status": subprocess.TimeoutExpired("claude", 5),
               "login status": (0, "Not logged in\n"), "auth list": (0, "└  1 credentials\n")})
    m = facts.machine(_login_adapters(tmp_path), run)
    assert m["logins"] == {"claude-code": None, "codex": False, "opencode": True, "cursor": None}


def test_logins_hold_only_booleans(monkeypatch, tmp_path):
    monkeypatch.setattr(facts.shutil, "which", lambda n: "/bin/" + n)
    m = facts.machine(_login_adapters(tmp_path)[:1], Run({"auth status": (0, SECRET)}))
    assert m["logins"] == {"claude-code": False}
    assert "alice@example.com" not in json.dumps(m) and "proj-a" not in json.dumps(m)
