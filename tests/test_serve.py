import getpass
import os
import socket
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
    install("kaptyorka-hub", ["/k", "serve"], run=fake_run(calls, fail=("launchctl",)), platform="darwin")
    plist = home / "Library/LaunchAgents/io.kaptyorka.kaptyorka-hub.plist"
    assert plist.exists()
    assert ["launchctl", "bootstrap", f"gui/{os.getuid()}", str(plist)] in calls
    assert calls.index(["launchctl", "bootout", f"gui/{os.getuid()}", str(plist)]) < calls.index(
        ["launchctl", "bootstrap", f"gui/{os.getuid()}", str(plist)])


def test_serve_creates_token_once_and_prints_three_lines(home, capsys, monkeypatch):
    monkeypatch.setattr(service, "install", lambda *a, **k: [])
    outs = []
    for _ in range(2):
        with pytest.raises(SystemExit) as e:
            run_cli(["serve", "--port", "9999"])
        assert e.value.code == 0
        outs.append(capsys.readouterr().out.splitlines())
    assert outs[0] == outs[1]
    token = (paths.home() / "hub-token").read_text().strip()
    host = socket.gethostname()
    assert outs[0] == [f"hub:   http://{host}:9999", f"token: {token}", f"page:  http://{host}:9999/#t={token}"]
    assert (paths.home() / "hub-token").stat().st_mode & 0o777 == 0o600


def test_who_prints_table(hub, home, capsys):
    hub.store.seen("k1", "m1", "claude", "proj")
    paths.save_config({"hub": hub.url, "token": hub.token})
    with pytest.raises(SystemExit) as e:
        run_cli(["who"])
    assert e.value.code == 0
    out = capsys.readouterr().out
    assert out.splitlines()[0].split() == ["NAME", "STATUS", "ACTIVITY", "LAST", "SEEN"]
    assert len(out.splitlines()) == 2


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
