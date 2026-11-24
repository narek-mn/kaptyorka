"""Stuck sessions (stuck.py): the bridge's measures, the hub's two stages, the page field and
overnight mode's wake-up. docs/superpowers/specs/2026-11-24-stuck-sessions.md"""
import json
import os
import subprocess
import sys
import time

import pytest

from helpers import call, person, run
from test_hub_manager import clock, hub, inbox, manager_online  # noqa: F401 (fixtures)
from test_hub_workers import bridge, heartbeat, member
from kaptyorka import overnight, stuck
from kaptyorka.adapters import claude as claude_mod
from kaptyorka.hub_feed import agent_row
from kaptyorka.store import Store
from kaptyorka.workers import HUB_KEY

WRAPPER = ("source /home/u/.claude/shell-snapshots/snapshot-bash-1-x.sh 2>/dev/null || true &&"
           " \\builtin shopt -u extglob 2>/dev/null || true && eval 'while pgrep -f run_suite.sh"
           " >/dev/null; do sleep 10; done; ./next.sh' < /dev/null && pwd -P >| /tmp/claude-1-cwd")


# ---------- the rule ----------

def test_idle_is_never_stuck_and_unknown_is_never_judged():
    old = [{"id": "b1", "silent_s": 99999, "command": "x"}]
    assert stuck.find("idle", 99999, old) is None
    assert stuck.find("", 99999, old) is None
    assert stuck.find("busy", None, None) is None  # an older bridge says neither
    assert stuck.find("shell", 99999, None) is None  # a finished turn: only its tasks count


def test_a_silent_task_and_a_silent_busy_session():
    t = {"id": "b1", "silent_s": stuck.WAKE_S, "command": "sleep"}
    assert stuck.find("shell", 5, [t]) == {"kind": "task", "age": stuck.WAKE_S, "task": t}
    assert stuck.find("shell", 5, [{**t, "silent_s": stuck.WAKE_S - 1}]) is None
    assert stuck.find("busy", stuck.WAKE_S, []) == {"kind": "busy", "age": stuck.WAKE_S}
    assert stuck.find("busy", stuck.WAKE_S - 1, []) is None
    # the longest silent task names the episode
    u = {"id": "b2", "silent_s": stuck.WAKE_S + 60, "command": "y"}
    assert stuck.find("busy", 0, [t, u])["task"] is u


def test_command_of_a_background_shell():
    assert stuck.command_of(["/bin/bash", "-c", WRAPPER]) == \
        "while pgrep -f run_suite.sh >/dev/null; do sleep 10; done; ./next.sh"
    assert stuck.command_of(["/bin/bash", "-c", "plain"]) == "plain"
    # a multi-line command, and one with its own `< /dev/null` and quotes (Claude Code 2.1.33x)
    multi = WRAPPER.replace("while pgrep -f run_suite.sh >/dev/null; do sleep 10; done; ./next.sh",
                            "O=~/x.txt; { echo \"=== FAST\";\n  make test < /dev/null; } > $O")
    assert stuck.command_of(["/bin/bash", "-c", multi]) == \
        "O=~/x.txt; { echo \"=== FAST\";\n  make test < /dev/null; } > $O"
    quoted = WRAPPER.replace("'while pgrep -f run_suite.sh >/dev/null; do sleep 10; done; ./next.sh'",
                             "'awk '\"'\"'{print $1}'\"'\"' log'")
    assert stuck.command_of(["/bin/bash", "-c", quoted]) == "awk '{print $1}' log"
    # an older wrapper with no `< /dev/null` after the command
    older = ("source /home/u/.claude/shell-snapshots/snapshot-bash-9.sh && eval 'npm run dev'"
             " && pwd -P >| /tmp/claude-2-cwd")
    assert stuck.command_of(["/bin/bash", "-c", older]) == "npm run dev"
    # a wrapper that cannot be read: no command, never the wrapper with its home path
    assert stuck.command_of(["/bin/bash", "-c", "source /home/u/.claude/shell-snapshots/s.sh"
                             " 2>/dev/null || true && run 'it"]) == ""
    assert stuck.command_of(["/bin/bash", "-c", "source x && eval 'unclosed"]) == ""
    assert stuck.safe_command("x" * 300).endswith("...") and len(stuck.safe_command("x" * 300)) == 100
    assert stuck.safe_command("curl -H 'token: ghp_" + "a" * 36 + "'") == ""


def test_span():
    assert [stuck.span(s) for s in (1800, 3599, 3600, 7800, 4 * 3600 + 600)] == \
        ["30 min", "59 min", "1 h", "2 h 10 min", "4 h"]


def test_texts():
    ep = {"kind": "task", "age": 3600.0, "task": {"id": "b1", "command": "sleep 9"}}
    assert stuck.wake_text(ep).startswith(
        "kaptyorka: your background task b1 (`sleep 9`) has printed nothing for 1 h.")
    assert stuck.wake_text(ep).endswith(
        "If it is meant to run (a server, a watcher), just say so in one line; you won't be asked"
        " about this task again.")
    text = stuck.people_text("box-1/claude-code/proj-a", "box-1", ep, "claude:abcdef12-3", True,
                             1800)
    assert text == ("Session box-1/claude-code/proj-a on box-1 looks stuck: its background task"
                    " b1 (`sleep 9`) has printed nothing for 1 h. A message reaches it, yet it has"
                    " not answered the hub's question 30 min ago. To stop it:"
                    " `claude stop abcdef12` on box-1.")
    bare = {"kind": "task", "age": 2100.0, "task": {"id": "b1", "command": ""}}  # a coordinator's
    assert stuck.wake_text(bare).startswith(
        "kaptyorka: your background task b1 has printed nothing for 35 min. Check")
    assert "its background task b1 has printed nothing for 35 min. A message" in \
        stuck.people_text("n", "box-1", bare, "claude:abcdef12", True, 1800)
    busy = {"kind": "busy", "age": 3600.0}
    busy_text = stuck.people_text("n", "box-1", busy, "k:x", False, 1800)
    assert "only after its current step ends" in busy_text and busy_text.endswith("press Esc.")
    m = stuck.manager_text("n", "box-1", "alice", ep, "claude:abcdef12", True, 3600,
                           {"branch": "main", "subject": "add x", "dirty": False}, again=True)
    assert m.startswith("stuck session: n of alice on box-1 is still stuck:")
    assert "It was asked to check 1 h ago and has not answered; alice was told." in m
    assert 'Git in its folder: branch main, last commit "add x", nothing uncommitted.' in m


# ---------- the bridge's measures ----------

def test_last_write_skips_messages_delivered_to_it(tmp_path):
    f = tmp_path / "t.jsonl"
    lines = [{"type": "assistant", "timestamp": "2026-11-24T08:00:00.000Z"},
             {"type": "last-prompt"},
             {"type": "queue-operation", "operation": "enqueue",
              "timestamp": "2026-11-24T09:00:00.000Z"}]
    f.write_text("\n".join(json.dumps(x) for x in lines) + "\n")
    st = f.stat()
    got = claude_mod.last_write(f, st.st_size, st.st_mtime)
    assert got == min(claude_mod._stamp("2026-11-24T08:00:00.000Z"), st.st_mtime)
    f.write_text("not json\n")
    assert claude_mod.last_write(f, f.stat().st_size, 123.0) == 123.0


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="/proc is Linux only")
def test_live_tasks_finds_a_child_shell_writing_to_a_task_file(tmp_path):
    out = tmp_path / "sess" / "tasks" / "bx1.output"
    out.parent.mkdir(parents=True)
    with open(out, "w") as f:
        p = subprocess.Popen(["/bin/bash", "-c", "true && eval 'sleep 30' < /dev/null && pwd -P"],
                             stdout=f, stderr=subprocess.STDOUT)
    try:
        os.utime(out, (time.time() - 100, time.time() - 100))
        kids = claude_mod.children()
        got = claude_mod.live_tasks(os.getpid(), kids, time.time())
        assert [(t["id"], t["command"]) for t in got] == [("bx1", "sleep 30")]
        assert 99 <= got[0]["silent_s"] < 110
        assert claude_mod.live_tasks(os.getpid() + 999999, kids, time.time()) == []
    finally:
        p.kill()
        p.wait()


def test_sessions_report_measures_only_while_busy(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".claude" / "sessions").mkdir(parents=True)
    proj = home / ".claude" / "projects" / "-w-proj-a"
    proj.mkdir(parents=True)
    (proj / "s1.jsonl").write_text(json.dumps({"type": "user", "timestamp":
                                               "2026-11-24T08:00:00.000Z"}) + "\n")
    sub = proj / "s1" / "subagents"
    sub.mkdir(parents=True)
    (sub / "agent-a1.jsonl").write_text("{}\n")
    os.utime(sub / "agent-a1.jsonl", (time.time() - 50, time.time() - 50))

    def write(status):
        (home / ".claude" / "sessions" / f"{os.getpid()}.json").write_text(json.dumps(
            {"pid": os.getpid(), "sessionId": "s1", "cwd": "/w/proj-a", "status": status}))
    monkeypatch.setattr(claude_mod, "children", lambda: {})
    a = claude_mod.ClaudeAdapter(home=home, log=lambda *_: None)
    write("idle")
    [s] = a.sessions()
    assert (s.write_s, s.tasks) == (None, None)
    write("busy")
    [s] = a.sessions()
    assert 49 <= s.write_s < 60  # the subagent wrote after the main transcript's last line
    assert s.tasks is None  # no /proc here: unknown, never "none"


# ---------- the hub ----------

H = 3600


def task_beat(hub, b, key, silent_s, activity="shell", background=True, write_s=None,
              extra=None, tasks=None):
    """One heartbeat: `key` with one background task silent `silent_s` (None: no task); the
    session itself silent as long as its task unless `write_s` says otherwise."""
    s = {"key": key, "tool": "claude-code", "project": "proj-a", "cwd": "/w/proj-a",
         "activity": activity, "background": background,
         "write_s": (silent_s or 0) if write_s is None else write_s,
         "tasks": tasks if tasks is not None else [] if silent_s is None else
         [{"id": "bq1", "silent_s": silent_s, "command": "while pgrep -f x; do sleep 10; done"}]}
    other = {"key": "claude:other", "tool": "claude-code", "project": "proj-a",
             "cwd": "/w/proj-a", "activity": "idle"}
    assert heartbeat(hub, b.token, "box-1", [s, other], extra=extra)[0] == 200


def hub_sent(hub):
    with hub.store.lock:
        return [(r["to_key"], r["text"]) for r in hub.store.db.execute(
            "SELECT to_key, text FROM messages WHERE sender_key=? ORDER BY id", (HUB_KEY,))
            if r["text"].startswith(("kaptyorka: your background task", "Session ",
                                     "kaptyorka: this session", "stuck session:"))]


def setup(hub):
    hub.store.project_register("proj-a", by="test")
    a_id, _ = member(hub, "alice")
    mgr, me = manager_online(hub)
    return bridge(hub, a_id), mgr, me


def step(hub, b, mgr, me, key, silent, **kw):
    """A heartbeat, the manager still online, one tick: what the hub sent in that tick."""
    n = len(hub_sent(hub))
    task_beat(hub, b, key, silent, **kw)
    run(call(mgr, me, "inbox"))
    hub.tick()
    return hub_sent(hub)[n:]


def test_first_sight_of_an_old_silence_asks_only_and_stages_follow_the_real_wake(hub, clock):
    b, mgr, me = setup(hub)
    key = "claude:abcdef12-0000"
    silent = 283 * H  # a task silent for 12 days when the hub first looks
    sent = step(hub, b, mgr, me, key, silent)
    assert [k for k, _ in sent] == [key]  # the question only: never two stages in one tick
    assert sent[0][1].startswith("kaptyorka: your background task bq1 (`while pgrep -f x; do"
                                 " sleep 10; done`) has printed nothing for 283 h.")
    assert "If it is meant to run (a server, a watcher), just say so in one line; you won't be" \
        " asked about this task again." in sent[0][1]
    for _ in range(5):  # nothing more until half an hour after the question
        clock[0] += 300
        silent += 300
        assert step(hub, b, mgr, me, key, silent) == []
    clock[0] += 300
    silent += 300
    sent = step(hub, b, mgr, me, key, silent)
    assert [k for k, _ in sent] == ["claude:other", "k:mgr"]  # her other session, the manager
    assert "has not answered the hub's question 30 min ago" in sent[0][1]
    assert "It was asked to check 30 min ago and has not answered" in sent[1][1]
    assert "is stuck:" in sent[1][1] and "283 h" in sent[1][1]
    clock[0] += stuck.AGAIN_S - 60
    silent += stuck.AGAIN_S - 60
    assert step(hub, b, mgr, me, key, silent) == []
    clock[0] += 60
    silent += 60
    sent = step(hub, b, mgr, me, key, silent)
    assert [k for k, _ in sent] == ["k:mgr"] and "is still stuck:" in sent[0][1]
    assert "asked to check 1 h ago" in sent[0][1]
    for _ in range(4):
        clock[0] += H
        silent += H
        assert step(hub, b, mgr, me, key, silent) == []  # every stage once


def test_a_hub_restart_mid_episode_sends_nothing_again(hub, clock):
    from kaptyorka.hub import build_app
    from test_hub_manager import URL
    b, mgr, me = setup(hub)
    key = "claude:abcdef12-0000"
    silent = 2 * H
    step(hub, b, mgr, me, key, silent)
    woke = hub.store.agent(key).stuck_ep["woke"]
    restarted = build_app(hub.store, manager_url=URL).state.tick
    clock[0] += 60
    task_beat(hub, b, key, silent + 60)
    run(call(mgr, me, "inbox"))
    n = len(hub_sent(hub))
    restarted()
    assert len(hub_sent(hub)) == n  # the question is not asked again
    assert hub.store.agent(key).stuck_ep["woke"] == woke
    clock[0] += stuck.TELL_S
    task_beat(hub, b, key, silent + 60 + stuck.TELL_S)
    run(call(mgr, me, "inbox"))
    restarted()
    restarted()
    sent = hub_sent(hub)[n:]
    assert [k for k, _ in sent] == ["claude:other", "k:mgr"]  # the next stage, once, on time
    assert "has not answered the hub's question 31 min ago" in sent[0][1]


def test_a_session_that_answers_is_never_asked_about_that_task_again(hub, clock):
    b, mgr, me = setup(hub)
    key = "claude:abcdef12-0000"
    silent = 5 * H
    assert len(step(hub, b, mgr, me, key, silent)) == 1  # asked
    clock[0] += 120
    silent += 120
    # it ran a turn: "that is the dev server, meant to run"
    assert step(hub, b, mgr, me, key, silent, write_s=30) == []
    a = hub.store.agent(key)
    assert a.stuck_acked == ("bq1",) and a.stuck_ep is None
    assert agent_row(a, clock[0])["stuck"] is None  # the page shows "Background task" only
    for _ in range(12):  # it sleeps again, its server silent for six more hours: no nagging
        clock[0] += 1800
        silent += 1800
        assert step(hub, b, mgr, me, key, silent) == []
    clock[0] += 30
    assert step(hub, b, mgr, me, key, 0) == []  # it printed, then a new long silence
    clock[0] += 3 * H
    assert step(hub, b, mgr, me, key, 3 * H) == []  # still the same task: never again
    # another task of the same session, silent: that one is asked about
    other = [{"id": "bq1", "silent_s": 3 * H + 10, "command": "server"},
             {"id": "bz9", "silent_s": stuck.WAKE_S + 5, "command": "make wait"}]
    clock[0] += 10
    sent = step(hub, b, mgr, me, key, 3 * H + 10, tasks=other)
    assert len(sent) == 1 and sent[0][1].startswith("kaptyorka: your background task bz9")


def test_an_ack_ends_with_its_task(hub, clock):
    b, mgr, me = setup(hub)
    key = "claude:abcdef12-0000"
    step(hub, b, mgr, me, key, 2 * H)
    clock[0] += 60
    step(hub, b, mgr, me, key, 2 * H + 60, write_s=10)
    assert hub.store.agent(key).stuck_acked == ("bq1",)
    clock[0] += 10
    step(hub, b, mgr, me, key, None, write_s=0)  # the task ended
    assert hub.store.agent(key).stuck_acked == ()


def test_late_heartbeats_keep_one_episode(hub, clock):
    """The bridge's measure lags the hub's clock by however late its heartbeat lands: still
    the same silence, asked once."""
    b, mgr, me = setup(hub)
    key = "claude:abcdef12-0000"
    silent = stuck.WAKE_S + 5
    step(hub, b, mgr, me, key, silent)
    for late in (10, 0, 10, 3, 10, 40):
        clock[0] += 20
        silent += 20
        step(hub, b, mgr, me, key, silent - late, write_s=silent)
    assert len(hub_sent(hub)) == 1


def test_hub_judges_nothing_for_idle_old_or_overnight_sessions(hub, clock):
    hub.store.project_register("proj-a", by="test")
    a_id, _ = member(hub, "alice")
    b = bridge(hub, a_id)
    # idle, however old its task list claims to be: never stuck
    task_beat(hub, b, "claude:i1", 99999, activity="idle")
    # an older bridge: no measures at all
    assert heartbeat(hub, b.token, "box-1", [{"key": "claude:o1", "tool": "claude-code",
                                             "project": "proj-a", "activity": "busy"}])[0] == 200
    hub.tick()
    assert hub_sent(hub) == []
    assert agent_row(hub.store.agent("claude:o1"), clock[0])["stuck"] is None
    # overnight mode: its own bridge wakes it, so the hub does not
    task_beat(hub, b, "claude:n1", stuck.WAKE_S + 5, extra={"overnight": ["claude:n1"]})
    hub.tick()
    assert hub_sent(hub) == []


def test_busy_without_writes_first_sight_asks_only_and_a_write_ends_it(hub, clock):
    b, mgr, me = setup(hub)
    key = "claude:b1"
    sent = step(hub, b, mgr, me, key, None, activity="busy", background=False,
                write_s=45 * 60)
    assert [k for k, _ in sent] == [key]
    assert sent[0][1].startswith("kaptyorka: this session has been busy for 45 min")
    clock[0] += stuck.TELL_S  # the delivered question is no write: the silence stays
    sent = step(hub, b, mgr, me, key, None, activity="busy", background=False,
                write_s=45 * 60 + stuck.TELL_S)
    assert [k for k, _ in sent] == ["claude:other", "k:mgr"] and "press Esc" in sent[0][1]
    clock[0] += 60
    assert step(hub, b, mgr, me, key, None, activity="busy", background=False,
                write_s=5) == []  # it wrote: the episode is over
    assert hub.store.agent(key).stuck_ep is None


def test_a_worker_s_boss_hears_it(hub, clock):
    from test_hub_workers import BOSS, start_worker
    hub.store.project_register("proj-a", by="test")
    alice = member(hub, "alice")
    b = start_worker(hub, alice)
    key, boss = "claude:w1", BOSS["key"]
    assert hub.ws.boss_key("proj-a-w1") == boss

    def beat(silent):
        s = {"key": key, "tool": "claude-code", "project": "proj-a", "cwd": "/w/proj-a",
             "activity": "shell", "background": True, "write_s": silent,
             "tasks": [{"id": "bq1", "silent_s": silent, "command": "x"}]}
        assert heartbeat(hub, b.token, "box-1", [s])[0] == 200
        run(call(alice[1], BOSS, "inbox"))  # the boss session stays online
        hub.tick()
    clock[0] += 1
    beat(H)
    clock[0] += stuck.TELL_S
    beat(H + stuck.TELL_S)
    told = [k for k, t in hub_sent(hub) if t.startswith("Session ")]
    assert told == [boss]  # alice's one other session is the boss: told once


def test_a_task_that_prints_now_and_then_starts_a_new_silence(tmp_path):
    t = [1000.0]
    store = Store(tmp_path / "h.db", clock=lambda: t[0])
    person(store, "alice")

    def out(silent):
        store.heartbeat("box-1", [{"key": "k1", "tool": "claude-code", "project": "p",
                                   "activity": "shell",
                                   "tasks": [{"id": "b1", "silent_s": silent, "command": "c"}]}])
        return store.agent("k1").tasks[0]["out"]
    assert out(0) == 1000.0
    t[0] += 30
    assert out(0) == 1000.0  # printed 30 s later: within the tolerance, the start stays
    t[0] += 40
    assert out(0) == 1070.0  # it keeps printing: the start moves on, never stays stuck
    store.close()


def test_store_keeps_one_start_per_silence(tmp_path):
    t = [1000.0]
    store = Store(tmp_path / "h.db", clock=lambda: t[0])
    pid, _ = person(store, "alice")

    def beat(**s):
        store.heartbeat("box-1", [{"key": "k1", "tool": "claude-code", "project": "p",
                                   "activity": "shell", **s}])
        return store.agent("k1")
    a = beat(write_s=10, tasks=[{"id": "b1", "silent_s": 100, "command": "c"}])
    assert (a.wrote, a.tasks) == (990.0, [{"id": "b1", "out": 900.0, "command": "c"}])
    t[0] += 10
    a = beat(write_s=21.5, tasks=[{"id": "b1", "silent_s": 111, "command": "c"}])
    assert (a.wrote, a.tasks[0]["out"]) == (990.0, 900.0)  # jitter: the same start
    t[0] += 60
    a = beat(write_s=0, tasks=[{"id": "b1", "silent_s": 0, "command": "c"}])
    assert (a.wrote, a.tasks[0]["out"]) == (1070.0, 1070.0)  # it wrote and printed
    a = beat(write_s="x", tasks=[{"id": "bad id!", "silent_s": 1}, {"id": "b2", "silent_s": -1}])
    assert (a.wrote, a.tasks) == (None, [])
    a = beat()
    assert (a.wrote, a.tasks) == (None, None)
    store.close()


# ---------- overnight mode ----------

def test_overnight_watch_wakes_once_per_silence(tmp_path, monkeypatch):
    monkeypatch.setenv("KAPTYORKA_HOME", str(tmp_path))
    st = overnight.turn_on("s1", "proj-a", "/w/proj-a", False, "claude:s1", "box-1", "t", "")
    now = st["started"] + 100
    sent = []
    ep = {"kind": "task", "age": stuck.WAKE_S + 1.0,
          "task": {"id": "bq1", "silent_s": stuck.WAKE_S + 1.0, "command": "sleep"}}

    def go(e, t):
        overnight.watch(lambda sid: True, lambda sid, text: sent.append(text), None, None,
                        now=t, silent=lambda sid: e)
    go(None, now)
    assert sent == []
    go(ep, now)
    go({**ep, "age": ep["age"] + 60}, now + 60)  # the same silence
    assert len(sent) == 1 and sent[0].startswith("kaptyorka: your background task bq1")
    go({**ep, "age": stuck.WAKE_S + 2.0}, now + 7200)  # it printed since: a new silence
    assert len(sent) == 2
    assert any("woke it: 30 min silent (background task bq1)" in n["text"]
               for n in overnight.notes("s1"))
    assert overnight.from_person("s1", {"prompt": sent[0]}) is False
