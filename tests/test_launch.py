"""The launcher: folders, worktrees, command lines and detaching, with fake run/popen."""
import json
import re
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
    assert argv[1:4] == ["--bg", "-n", "proj-a-w1"] and len(argv) == 7
    assert argv[4] == "--settings" and json.loads(argv[5]) == {
        "worktree": {"bgIsolation": "none"}, "permissions": {"deny": ["EnterWorktree"]}}


def test_claude_resume(launcher):
    argv = launcher.command(order(resume="abc-123"), "/w")
    assert argv[:4] == ["claude", "--bg", "-n", "proj-a-w1"]
    assert argv[6:8] == ["--resume", "abc-123"]
    assert len(argv) == 9 and argv[-1].startswith("You are")


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
    assert argv[:6] == ["systemd-run", "--user", "--scope", "--quiet", "--collect", "--unit"]
    assert re.fullmatch(r"kaptyorka-worker-proj-a-w1-[0-9a-f]{8}", argv[6])
    assert argv[7:] == ["tmux", "new-session"]
    assert kw["cwd"] == "/w" and kw["stdin"] == subprocess.DEVNULL
    assert kw["stderr"] == subprocess.STDOUT and not kw.get("start_new_session")
    assert kw["stdout"].name == str(home / ".kaptyorka" / "workers" / "proj-a-w1.log")
    assert popen.proc.waited == 10
    mac = FakePopen()
    make(home, popen=mac, platform="darwin").detach(["claude", "--bg"], "/w", "proj-a-w1")
    argv, kw = mac.calls[0]
    assert argv == ["claude", "--bg"] and kw["start_new_session"] is True
    assert mac.proc.waited == 60


class FakeSystemd(FakePopen):
    """systemd-run as systemd 255 does it: a scope name still loaded (an earlier run's scope
    still stopping) is refused; a started scope stays loaded."""

    def __init__(self):
        super().__init__()
        self.loaded = set()

    def __call__(self, argv, **kw):
        unit = argv[argv.index("--unit") + 1]
        if unit in self.loaded:
            self.rc, self.output = 1, (f"Failed to start transient scope unit: Unit {unit}.scope"
                                       " was already loaded or has a fragment file.\n")
        else:
            self.rc, self.output = 0, BG
            self.loaded.add(unit)
        return super().__call__(argv, **kw)


def test_a_relaunch_while_the_last_scope_is_still_loaded_starts(home, with_systemd):
    run, systemd = FakeRun(), FakeSystemd()
    launcher = make(home, run=run, popen=systemd)
    launcher.detach(["claude", "--bg"], "/w", "manager")
    assert len(systemd.loaded) == 1  # the first run's scope has not gone yet
    assert "backgrounded" in launcher.detach(["claude", "--bg"], "/w", "manager")
    units = [argv[argv.index("--unit") + 1] for argv, _ in systemd.calls]
    assert len(units) == 2 and units[0] != units[1]
    assert run.calls == []  # the old scope is left to end by itself, never stopped here


def test_detach_without_systemd_run_starts_a_new_session(home, no_systemd):
    run, popen = FakeRun(), FakePopen()
    make(home, run=run, popen=popen).detach(["tmux", "new-session"], "/w", "proj-a-w1")
    argv, kw = popen.calls[0]
    assert argv == ["tmux", "new-session"] and kw["start_new_session"] is True
    assert run.calls == []  # no scope to clear


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


def test_manager_command_runs_on_its_own_settings_and_mcp_config(launcher, monkeypatch):
    m = "/h/.kaptyorka/manager"
    monkeypatch.setattr(launch_mod.shutil, "which", lambda n: f"/usr/bin/{n}")
    argv = launcher.command(order(manager=True, worker="manager", folder=m), m)
    # an interactive session in its own tmux session: Claude Code's supervisor never stops it
    assert argv == ["tmux", "new-session", "-d", "-s", "kaptyorka-manager", "-c", m,
                    "/usr/bin/claude", "-n", "manager", "--settings", f"{m}/settings.json",
                    "--strict-mcp-config", "--mcp-config", f"{m}/mcp.json",
                    "--setting-sources", "project,local", "--tools=ToolSearch,Agent",
                    FIRST + "fix the tests"]
    assert "--bg" not in argv and "--dangerously-skip-permissions" not in argv
    assert "--resume" not in argv
    with pytest.raises(LaunchError, match="no manager support"):
        launcher.command(order(manager=True, worker="manager", tool="codex", folder=m), m)


# the manager: one interactive session in its own tmux session, never two

MGR_KEY = "claude:0011aabb-0000-0000-0000-000000000000"


def manager_order(home, **kw):
    m = home / ".kaptyorka" / "manager"
    m.mkdir(parents=True, exist_ok=True)
    return order(manager=True, worker="manager", project="manager", folder=str(m), **kw)


def test_manager_launch_ends_an_older_manager_first(home, with_systemd):
    run, popen = FakeRun(1, "can't find session: kaptyorka-manager"), FakePopen(0, "")
    o = manager_order(home)
    old_bg = Session("claude:99887766-aaaa", "claude-code", "manager", cwd=o["folder"],
                     background=True)
    elsewhere = Session("claude:55443322-bbbb", "claude-code", "proj-a", cwd=str(home / "proj-a"),
                        background=True)
    result = make(home, run=run, popen=popen).launch(o, [old_bg, elsewhere])
    assert run.calls == [["tmux", "kill-session", "-t", "=kaptyorka-manager"],
                         ["claude", "stop", "99887766"]]  # never the other folder's session
    argv = popen.calls[0][0]
    assert argv[:6] == ["systemd-run", "--user", "--scope", "--quiet", "--collect", "--unit"]
    assert argv[7:13] == ["tmux", "new-session", "-d", "-s", "kaptyorka-manager", "-c"]
    assert result["job"] is None and result["key"] is None  # found by its folder instead


def test_manager_found_by_its_folder_never_a_background_job(home):
    o = manager_order(home)
    launcher = make(home)
    bg = Session("claude:99887766-aaaa", "claude-code", "manager", cwd=o["folder"],
                 background=True)
    other = Session("claude:1", "claude-code", "proj-a", cwd=str(home / "proj-a"))
    assert launcher.find_key(o, [bg, other]) is None
    live = Session(MGR_KEY, "claude-code", "manager", cwd=o["folder"] + "/")
    assert launcher.find_key(o, [bg, other, live]) == MGR_KEY


def test_manager_close_ends_its_tmux_session_and_an_older_job(home):
    run = FakeRun()
    make(home, run=run).close({"worker": "manager", "tool": "claude-code", "key": MGR_KEY,
                               "manager": True})
    assert run.calls == [["tmux", "kill-session", "-t", "=kaptyorka-manager"],
                         ["claude", "stop", "0011aabb"]]
    for said in ("can't find session: kaptyorka-manager", "no server running on /tmp/x"):
        make(home, run=FakeRun(1, said)).close({"worker": "manager", "tool": "claude-code",
                                                "key": None, "manager": True})
    with pytest.raises(LaunchError, match="Permission denied"):
        make(home, run=FakeRun(1, "error connecting to /tmp/x (Permission denied)")).close(
            {"worker": "manager", "tool": "claude-code", "manager": True})


def test_trust_prompt_read_from_the_managers_pane(home):
    seen = []

    def run(cmd, **kw):
        seen.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, " ❯ No, exit\n   Yes, I trust this folder\n", "")
    assert make(home, run=run).trust_prompt({"worker": "manager", "manager": True})
    assert seen == [["tmux", "capture-pane", "-p", "-t", "=kaptyorka-manager:"]]
    assert not make(home, run=FakeRun(0, "")).trust_prompt({"worker": "manager", "manager": True})
    assert not make(home, run=FakeRun(1, "can't find session")).trust_prompt(
        {"worker": "manager", "manager": True})


def test_collect_dropped_once_for_an_old_systemd(home, with_systemd):
    class OldSystemd(FakePopen):
        def __call__(self, argv, **kw):
            self.rc, self.output = ((1, "systemd-run: unrecognized option '--collect'\n")
                                    if "--collect" in argv else (0, ""))
            return super().__call__(argv, **kw)
    popen = OldSystemd()
    make(home, popen=popen).detach(["tmux", "new-session"], "/w", "w1")
    assert ["--collect" in argv for argv, _ in popen.calls] == [True, False]
    broken = FakePopen(1, "Failed to start transient scope unit: Access denied\n")
    with pytest.raises(LaunchError, match="Access denied"):
        make(home, popen=broken).detach(["tmux", "new-session"], "/w", "w1")
    assert len(broken.calls) == 1  # any other refusal: no second try
