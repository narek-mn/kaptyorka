import json
import sys
from pathlib import Path

from kaptyorka import paths
from kaptyorka.join import main
from test_claude import fake_claude_cli  # noqa: F401

EXE = str(Path(sys.argv[0]).resolve())


class FakeAdapter:
    prefix = "fake"

    def __init__(self, tool, installed=True, note=None):
        self.tool, self._installed, self.restart_note = tool, installed, note
        self.configured = []

    def installed(self):
        return self._installed

    def configure(self, kaptyorka):
        self.configured.append(kaptyorka)
        return [f"wired {self.tool}"]


def no_service(calls):
    def install(name, argv, **kw):
        calls.append((name, argv))
        return ["service ok"]
    return install


def test_join_bad_token_writes_nothing(hub, home, capsys):
    a = FakeAdapter("a")
    assert main(hub.url, "bad", "pc", adapters=[a], install=no_service([])) == 1
    assert not paths.config_path().exists() and a.configured == []
    assert "wrong token" in capsys.readouterr().out


def test_join_unreachable_writes_nothing(home, capsys):
    assert main("http://127.0.0.1:1", "T", "pc", adapters=[], install=no_service([])) == 1
    assert not paths.config_path().exists()
    assert "kaptyorka hub unreachable at http://127.0.0.1:1" in capsys.readouterr().out


def test_join_configures_installed_only(hub, home, capsys):
    on, off, calls = FakeAdapter("on"), FakeAdapter("off", installed=False), []
    assert main(hub.url + "/", "T", "pc", adapters=[on, off], install=no_service(calls)) == 0
    assert on.configured == [EXE] and off.configured == []
    assert paths.load_config() == {"hub": hub.url, "token": "T", "machine": "pc"}
    assert calls == [("kaptyorka-bridge", [EXE, "bridge"])]
    out = capsys.readouterr().out
    assert "on: wired on" in out and "service ok" in out and out.endswith("done, nothing to restart\n")


def test_join_twice_same_result(hub, home, fake_claude_cli):  # noqa: F811
    from kaptyorka.adapters.claude import ClaudeAdapter
    settings = home / ".claude/settings.json"
    main(hub.url, "T", "pc", adapters=[ClaudeAdapter(home)], install=no_service([]))
    first = settings.read_text()
    main(hub.url, "T", "pc", adapters=[ClaudeAdapter(home)], install=no_service([]))
    assert settings.read_text() == first and json.loads(first)["hooks"]


def test_join_prints_restart_notes(hub, home, capsys):
    a = FakeAdapter("claude-code", note="Claude Code: restart")
    main(hub.url, "T", "pc", adapters=[a, FakeAdapter("b")], install=no_service([]))
    assert capsys.readouterr().out.splitlines()[-1] == "restart: Claude Code: restart"


def test_join_only_limits_adapters(hub, home):
    a, b = FakeAdapter("claude-code"), FakeAdapter("codex")
    main(hub.url, "T", "pc", adapters=[a, b], install=no_service([]), only=["claude-code"])
    assert a.configured and not b.configured


def test_join_skip_service_env(hub, home, monkeypatch):
    monkeypatch.setenv("KAPTYORKA_SKIP_SERVICE", "1")
    calls = []
    assert main(hub.url, "T", "pc", adapters=[], install=no_service(calls)) == 0
    assert calls == []


def test_join_service_error_after_configs(hub, home, capsys):
    from kaptyorka.service import ServiceError

    def boom(*a, **k):
        raise ServiceError("no systemd")
    a = FakeAdapter("a")
    assert main(hub.url, "T", "pc", adapters=[a], install=boom) == 1
    cap = capsys.readouterr()
    assert a.configured and "bridge service failed: no systemd" in cap.err
