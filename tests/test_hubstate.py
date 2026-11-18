"""The hub field in a Claude Code status line: `kaptyorka hub-state` (on, off, lost, nothing),
its shared cache and its speed, the `kaptyorka statusline` wrapper, and how join and leave
wire it into ~/.claude/settings.json. Everything runs in a temp HOME."""
import json
import os
import shlex
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from helpers import write_session_file
from test_claude import fake_claude_cli  # noqa: F401
from kaptyorka import hubstate, offhub, paths
from kaptyorka.store import session_id as handle_of

pytestmark = pytest.mark.team_only  # the real personal-folder gate
SID = "sid-hub-1"
KEY = f"claude:{SID}"


@pytest.fixture
def joined(home):
    paths.save_config({"hub": "http://127.0.0.1:1", "token": "T", "machine": "box-1"})
    offhub.save_team({"proj-a"}, set())
    write_session_file(home, os.getpid(), cwd="/w/proj-a", session_id=SID)
    return home


def put_cache(ids=(), ok=True, age=0.0):
    (paths.home() / hubstate.CACHE).write_text(json.dumps({"at": time.time() - age, "ok": ok, "ids": list(ids)}))


def never():
    raise AssertionError("no refresh expected")


def test_no_config_prints_nothing(home, capsys):
    from test_cli import entry_with_exit

    assert hubstate.state(SID, spawn=never) is None
    assert entry_with_exit(["hub-state", SID]) == 0
    assert capsys.readouterr().out == ""


def test_on_from_a_real_hub(joined, hub):
    paths.save_config({"hub": hub.url, "token": hub.token, "machine": "box-1"})
    hub.store.seen(KEY, "box-1", "claude-code", "proj-a", principal=hub.store.authenticate("T"))
    got = hubstate.refresh()
    assert got["ok"] and handle_of(KEY) in got["ids"]
    assert not (paths.home() / hubstate.REFRESHING).exists()
    assert hubstate.state(SID, spawn=never) == "on"


def test_off_when_the_session_chose_it(joined):
    put_cache([handle_of(KEY)])
    (paths.home() / "off").mkdir(exist_ok=True)
    (paths.home() / "off" / SID).write_text(json.dumps({"off": True, "pid": os.getpid(), "start": None,
                                                        "gone": False}))
    assert hubstate.state(SID, spawn=never) == "off"


def test_off_in_a_personal_folder_or_with_no_team_list(joined, home):
    put_cache([handle_of(KEY)])
    write_session_file(home, os.getpid(), cwd="/w/my-notes", session_id=SID)
    assert hubstate.state(SID, spawn=never) == "off"
    write_session_file(home, os.getpid(), cwd="/w/proj-a--2", session_id=SID)  # a worktree counts
    assert hubstate.state(SID, spawn=never) == "on"
    offhub.save_team(None, set())  # the hub has not named its team projects
    assert hubstate.state(SID, spawn=never) == "off"
    # no live session file: the folder the status line gives decides
    (home / ".claude" / "sessions" / f"{os.getpid()}.json").unlink()
    offhub.save_team({"proj-a"}, set())
    assert hubstate.state(SID, cwd="/w/other", spawn=never) == "off"
    assert hubstate.state(SID, cwd="/w/proj-a", spawn=never) == "on"


def test_lost_when_not_listed_or_hub_unreachable(joined):
    put_cache(["#00000000"])
    assert hubstate.state(SID, spawn=never) == "lost"
    got = hubstate.refresh(timeout=1)  # the config points at a closed port
    assert got["ok"] is False and got["ids"] == []
    assert hubstate.state(SID, spawn=never) == "lost"


def test_a_new_session_is_not_lost_yet(joined, home):
    put_cache([])
    write_session_file(home, os.getpid(), cwd="/w/proj-a", session_id=SID,
                       extra={"startedAt": int(time.time() * 1000)})
    assert hubstate.state(SID, spawn=never) is None
    write_session_file(home, os.getpid(), cwd="/w/proj-a", session_id=SID,
                       extra={"startedAt": int((time.time() - hubstate.GRACE_S) * 1000)})
    assert hubstate.state(SID, spawn=never) == "lost"


def test_stale_cache_answers_at_once_and_refreshes_once(joined):
    spawned = []
    put_cache([handle_of(KEY)], age=hubstate.FRESH_S + 1)
    assert hubstate.state(SID, spawn=lambda: spawned.append(1)) == "on"
    assert hubstate.state(SID, spawn=lambda: spawned.append(1)) == "on"
    assert spawned == [1]  # one refresh for the machine, not one per call
    lock = paths.home() / hubstate.REFRESHING
    old = time.time() - hubstate.REFRESH_LOCK_S - 1
    os.utime(lock, (old, old))  # that refresh died
    hubstate.state(SID, spawn=lambda: spawned.append(1))
    assert spawned == [1, 1]
    lock.unlink()
    put_cache([handle_of(KEY)], age=hubstate.STALE_S + 1)  # too old to vouch for anything
    assert hubstate.state(SID, spawn=lambda: None) == "lost"
    lock.unlink()
    (paths.home() / hubstate.CACHE).unlink()
    assert hubstate.state(SID, spawn=lambda: spawned.append(1)) is None  # nothing known yet
    assert len(spawned) == 3


@pytest.fixture
def silent_hub():
    """A hub address that accepts connections and never answers."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen(16)
    held, stop = [], threading.Event()

    def accept():
        s.settimeout(0.2)
        while not stop.is_set():
            try:
                held.append(s.accept()[0])
            except OSError:
                pass
    t = threading.Thread(target=accept, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{s.getsockname()[1]}"
    stop.set()
    t.join()
    for c in held:
        c.close()
    s.close()


def test_fast_even_when_the_hub_hangs(joined, silent_hub):
    paths.save_config({"hub": silent_hub, "token": "T", "machine": "box-1"})
    put_cache([handle_of(KEY)], age=hubstate.FRESH_S + 5)
    t0 = time.perf_counter()
    assert hubstate.state(SID) == "on"  # the real background refresh is started
    took = time.perf_counter() - t0
    assert took < 0.05, took
    # the whole command, as a status line runs it: a fresh Python process
    env = {**os.environ}
    t0 = time.perf_counter()
    r = subprocess.run([sys.executable, "-c", "import sys; sys.argv = ['kaptyorka', 'hub-state', %r];"
                        " from kaptyorka.cli import entry; entry()" % SID],
                       capture_output=True, text=True, env=env, timeout=10)
    took = time.perf_counter() - t0
    assert r.returncode == 0 and r.stdout == "on\n", r.stderr
    # This bound only guards against waiting on the hanging hub, which would take the
    # 5 s+ socket wait. A cold Python start on CI runners can take about 1 s alone.
    assert took < 2.0, took


# the status line wrapper

def test_statusline_alone_prints_only_the_field(joined, monkeypatch, capsys):
    put_cache([handle_of(KEY)])
    assert hubstate.statusline("", json.dumps({"session_id": SID})) == 0
    assert capsys.readouterr().out == hubstate.FIELDS["on"] + "\n"
    put_cache([])
    hubstate.statusline("", json.dumps({"session_id": SID}))
    assert capsys.readouterr().out == hubstate.FIELDS["lost"] + "\n"
    assert hubstate.FIELDS["lost"] == "\033[1;48;5;214;38;5;16m hub ✗ \033[0m"


def test_statusline_wraps_their_command(joined, capsys):
    put_cache([handle_of(KEY)])
    stdin = json.dumps({"session_id": SID, "model": {"display_name": "M"}})
    assert hubstate.statusline("cat; echo", stdin) == 0  # their command gets the same input
    assert capsys.readouterr().out == stdin + " " + hubstate.FIELDS["on"] + "\n"
    assert hubstate.statusline("printf 'line one\\nline two\\n'; echo oops >&2; exit 3", stdin) == 3
    out = capsys.readouterr()
    assert out.out == "line one\nline two " + hubstate.FIELDS["on"] + "\n" and out.err == "oops\n"
    assert hubstate.statusline("true", stdin) == 0
    assert capsys.readouterr().out == hubstate.FIELDS["on"] + "\n"


def test_statusline_lets_a_slow_command_finish(joined, capsys):
    put_cache([handle_of(KEY)])
    assert hubstate.statusline("echo early; sleep 1; echo late", json.dumps({"session_id": SID})) == 0
    assert capsys.readouterr().out == "early\nlate " + hubstate.FIELDS["on"] + "\n"


def test_statusline_survives_a_broken_field(joined, monkeypatch, capsys):
    def boom(*a, **k):
        raise RuntimeError("x")
    monkeypatch.setattr(hubstate, "state", boom)
    assert hubstate.statusline("echo mine", "not json") == 0
    assert capsys.readouterr().out == "mine\n"


def test_cli_statusline_passes_stdin(joined, monkeypatch, capsys):
    import io

    from test_cli import entry_with_exit

    put_cache([handle_of(KEY)])
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"session_id": SID})))
    assert entry_with_exit(["statusline", "--", "echo mine"]) == 0
    assert capsys.readouterr().out == "mine " + hubstate.FIELDS["on"] + "\n"


# join and leave

def settings(home):
    return json.loads((home / ".claude" / "settings.json").read_text())


def test_join_without_a_status_line_sets_one_and_leave_removes_it(home, fake_claude_cli):
    from kaptyorka.adapters.claude import ClaudeAdapter

    a = ClaudeAdapter(home, run=fake_claude_cli.run)
    (home / ".claude").mkdir()
    (home / ".claude" / "settings.json").write_text(json.dumps({"theme": "dark"}))
    lines = a.configure("/k")
    assert "Claude Code: the status line shows the hub field (kaptyorka leave removes it)" in lines
    assert settings(home)["statusLine"] == {"type": "command", "command": "/k statusline", "padding": 0}
    assert not [l for l in a.configure("/k") if "status line" in l or "settings" in l]  # a second join adds nothing
    a.unconfigure()
    after = settings(home)
    after.pop("hooks", None)  # `/k` hooks are not recognised as kaptyorka's by leave
    assert after == {"theme": "dark"}
    assert not (paths.home() / "statusline.json").exists()


def test_join_wraps_their_status_line_and_leave_puts_it_back(home, hub, fake_claude_cli, capsys):
    from kaptyorka.adapters.claude import ClaudeAdapter
    from kaptyorka.join import main
    from test_join import no_service

    theirs = {"type": "command", "command": "python3 ~/.claude/line.py --say 'a b' | head -1", "padding": 2}
    before = {"statusLine": theirs, "model": "x"}
    (home / ".claude").mkdir()
    (home / ".claude" / "settings.json").write_text(json.dumps(before, indent=2))
    main(hub.url, "T", "pc", adapters=[ClaudeAdapter(home, run=fake_claude_cli.run)], install=no_service([]))
    out = capsys.readouterr().out
    assert ("Claude Code: added the hub field after your status line;"
            " kaptyorka leave puts yours back as it was") in out
    line = settings(home)["statusLine"]
    argv = shlex.split(line["command"])
    assert argv[1:3] == ["statusline", "--"] and argv[3] == theirs["command"] and len(argv) == 4
    assert line["padding"] == 2 and line["type"] == "command"
    first = (home / ".claude" / "settings.json").read_text()
    main(hub.url, "T", "pc", adapters=[ClaudeAdapter(home, run=fake_claude_cli.run)], install=no_service([]))
    assert (home / ".claude" / "settings.json").read_text() == first  # not wrapped twice
    assert "added the hub field" not in capsys.readouterr().out
    lines = ClaudeAdapter(home, run=fake_claude_cli.run).unconfigure()
    assert "Claude Code: your status line is back as it was" in lines
    after = settings(home)
    after.pop("hooks", None)  # hooks written for the test runner's path, not a kaptyorka one
    assert after == before and list(after) == list(before)


def test_wrapped_command_runs_their_line(home, joined, fake_claude_cli, capsys):
    """The command join writes, run the way Claude Code runs a status line (through a shell)."""
    from kaptyorka.adapters.claude import ClaudeAdapter

    put_cache([handle_of(KEY)])
    script = home / "line.sh"
    script.write_text("#!/bin/sh\nread x; echo \"their line: $x\" | cut -c1-24\n")
    script.chmod(0o755)
    (home / ".claude" / "settings.json").write_text(json.dumps(
        {"statusLine": {"type": "command", "command": f"{script} 'x y' | cat"}}))
    exe = home / "kaptyorka"
    exe.write_text(f"#!{sys.executable}\nimport sys\nfrom kaptyorka.cli import entry\nentry()\n")
    exe.chmod(0o755)
    ClaudeAdapter(home, run=fake_claude_cli.run).configure(str(exe))
    cmd = settings(home)["statusLine"]["command"]
    r = subprocess.run(cmd, shell=True, input=json.dumps({"session_id": SID}) + "\n",
                       capture_output=True, text=True, timeout=10)
    assert r.returncode == 0 and r.stdout == 'their line: {"session_id ' + hubstate.FIELDS["on"] + "\n"


def test_leave_keeps_a_status_line_changed_since_join(home, fake_claude_cli):
    from kaptyorka.adapters.claude import ClaudeAdapter

    a = ClaudeAdapter(home, run=fake_claude_cli.run)
    (home / ".claude").mkdir()
    (home / ".claude" / "settings.json").write_text(json.dumps({"statusLine": {"type": "command", "command": "old"}}))
    a.configure("/k")
    data = settings(home)
    data["statusLine"] = {"type": "command", "command": "brand new"}
    (home / ".claude" / "settings.json").write_text(json.dumps(data))
    a.unconfigure()
    assert settings(home)["statusLine"] == {"type": "command", "command": "brand new"}


def test_leave_unwraps_without_the_saved_file(home, fake_claude_cli):
    from kaptyorka.adapters.claude import ClaudeAdapter

    a = ClaudeAdapter(home, run=fake_claude_cli.run)
    (home / ".claude").mkdir()
    (home / ".claude" / "settings.json").write_text(json.dumps({"statusLine": {"type": "command", "command": "mine --x"}}))
    a.configure("/usr/bin/kaptyorka")
    (paths.home() / "statusline.json").unlink()
    assert not [l for l in a.configure("/usr/bin/kaptyorka") if "status line" in l]
    assert shlex.split(settings(home)["statusLine"]["command"])[3:] == ["mine --x"]
    (paths.home() / "statusline.json").unlink()
    a.unconfigure()
    assert settings(home)["statusLine"] == {"type": "command", "command": "mine --x"}
