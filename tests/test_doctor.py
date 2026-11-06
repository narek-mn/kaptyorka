import json
from types import SimpleNamespace

import pytest

from kaptyorka import doctor, http, paths
from kaptyorka.adapters.claude import ClaudeAdapter

EXE = "/opt/bin/kaptyorka"


def write_claude(home, hooks=True, mcp=True):
    (home / ".claude").mkdir(parents=True, exist_ok=True)
    h = {}
    if hooks:
        h = {"SessionStart": [{"hooks": [{"type": "command", "command": f"{EXE} hook claude-code start"}]}],
             "UserPromptSubmit": [{"hooks": [{"type": "command", "command": f"{EXE} hook claude-code prompt"}]}]}
    (home / ".claude" / "settings.json").write_text(json.dumps({"hooks": h}))
    (home / ".claude.json").write_text(json.dumps({"mcpServers": {"kaptyorka": {}} if mcp else {}}))


class Run:
    def __init__(self, active=True, tmux=True):
        self.calls, self.active, self.tmux = [], active, tmux

    def __call__(self, cmd, **kw):
        self.calls.append(cmd)
        if cmd[0] == "tmux" and not self.tmux:
            raise FileNotFoundError
        if cmd[:3] == ["systemctl", "--user", "is-active"]:
            return SimpleNamespace(returncode=0 if self.active else 3, stdout="", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")


@pytest.fixture
def unit(home):
    d = home / ".config/systemd/user"
    d.mkdir(parents=True)
    (d / "kaptyorka-bridge.service").write_text("")


@pytest.fixture
def answer(monkeypatch):
    got = {"status": 200}

    def fake(method, url, token, body=None, **kw):
        if got.get("down"):
            raise http.HubUnreachable(url)
        return got["status"], {}

    monkeypatch.setattr(http, "request", fake)
    return got


CFG = {"hub": "http://h:1", "token": "t"}


def by_name(results):
    return {c.name: c for c in results}


def test_tool_configured_needs_mcp_and_hooks(home):
    a = ClaudeAdapter(home=home, run=Run())
    assert not doctor.tool_configured(a, home)
    write_claude(home)
    assert doctor.tool_configured(a, home)
    write_claude(home, hooks=False)
    assert not doctor.tool_configured(a, home)
    write_claude(home, mcp=False)
    assert not doctor.tool_configured(a, home)


def test_tool_configured_ignores_foreign_hook(home):
    a = ClaudeAdapter(home=home, run=Run())
    write_claude(home)
    s = json.loads((home / ".claude/settings.json").read_text())
    s["hooks"]["SessionStart"][0]["hooks"][0]["command"] = "/bin/other hook claude-code start"
    (home / ".claude/settings.json").write_text(json.dumps(s))
    assert not doctor.tool_configured(a, home)


def test_checks_all_ok(home, unit, answer):
    write_claude(home)
    a = ClaudeAdapter(home=home, run=Run())
    a.installed = lambda: True
    res = doctor.checks(CFG, [a], home, Run(), "linux")
    assert [c.name for c in res] == ["config", "hub reachable", "token valid", "bridge service",
                                     "tmux", "tool: claude-code"]
    assert all(c.ok for c in res)


def test_checks_failures_carry_fix_commands(home, unit, answer):
    answer["status"] = 401
    a = ClaudeAdapter(home=home, run=Run())
    a.installed = lambda: True
    res = by_name(doctor.checks(CFG, [a], home, Run(active=False, tmux=False), "linux"))
    assert res["token valid"].ok is False and res["hub reachable"].ok is True
    assert res["bridge service"].fix == "systemctl --user restart kaptyorka-bridge"
    assert res["tmux"].fix == "sudo apt install -y tmux"
    assert res["tool: claude-code"].ok is False
    answer["down"] = True
    assert not by_name(doctor.checks(CFG, [], home, Run(), "linux"))["hub reachable"].ok


def test_not_joined_and_hub_service_only_on_hub_machine(home, unit):
    res = by_name(doctor.checks({}, [], home, Run(), "linux"))
    assert not res["config"].ok and res["config"].fix == "kaptyorka join"
    assert "hub service" not in res
    (paths.home()).mkdir(parents=True, exist_ok=True)
    (paths.home() / "hub.json").write_text("{}")
    assert "hub service" in by_name(doctor.checks({}, [], home, Run(), "linux"))


def test_run_prints_lines_and_fix_rewires_and_restarts(monkeypatch, home, unit, answer, capsys):
    a = ClaudeAdapter(home=home, run=Run())
    a.installed = lambda: True
    configured = []
    a.configure = lambda exe: configured.append(exe) or write_claude(home) or []
    r = Run(active=False)

    def restart(name, run, platform):
        r.active = True
        return [f"{name}: restarted"]

    import kaptyorka.service as service
    monkeypatch.setattr(service, "restart", restart)
    assert doctor.run(CFG, fix=False, adapters=[a], home=home, run=r, platform="linux") == 1
    out = capsys.readouterr().out
    assert "FAIL bridge service: stopped -> systemctl --user restart kaptyorka-bridge" in out
    assert "ok   config" in out and configured == []
    assert doctor.run(CFG, fix=True, adapters=[a], home=home, run=r, platform="linux", exe=EXE) == 0
    assert configured == [EXE]
    assert "FAIL" not in capsys.readouterr().out
