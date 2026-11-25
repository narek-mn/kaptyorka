import json
import os
import stat

import pytest

from kaptyorka import hook, overnight, paths
from kaptyorka.adapters.base import Session
from kaptyorka.bridge import Bridge
from kaptyorka import bridge as bridge_mod


@pytest.fixture
def ctx(tmp_path):
    """A home folder under the test's temp folder (resolved: macOS keeps it under /private)."""
    home = (tmp_path / "home" / "alice").resolve()
    (home / "proj").mkdir(parents=True)
    (home / "recordings").mkdir()
    (home / "link-to-rec").symlink_to(home / "recordings")
    return {"cwd": str(home / "proj"), "home": str(home), "allowed": [],
            "lists": {"hardware": ["motorctl", "/dev/ttyUSB0", str(home / "proj/scripts/home_axes.sh")],
                      "protected": ["~/recordings"], "shared": ["main", "develop"]},
            "own": [str(tmp_path / "k" / "overnight"), str(tmp_path / "k" / "overnight.json")],
            "branch_of": lambda cwd: "feature"}


BLOCKED = [
    ("sudo apt install x", "admin"),
    ("make test && sudo make install", "admin"),
    ("echo hi; su -c ls", "admin"),
    ("ls | pkexec rm x", "admin"),
    ("doas reboot", "admin"),
    ("/usr/bin/sudo ls", "admin"),
    ("env FOO=1 nice -n 5 sudo ls", "admin"),
    ("timeout 30 sudo ls", "admin"),
    ("echo $(sudo cat /etc/shadow)", "admin"),
    ("bash -c 'sudo ls'", "admin"),
    ("find . -name x -exec sudo rm {} \\;", "admin"),
    ("ls | xargs sudo rm", "admin"),
    ("git push origin +main", "git"),
    ("git push -f origin main", "git"),
    ("git push --force-with-lease origin develop", "git"),
    ("git push origin :main", "git"),
    ("git push --delete origin main", "git"),
    ("git -C . push --force origin HEAD:main", "git"),
    ("rm -rf ~", "delete"),
    ("rm -rf ~/*", "delete"),
    ("rm -rf /", "delete"),
    ("rm -rf /usr/lib/foo", "delete"),
    ("rm -rf /etc", "delete"),
    ("cd .. && cd .. && rm -rf alice", "delete"),
    ("rm -rf /home/bob/x", "delete"),
    ("rm -rf ~/recordings/old", "delete"),
    ("rm -rf ~/link-to-rec/old", "delete"),
    ("cp a.wav ~/recordings/b.wav", "delete"),
    ("echo x > ~/recordings/notes.txt", "delete"),
    ("mv ~/recordings/a /tmp/a", "delete"),
    ("find ~ -name '*.wav' -delete", "delete"),
    ("motorctl --home", "hardware"),
    ("ros2 run arm motorctl", "hardware"),
    ("python3 scripts/home_axes.sh", "hardware"),
    ("./scripts/home_axes.sh --all", "hardware"),
    ("echo 1 > /dev/ttyUSB0", "hardware"),
    ("stty -F /dev/ttyUSB0 115200", "hardware"),
    ('eval "$CMD"', "unclear"),
    ('bash -c "$X"', "unclear"),
    ("find . -name '*.pyc' | xargs rm", "unclear"),
    ("$EDITOR notes.txt", "unclear"),
    ('rm -rf "$DIR"', "unclear"),
    ("echo 'open quote", "unclear"),
    ("kaptyorka overnight off", "rules"),
    ("kaptyorka overnight note done x && motorctl --home", "hardware"),
    ("kaptyorka overnight note done \"$(sudo id)\"", "admin"),
    ("kaptyorka hub overnight off", "rules"),
]

ALLOWED = [
    "ls -la",
    "make test && pytest -q",
    "rm -rf ../x",
    "rm -rf build node_modules",
    "rm -rf /tmp/build",
    "rm ~/old.log",
    "cd .. && rm -rf proj/build",
    "git push origin feature",
    "git push -f origin feature",
    "git push",
    "git reset --hard",
    "git commit -m \"$(cat <<'EOF'\nDon't break sudo (really\nEOF\n)\"",
    "echo hi 2>&1 | tee out.log",
    "echo x > /etc-like-name.txt",
    "find . -name '*.pyc' -delete",
    "cp a ~/backups/b",
    "vim ~/.bashrc",
    "kaptyorka overnight note done 'tests pass'",
    "/opt/k/kaptyorka overnight note skipped 'motorctl --home: blocked; sudo needed too'",
    "ls # sudo in a comment",
    "echo sudo",
]


@pytest.mark.parametrize("cmd,cls", BLOCKED)
def test_blocked(ctx, cmd, cls):
    assert overnight.check(cmd, ctx)[0] == cls


@pytest.mark.parametrize("cmd", ALLOWED)
def test_allowed(ctx, cmd):
    assert overnight.check(cmd, ctx) == ("", "")


def test_on_a_shared_branch_history_rewrite_is_blocked(ctx):
    c = {**ctx, "branch_of": lambda cwd: "main"}
    for cmd in ("git push --force", "git rebase -i HEAD~3", "git commit --amend",
                "git reset --hard origin/main", "git filter-repo --path x"):
        assert overnight.check(cmd, c)[0] == "git", cmd
    assert overnight.check("git rebase --abort", c) == ("", "")
    assert overnight.check("git push origin main", c) == ("", "")
    assert overnight.check("git push -f", {**c, "lists": {**c["lists"], "shared": ["release"]}}) \
        == ("", "")


def test_an_allowed_class_is_let_through_only_that_class(ctx):
    c = {**ctx, "allowed": ["hardware"]}
    assert overnight.check("motorctl --home", c) == ("", "")
    assert overnight.check("sudo motorctl", c)[0] == "admin"
    assert overnight.check("sudo ls", {**ctx, "allowed": ["admin"]}) == ("", "")


def test_unclear_needs_every_class_allowed(ctx):
    assert overnight.check('eval "$X"', {**ctx, "allowed": ["admin", "git", "hardware"]})[0] == "unclear"
    assert overnight.check('eval "$X"', {**ctx, "allowed": list(overnight.CLASSES)}) == ("", "")


def test_write_and_edit_tools(ctx):
    home = ctx["home"]
    assert overnight.check_tool("Write", {"file_path": f"{home}/recordings/a.txt"}, ctx)[0] == "delete"
    assert overnight.check_tool("Edit", {"file_path": f"{home}/link-to-rec/a.txt"}, ctx)[0] == "delete"
    assert overnight.check_tool("Write", {"file_path": "/dev/ttyUSB0"}, ctx)[0] == "hardware"
    assert overnight.check_tool("Write", {"file_path": ctx["own"][1]}, ctx)[0] == "rules"
    assert overnight.check_tool("Write", {"file_path": f"{home}/elsewhere/a.txt"}, ctx) == ("", "")
    assert overnight.check_tool("Edit", {"file_path": "/etc/hosts"}, ctx) == ("", "")  # sudo guards it
    assert overnight.check_tool("Read", {"file_path": f"{home}/recordings/a"}, ctx) == ("", "")


def test_nothing_is_built_in(home):
    assert overnight.lists("proj-a") == {"hardware": [], "protected": [], "shared": ["main", "develop"]}


def test_ask_rules_match(tmp_path):
    rules = ["Bash(npm publish:*)", "Write(/secret/**)", "mcp__gh"]
    assert overnight.asks("Bash", {"command": "npm publish --tag x"}, rules) == "Bash(npm publish:*)"
    assert overnight.asks("Bash", {"command": "make && npm publish"}, rules) == "Bash(npm publish:*)"
    assert overnight.asks("Bash", {"command": "npm test"}, rules) == ""
    assert overnight.asks("Write", {"file_path": "/secret/a/b"}, rules) == "Write(/secret/**)"
    assert overnight.asks("mcp__gh__create_pr", {}, rules) == "mcp__gh"
    assert overnight.asks("Bash", {"command": "ls"}, ["Bash"]) == "Bash"


# ---------- the hook ----------

def run_hook(event, data, capsys):
    hook.main("claude-code", event, json.dumps(data))
    out = capsys.readouterr().out.strip()
    return json.loads(out) if out else None


def on(sid="s1", project="proj-a", cwd="/w/proj-a", **kw):
    st = overnight.turn_on(sid, project, cwd, False, f"claude:{sid}", "box-1", "the task", "")
    if kw:
        overnight.save(sid, {**st, **kw})
    return overnight.state(sid)


def test_prompt_turns_it_on_with_the_rules_and_one_line(home, capsys):
    got = run_hook("prompt", {"session_id": "s1", "cwd": "/w/proj-a",
                              "prompt": "Overnight task: move the parser. You may move the motors."},
                   capsys)
    assert got["systemMessage"] == ("kaptyorka: overnight mode is on for this task. To stop it:"
                                    " /hub overnight off")
    ctxt = got["hookSpecificOutput"]["additionalContext"]
    assert "Overnight rules for this task:" in ctxt and "Plus the team and project rules" in ctxt
    assert overnight.state("s1")["allowed"] == ["hardware"]


@pytest.mark.parametrize("prompt", ["run this overnight please", "do it while I sleep",
                                    "finish by the morning", "/hub overnight"])
def test_similar_phrases_turn_it_on(home, capsys, prompt):
    run_hook("prompt", {"session_id": "s1", "cwd": "/w/x", "prompt": prompt}, capsys)
    assert overnight.state("s1") is not None


def test_plain_prompt_leaves_it_off(home, capsys):
    assert run_hook("prompt", {"session_id": "s1", "cwd": "/w/x", "prompt": "fix the test"},
                    capsys) is None
    assert overnight.state("s1") is None


def test_plan_mode_is_warned(home):
    st = on(mode="plan")
    assert "plan mode" in overnight.rules_text(st)


def test_reply_changes_the_rules_and_is_repeated(home, capsys):
    on()
    got = run_hook("prompt", {"session_id": "s1", "cwd": "/w/proj-a",
                              "prompt": "you may move the motors. hardware: motorctl, /dev/ttyUSB0"},
                   capsys)
    assert got["systemMessage"].splitlines() == [
        "Overnight: commands that move real hardware are allowed for this task.",
        "Overnight: motorctl, /dev/ttyUSB0 added to the hardware list of project proj-a."]
    assert overnight.state("s1")["allowed"] == ["hardware"]
    assert overnight.lists("proj-a")["hardware"] == ["motorctl", "/dev/ttyUSB0"]
    assert stat.S_IMODE(os.stat(paths.home() / "overnight.json").st_mode) == 0o600
    run_hook("prompt", {"session_id": "s1", "cwd": "/w/proj-a", "prompt": "ok"}, capsys)
    st = overnight.state("s1")
    assert st is not None and st["setup"] is False


def test_a_person_typing_turns_it_off_with_the_report(home, capsys):
    on(setup=False)
    got = run_hook("prompt", {"session_id": "s1", "cwd": "/w/proj-a", "prompt": "how did it go?"},
                   capsys)
    assert overnight.state("s1") is None
    assert got["systemMessage"].startswith("kaptyorka: overnight mode is off: you are back.")
    assert "Morning report" in got["systemMessage"]


def test_a_hub_message_never_counts_as_a_person(home, capsys):
    on(setup=False)
    text = "Kaptyorka message from box-2/claude-code/proj-a:\n\nall green on box-2"
    overnight.delivered("s1", text)
    wrapped = f"Another Claude session sent a message:\n{text}\n\nThis came from another session"
    assert run_hook("prompt", {"session_id": "s1", "cwd": "/w/proj-a", "prompt": wrapped},
                    capsys) is None
    assert run_hook("prompt", {"session_id": "s1", "cwd": "/w/proj-a", "prompt": text},
                    capsys) is None
    assert overnight.state("s1") is not None


def test_a_subagent_report_or_task_notice_never_turns_it_on(home, capsys):
    # a real hand-back body: Claude Code hands it to the session as a prompt before the
    # transcript marks it as a peer's, and its words may name overnight mode
    report = ("[Subagent hand-back] The text below is the final report of a subagent this session"
              " delegated to.\n  Never for a page boss, nor while overnight mode is on.")
    for prompt in (report, f'<agent-message from="a1">\n{report}\n</agent-message>',
                   "<task-notification>\n<summary>overnight run finished</summary>",
                   "<system-reminder>run this overnight</system-reminder>"):
        assert run_hook("prompt", {"session_id": "s9", "cwd": "/w/proj-a", "prompt": prompt},
                        capsys) is None
        assert overnight.state("s9") is None
    assert not overnight.triggered(report)  # and the words alone no longer count either


def test_transcript_origin_decides_when_written(home, tmp_path):
    t = tmp_path / "t.jsonl"
    t.write_text(json.dumps({"type": "user", "promptId": "p1", "origin": {"kind": "peer"}}) + "\n"
                 + json.dumps({"type": "user", "promptId": "p2", "origin": {"kind": "human"}}) + "\n")
    base = {"transcript_path": str(t), "prompt": "go on"}
    assert overnight.from_person("s1", {**base, "prompt_id": "p1"}) is False
    assert overnight.from_person("s1", {**base, "prompt_id": "p2"}) is True
    assert overnight.from_person("s1", {**base, "prompt_id": "p3"}) is True  # not written yet


def test_bridge_remembers_what_it_delivered(hub, home, monkeypatch):
    from kaptyorka import facts

    monkeypatch.setattr(facts, "_tool_version", lambda tool, run: "")
    claude = type("A", (), {"tool": "claude-code", "prefix": "claude", "restart_note": None,
                            "installed": lambda self: True,
                            "sessions": lambda self: [Session("claude:s1", "claude-code", "proj-a")],
                            "deliver": lambda self, key, text: got.append(text)})()
    got = []
    on()
    bridge = Bridge({"hub": hub.url, "token": hub.token, "machine": "box-2"}, [claude],
                    bridge_mod.log)
    bridge.heartbeat()
    from helpers import call, run
    run(call(hub, {"key": "claude:1", "machine": "pc", "tool": "claude-code", "project": "proj-a"},
             "send", to="box-2/claude-code/proj-a", text="hi from pc"))
    assert bridge.poll_once(wait=1) == 1
    assert overnight.from_person("s1", {"prompt": got[0]}) is False


def test_question_is_denied_with_the_morning_rule(home, capsys):
    on()
    got = run_hook("pretool", {"session_id": "s1", "cwd": "/w/proj-a",
                               "tool_name": "AskUserQuestion",
                               "tool_input": {"questions": [{"question": "Which one?"}]}}, capsys)
    out = got["hookSpecificOutput"]
    assert out["permissionDecision"] == "deny" and "No human until morning" in out[
        "permissionDecisionReason"]


def test_pretool_denies_blocked_and_says_nothing_otherwise(home, capsys):
    on()
    got = run_hook("pretool", {"session_id": "s1", "cwd": "/w/proj-a", "tool_name": "Bash",
                               "tool_input": {"command": "sudo ls"}}, capsys)
    assert got["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert run_hook("pretool", {"session_id": "s1", "cwd": "/w/proj-a", "tool_name": "Bash",
                                "tool_input": {"command": "ls"}}, capsys) is None  # never "allow"
    assert [n["kind"] for n in overnight.notes("s1")] == ["event", "skipped"]


def test_pretool_with_overnight_off_is_silent(home, capsys):
    assert run_hook("pretool", {"session_id": "s1", "tool_name": "Bash",
                                "tool_input": {"command": "sudo ls"}}, capsys) is None


def test_permission_inside_allows_outside_and_ask_rule_deny(home, capsys, monkeypatch):
    on()
    base = {"session_id": "s1", "cwd": "/w/proj-a", "tool_name": "Bash"}
    got = run_hook("permission", {**base, "tool_input": {"command": "npm test"}}, capsys)
    assert got["hookSpecificOutput"]["decision"] == {"behavior": "allow"}
    got = run_hook("permission", {**base, "tool_input": {"command": "sudo ls"}}, capsys)
    assert got["hookSpecificOutput"]["decision"]["behavior"] == "deny"
    monkeypatch.setattr(overnight, "ask_rules", lambda cwd: ["Bash(npm publish:*)"])
    got = run_hook("permission", {**base, "tool_input": {"command": "npm publish"}}, capsys)
    d = got["hookSpecificOutput"]["decision"]
    assert d["behavior"] == "deny" and "ask a person" in d["message"]


def test_ask_rules_read_from_settings_only(home, tmp_path):
    (home / ".claude").mkdir()
    (home / ".claude" / "settings.json").write_text(json.dumps(
        {"permissions": {"ask": ["Bash(git push:*)"], "deny": ["Bash(rm:*)"]}}))
    before = (home / ".claude" / "settings.json").read_text()
    assert overnight.ask_rules(str(tmp_path)) == ["Bash(git push:*)"]
    assert (home / ".claude" / "settings.json").read_text() == before


def test_stop_nudges_three_times_then_lets_it_stop(home, capsys):
    on()
    data = {"session_id": "s1", "cwd": "/w/proj-a", "last_assistant_message": "half done"}
    for _ in range(overnight.NUDGES):
        assert run_hook("stop", data, capsys)["decision"] == "block"
    assert run_hook("stop", data, capsys) is None
    run_hook("pretool", {"session_id": "s1", "tool_name": "Bash", "tool_input": {"command": "ls"}},
             capsys)  # it works again: the count starts over
    assert run_hook("stop", data, capsys)["decision"] == "block"


def test_stop_with_the_done_mark_finishes(home, capsys):
    on()
    overnight.note("s1", "done", "parser moved")
    assert run_hook("stop", {"session_id": "s1", "last_assistant_message":
                             f"all set. {overnight.DONE_MARK}"}, capsys) is None
    assert overnight.state("s1") is None
    reports = list((paths.home() / "overnight" / "reports").glob("*.md"))
    assert len(reports) == 1 and "parser moved" in reports[0].read_text()
    assert stat.S_IMODE(reports[0].stat().st_mode) == 0o600


def test_sixteen_hour_cap(home, capsys):
    st = on()
    overnight.save("s1", {**st, "started": st["started"] - overnight.CAP_S - 1})
    assert run_hook("pretool", {"session_id": "s1", "tool_name": "Bash",
                                "tool_input": {"command": "sudo ls"}}, capsys) is None
    assert overnight.state("s1") is None


def test_watch_resumes_twice_then_gives_up(home):
    on()
    calls = []

    def resume(sid, st):
        calls.append(sid)
        return True, "restarted"
    t = overnight.state("s1")["started"]
    for i in range(4):
        overnight.watch(lambda sid: False, lambda s, x: None, resume, overnight.finish,
                        now=t + 10 + i * (overnight.RESUME_GRACE_S + 1))
    assert calls == ["s1", "s1"] and overnight.state("s1") is None


def test_watch_waits_out_a_limit_with_bounded_pushes(home):
    on()
    t = overnight.state("s1")["started"]
    overnight.on_failure("s1", {"error": "rate_limit"}, now=t)
    sent = []
    for minutes in (10, 31, 40, 62, 6 * 60 + 1):
        overnight.watch(lambda sid: True, lambda s, x: sent.append(x), lambda s, st: (True, ""),
                        overnight.finish, now=t + minutes * 60)
    assert len(sent) == 2 and overnight.state("s1")["waiting"] is None
    assert overnight.from_person("s1", {"prompt": sent[0]}) is False


def test_resume_is_tmux_and_claude_resume_only():
    argv = overnight.resume_argv("abc-123", {"cwd": "/w/proj-a"})
    assert argv[:3] == ["tmux", "new-session", "-d"] and "--resume" in argv
    assert not any("dangerously" in a or "permission-mode" in a for a in argv)


def test_report_text(home):
    st = on()
    t = st["started"]
    overnight.note("s1", "done", "parser moved, 214 tests pass", t + 60)
    overnight.note("s1", "decision", "kept the old flag (recommended)", t + 120)
    overnight.note("s1", "skipped", "git push --force to main", t + 180)
    overnight.note("s1", "decision", "token sk-ant-api03-" + "x" * 30, t + 200)  # dropped
    text = overnight.report("s1", st, "finished", t + 3600)
    assert text.splitlines()[0] == 'Overnight task: "the task"  (box-1, proj-a)'
    assert "Done\n  - parser moved, 214 tests pass" in text
    assert "Decisions made for you\n  - kept the old flag (recommended)\n" in text
    assert "Skipped, needs your yes\n  - git push --force to main" in text
    assert "sk-ant" not in text
    assert stat.S_IMODE(os.stat(paths.home() / "overnight" / "s1.log").st_mode) == 0o600


def test_personal_session_sends_nothing(home):
    on()
    posted = []
    overnight.finish("s1", "finished", post=lambda st, body: posted.append(body))
    on(sid="s2")
    overnight.clash("s2", "project", "No pushes on Friday", "git",
                    post=lambda st, body: posted.append(body))
    assert posted == []
    assert overnight.notes("s2")[-1]["kind"] == "clash"


def test_team_session_posts_report_and_clash(home):
    st = on()
    overnight.save("s1", {**st, "team": True})
    posted = []
    overnight.clash("s1", "project", 'No "pushes" on Friday', "git",
                    post=lambda st, body: posted.append(body) or True)
    overnight.finish("s1", "finished", post=lambda st, body: posted.append(body))
    assert posted[0] == {"kind": "clash", "scope": "project", "rule": "No 'pushes' on Friday",
                         "class": "git"}
    assert posted[1]["kind"] == "report"


def test_clash_texts_are_fixed():
    d = overnight.clash_detail("project", "No pushes on Friday", "git", "proj-a")
    assert d == 'project rule "No pushes on Friday" forbade git in proj-a'
    n = overnight.clash_notice("alice", d)
    assert ("Your overnight task allowed force push or history rewrite on shared branches, but"
            " the project rule 'No pushes on Friday' forbids it, so the session skipped it.") in n
    assert overnight.clash_notice("alice", "anything else") == ""
    assert overnight.clash_detail("x", "y", "z", "p") == 'team rule "y" forbade other in p'


def test_hub_overnight_command(home, monkeypatch):
    from kaptyorka import offhub

    env = {"CLAUDE_CODE_SESSION_ID": "s9"}
    monkeypatch.setattr(offhub, "session", lambda sid, home=None: (1, {"cwd": "/w/x"}))
    assert offhub.command(["overnight"], env, cwd="/w/x") == (
        0, "kaptyorka: overnight mode is on for this task. To stop it: /hub overnight off")
    code, line = offhub.command(["overnight", "off"], env, cwd="/w/x")
    assert code == 0 and line.startswith("kaptyorka: overnight mode is off. Morning report: ")
    assert offhub.command(["overnight"], {offhub.OPENCODE_SESSION: "ses_1"}, cwd="/w/x") == (
        1, "kaptyorka: overnight mode is not there yet for opencode; it works in Claude Code only")


def test_hub_report_goes_to_that_persons_page_only(hub):
    from helpers import person
    from kaptyorka import http

    _, token = person(hub.store, "alice")
    person(hub.store, "bob")
    h = {"X-Kaptyorka-Key": "claude:s1", "X-Kaptyorka-Machine": "box-1",
         "X-Kaptyorka-Tool": "claude-code", "X-Kaptyorka-Project": "proj-a"}
    text = "Overnight task: x\nDone\n  - key ghp_" + "a" * 36 + "\n  - parser moved"
    status, _ = http.request("POST", hub.url + "/api/overnight", token,
                             {"kind": "report", "text": text}, headers=h)
    assert status == 200
    with hub.store.lock:
        rows = hub.store.db.execute("SELECT to_key, text FROM messages").fetchall()
    assert [r["to_key"] for r in rows] == ["web:alice"]
    assert "parser moved" in rows[0]["text"] and "ghp_" not in rows[0]["text"]
    status, _ = http.request("POST", hub.url + "/api/overnight", token,
                             {"kind": "clash", "scope": "project", "rule": "No pushes on Friday",
                              "class": "git"}, headers=h)
    assert status == 200
    row = next(r for r in hub.store.log(20) if r["kind"] == overnight.CLASH_KIND)
    assert row["person"] == "alice"
    assert row["detail"] == 'project rule "No pushes on Friday" forbade git in proj-a'
    assert http.request("POST", hub.url + "/api/overnight", hub.token, {"kind": "report"},
                        headers=h)[0] == 400  # the shared token names no person


# ---------- the hook command form ----------

def test_tool_call_hooks_skip_python_while_off(tmp_path):
    import subprocess

    from kaptyorka.adapters.base import NIGHT_GUARD
    from kaptyorka.adapters.claude import hook_entry

    cmd = hook_entry("/opt/k y/bin/kaptyorka", "pretool")["hooks"][0]["command"]
    assert cmd == NIGHT_GUARD + "'/opt/k y/bin/kaptyorka' hook claude-code pretool"
    assert hook_entry("/opt/kaptyorka", "stop")["hooks"][0]["command"] == \
        "/opt/kaptyorka hook claude-code stop"
    fake = tmp_path / "kaptyorka"
    fake.write_text("#!/bin/sh\necho RAN\n")
    fake.chmod(0o755)
    k = tmp_path / "k"
    sh = NIGHT_GUARD + f"{fake} hook claude-code pretool"
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path), "KAPTYORKA_HOME": str(k)}
    assert subprocess.run(["sh", "-c", sh], env=env, capture_output=True, text=True).stdout == ""
    (k / "overnight" / "on").mkdir(parents=True)
    assert subprocess.run(["sh", "-c", sh], env=env, capture_output=True, text=True).stdout == ""
    (k / "overnight" / "on" / "s1").touch()
    assert subprocess.run(["sh", "-c", sh], env=env, capture_output=True, text=True).stdout == "RAN\n"


def test_marker_follows_on_and_off(home):
    on()
    assert (paths.home() / "overnight" / "on" / "s1").exists()
    overnight.turn_off("s1", "test")
    assert not (paths.home() / "overnight" / "on" / "s1").exists()


def test_guarded_form_is_ours_for_install_leave_and_doctor(home):
    from kaptyorka import doctor
    from kaptyorka.adapters.base import is_our_hook
    from kaptyorka.adapters.claude import HOOKS, ClaudeAdapter

    exe = "/opt/k y/bin/kaptyorka"
    old = {event: [{"hooks": [{"type": "command", "command": f"'{exe}' hook claude-code {name}"}]}]
           for event, name in HOOKS}  # an install from before the guard
    (home / ".claude").mkdir()
    (home / ".claude" / "settings.json").write_text(json.dumps({"hooks": old}))
    (home / ".claude.json").write_text(json.dumps({"mcpServers": {"kaptyorka": {}}}))
    a = ClaudeAdapter(home=home, log=lambda line: None,
                      run=lambda *x, **k: type("R", (), {"returncode": 0, "stdout": ""})())
    assert a.add_hooks(exe)  # the bridge's start: the old line is replaced, not doubled
    hooks = json.loads((home / ".claude" / "settings.json").read_text())["hooks"]
    pre = [h["command"] for e in hooks["PreToolUse"] for h in e["hooks"]]
    assert len(pre) == 1 and pre[0].startswith("[ -n ") and is_our_hook(pre[0], "claude-code",
                                                                        ("pretool",))
    assert a.add_hooks(exe) == []
    assert doctor.tool_configured(a, home)
    a.unconfigure()
    assert "hooks" not in json.loads((home / ".claude" / "settings.json").read_text())


@pytest.mark.parametrize("prompt", [
    "turn on overnight mode and continue until I'm back tomorrow",
    "work on this overnight",
    "keep going while I sleep",
    "overnight task: fix the flaky test",
])
def test_asks_for_tonight_trigger(prompt):
    assert overnight.triggered(prompt)


@pytest.mark.parametrize("prompt", [
    "today the session must give me the overnight task results, but it just ran one shell all"
    " night",
    "show me the overnight task results",
    "where is the overnight report?",
    "summarize the output of the overnight run",
    "what did you do last night?",
    "this morning the overnight run was stuck",
    "the overnight task failed, why?",
])
def test_words_about_a_past_night_do_not_trigger(prompt):
    assert not overnight.triggered(prompt)


DOCTOR_PASTE = (
    '<pasted_content name="doctor.txt">\n'
    "note tool: opencode overnight: not yet: overnight mode works in Claude Code only\n"
    "</pasted_content>")


@pytest.mark.parametrize("prompt", [
    "overnight task - fix the flaky test",
    "Overnight task, tidy the docs",
    "this is an overnight task",
    "work overnight on the parser",
    "run this overnight",
    "do this overnight please",
    "work the whole night on the importer",
    "fix the tests. Overnight task: they must be green by morning, not flaky",
])
def test_clear_orders_trigger(prompt):
    assert overnight.triggered(prompt)


@pytest.mark.parametrize("prompt", [
    DOCTOR_PASTE,
    "note tool: opencode overnight: not yet: overnight mode works in Claude Code only",
    "No need for overnight mode. I am at office and can assist you if needed.",
    "not overnight",
    "don't use overnight mode",
    "please don't work overnight on this",
    "without overnight mode",
    "stop overnight mode",
    "turn off overnight mode",
    "disable overnight",
    "no overnight",
    "i need this by morning",
    "don't push until I'm back",
    "wait until I'm back",
    "I could not sleep, was it running while I sleep?",
    "why did overnight mode turn on?",
    "overnight mode is buggy",
    "the overnight trigger fires too often",
    "we did an overnight run yesterday",
    "is overnight mode on?",
    "see this:\n> run this overnight\nwhat does it mean?",
    "the log says `work overnight on it` and `overnight task: x`",
    "```\nrun this overnight\n```",
])
def test_mentions_never_trigger(prompt):
    assert not overnight.triggered(prompt)


def test_a_past_night_and_a_new_ask_still_triggers():
    assert overnight.triggered("last night went fine; work on the docs overnight too")


def test_report_shows_only_the_current_night(home):
    old = on()
    overnight.note("s1", "done", "an old night's work", old["started"] + 60)
    overnight.finish("s1", "you are back", old["started"] + 600, post=lambda *a: None)
    later = old["started"] + 86400
    st = overnight.turn_on("s1", "proj-a", "/w/proj-a", False, "k", "box-1", "new task", "",
                           now=later)
    overnight.note("s1", "done", "tonight's work", later + 60)
    text = overnight.report("s1", st, "finished", later + 3600)
    assert "tonight's work" in text
    assert "old night" not in text and text.count("overnight mode on") == 1
