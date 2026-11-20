import json
import subprocess
from types import SimpleNamespace

import pytest

from kaptyorka import fleet, login_copy
from kaptyorka.login_copy import REMOTE_WRITE

SECRET = "SECRET-TOKEN-TEXT"
TARGET = "alice@box-1"


@pytest.fixture(autouse=True)
def _linux_unless_said(monkeypatch):
    """login-copy defaults to the running OS (darwin on macOS CI); tests that don't name one mean Linux."""
    real_copy, real_all = login_copy.copy, login_copy.copy_all
    monkeypatch.setattr(login_copy, "copy", lambda *a, platform="linux", **k: real_copy(*a, platform=platform, **k))
    monkeypatch.setattr(login_copy, "copy_all", lambda *a, platform="linux", **k: real_all(*a, platform=platform, **k))
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


# Codex and OpenCode. Status shapes are the tools' own: codex-rs/cli/src/login.rs prints
# everything on stderr; OpenCode 1.19 `auth list` ends with "N credentials".
from pathlib import Path  # noqa: E402



from kaptyorka.login_copy import (CODEX_STATUS, CODEX_WRITE, OPENCODE_IMPORT,  # noqa: E402
                                  OPENCODE_STATUS, OPENCODE_WRITE, STATUS)

OK = SimpleNamespace(returncode=0, stdout="", stderr="")
CODEX_IN = SimpleNamespace(returncode=0, stdout="", stderr="Logged in using ChatGPT\n")
CODEX_OUT = SimpleNamespace(returncode=1, stdout="", stderr="Not logged in\n")
OC1 = SimpleNamespace(returncode=0, stdout="1.19.5\n", stderr="")
OC2 = SimpleNamespace(returncode=0, stdout="2.3.2\n", stderr="")


def oc_list(n, version="1.19.5"):
    return SimpleNamespace(returncode=0, stdout=f"{version}\n\x1b[0m\n┌  Credentials ~/x\n│\n└  {n} credentials\n",
                           stderr="")


def fake(local, remote):
    """local: argv[0] -> result; remote: ssh command -> list of results, used in order."""
    calls = []
    remote = {k: list(v) for k, v in remote.items()}

    def run(argv, **kw):
        calls.append((argv, kw))
        if argv[0] != "ssh":
            got = local.get(" ".join(argv), local.get(argv[0], FileNotFoundError(argv[0])))  # not installed
            if isinstance(got, Exception):
                raise got
            return got
        answers = remote[argv[-1]]
        return answers.pop(0) if len(answers) > 1 else answers[0]

    run.calls = calls
    return run


def sent(run, command):
    return [kw.get("input") for argv, kw in run.calls if argv[0] == "ssh" and argv[-1] == command]


def tool_dir(tmp_path, name, text=SECRET):
    d = tmp_path / name
    d.mkdir()
    (d / "auth.json").write_text(text)
    return d


def test_codex_copies_only_on_stdin(tmp_path):
    run = fake({"codex": CODEX_IN}, {CODEX_STATUS: [CODEX_OUT, CODEX_IN], CODEX_WRITE: [OK]})
    got = login_copy.copy(TARGET, run=run, config_dir=tool_dir(tmp_path, "codex"), tool="codex")
    assert got == (True, "alice@box-1: Codex is logged in there")
    assert sent(run, CODEX_WRITE) == [SECRET]
    assert '"${CODEX_HOME:-$HOME/.codex}"' in CODEX_WRITE and 'mv -f "$t" "$d/auth.json"' in CODEX_WRITE
    assert all(SECRET not in a for argv, _ in run.calls for a in argv)
    assert ["codex", "login", "status"] in [argv for argv, _ in run.calls]


def test_codex_refuses_a_logged_in_target_and_force_still_checks(tmp_path):
    d = tool_dir(tmp_path, "codex")
    api = SimpleNamespace(returncode=0, stdout="", stderr="API key configured - sk-a***z\n")
    run = fake({"codex": CODEX_IN}, {CODEX_STATUS: [api]})
    ok, line = login_copy.copy(TARGET, run=run, config_dir=d, tool="codex")
    assert not ok and line == "alice@box-1: Codex is already logged in there; add --force to replace that login"
    assert not sent(run, CODEX_WRITE)
    run = fake({"codex": CODEX_IN}, {CODEX_STATUS: [api, CODEX_OUT], CODEX_WRITE: [OK]})
    ok, line = login_copy.copy(TARGET, force=True, run=run, config_dir=d, tool="codex")
    assert not ok and line == "alice@box-1: copied, but Codex there still says it is not logged in; run codex login there"
    assert len(sent(run, CODEX_STATUS)) == 2


def test_codex_unknown_state_never_echoes_its_output(tmp_path):
    bad = SimpleNamespace(returncode=1, stdout="", stderr="Error checking login status: sk-a***z broken\n")
    for force in (False, True):
        run = fake({"codex": CODEX_IN}, {CODEX_STATUS: [bad]})
        ok, line = login_copy.copy(TARGET, force=force, run=run, config_dir=tool_dir(tmp_path, f"c{force}"), tool="codex")
        assert not ok and line == ("alice@box-1: could not read Codex's login state there; "
                                   "run codex login status there to see why")
        assert not sent(run, CODEX_WRITE)


def test_codex_keyring_means_nothing_to_copy(tmp_path):
    want = (False, "Codex keeps its login in the system keyring on this machine, so there is no file to copy")
    for mode, with_file in (("keyring", True), ("ephemeral", False), ("auto", False)):
        d = tmp_path / mode
        d.mkdir()
        if with_file:
            (d / "auth.json").write_text(SECRET)
        (d / "config.toml").write_text(f'model = "x"\ncli_auth_credentials_store = "{mode}"\n[profiles.a]\n')
        run = fake({"codex": CODEX_IN}, {})
        assert login_copy.copy(TARGET, run=run, config_dir=d, tool="codex") == want
        assert not run.calls
    # auto with a file falls back to it; a key inside a table is not the top-level setting
    for name, toml in (("auto2", 'cli_auth_credentials_store = "auto"\n'),
                       ("table", '[x]\ncli_auth_credentials_store = "keyring"\n')):
        d = tool_dir(tmp_path, name)
        (d / "config.toml").write_text(toml)
        run = fake({"codex": CODEX_IN}, {CODEX_STATUS: [CODEX_OUT, CODEX_IN], CODEX_WRITE: [OK]})
        assert login_copy.copy(TARGET, run=run, config_dir=d, tool="codex")[0]


def test_codex_not_logged_in_here_or_not_installed_there(tmp_path):
    d = tool_dir(tmp_path, "codex")
    run = fake({"codex": CODEX_OUT}, {})
    assert login_copy.copy(TARGET, run=run, config_dir=d, tool="codex") == (
        False, "Codex is not logged in on this machine; run codex login first")
    assert all(argv[0] != "ssh" for argv, _ in run.calls)
    run = fake({"codex": CODEX_IN}, {CODEX_STATUS: [SimpleNamespace(returncode=127, stdout="", stderr="")]})
    assert login_copy.copy(TARGET, run=run, config_dir=d, tool="codex") == (
        False, "alice@box-1: Codex is not installed there")


def test_opencode1_copies_to_a_target_with_no_stored_login(tmp_path):
    d = tool_dir(tmp_path, "oc", '{"anthropic": {"type": "api", "key": "' + SECRET + '"}}')
    run = fake({"opencode": OC1}, {OPENCODE_STATUS: [oc_list(0), oc_list(1)], OPENCODE_WRITE: [OK]})
    assert login_copy.copy(TARGET, run=run, config_dir=d, tool="opencode") == (True, "alice@box-1: OpenCode is logged in there")
    assert sent(run, OPENCODE_WRITE) == [(d / "auth.json").read_text()]
    assert '"${XDG_DATA_HOME:-$HOME/.local/share}/opencode"' in OPENCODE_WRITE
    assert "$HOME/.opencode/bin" in OPENCODE_STATUS and "1.*) opencode auth list" in OPENCODE_STATUS


def test_opencode_refuses_stored_login_unknown_and_version_3(tmp_path):
    d = tool_dir(tmp_path, "oc", '{"a": {"type": "api", "key": "k"}}')
    run = fake({"opencode": OC1}, {OPENCODE_STATUS: [oc_list(2)]})
    ok, line = login_copy.copy(TARGET, run=run, config_dir=d, tool="opencode")
    assert not ok and line.endswith("OpenCode is already logged in there; add --force to replace that login")
    unknown = SimpleNamespace(returncode=0, stdout="1.19.5\nsomething else\n", stderr="")
    for force in (False, True):  # unknown is never "not logged in"
        run = fake({"opencode": OC1}, {OPENCODE_STATUS: [unknown]})
        ok, line = login_copy.copy(TARGET, force=force, run=run, config_dir=d, tool="opencode")
        assert not ok and line == ("alice@box-1: could not read OpenCode's login state there; "
                                   "run opencode auth list there to see why")
        assert not sent(run, OPENCODE_WRITE)
    run = fake({"opencode": OC1}, {OPENCODE_STATUS: [SimpleNamespace(returncode=0, stdout="3.0.0\n", stderr="")]})
    assert not login_copy.copy(TARGET, force=True, run=run, config_dir=d, tool="opencode")[0]
    assert not sent(run, OPENCODE_WRITE)


# OpenCode 2 (v2.3.2, packages/cli/src/commands/handlers/auth/{export,import,list}.ts): export
# prints the stored credentials as a JSON array; import reads that from stdin and skips ids
# already there; `auth list --format json` lists integrations with their connections.
EXPORT = "opencode auth export --standalone"
EXPORTED = SimpleNamespace(returncode=0, stdout=json.dumps([{"id": "cred_1", "integrationID": "anthropic",
                                                              "value": {"type": "key", "key": SECRET}}]), stderr="")


def oc2_list(*connections):
    body = [{"id": "anthropic", "name": "Anthropic", "connections": list(connections)}] if connections else []
    return SimpleNamespace(returncode=0, stdout="2.3.2\n" + json.dumps(body), stderr="")


STORED = {"type": "credential", "id": "cred_1", "label": "work", "method": "key"}
ENV = {"type": "env", "name": "ANTHROPIC_API_KEY"}


def test_opencode2_copies_through_export_and_import_on_stdin(tmp_path):
    run = fake({"opencode": OC2, EXPORT: EXPORTED},
               {OPENCODE_STATUS: [oc2_list(ENV), oc2_list(ENV, STORED)], OPENCODE_IMPORT: [OK]})
    got = login_copy.copy(TARGET, run=run, config_dir=tmp_path / "none", tool="opencode")
    assert got == (True, "alice@box-1: OpenCode is logged in there")
    assert sent(run, OPENCODE_IMPORT) == [EXPORTED.stdout] and not sent(run, OPENCODE_WRITE)
    assert all(SECRET not in a for argv, _ in run.calls for a in argv)
    # a private server per command, never the background service
    assert "--standalone" in OPENCODE_IMPORT and "2.*) opencode auth list --standalone --format json" in OPENCODE_STATUS


def test_opencode2_refuses_a_stored_login_and_force_still_checks(tmp_path):
    other = {**STORED, "id": "cred_9"}
    run = fake({"opencode": OC2, EXPORT: EXPORTED}, {OPENCODE_STATUS: [oc2_list(other)]})
    ok, line = login_copy.copy(TARGET, run=run, config_dir=tmp_path, tool="opencode")
    assert not ok and line == "alice@box-1: OpenCode is already logged in there; add --force to replace that login"
    assert not sent(run, OPENCODE_IMPORT)
    # --force imports, then checks that this machine's credentials are there now
    run = fake({"opencode": OC2, EXPORT: EXPORTED}, {OPENCODE_STATUS: [oc2_list(other)], OPENCODE_IMPORT: [OK]})
    ok, line = login_copy.copy(TARGET, force=True, run=run, config_dir=tmp_path, tool="opencode")
    assert not ok and line.startswith("alice@box-1: copied, but OpenCode there still says it is not logged in")
    assert len(sent(run, OPENCODE_STATUS)) == 2


def test_opencode2_never_shows_export_or_import_errors(tmp_path):
    leaky = SimpleNamespace(returncode=1, stdout="", stderr=f"Expected array, got {SECRET}")
    run = fake({"opencode": OC2, EXPORT: leaky}, {})
    assert login_copy.copy(TARGET, run=run, config_dir=tmp_path, tool="opencode") == (
        False, "OpenCode: could not export this machine's login (opencode auth export failed)")
    assert all(argv[0] != "ssh" for argv, _ in run.calls)
    run = fake({"opencode": OC2, EXPORT: EXPORTED}, {OPENCODE_STATUS: [oc2_list()], OPENCODE_IMPORT: [leaky]})
    ok, line = login_copy.copy(TARGET, run=run, config_dir=tmp_path, tool="opencode")
    assert not ok and line == "alice@box-1: copy failed: opencode auth import there exited with 1"
    run = fake({"opencode": OC2, EXPORT: SimpleNamespace(returncode=0, stdout="[]", stderr="")}, {})
    assert login_copy.copy(TARGET, run=run, config_dir=tmp_path, tool="opencode") == (
        False, "OpenCode is not logged in on this machine; run opencode auth login first")


def test_opencode_versions_must_match(tmp_path):
    want = ("alice@box-1: OpenCode {} there and OpenCode {} here keep logins differently (a file, a database); "
            "login-copy copies OpenCode only between the same major version")
    d = tool_dir(tmp_path, "oc", '{"a": {"type": "api", "key": "k"}}')
    run = fake({"opencode": OC1}, {OPENCODE_STATUS: [oc2_list()]})
    assert login_copy.copy(TARGET, force=True, run=run, config_dir=d, tool="opencode") == (False, want.format(2, 1))
    run = fake({"opencode": OC2, EXPORT: EXPORTED}, {OPENCODE_STATUS: [oc_list(0)]})
    assert login_copy.copy(TARGET, run=run, config_dir=d, tool="opencode") == (False, want.format(1, 2))
    assert not sent(run, OPENCODE_WRITE) and not sent(run, OPENCODE_IMPORT)


def test_opencode_empty_file_is_no_login(tmp_path):
    run = fake({"opencode": OC1}, {})
    assert login_copy.copy(TARGET, run=run, config_dir=tool_dir(tmp_path, "oc", "{}"), tool="opencode") == (
        False, "OpenCode is not logged in on this machine; run opencode auth login first")


def test_codex_and_opencode_are_linux_only(tmp_path):
    for tool in ("codex", "opencode"):
        run = fake({}, {})
        got = login_copy.copy(TARGET, run=run, config_dir=tool_dir(tmp_path, tool), tool=tool, platform="darwin")
        assert got[0] is False and got[1].endswith("login-copy works from Linux only for now")
        assert not run.calls


def test_copy_all_one_line_per_logged_in_tool(tmp_path):
    (tmp_path / "claude").mkdir()
    dirs = {"claude": creds(tmp_path / "claude"), "codex": tool_dir(tmp_path, "codex"), "opencode": tmp_path / "none"}
    missing = SimpleNamespace(returncode=127, stdout="", stderr="")
    run = fake({"claude": LOCAL_OK, "codex": CODEX_IN, "opencode": OC1},
               {STATUS: [LOGGED_OUT, LOCAL_OK], REMOTE_WRITE: [OK], CODEX_STATUS: [missing]})
    assert login_copy.copy_all(TARGET, run=run, dirs=dirs, platform="linux") == (
        True, ["alice@box-1: Claude Code is logged in there", "alice@box-1: Codex is not installed there"])
    # already logged in there is skipped, not failed; a failed copy fails the whole run
    run = fake({"claude": LOCAL_OK, "codex": CODEX_IN},
               {STATUS: [LOCAL_OK], CODEX_STATUS: [CODEX_OUT], CODEX_WRITE: [SimpleNamespace(returncode=1, stdout="", stderr="disk full")]})
    ok, lines = login_copy.copy_all(TARGET, run=run, dirs=dirs, platform="linux")
    assert not ok and lines == ["alice@box-1: Claude Code is already logged in there; add --force to replace that login",
                                "alice@box-1: copy failed: disk full"]
    run = fake({}, {})
    nothing = {"claude": tmp_path / "n1", "codex": tmp_path / "n2", "opencode": tmp_path / "n3"}
    assert login_copy.copy_all(TARGET, run=run, dirs=nothing, platform="linux") == (
        False, ["no tool is logged in on this machine; log in to Claude Code, Codex or OpenCode first"])
    assert all(argv[0] != "ssh" for argv, _ in run.calls)
    run = fake({}, {})
    assert login_copy.copy_all("-oX", run=run, dirs=nothing) == (False, ["-oX: not a user@host"])
    assert not run.calls


@pytest.mark.parametrize("command,where", [(CODEX_WRITE, ".codex"), (OPENCODE_WRITE, ".local/share/opencode"),
                                           (REMOTE_WRITE, ".claude")])
def test_write_step_in_a_real_shell(tmp_path, command, where):
    """The write step itself (a plain sh line, run here instead of over ssh): 600, no temp left."""
    env = {"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"}
    subprocess.run(["sh", "-c", command], input=SECRET, text=True, env=env, check=True)
    target = tmp_path / where
    files = sorted(p.name for p in target.iterdir())
    assert files == [".credentials.json" if where == ".claude" else "auth.json"]
    assert (target / files[0]).read_text() == SECRET and (target / files[0]).stat().st_mode & 0o777 == 0o600
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "mv").write_text("#!/bin/sh\nexit 1\n")
    (bin_dir / "mv").chmod(0o755)
    env["PATH"] = f"{bin_dir}:/usr/bin:/bin"
    r = subprocess.run(["sh", "-c", command], input="NEW", text=True, env=env)
    assert r.returncode == 1 and sorted(p.name for p in target.iterdir()) == files
    assert (target / files[0]).read_text() == SECRET
    assert Path(target).stat().st_mode & 0o077 == 0


def test_copy_all_notes_what_cannot_be_copied_without_failing(tmp_path):
    codex = tool_dir(tmp_path, "codex")
    (codex / "config.toml").write_text('cli_auth_credentials_store = "keyring"\n')
    (tmp_path / "claude").mkdir()
    dirs = {"claude": creds(tmp_path / "claude"), "codex": codex, "opencode": tool_dir(tmp_path, "oc", '{"a": {}}')}
    run = fake({"claude": LOCAL_OK, "opencode": OC1},
               {STATUS: [LOGGED_OUT, LOCAL_OK], REMOTE_WRITE: [OK], OPENCODE_STATUS: [oc2_list()]})
    ok, lines = login_copy.copy_all(TARGET, run=run, dirs=dirs, platform="linux")
    assert ok and lines[0] == "alice@box-1: Claude Code is logged in there"
    assert lines[1].startswith("Codex keeps its login in the system keyring")
    assert lines[2].startswith("alice@box-1: OpenCode 2 there and OpenCode 1 here")
    # only notes: nothing was copied, so not a success
    dirs["claude"] = tmp_path / "none"
    run = fake({"opencode": OC1}, {OPENCODE_STATUS: [oc2_list()]})
    assert login_copy.copy_all(TARGET, run=run, dirs=dirs, platform="linux")[0] is False
