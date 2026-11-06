import shlex
from pathlib import Path
from types import SimpleNamespace

from kaptyorka import fleet

URL = "http://h:1/i/" + "C" * 22
KEY = "ANTHROPIC_API_KEY=sk-secret-123"


def make_run(fail_step=None, tools="/u/claude\n/u/codex\n"):
    calls = []

    def run(argv, **kw):
        calls.append((argv, kw))
        cmd = argv[-1]
        step = ("pipx" if "pipx --version" in cmd else "install" if "pipx install" in cmd
                else "join" if "kaptyorka join" in cmd else "tools")
        if step == fail_step:
            return SimpleNamespace(returncode=1, stdout="", stderr="boom\nsecond line")
        return SimpleNamespace(returncode=0, stdout=tools if step == "tools" else "", stderr="")
    return run, calls


def add(targets, run, key=None, tmp=None):
    kf = None
    if key:
        kf = Path(tmp) / "key"
        kf.write_text(key + "\n")
    return fleet.add(targets, "alice", kf, "http://h:1", lambda p: URL, run=run)


def test_success_line_and_ssh_shape():
    run, calls = make_run()
    assert add(["alice@box-1"], run) == ["alice@box-1: joined as alice@box-1; tools: claude-code, codex"]
    assert len(calls) == 4
    for argv, _ in calls:
        assert argv[:4] == ["ssh", "-o", "BatchMode=yes", "alice@box-1"] and len(argv) == 5
    assert "pipx --version || (python3 -m pip install --user pipx && python3 -m pipx ensurepath)" in calls[0][0][4]
    assert "pipx install kaptyorka || pipx upgrade kaptyorka" in calls[1][0][4]
    assert f"kaptyorka join {shlex.quote(URL)} --name alice@box-1" in calls[2][0][4]
    assert "which claude opencode codex" in calls[3][0][4]


def test_quoting_of_substituted_values():
    run, calls = make_run()
    fleet.add(["al ice@box;1"], "alice", None, "h", lambda p: "http://h/i/a;b c", run=run)
    cmd = calls[2][0][4]
    assert shlex.quote("http://h/i/a;b c") in cmd and shlex.quote("al ice@box;1") in cmd


def test_key_only_on_stdin_of_join(tmp_path):
    run, calls = make_run()
    lines = add(["alice@box-1"], run, key=KEY, tmp=tmp_path)
    assert "--env-stdin" in calls[2][0][4]
    assert calls[2][1]["input"] == KEY + "\n"
    for i, (argv, kw) in enumerate(calls):
        assert "sk-secret" not in " ".join(argv)
        if i != 2:
            assert "input" not in kw or kw["input"] is None
    assert "sk-secret" not in "\n".join(lines)
    assert all("--env-stdin" not in c[0][4] for c in calls if c is not calls[2])


def test_no_key_no_env_stdin():
    run, calls = make_run()
    add(["alice@box-1"], run)
    assert "--env-stdin" not in calls[2][0][4]


def test_failure_line_stops_that_target_only(tmp_path):
    run, calls = make_run(fail_step="install")
    lines = add(["alice@box-1"], run, key=KEY, tmp=tmp_path)
    assert len(lines) == 1 and len(calls) == 2
    assert lines[0].startswith("alice@box-1: failed at install: boom; run by hand: ")
    assert "pipx install kaptyorka" in lines[0] and "sk-secret" not in lines[0]


def test_failure_at_join_and_next_target_continues():
    run, calls = make_run(fail_step="join")
    lines = add(["a@x", "b@y"], run)
    assert [l.split(":")[0] for l in lines] == ["a@x", "b@y"]
    assert all("failed at join: boom" in l for l in lines)


def test_no_tools():
    run, _ = make_run(tools="")
    assert add(["a@x"], run) == ["a@x: joined as a@x; tools: none"]


def test_list_format():
    rows = [{"host": "box-1", "devices": [
        {"os_user": "alice", "person": "alice", "online": True, "version": "1.2"},
        {"os_user": "bob", "person": "bob", "online": False, "version": "1.1"}]}]
    assert fleet.list(rows) == ["box-1 alice        alice      online 1.2",
                                "box-1 bob          bob        offline 1.1"]
