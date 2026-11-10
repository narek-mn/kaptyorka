import json
import subprocess
from types import SimpleNamespace

from kaptyorka import fleet, login_copy
from kaptyorka.login_copy import REMOTE_WRITE

SECRET = "SECRET-TOKEN-TEXT"
TARGET = "alice@box-1"
LOCAL_OK = SimpleNamespace(returncode=0, stdout=json.dumps({"loggedIn": True}), stderr="")
LOGGED_OUT = SimpleNamespace(returncode=1, stdout=json.dumps({"loggedIn": False}), stderr="")


def creds(tmp_path):
    (tmp_path / ".credentials.json").write_text(SECRET)
    return tmp_path


def make_run(local=LOCAL_OK, remote=(LOGGED_OUT, LOCAL_OK), write=None, raises=None):
    """A fake run: records (argv, kwargs); remote status answers come from `remote` in order."""
    calls = []
    remote_answers = list(remote)
    write = write or SimpleNamespace(returncode=0, stdout="", stderr="")

    def run(argv, **kw):
        calls.append((argv, kw))
        if argv[0] == "claude":
            return local
        if argv[-1] == REMOTE_WRITE:
            if raises == "write":
                raise subprocess.TimeoutExpired(argv, 30)
            return write
        if raises == "status":
            raise subprocess.TimeoutExpired(argv, 30)
        return remote_answers.pop(0) if len(remote_answers) > 1 else remote_answers[0]

    run.calls = calls
    return run


def writes(run):
    return [c for c in run.calls if c[0][-1] == REMOTE_WRITE]


def test_copies_and_verifies(tmp_path):
    run = make_run()
    assert login_copy.copy(TARGET, run=run, config_dir=creds(tmp_path)) == (True, "alice@box-1: Claude Code is logged in there")
    argv, kw = writes(run)[0]
    assert kw["input"] == SECRET
    assert argv == fleet._ssh(TARGET, REMOTE_WRITE)


def test_secret_never_in_argv_or_output(tmp_path):
    lines = []
    run = make_run()
    lines.append(login_copy.copy(TARGET, run=run, config_dir=creds(tmp_path))[1])
    bad = make_run(write=SimpleNamespace(returncode=1, stdout="", stderr="disk full"))
    ok, line = login_copy.copy(TARGET, run=bad, config_dir=tmp_path)
    assert not ok and "disk full" in line
    lines.append(line)
    for r in (run, bad):
        for argv, _ in r.calls:
            assert all(SECRET not in a for a in argv)
    assert all(SECRET not in l for l in lines)


def test_refuses_a_logged_in_target_without_force(tmp_path):
    run = make_run(remote=(LOCAL_OK,))
    ok, line = login_copy.copy(TARGET, run=run, config_dir=creds(tmp_path))
    assert not ok and line.endswith("already logged in there; add --force to replace that login")
    assert not writes(run)
    run = make_run(remote=(LOCAL_OK,))
    login_copy.copy(TARGET, force=True, run=run, config_dir=tmp_path)
    assert len(writes(run)) == 1


def test_refuses_when_not_logged_in_here(tmp_path):
    run = make_run(local=LOGGED_OUT)
    ok, line = login_copy.copy(TARGET, run=run, config_dir=creds(tmp_path))
    assert not ok and line == "Claude Code is not logged in on this machine; run claude auth login first"
    assert all(c[0][0] != "ssh" for c in run.calls)


def test_no_local_file(tmp_path):
    run = make_run()
    assert login_copy.copy(TARGET, run=run, config_dir=tmp_path, platform="linux") == (
        False, "no Claude Code login on this machine; run claude auth login first")
    assert login_copy.copy(TARGET, run=run, config_dir=tmp_path, platform="darwin") == (
        False, "this Mac keeps the Claude login in the keychain; login-copy works from Linux only for now")
    assert not run.calls


def test_target_without_claude(tmp_path):
    run = make_run(remote=(SimpleNamespace(returncode=127, stdout="", stderr="claude: not found"),))
    ok, line = login_copy.copy(TARGET, run=run, config_dir=creds(tmp_path))
    assert not ok and line == "alice@box-1: Claude Code is not installed there"
    assert not writes(run)


def test_unreachable_target(tmp_path):
    err = "ssh: connect to host box-1 port 22: No route to host"
    run = make_run(remote=(SimpleNamespace(returncode=255, stdout="", stderr=err),))
    ok, line = login_copy.copy(TARGET, run=run, config_dir=creds(tmp_path))
    assert not ok and line == f"alice@box-1: could not reach it over ssh: {err}"
    assert not writes(run)


def test_ssh_timeout(tmp_path):
    want = "alice@box-1: could not reach it over ssh: timed out"
    for where in ("status", "write"):
        run = make_run(raises=where)
        assert login_copy.copy(TARGET, run=run, config_dir=creds(tmp_path)) == (False, want)


def test_unreadable_remote_state(tmp_path):
    bad = SimpleNamespace(returncode=1, stdout="not json", stderr="bash: line 1: claude: Permission denied")
    for force in (False, True):
        run = make_run(remote=(bad,))
        ok, line = login_copy.copy(TARGET, force=force, run=run, config_dir=creds(tmp_path))
        assert not ok and line == ("alice@box-1: could not read Claude Code's login state there: "
                                   "bash: line 1: claude: Permission denied")
        assert not writes(run)


def test_bad_target(tmp_path):
    for target in ("-oProxyCommand=x", "alice@box 1"):
        run = make_run()
        assert login_copy.copy(target, run=run, config_dir=creds(tmp_path)) == (False, f"{target}: not a user@host")
        assert not run.calls


def test_copied_but_still_logged_out(tmp_path):
    run = make_run(remote=(LOGGED_OUT, LOGGED_OUT))
    ok, line = login_copy.copy(TARGET, run=run, config_dir=creds(tmp_path))
    assert not ok and line == ("alice@box-1: copied, but Claude Code there still says it is not logged in; "
                               "run claude auth login there")
