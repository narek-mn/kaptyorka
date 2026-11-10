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
             "UserPromptSubmit": [{"hooks": [{"type": "command", "command": f"{EXE} hook claude-code prompt"}]}],
             "Stop": [{"hooks": [{"type": "command", "command": f"{EXE} hook claude-code stop"}]}]}
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
    assert res["bridge service"].fix == "kaptyorka doctor --fix"
    assert res["tmux"].fix == "sudo apt install -y tmux"
    assert res["tool: claude-code"].ok is False
    answer["down"] = True
    assert not by_name(doctor.checks(CFG, [], home, Run(), "linux"))["hub reachable"].ok


def test_hub_service_only_on_hub_machine(home, unit, answer):
    assert "hub service" not in by_name(doctor.checks(CFG, [], home, Run(), "linux"))
    (paths.home()).mkdir(parents=True, exist_ok=True)
    (paths.home() / "hub.json").write_text('{"host": "h", "port": 1}')
    assert "hub service" in by_name(doctor.checks(CFG, [], home, Run(), "linux"))


def test_doctor_not_joined_one_line(home, unit, capsys):
    res = doctor.checks({}, [], home, Run(), "linux")
    assert [c.name for c in res] == ["config"]
    assert doctor.run({}, adapters=[], home=home, run=Run(), platform="linux") == 1
    assert capsys.readouterr().out == "not joined: kaptyorka join <invite link>\n"


def test_doctor_fix_texts(home, unit, answer):
    answer["down"] = True
    res = by_name(doctor.checks(CFG, [], home, Run(active=False), "linux"))
    assert res["hub reachable"].fix == "is the hub running? On the hub machine: kaptyorka start"
    assert res["bridge service"].fix == "kaptyorka doctor --fix"
    answer["down"] = False
    answer["status"] = 401
    res = by_name(doctor.checks(CFG, [], home, Run(), "linux"))
    assert res["token valid"].fix == "ask your admin for a new invite link, then: kaptyorka join <link>"
    (paths.home()).mkdir(parents=True, exist_ok=True)
    (paths.home() / "hub.json").write_text('{"host": "h", "port": 1}')
    res = by_name(doctor.checks(CFG, [], home, Run(active=False), "linux"))
    assert res["hub service"].fix == "kaptyorka start"


def test_doctor_checks_only_the_joined_tools(home, unit, answer):
    on, off = fake_adapter("claude-code", home), fake_adapter("cursor", home)
    names = [c.name for c in doctor.checks({**CFG, "tools": ["claude-code"]}, [on, off], home, Run(), "linux")]
    assert "tool: claude-code" in names and "tool: cursor" not in names
    names = [c.name for c in doctor.checks(CFG, [on, off], home, Run(), "linux")]
    assert "tool: claude-code" in names and "tool: cursor" in names


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
    assert "FAIL bridge service: stopped -> kaptyorka doctor --fix" in out
    assert "ok   config" in out and configured == []
    assert doctor.run(CFG, fix=True, adapters=[a], home=home, run=r, platform="linux", exe=EXE) == 0
    assert configured == [EXE]
    assert "FAIL" not in capsys.readouterr().out


def fake_adapter(tool, home, installed=True):
    configured = []
    return SimpleNamespace(tool=tool, home=home, installed=lambda: installed,
                           configure=lambda exe: configured.append(exe) or [], configured=configured)


def put(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text if isinstance(text, str) else json.dumps(text))


def test_opencode_configured(home):
    a = SimpleNamespace(tool="opencode")
    assert not doctor.tool_configured(a, home)
    conf = home / ".config/opencode"
    put(conf / "opencode.json", {"mcp": {"kaptyorka": {"type": "local"}}})
    assert not doctor.tool_configured(a, home)  # plugin missing
    put(conf / "plugins/kaptyorka.js", "x")
    assert doctor.tool_configured(a, home)
    put(conf / "opencode.json", {"mcp": {}})
    assert not doctor.tool_configured(a, home)


def codex_files(conf, toml=True, hooks=True):
    put(conf / "config.toml", "[mcp_servers.kaptyorka]\ncommand = \"x\"\n" if toml else "")
    h = {}
    if hooks:
        h = {"SessionStart": [{"matcher": "startup|resume",
                               "hooks": [{"type": "command", "command": f"{EXE} hook codex start"}]}],
             "UserPromptSubmit": [{"hooks": [{"type": "command", "command": f"{EXE} hook codex prompt"}]}],
             "Stop": [{"hooks": [{"type": "command", "command": f"{EXE} hook codex stop"}]}]}
    put(conf / "hooks.json", {"hooks": h})


def test_codex_configured(home, monkeypatch):
    a = SimpleNamespace(tool="codex")
    assert not doctor.tool_configured(a, home)
    conf = home / ".codex"
    codex_files(conf)
    assert doctor.tool_configured(a, home)
    codex_files(conf, toml=False)
    assert not doctor.tool_configured(a, home)
    codex_files(conf, hooks=False)
    assert not doctor.tool_configured(a, home)
    other = home / "elsewhere"
    monkeypatch.setenv("CODEX_HOME", str(other))
    assert not doctor.tool_configured(a, home)  # honours codex_home
    codex_files(other)
    assert doctor.tool_configured(a, home)


def test_cursor_configured(home):
    a = SimpleNamespace(tool="cursor")
    assert not doctor.tool_configured(a, home)
    d = home / ".cursor"
    put(d / "mcp.json", {"mcpServers": {"kaptyorka": {}}})
    assert not doctor.tool_configured(a, home)  # hooks missing
    hooks = {"version": 1, "hooks": {"stop": [{"command": f"{EXE} hook cursor stop"}],
                                     "postToolUse": [{"command": f"{EXE} hook cursor tool"}]}}
    put(d / "hooks.json", hooks)
    assert doctor.tool_configured(a, home)
    del hooks["hooks"]["stop"]
    put(d / "hooks.json", hooks)
    assert not doctor.tool_configured(a, home)


def test_fix_reconfigures_only_the_failing_tool(home, unit, answer, capsys):
    good, bad = fake_adapter("claude-code", home), fake_adapter("cursor", home)
    write_claude(home)
    assert doctor.run(CFG, fix=True, adapters=[good, bad], home=home, run=Run(), platform="linux",
                      exe=EXE) == 1  # fake configure writes nothing: still failing
    assert bad.configured == [EXE] and good.configured == []
    out = capsys.readouterr().out
    assert "ok   tool: claude-code" in out and "FAIL tool: cursor" in out


def test_fix_does_not_restart_a_missing_bridge(home, answer, monkeypatch, capsys):
    import kaptyorka.service as service

    calls = []
    monkeypatch.setattr(service, "restart", lambda *a, **k: calls.append(a) or [])
    assert doctor.run(CFG, fix=True, adapters=[], home=home, run=Run(), platform="linux", exe=EXE) == 1
    assert calls == []
    assert "FAIL bridge service: not installed -> kaptyorka join" in capsys.readouterr().out


def test_missing_stop_hook_is_flagged(home):
    write_claude(home)
    a = SimpleNamespace(tool="claude-code")
    assert doctor.tool_configured(a, home)
    path = home / ".claude" / "settings.json"
    data = json.loads(path.read_text())
    del data["hooks"]["Stop"]
    path.write_text(json.dumps(data))
    assert not doctor.tool_configured(a, home)
    conf = home / ".codex"
    codex_files(conf)
    path = conf / "hooks.json"
    data = json.loads(path.read_text())
    del data["hooks"]["Stop"]
    path.write_text(json.dumps(data))
    assert not doctor.tool_configured(SimpleNamespace(tool="codex"), home)


def test_doctor_sees_portable_hooks(home):
    a = ClaudeAdapter(home=home, run=Run())
    write_claude(home)
    s = json.loads((home / ".claude/settings.json").read_text())
    for ev, n in (("SessionStart", "start"), ("UserPromptSubmit", "prompt"), ("Stop", "stop")):
        s["hooks"][ev][0]["hooks"][0]["command"] = (
            f"[ -x ~/.local/bin/kaptyorka ] && ~/.local/bin/kaptyorka hook claude-code {n} || true")
    (home / ".claude/settings.json").write_text(json.dumps(s))
    assert doctor.tool_configured(a, home)


def test_doctor_skips_hub_service_on_member(home, unit, answer):
    (paths.home()).mkdir(parents=True, exist_ok=True)
    (paths.home() / "hub.json").write_text('{"host": "box-1", "port": 8765}')
    cfg = {"hub": "http://10.0.0.9:8765", "token": "t"}
    res = by_name(doctor.checks(cfg, [], home, Run(active=False), "linux"))
    assert "hub service" not in res
