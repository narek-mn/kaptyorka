import getpass
import os
import subprocess
import sys
from unittest import mock

import pytest

from kaptyorka import cli, paths, service
from kaptyorka.service import install, unit_text


def run_cli(args):
    with mock.patch.object(sys, "argv", ["kaptyorka", *args]):
        cli.entry()


def fake_run(calls, fail=()):
    def run(cmd, **kw):
        calls.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 1 if cmd[0] in fail else 0, "", "")
    return run


def test_systemd_unit_runs_absolute_kaptyorka():
    text = unit_text("kaptyorka-hub", ["/opt/x/kaptyorka", "serve", "--foreground", "--port", "8765"], "linux")
    assert "ExecStart=/opt/x/kaptyorka serve --foreground --port 8765" in text
    assert "Restart=always" in text and "WantedBy=default.target" in text


def test_launchd_plist_keepalive(home):
    text = unit_text("kaptyorka-bridge", ["/opt/x/kaptyorka", "bridge"], "darwin")
    assert "<key>KeepAlive</key>" in text and "<key>RunAtLoad</key>" in text
    assert "<string>/opt/x/kaptyorka</string>" in text
    assert "kaptyorka-bridge.log" in text


def test_install_linux_commands(home):
    calls = []
    install("kaptyorka-hub", ["/k", "serve"], run=fake_run(calls), platform="linux")
    assert ["systemctl", "--user", "daemon-reload"] in calls
    assert ["systemctl", "--user", "enable", "--now", "kaptyorka-hub"] in calls
    assert (home / ".config/systemd/user/kaptyorka-hub.service").exists()


def test_install_linux_restarts_active_service(home):
    calls = []
    install("kaptyorka-hub", ["/k", "serve"], run=fake_run(calls), platform="linux")
    assert ["systemctl", "--user", "is-active", "--quiet", "kaptyorka-hub"] in calls
    assert ["systemctl", "--user", "restart", "kaptyorka-hub"] in calls


def test_install_linux_linger_failure_prints_one_command(home):
    user = getpass.getuser()
    lines = install("kaptyorka-hub", ["/k", "serve"], run=fake_run([], fail=("loginctl",)), platform="linux")
    assert f"run once: sudo loginctl enable-linger {user}" in lines


def test_install_darwin_bootstrap(home):
    calls = []
    lines = install("kaptyorka-hub", ["/k", "serve"], run=fake_run(calls), platform="darwin")
    plist = home / "Library/LaunchAgents/io.kaptyorka.kaptyorka-hub.plist"
    assert plist.exists()
    assert lines == [f"installed {plist}"]
    assert ["launchctl", "bootstrap", f"gui/{os.getuid()}", str(plist)] in calls
    assert calls.index(["launchctl", "bootout", f"gui/{os.getuid()}", str(plist)]) < calls.index(
        ["launchctl", "bootstrap", f"gui/{os.getuid()}", str(plist)])


def test_serve_creates_token_once_and_prints_three_lines(home, capsys, monkeypatch):
    monkeypatch.setattr(service, "install", lambda *a, **k: [])
    outs = []
    for _ in range(2):
        with pytest.raises(SystemExit) as e:
            run_cli(["serve", "--port", "9999", "--host", "10.0.0.5"])
        assert e.value.code == 0
        outs.append(capsys.readouterr().out.splitlines())
    assert outs[0] == outs[1]
    token = (paths.home() / "hub-token").read_text().strip()
    assert outs[0] == ["hub:   http://10.0.0.5:9999", f"token: {token}", f"page:  http://10.0.0.5:9999/#t={token}"]
    assert (paths.home() / "hub-token").stat().st_mode & 0o777 == 0o600


def test_who_prints_table(hub, home, capsys):
    hub.store.heartbeat("m1", [{"key": "k1", "tool": "claude", "project": "proj", "label": "my-label"}])
    paths.save_config({"hub": hub.url, "token": hub.token})
    with pytest.raises(SystemExit) as e:
        run_cli(["who"])
    assert e.value.code == 0
    out = capsys.readouterr().out
    assert out.splitlines()[0].split() == ["NAME", "LABEL", "STATUS", "ACTIVITY", "LAST", "SEEN"]
    assert len(out.splitlines()) == 2
    assert paths.load_config() and hub.store.online()[0].name in out and "my-label" in out


def test_who_not_joined(home, capsys):
    with pytest.raises(SystemExit) as e:
        run_cli(["who"])
    assert e.value.code == 1
    assert "not joined: run kaptyorka join" in capsys.readouterr().out


def test_who_unreachable(home, capsys):
    paths.save_config({"hub": "http://127.0.0.1:1", "token": "T"})
    with pytest.raises(SystemExit) as e:
        run_cli(["who"])
    assert e.value.code == 1
    assert "kaptyorka hub unreachable at http://127.0.0.1:1" in capsys.readouterr().out


def test_who_non_200(hub, home, capsys):
    paths.save_config({"hub": hub.url, "token": "X"})
    with pytest.raises(SystemExit) as e:
        run_cli(["who"])
    assert e.value.code == 1
    assert f"kaptyorka hub at {hub.url} answered 401" in capsys.readouterr().out


def test_install_linux_enable_failure_raises(home):
    def run(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 1 if "enable" in cmd else 0, "", "boom")
    with pytest.raises(service.ServiceError, match="boom"):
        install("kaptyorka-hub", ["/k"], run=run, platform="linux")


def test_install_missing_systemctl_raises(home):
    def run(cmd, **kw):
        raise FileNotFoundError
    with pytest.raises(service.ServiceError):
        install("kaptyorka-hub", ["/k"], run=run, platform="linux")


def test_serve_service_error_exits_1(home, capsys, monkeypatch):
    def boom(*a, **k):
        raise service.ServiceError("systemctl said no")
    monkeypatch.setattr(service, "install", boom)
    with pytest.raises(SystemExit) as e:
        run_cli(["serve"])
    assert e.value.code == 1
    cap = capsys.readouterr()
    assert cap.out == "" and "systemctl said no" in cap.err


def test_darwin_bootstrap_retries(home):
    calls, n = [], []
    def run(cmd, **kw):
        calls.append(list(cmd))
        bad = cmd[1] == "bootstrap" and len(n) < 2
        if cmd[1] == "bootstrap":
            n.append(1)
        return subprocess.CompletedProcess(cmd, 1 if bad else 0, "", "busy")
    install("kaptyorka-hub", ["/k"], run=run, platform="darwin", sleep=lambda s: None)
    assert sum(c[1] == "bootstrap" for c in calls) == 3


def test_systemd_unit_quotes_and_escapes_percent():
    text = unit_text("x", ["/a b/k", "100%"], "linux")
    assert "ExecStart='/a b/k' 100%%" in text and "RestartSec=2" in text


def test_systemd_unit_sets_environment(home):
    text = unit_text("kaptyorka-bridge", ["/k", "bridge"], "linux",
                     {"PATH": '/a b:/h/%u:"x"\\y'})
    assert 'Environment="PATH=/a b:/h/%%u:\\"x\\"\\\\y"\n' in text
    assert text.index("Environment=") < text.index("ExecStart=")


def test_launchd_plist_sets_environment(home):
    text = unit_text("kaptyorka-bridge", ["/k", "bridge"], "darwin", {"PATH": "/a&b:/c"})
    assert "<key>EnvironmentVariables</key>" in text
    assert "<key>PATH</key><string>/a&amp;b:/c</string>" in text
    assert "EnvironmentVariables" not in unit_text("n", ["/k"], "darwin")


def test_stop_linux_disables_unit():
    calls = []
    assert service.stop("kaptyorka-bridge", run=fake_run(calls), platform="linux") == [
        "kaptyorka-bridge: stopped"]
    assert calls == [["systemctl", "--user", "disable", "--now", "kaptyorka-bridge"]]


def test_stop_darwin_boots_out_unit():
    calls = []
    service.stop("kaptyorka-hub", run=fake_run(calls), platform="darwin")
    assert calls == [["launchctl", "bootout", f"gui/{os.getuid()}/io.kaptyorka.kaptyorka-hub"]]


def test_stop_not_installed_is_fine():
    assert service.stop("kaptyorka-hub", run=fake_run([], fail=("systemctl",)),
                        platform="linux") == ["kaptyorka-hub: not running"]


def test_stop_command_prints_both_and_exits_0(capsys, monkeypatch):
    seen = []
    monkeypatch.setattr(service, "stop", lambda name, **k: seen.append(name) or [f"{name}: stopped"])
    with pytest.raises(SystemExit) as e:
        run_cli(["stop"])
    assert e.value.code == 0 and seen == ["kaptyorka-bridge", "kaptyorka-hub"]
    assert "kaptyorka-hub: stopped" in capsys.readouterr().out
