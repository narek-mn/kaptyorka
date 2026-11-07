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
    assert "does not accept that token" in capsys.readouterr().err


def test_join_unreachable_writes_nothing(home, capsys):
    assert main("http://127.0.0.1:1", "T", "pc", adapters=[], install=no_service([])) == 1
    assert not paths.config_path().exists()
    assert "cannot reach the hub at http://127.0.0.1:1" in capsys.readouterr().err


def test_join_configures_installed_only(hub, home, capsys):
    on, off, calls = FakeAdapter("on"), FakeAdapter("off", installed=False), []
    assert main(hub.url + "/", "T", "pc", adapters=[on, off], install=no_service(calls)) == 0
    assert on.configured == [EXE] and off.configured == []
    assert paths.load_config() == {"hub": hub.url, "token": "T", "machine": "pc"}
    assert calls == [("kaptyorka-bridge", [EXE, "bridge"])]
    out = capsys.readouterr().out
    assert "on: wired on" in out and "bridge: service ok" in out
    assert out.endswith(f"done, nothing to restart\npage: {hub.url}/#t=T\n")


def test_join_twice_same_result(hub, home, fake_claude_cli):  # noqa: F811
    from kaptyorka.adapters.claude import ClaudeAdapter
    settings = home / ".claude/settings.json"
    main(hub.url, "T", "pc", adapters=[ClaudeAdapter(home, run=fake_claude_cli.run)], install=no_service([]))
    first = settings.read_text()
    main(hub.url, "T", "pc", adapters=[ClaudeAdapter(home, run=fake_claude_cli.run)], install=no_service([]))
    assert settings.read_text() == first and json.loads(first)["hooks"]


def test_join_prints_restart_notes(hub, home, capsys):
    a = FakeAdapter("claude-code", note="Claude Code: restart")
    assert main(hub.url, "T", "pc", adapters=[a, FakeAdapter("b")], install=no_service([])) == 0
    assert capsys.readouterr().out.splitlines()[-2:] == ["restart: Claude Code: restart",
                                                          f"page: {hub.url}/#t=T"]


def test_join_only_limits_adapters(hub, home):
    a, b = FakeAdapter("claude-code"), FakeAdapter("codex")
    assert main(hub.url, "T", "pc", adapters=[a, b], install=no_service([]), only=["claude-code"]) == 0
    assert a.configured and not b.configured
    assert paths.load_config()["tools"] == ["claude-code"]  # the bridge drives only these


def test_join_skip_service_env(hub, home, monkeypatch):
    monkeypatch.setenv("KAPTYORKA_SKIP_SERVICE", "1")
    calls = []
    assert main(hub.url, "T", "pc", adapters=[FakeAdapter("a")], install=no_service(calls)) == 0
    assert calls == []


def test_join_service_error_after_configs(hub, home, capsys):
    from kaptyorka.service import ServiceError

    def boom(*a, **k):
        raise ServiceError("no systemd")
    a = FakeAdapter("a")
    assert main(hub.url, "T", "pc", adapters=[a], install=boom) == 1
    cap = capsys.readouterr()
    assert a.configured and "bridge service failed: no systemd" in cap.err


class Raising(FakeAdapter):
    def configure(self, kaptyorka):
        raise RuntimeError("boom")


def test_join_adapter_failure_continues(hub, home, capsys):
    bad, good, calls = Raising("bad"), FakeAdapter("good"), []
    assert main(hub.url, "T", "pc", adapters=[bad, good], install=no_service(calls)) == 1
    cap = capsys.readouterr()
    assert "bad: failed: boom" in cap.err and good.configured and calls


def test_join_unknown_only_rejected(hub, home, capsys):
    a = FakeAdapter("codex")
    assert main(hub.url, "T", "pc", adapters=[a], install=no_service([]), only=["codx"]) == 2
    assert "unknown tool codx; valid: codex" in capsys.readouterr().err
    assert not paths.config_path().exists()


def test_join_nothing_installed(hub, home, capsys):
    a = FakeAdapter("codex", installed=False)
    assert main(hub.url, "T", "pc", adapters=[a], install=no_service([])) == 1
    assert "no supported agent tool found" in capsys.readouterr().err
    assert main(hub.url, "T", "pc", adapters=[a], install=no_service([]), only=["codex"]) == 1
    assert "codex is not installed" in capsys.readouterr().err


def test_join_hub_without_scheme_and_quoted_token(hub, home):
    bare = hub.url.split("://")[1]
    assert main(bare + "//", " 'T' ", "pc", adapters=[FakeAdapter("a")], install=no_service([])) == 0
    assert paths.load_config()["hub"] == hub.url and paths.load_config()["token"] == "T"


def test_join_passes_path_to_the_service(hub, home, monkeypatch):
    seen = {}

    def install(name, argv, **kw):
        seen.update(kw)
        return []
    monkeypatch.setenv("PATH", "/x/bin:/y/bin")
    main(hub.url, "T", "pc", adapters=[FakeAdapter("a")], install=install)
    assert seen["env"] == {"PATH": "/x/bin:/y/bin"}


def test_join_only_adds_to_earlier_tools(hub, home):
    a, b = FakeAdapter("claude-code"), FakeAdapter("codex")
    main(hub.url, "T", "pc", adapters=[a, b], install=no_service([]), only=["claude-code"])
    main(hub.url, "T", "pc", adapters=[a, b], install=no_service([]), only=["codex"])
    assert paths.load_config()["tools"] == ["claude-code", "codex"]
    main(hub.url, "T", "pc", adapters=[a, b], install=no_service([]))
    assert "tools" not in paths.load_config()


def test_redeem_returns_hub_token_secret(monkeypatch):
    from kaptyorka import join

    seen = {}

    def fake(method, url, token, body=None, **kw):
        seen.update(method=method, url=url, body=body)
        return 200, {"token": "tok", "secret": "sec", "hub": "http://h:1", "person": "alice"}

    monkeypatch.setattr("kaptyorka.http.request", fake)
    assert join.redeem("http://h:1/i/CODE123", "pc") == ("http://h:1", "tok", "sec", "alice")
    assert seen["method"] == "POST" and seen["url"] == "http://h:1/i/CODE123"
    assert seen["body"]["device_name"] == "pc" and seen["body"]["host"] and seen["body"]["os_user"]


def test_redeem_failure_message(monkeypatch, capsys):
    from kaptyorka import join

    monkeypatch.setattr("kaptyorka.http.request", lambda *a, **k: (400, "invite expired"))
    assert join.redeem("http://h:1/i/CODE123", "pc") is None
    assert "invite expired" in capsys.readouterr().err
    assert join.redeem("not a url", "pc") is None


def test_join_saves_secret(hub, home):
    a = FakeAdapter("a")
    assert main(hub.url, "T", "pc", adapters=[a], install=no_service([]), secret="s3") == 0
    assert paths.load_config()["secret"] == "s3"


def test_join_keeps_saved_secret_for_same_token(hub, home):
    paths.save_config({"hub": hub.url, "token": "T", "machine": "pc", "secret": "s3"})
    assert main(hub.url, "T", "pc", adapters=[FakeAdapter("a")], install=no_service([])) == 0
    assert paths.load_config()["secret"] == "s3"


def test_join_saves_person(hub, home):
    from kaptyorka import http, join

    assert http.request("POST", hub.url + "/api/people", "T", {"name": "alice"})[0] == 200
    _, inv = http.request("POST", hub.url + "/api/invites", "T", {"person": "alice"})
    got = join.redeem(inv["url"], "box-1")
    assert got is not None and got[3] == "alice"
    hub_url, token, secret, person = got
    assert main(hub_url, token, "box-1", adapters=[FakeAdapter("a")], install=no_service([]),
                secret=secret, person=person) == 0
    assert paths.load_config()["person"] == "alice"
    # a plain rejoin with the same token keeps it
    assert main(hub_url, token, "box-1", adapters=[FakeAdapter("a")], install=no_service([])) == 0
    assert paths.load_config()["person"] == "alice"


def test_join_env_stdin_reaches_service_env(hub, home):
    seen = []

    def install(name, argv, **kw):
        seen.append(kw.get("env"))
        return ["ok"]

    assert main(hub.url, "T", "pc", adapters=[FakeAdapter("a")], install=install,
                env={"API_KEY": "sk-x"}) == 0
    assert seen[0]["API_KEY"] == "sk-x" and "PATH" in seen[0]
    from kaptyorka.join import read_env
    assert read_env("A=1\nB= x=y \n") == {"A": "1", "B": " x=y "}


def test_read_env_refuses_bad_input():
    import pytest
    from kaptyorka.join import read_env
    for bad in ("", "\n", "A=1\n\nB=2\n", "bad line\n", "1A=x\n", "sk-secret\n"):
        with pytest.raises(ValueError, match="bad --env-stdin line") as e:
            read_env(bad)
        assert "secret" not in str(e.value)


def test_join_env_stdin_bad_line_exits_1(monkeypatch, home, capsys):
    import io
    from kaptyorka import cli
    monkeypatch.setattr("sys.stdin", io.StringIO("oops\n"))
    monkeypatch.setattr("sys.argv", ["kaptyorka", "join", "--hub", "h", "--token", "t", "--env-stdin"])
    import pytest
    with pytest.raises(SystemExit) as e:
        cli.entry()
    assert e.value.code == 1 and "bad --env-stdin line 1" in capsys.readouterr().err
