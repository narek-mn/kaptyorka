import json
import os
import tempfile
from pathlib import Path

import pytest

from fakes.codex_app_server import FakeCodex
from kaptyorka import hook, identity
from kaptyorka.adapters import codex
from kaptyorka.adapters.base import DeliveryError, Session
from kaptyorka.adapters.codex import CodexAdapter

CWD = os.getcwd()
APPROVE = "codex: approve the kaptyorka hooks once with /hooks in Codex"


def serve(codex_home: Path, threads, loaded):
    sock = codex_home / "app-server-control" / "app-server-control.sock"
    sock.parent.mkdir(parents=True, exist_ok=True)
    return FakeCodex(sock, threads, loaded, str(codex_home / "hooks.json"))


@pytest.fixture
def codex_home(monkeypatch):
    d = Path(tempfile.mkdtemp(prefix="cx"))  # short: the default socket path stays under 108 bytes
    monkeypatch.setenv("CODEX_HOME", str(d))
    yield d
    for p in sorted(d.rglob("*"), reverse=True):
        p.rmdir() if p.is_dir() else p.unlink()
    d.rmdir()


@pytest.fixture
def fake_codex(codex_home):
    srv = serve(codex_home, [
        {"id": "th-a", "cwd": CWD, "updatedAt": 300},
        {"id": "th-b", "cwd": "/p/beta", "updatedAt": 200},
        {"id": "th-old", "cwd": CWD, "updatedAt": 100},
        {"id": "th-gone", "cwd": CWD, "updatedAt": 999},  # not loaded: nobody has it open
    ], ["th-a", "th-ghost", "th-b", "th-old"])  # th-ghost: unloaded between the calls
    yield srv
    srv.stop()


def no_daemon_start(monkeypatch):
    runs = []
    monkeypatch.setattr(codex.subprocess, "run", lambda *a, **kw: runs.append(a[0]) or
                        codex.subprocess.CompletedProcess(a[0], 0, "", ""))
    return runs


def test_sessions_from_loaded_threads(fake_codex):
    project = os.path.basename(CWD)
    assert CodexAdapter().sessions() == [Session("codex:th-a", "codex", project, ""),
                                         Session("codex:th-b", "codex", "beta", ""),
                                         Session("codex:th-old", "codex", project, "")]
    assert [c[0] for c in fake_codex.calls[:2]] == ["initialize", "initialized"]


def test_deliver_starts_turn(fake_codex):
    CodexAdapter().deliver("codex:th-b", 'hi "team" ✓\nline2' + "x" * 70000)
    assert fake_codex.calls[-1] == ("turn/start", {
        "threadId": "th-b", "input": [{"type": "text", "text": 'hi "team" ✓\nline2' + "x" * 70000}]})


def test_deliver_rpc_error_raises(fake_codex):
    fake_codex.error = {"code": -32600, "message": "thread busy"}
    with pytest.raises(DeliveryError, match="thread busy"):
        CodexAdapter().deliver("codex:th-a", "hi")


def test_deliver_unloaded_thread_raises(fake_codex):
    with pytest.raises(DeliveryError):
        CodexAdapter().deliver("codex:th-gone", "hi")
    assert "turn/start" not in [c[0] for c in fake_codex.calls]


def test_deliver_without_daemon_raises(codex_home):
    with pytest.raises(DeliveryError):
        CodexAdapter().deliver("codex:th-a", "hi")


def test_codex_key_matches_shim_and_bridge(fake_codex):
    key = identity.RESOLVERS["codex"](12345, CWD)
    assert key == "codex:th-a"
    assert CodexAdapter().sessions()[0].key == key
    assert identity.RESOLVERS["codex"](12345, "/nowhere") is None


def test_identify_uses_shim_cwd(fake_codex, monkeypatch):
    # under the daemon the MCP server's parent is the daemon, whose cwd is not the project
    monkeypatch.setattr(identity, "ancestors", lambda pid: [(1, "codex")])
    ident = identity.identify("pc", pid=2)
    assert (ident.key, ident.tool, ident.project) == ("codex:th-a", "codex", os.path.basename(CWD))


def test_resolver_none_without_daemon(codex_home):
    assert identity.RESOLVERS["codex"](12345, CWD) is None


def test_long_socket_path(tmp_path, monkeypatch):
    deep = tmp_path / ("d" * 60) / ("e" * 60)
    monkeypatch.setenv("CODEX_HOME", str(deep))
    srv = serve(deep, [{"id": "th-x", "cwd": "/p/x", "updatedAt": 1}], ["th-x"])
    try:
        assert [s.key for s in CodexAdapter().sessions()] == ["codex:th-x"]
        assert os.getcwd() == CWD
    finally:
        srv.stop()


def test_sessions_start_daemon_at_most_once_a_minute(codex_home, monkeypatch):
    runs = no_daemon_start(monkeypatch)
    now = [1000.0]
    monkeypatch.setattr(codex.time, "monotonic", lambda: now[0])
    lines = []
    a = CodexAdapter(log=lines.append)
    assert a.sessions() == [] and a.sessions() == []
    now[0] += 61
    assert a.sessions() == []
    assert runs == [["codex", "app-server", "daemon", "start"]] * 2
    assert lines == ["codex: app-server daemon started"] * 2


def configure(home, kaptyorka="/k", **kw):
    return CodexAdapter(home, **kw).configure(kaptyorka)


def test_configure_toml_block_by_markers(home, codex_home, monkeypatch):
    monkeypatch.setenv("SHELL", "/bin/bash")
    toml = codex_home / "config.toml"
    toml.write_text('model = "o3"\n\n[mcp_servers.other]\ncommand = "x"\n')
    first = configure(home)
    text = toml.read_text()
    assert text == ('model = "o3"\n\n[mcp_servers.other]\ncommand = "x"\n\n# kaptyorka start\n'
                    '[mcp_servers.kaptyorka]\ncommand = "/k"\nargs = ["mcp"]\n# kaptyorka end\n')
    assert APPROVE in first
    assert "alias codex='codex --remote unix://'" in (home / ".bashrc").read_text().splitlines()
    assert len(first) == 4
    assert configure(home) == []
    assert toml.read_text() == text
    assert configure(home, "/new/k") == [f"Codex: kaptyorka hooks added to {codex_home / 'hooks.json'}",
                                         APPROVE, f"Codex: kaptyorka MCP server set in {toml}"]
    text = toml.read_text()
    assert text.count("# kaptyorka start") == 1 and '"/new/k"' in text and '"/k"' not in text
    assert text.startswith('model = "o3"\n\n[mcp_servers.other]\ncommand = "x"\n')
    assert (home / ".bashrc").read_text().count("alias codex=") == 1


def test_configure_merges_hooks_json(home, codex_home, monkeypatch):
    monkeypatch.setenv("SHELL", "/bin/zsh")
    hooks = codex_home / "hooks.json"
    other = {"hooks": [{"type": "command", "command": "other"}]}
    hooks.write_text(json.dumps({"hooks": {"SessionStart": [other]}}))
    configure(home, "/old/k")
    configure(home)
    assert json.loads(hooks.read_text()) == {"hooks": {
        "SessionStart": [other, {"matcher": "startup|resume", "hooks": [
            {"type": "command", "command": "/k hook codex start"}]}],
        "UserPromptSubmit": [{"hooks": [{"type": "command", "command": "/k hook codex prompt"}]}]}}


def test_configure_trusts_hooks_through_daemon(home, codex_home, fake_codex, monkeypatch):
    monkeypatch.setenv("SHELL", "/bin/bash")
    toml = codex_home / "config.toml"
    first = configure(home)
    assert APPROVE not in first
    assert "Codex: kaptyorka hooks trusted in " + str(toml) in first
    entries = {h["key"]: h["currentHash"] for h in fake_codex._hooks()}
    assert len(entries) == 2
    text = toml.read_text()
    for key, digest in entries.items():
        assert f'[hooks.state.{json.dumps(key)}]\ntrusted_hash = "{digest}"\n' in text
    assert text.index("[hooks.state.") < text.index("# kaptyorka end")
    fake_codex.trusted = entries
    assert configure(home) == []
    assert toml.read_text() == text


def test_configure_never_duplicates_a_trust_table(home, codex_home, fake_codex, monkeypatch):
    """Codex wrote its own entry when the user approved with /hooks; ours would be a duplicate."""
    monkeypatch.setenv("SHELL", "/bin/bash")
    configure(home, "/old/k")
    key = next(h["key"] for h in fake_codex._hooks() if h["command"].endswith("start"))
    toml = codex_home / "config.toml"
    toml.write_text(toml.read_text().split("# kaptyorka start")[0] +
                    f'[hooks.state.{json.dumps(key)}]\ntrusted_hash = "sha256:old"\n')
    fake_codex.trusted = {key: "sha256:old"}
    lines = configure(home)
    assert APPROVE in lines
    assert toml.read_text().count(f"[hooks.state.{json.dumps(key)}]") == 1


def test_configure_leaves_broken_hooks_json_alone(home, codex_home, monkeypatch):
    monkeypatch.setenv("SHELL", "/bin/bash")
    (codex_home / "hooks.json").write_text("{not json")
    lines = []
    configure(home, log=lines.append)
    assert (codex_home / "hooks.json").read_text() == "{not json"
    assert f"left alone, not valid JSON: {codex_home / 'hooks.json'}" in lines


def test_hook_formatter_codex():
    assert json.loads(hook.FORMATTERS["codex"]("start", "X")) == {
        "hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": "X"}}
    assert json.loads(hook.FORMATTERS["codex"]("prompt", "X")) == {
        "hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": "X"}}
