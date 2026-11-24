"""compat.py on fake installs: the registry, each cheap probe on a sound and a changed Claude
Code, the bridge's cached facts, the hub's machine warning and its once-per-version news, the
doctor line; and that each feature whose internal broke degrades instead of failing."""
import json
import os
import shutil
import socket
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from helpers import person, write_session_file
from kaptyorka import compat, doctor, stuck
from kaptyorka.adapters.base import DeliveryError
from kaptyorka.adapters.claude import CLAUDE_CODE_TESTED, ClaudeAdapter
from kaptyorka.store import Store
from kaptyorka.workers import WorkerStore

HELP = """Usage: claude [options] [command] [prompt]
Options:
  --bg, --background                    Start the session in the background
  -n, --name <name>                     Set a display name for this session
  -r, --resume [value]                  Resume a conversation by session ID or
  --settings <file-or-json>             Settings file or JSON string to load
  --no-chrome                           Turn off Claude in Chrome
Commands:
  agents                                List background sessions
  rm <id>                               If it's safe, remove a background session
  stop|kill <id>                        Stop a background session.
"""


class Run:
    def __init__(self, answers):
        self.answers, self.calls = answers, []

    def __call__(self, argv, **kw):
        self.calls.append(argv)
        for k, (code, out) in self.answers.items():
            if k in " ".join(argv):
                return SimpleNamespace(returncode=code, stdout=out, stderr="")
        return SimpleNamespace(returncode=127, stdout="", stderr="not found")


SOUND = Run({"--version": (0, "2.1.331 (Claude Code)\n"), "--help": (0, HELP),
             "auth status": (1, '{"loggedIn": false, "authMethod": "none"}\n')})


@pytest.fixture
def sock():
    d = Path(tempfile.mkdtemp(prefix="kc", dir="/tmp"))  # short: Unix socket path limits
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.bind(str(d / "in.sock"))
    yield d / "in.sock"
    s.close()
    shutil.rmtree(d, ignore_errors=True)


def notes(tmp, *names):
    """Claude Code's child notes: <tmp>/claude-<uid>/<folder>/<starter id>/started/<name>."""
    d = tmp / f"claude-{os.getuid()}" / "-w-proj-a" / "11111111-2222-3333-4444-555555555555" / "started"
    d.mkdir(parents=True, exist_ok=True)
    for n in names:
        (d / n).touch()
    return tmp


CHILD = "66666666-7777-8888-9999-000000000000"


def sound_install(home, sock, status="busy"):
    """A live session (this test's own pid) as Claude Code 2.1.331 writes it, its transcript
    with a title and timestamps."""
    cwd = "/w/proj-a"
    write_session_file(home, os.getpid(), cwd=cwd, status=status, version="2.1.331", sock=sock,
                       peer_token="P", name="proj-a-x1", name_source="derived",
                       extra={"procStart": "1", "kind": "interactive",
                              "startedAt": (time.time() - 600) * 1000})
    t = home / ".claude" / "projects" / "-w-proj-a"
    t.mkdir(parents=True)
    (t / f"sid-{os.getpid()}.jsonl").write_text(
        json.dumps({"type": "derived-from", "sessions": [{"id": "parent-1", "via": "launch"}]}) + "\n"
        + json.dumps({"type": "user", "timestamp": "2026-11-24T10:00:00.000Z"}) + "\n"
        + json.dumps({"type": "ai-title", "aiTitle": "Fix the login page"}) + "\n")


def broken_ids(res):
    return sorted(i for i, (s, _) in res.items() if s == compat.BROKEN)


def test_every_dependency_has_a_probe_or_says_it_has_none():
    assert len({d.id for d in compat.DEPS}) == len(compat.DEPS)
    for d in compat.DEPS:
        assert compat.ID.match(d.id) and d.tool in compat.FACT
        assert (d.id in compat.PROBES) == d.probed, d.id
        if d.probed:
            assert d.broken and d.off and d.feature, d.id
    # every tool kaptyorka reads internals of is listed
    assert {d.tool for d in compat.DEPS} == {"claude-code", "opencode", "codex"}


def test_a_sound_claude_install_passes_every_cheap_probe(home, sock):
    sound_install(home, sock)
    tmp = notes(home / "tmp", f"unknown-{CHILD}", f"{CHILD}.0bdd790c2280a985")
    res = compat.cheap(["claude-code"], compat.Ctx(home, SOUND, tmp=tmp))
    assert broken_ids(res) == []
    assert {i for i, (s, _) in res.items() if s == compat.UNKNOWN} == {"claude.tasks"}  # no shell session
    assert "claude.bg_launch" not in res  # starts a session: only the slow test


def test_no_live_session_is_no_evidence(home):
    res = compat.cheap(["claude-code"], compat.Ctx(home, SOUND, tmp=home / "tmp"))
    assert broken_ids(res) == []
    assert res["claude.session_file"][0] == compat.UNKNOWN


def test_a_changed_claude_install_names_each_broken_internal(home, tmp_path):
    """A Claude Code that renamed what kaptyorka reads: each probe names its own break."""
    write_session_file(home, os.getpid(), status="working", name="x", name_source="auto",
                       extra={"startedAt": (time.time() - 600) * 1000})  # no kind, no socket, no procStart
    run = Run({"--version": (0, "Claude Code version 3\n"), "--help": (0, "Usage: claude\n"),
               "auth status": (0, "Logged in\n")})
    res = compat.cheap(["claude-code"], compat.Ctx(home, run, tmp=notes(tmp_path, "child-1")))
    assert broken_ids(res) == ["claude.auth", "claude.child_notes", "claude.cli", "claude.inbox", "claude.kind",
                               "claude.name", "claude.session_file", "claude.status", "claude.transcript",
                               "claude.version"]
    lines = compat.report(res)
    assert "claude.cli: `claude --help` no longer lists --bg, stop or rm (Claude Code workers fail to" \
           " start or stop): missing --bg, -n, --name, --settings, --resume, --no-chrome, stop, rm, agents" in lines[2]


@pytest.mark.skipif(not Path("/proc/self/stat").exists(), reason="needs /proc")
def test_a_shell_session_without_a_task_child_is_broken(home, sock):
    sound_install(home, sock, status="shell")
    res = compat.cheap(["claude-code"], compat.Ctx(home, SOUND))
    assert res["claude.tasks"][0] == compat.BROKEN


def test_a_probe_that_raises_is_unknown_never_broken(monkeypatch, home):
    monkeypatch.setitem(compat.PROBES, "claude.kind", lambda ctx: 1 / 0)
    assert compat.probe(compat.BY_ID["claude.kind"], compat.Ctx(home, SOUND))[0] == compat.UNKNOWN


def test_bridge_facts_are_cached_and_keep_a_break_without_new_evidence(home, sock, monkeypatch):
    monkeypatch.setattr(compat, "_cache", {})
    write_session_file(home, os.getpid(), status="busy", extra={"kind": "interactive", "procStart": "1"})  # no socket
    tools = {"claude-code": "2.1.331 (Claude Code)"}
    run = Run(dict(SOUND.answers))
    assert compat.facts(tools, compat.Ctx(home, run, tmp=home), now=0) == {"claude_compat": ["claude.inbox"]}
    n = len(run.calls)
    compat.facts(tools, compat.Ctx(home, run, tmp=home), now=100)
    assert len(run.calls) == n  # cached
    assert not any("--version" in a for a in run.calls)  # the facts' own --version line is used
    (home / ".claude" / "sessions" / f"{os.getpid()}.json").unlink()  # the session ends
    assert compat.facts(tools, compat.Ctx(home, run, tmp=home), now=compat.TTL_S + 1) == {"claude_compat": ["claude.inbox"]}
    sound_install(home, sock)  # a session with an inbox again: fixed
    assert compat.facts(tools, compat.Ctx(home, run, tmp=home), now=2 * compat.TTL_S + 2) == {"claude_compat": "ok"}
    again = Run(dict(SOUND.answers))
    compat.facts({"claude-code": "2.1.332 (Claude Code)"}, compat.Ctx(home, again, tmp=home), now=2 * compat.TTL_S + 3)
    assert again.calls  # a new version is probed at once


def test_the_hub_keeps_only_ids_and_warns_the_admin(tmp_path):
    s = Store(tmp_path / "hub.db")
    ws = WorkerStore(s)
    _, tok = person(s, "alice", "admin")
    p = s.authenticate(tok)
    s.heartbeat("box-1", [], principal=p, facts={"host": "box-1", "os_user": "u"})
    with s.lock:
        s.db.execute("UPDATE devices SET host='box-1', os_user='u' WHERE id=?", (p.device_id,))
    ws.facts_seen(p.device_id, {
        "host": "box-1", "os_user": "u", "tools": {"claude-code": "2.1.340 (Claude Code)"},
        "claude_compat": ["claude.tasks", "x" * 300, "<b>"], "codex_compat": "ok", "opencode_compat": 5}, {})
    with s.lock:
        f = json.loads(s.db.execute("SELECT facts FROM device_facts").fetchone()["facts"])
    assert f["claude_compat"] == ["claude.tasks"] and f["codex_compat"] == "ok" and "opencode_compat" not in f
    w = [x for x in ws.warnings(time.time(), "0.0.1") if "internals" in x["short"]]
    assert [x["text"] for x in w] == [
        "Claude Code 2.1.340 on u@box-1: background task files not found (stuck check off for background"
        " tasks). Run kaptyorka's compatibility test (see CLAUDE.md)."]
    assert w[0]["short"] == "Claude Code internals changed"
    s.close()


def _claude_on(s, name, version):
    _, tok = person(s, name)
    WorkerStore(s).facts_seen(s.authenticate(tok).device_id, {
        "host": f"box-{name}", "os_user": name, "tools": {"claude-code": f"{version} (Claude Code)"}}, {})


def test_a_new_claude_version_is_told_once_with_its_machine_count(tmp_path):
    s = Store(tmp_path / "hub.db")
    _claude_on(s, "alice", "2.1.340")
    _claude_on(s, "bob", CLAUDE_CODE_TESTED)  # the tested one: nothing
    assert compat.version_news(s, 1000) == []  # just seen: wait for the others
    _claude_on(s, "carol", "2.1.340")
    _claude_on(s, "dave", "2.1.340")
    told = compat.version_news(s, 1000 + compat.NEWS_WAIT_S)
    assert told == [f"Claude Code 2.1.340 appeared on 3 machines; kaptyorka is tested with {CLAUDE_CODE_TESTED};"
                    " run the compatibility test (tests/test_claude_compat_slow.py) before relying on push,"
                    " workers and the stuck check there"]
    assert compat.version_news(s, 99999) == []  # once, also after a hub restart (settings row)
    _claude_on(s, "erin", "2.1.341")
    assert compat.version_news(s, 99999) == []
    assert "2.1.341 appeared on 1 machine;" in compat.version_news(s, 99999 + compat.NEWS_WAIT_S)[0]
    s.close()


def test_doctor_shows_broken_internals_and_notes_what_it_could_not_check(home):
    write_session_file(home, os.getpid(), status="busy", extra={"kind": "interactive", "procStart": "1"})
    c = doctor._internals("claude-code", home, SOUND)
    assert not c.ok and c.detail == "inbox socket not found (push off: sessions read the hub on their next turn)"
    assert "tasks" in c.info and "transcript" in c.info
    c = doctor._internals("codex", home, Run({"--version": (0, "codex-cli 0.166.0\n")}))
    assert c.ok and c.info == "not checked now: app_server, thread_status"  # no daemon running


# --- each feature degrades on its own when its internal breaks ---------------------------

def test_degrade_session_file_without_ids_is_skipped(home):
    (home / ".claude" / "sessions").mkdir(parents=True)
    (home / ".claude" / "sessions" / f"{os.getpid()}.json").write_text(json.dumps({"pid": os.getpid(), "id": "x"}))
    assert ClaudeAdapter(home, log=lambda *_: None).sessions() == []  # not listed, no crash


def test_degrade_no_inbox_socket_falls_back_to_pull(home):
    write_session_file(home, os.getpid(), extra={"inboxSocket": "/tmp/x"})  # a renamed field
    a = ClaudeAdapter(home, log=lambda *_: None)
    key = f"claude:sid-{os.getpid()}"
    assert [s.key for s in a.sessions()] == [key]  # still listed
    with pytest.raises(DeliveryError, match="reads the hub on its next turn"):
        a.deliver(key, "hi")  # the hub keeps the message for the session's next turn


def test_degrade_no_transcript_turns_the_busy_check_off(home):
    write_session_file(home, os.getpid(), status="busy")
    s = ClaudeAdapter(home, log=lambda *_: None).sessions()[0]
    assert s.title == "" and s.write_s is None
    assert stuck.find("busy", s.write_s, s.tasks) is None  # never a false "stuck"


def test_degrade_no_task_files_turns_the_task_check_off(home):
    write_session_file(home, os.getpid(), status="shell")
    s = ClaudeAdapter(home, log=lambda *_: None).sessions()[0]
    assert not s.tasks and stuck.find("shell", s.write_s, s.tasks) is None


def test_degrade_unknown_status_is_never_stuck():
    assert stuck.find("working", 10_000, None) is None


def test_degrade_no_name_source_shows_the_name(home):
    write_session_file(home, os.getpid(), name="proj-a-x1", name_source=None)
    assert ClaudeAdapter(home, log=lambda *_: None).sessions()[0].label == "proj-a-x1"
