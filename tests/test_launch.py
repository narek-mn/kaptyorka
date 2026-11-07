"""The launcher: folders, worktrees, command lines and detaching, with fake run/popen."""
import json
import subprocess

import pytest

from kaptyorka import launch as launch_mod
from kaptyorka.adapters.base import Session
from kaptyorka.launch import LaunchError, Launcher

# what `claude --bg` prints: the short id that `claude stop` takes, a prefix of the session id
BG = "Starting background service…\nbackgrounded · ab12cd34 · proj-a-w1\n"
KEY = "claude:ab12cd34-0000-4000-8000-000000000000"
FIRST = 'You are kaptyorka worker `proj-a-w1` for `box-1/codex/proj-a` (alice). Your task:\n\n'


def order(**kw):
    base = {"kind": "launch", "id": 1, "issued": 1000.0, "worker": "proj-a-w1",
            "person": "alice", "tool": "claude-code", "project": "proj-a", "folder": "",
            "worktree": False, "task": "fix the tests", "boss": "box-1/codex/proj-a",
            "resume": None, "first_message": FIRST + kw.get("task", "fix the tests"),
            "sig": "x"}
    return {**base, **kw}


class FakeRun:
    def __init__(self, rc=0, stderr=""):
        self.calls, self.rc, self.stderr = [], rc, stderr

    def __call__(self, cmd, **kw):
        self.calls.append(cmd)
        return subprocess.CompletedProcess(cmd, self.rc, "", self.stderr)


class FakeProc:
    def __init__(self, rc):
        self.returncode, self.waited = rc, None

    def wait(self, timeout=None):
        self.waited = timeout
        return self.returncode


class FakePopen:
    def __init__(self, rc=0, output=BG):
        self.calls, self.rc, self.output = [], rc, output

    def __call__(self, argv, **kw):
        self.calls.append((argv, kw))
        kw["stdout"].write(self.output.encode())
        kw["stdout"].flush()
        self.proc = FakeProc(self.rc)
        return self.proc


@pytest.fixture
def no_systemd(monkeypatch):
    monkeypatch.setattr(launch_mod.shutil, "which", lambda n: None)


@pytest.fixture
def with_systemd(monkeypatch):
    monkeypatch.setattr(launch_mod.shutil, "which", lambda n: f"/usr/bin/{n}")


def make(home, run=None, popen=None, platform="linux"):
    return Launcher({"machine": "box-1"}, home, run=run or FakeRun(), popen=popen or FakePopen(),
                    platform=platform, log=lambda line: None)


@pytest.fixture
def launcher(home):
    return make(home)


def test_task_text_is_one_argument(launcher):
    task = 'say "hi"; $(rm -rf /) -x\nline2'
    argv = launcher.command(order(task=task), "/home/alice/proj-a")
    assert argv[-1] == FIRST + task and 'say "hi"' in argv[-1]
    assert argv[0] == "claude" and "--bg" in argv and "--dangerously-skip-permissions" not in argv
    assert argv[1:4] == ["--bg", "-n", "proj-a-w1"] and len(argv) == 5


def test_claude_resume(launcher):
    argv = launcher.command(order(resume="abc-123"), "/w")
    assert argv[:6] == ["claude", "--bg", "-n", "proj-a-w1", "--resume", "abc-123"]
    assert len(argv) == 7 and argv[-1].startswith("You are")


def test_tmux_tools_command_lines(launcher):
    task = "x; tmux kill-server"
    codex = launcher.command(order(tool="codex", task=task), "/w/proj-a")
    assert codex == ["tmux", "new-session", "-d", "-s", "proj-a-w1", "-c", "/w/proj-a",
                     "codex", FIRST + task]
    assert launcher.command(order(tool="codex", resume="r1"), "/w")[7:10] == \
        ["codex", "resume", "r1"]
    oc = launcher.command(order(tool="opencode", resume="ses_1"), "/w")
    assert oc == ["tmux", "new-session", "-d", "-s", "proj-a-w1", "-c", "/w", "opencode",
                  "--session", "ses_1", "--prompt", FIRST + "fix the tests"]
    for argv in (codex, oc):
        assert not any("skip" in a or "dangerous" in a or "--auto" == a or "yolo" in a
                       for a in argv)
    with pytest.raises(LaunchError, match="no worker support for cursor"):
        launcher.command(order(tool="cursor"), "/w")


def test_option_looking_values_are_refused(launcher):
    with pytest.raises(LaunchError):
        launcher.command(order(first_message="--dangerously-skip-permissions"), "/w")
    with pytest.raises(LaunchError):
        launcher.command(order(tool="codex", resume="--yolo"), "/w")
    with pytest.raises(LaunchError, match="bad worker name"):
        launcher.command(order(worker="../../etc/x"), "/w")


def test_folder_default_learned_and_missing(home, launcher):
    given = home / "given"
    given.mkdir()
    assert launcher.folder_for(order(folder=str(given))) == (str(given), False)
    learned = home / "code" / "proj-a"
    learned.mkdir(parents=True)
    (home / ".kaptyorka").mkdir(exist_ok=True)
    (home / ".kaptyorka" / "folders.json").write_text(json.dumps({"proj-a": str(learned)}))
    assert launcher.folder_for(order()) == (str(learned), False)
    (home / ".kaptyorka" / "folders.json").write_text("{}")
    (home / "proj-a").mkdir()
    assert launcher.folder_for(order()) == (str(home / "proj-a"), False)
    with pytest.raises(LaunchError) as e:
        launcher.folder_for(order(project="proj-b", folder=str(home / "gone")))
    assert str(e.value) == (f"no folder for proj-b on this machine (looked in "
                            f"{home / 'gone'}, {home / 'proj-b'})")


def test_occupied_folder_gets_worktree_with_branch(home):
    run = FakeRun()
    launcher = make(home, run=run)
    folder = home / "proj-a"
    folder.mkdir()
    (home / "proj-a--1").mkdir()  # taken: the next free one is --2
    mine = [Session("codex:1", "codex", "proj-a", cwd=str(folder))]
    assert launcher.occupied(str(folder), mine) and not launcher.occupied(str(home), mine)
    path = str(home / "proj-a--2")
    assert launcher.folder_for(order(folder=str(folder)), mine) == (path, True)
    assert run.calls == [["git", "-C", str(folder), "worktree", "add", "-b",
                          "work/proj-a-w1", path]]
    run.calls.clear()
    assert launcher.folder_for(order(folder=str(folder), worktree=True)) == (path, True)
    assert run.calls[0][-1] == path


def test_worktree_failure_carries_git_stderr(home):
    (home / "proj-a").mkdir()
    launcher = make(home, run=FakeRun(128, "fatal: not a git repository"))
    with pytest.raises(LaunchError, match="fatal: not a git repository"):
        launcher.folder_for(order(folder=str(home / "proj-a"), worktree=True))


def test_detach_uses_systemd_scope_on_linux_and_setsid_on_mac(home, with_systemd):
    popen = FakePopen()
    make(home, popen=popen).detach(["tmux", "new-session"], "/w", "proj-a-w1")
    argv, kw = popen.calls[0]
    assert argv == ["systemd-run", "--user", "--scope", "--quiet", "--unit",
                    "kaptyorka-worker-proj-a-w1", "tmux", "new-session"]
    assert kw["cwd"] == "/w" and kw["stdin"] == subprocess.DEVNULL
    assert kw["stderr"] == subprocess.STDOUT and not kw.get("start_new_session")
    assert kw["stdout"].name == str(home / ".kaptyorka" / "workers" / "proj-a-w1.log")
    assert popen.proc.waited == 10
    mac = FakePopen()
    make(home, popen=mac, platform="darwin").detach(["claude", "--bg"], "/w", "proj-a-w1")
    argv, kw = mac.calls[0]
    assert argv == ["claude", "--bg"] and kw["start_new_session"] is True
    assert mac.proc.waited == 60


def test_detach_without_systemd_run_starts_a_new_session(home, no_systemd):
    popen = FakePopen()
    make(home, popen=popen).detach(["tmux", "new-session"], "/w", "proj-a-w1")
    argv, kw = popen.calls[0]
    assert argv == ["tmux", "new-session"] and kw["start_new_session"] is True


def test_untrusted_workspace_is_a_clear_error(home, no_systemd):
    popen = FakePopen(0, "Error: Workspace not trusted. Run claude there first.\n")
    with pytest.raises(LaunchError) as e:
        make(home, popen=popen).detach(["claude", "--bg"], "/w/proj-a", "proj-a-w1")
    assert str(e.value) == "Claude Code does not trust /w/proj-a; open claude there once and accept"
    popen = FakePopen(1, "tmux: no server\n")
    with pytest.raises(LaunchError, match="tmux: no server"):
        make(home, popen=popen).detach(["tmux", "new-session"], "/w", "proj-a-w1")


def test_launch_returns_key_folder_and_brief_flag(home, no_systemd):
    (home / "proj-a").mkdir()
    popen = FakePopen()
    launcher = make(home, popen=popen)
    got = launcher.launch(order(folder=str(home / "proj-a")))
    assert got == {"key": None, "job": "ab12cd34", "folder": str(home / "proj-a"),
                   "worktree": False, "needs_brief": False}
    got = launcher.launch(order(tool="codex", folder=str(home / "proj-a")))
    assert got["key"] is None and got["job"] is None and got["needs_brief"] is False
    with pytest.raises(LaunchError, match="claude --bg printed no session id"):
        make(home, popen=FakePopen(0, "")).launch(order(folder=str(home / "proj-a")))


def test_close_commands(home):
    run = FakeRun()
    launcher = make(home, run=run)
    launcher.close({"worker": "proj-a-w1", "tool": "claude-code", "key": KEY})  # the hub's order
    launcher.close({"worker": "proj-a-w1", "tool": "claude-code", "job": "ffee0011"})  # the bridge's
    launcher.close({"worker": "proj-a-w1", "tool": "codex", "key": "codex:1"})
    assert run.calls == [["claude", "stop", "ab12cd34"], ["claude", "stop", "ffee0011"],
                         ["tmux", "kill-session", "-t", "=proj-a-w1"]]  # never w10
    with pytest.raises(LaunchError, match="no Claude Code session id to stop"):
        launcher.close({"worker": "proj-a-w1", "tool": "claude-code"})  # never `stop <name>`
    with pytest.raises(LaunchError, match="Permission denied"):
        make(home, run=FakeRun(1, "error connecting to /tmp/tmux-1000/default (Permission denied)")
             ).close({"worker": "proj-a-w1", "tool": "codex"})


def test_closing_a_gone_session_is_done(home):
    for tool, said in [("codex", "can't find session: proj-a-w1"),
                       ("opencode", "no server running on /tmp/tmux-1000/default"),
                       ("codex", "error connecting to /tmp/tmux-1000/default"
                                 " (No such file or directory)"),
                       ("codex", "can't find pane: proj-a-w1"),
                       ("claude-code", "No job matching 'ab12cd34'. Run 'claude agents'")]:
        make(home, run=FakeRun(1, said)).close({"worker": "proj-a-w1", "tool": tool, "key": KEY})


def test_find_key(launcher):
    o = {**order(tool="codex"), "folder": "/w/proj-a--1"}
    sessions = [Session("codex:1", "codex", "proj-a", cwd="/w/proj-a"),
                Session("opencode:2", "opencode", "proj-a", cwd="/w/proj-a--1"),
                Session("codex:3", "codex", "proj-a", cwd="/w/proj-a--1")]
    assert launcher.find_key(o, sessions) == "codex:3"
    assert launcher.find_key(o, sessions[:2]) is None
    c = {**order(), "key": "claude:u1"}
    assert launcher.find_key(c, [Session("claude:u1", "claude-code", "proj-a")]) == "claude:u1"
    assert launcher.find_key(c, sessions) is None
    j = {**order(), "key": None, "job": "ab12cd34"}  # just launched: only the short id is known
    assert launcher.find_key(j, sessions + [Session(KEY, "claude-code", "proj-a")]) == KEY
    assert launcher.find_key(j, sessions) is None


def test_manager_order_gets_no_extra_environment(home, no_systemd):
    (home / ".kaptyorka" / "manager").mkdir(parents=True)
    popen = FakePopen()
    make(home, popen=popen).launch(order(manager=True, folder=str(home / ".kaptyorka" / "manager"),
                                         env={"KAPTYORKA_CONFIG": "/m/config.json"}))
    assert popen.calls[0][1]["env"] is launch_mod.os.environ


def test_folder_override(home):
    run = FakeRun()
    launcher = make(home, run=run)
    m = home / ".kaptyorka" / "manager"
    m.mkdir(parents=True)
    (home / "manager").mkdir()  # the default lookup would pick this one
    busy = [Session("claude:1", "claude-code", "manager", cwd=str(m))]
    assert launcher.folder_for(order(manager=True, project="manager", folder=str(m) + "/"),
                               busy) == (str(m), False)
    assert launcher.folder_for(order(manager=True, folder=str(m), worktree=True)) \
        == (str(m), False)
    assert run.calls == []  # never a worktree
    for bad in ("", "rel/manager", str(home / "gone"), None, str(home / "manager")):
        with pytest.raises(LaunchError, match="no manager folder"):
            launcher.folder_for(order(manager=True, project="manager", folder=bad))
