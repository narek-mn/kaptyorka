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
            run_cli(["start", "--port", "9999", "--host", "10.0.0.5"])
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
        run_cli(["start"])
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


def test_stop_darwin_boots_out_then_disables(home):
    plist = home / "Library/LaunchAgents/io.kaptyorka.kaptyorka-hub.plist"
    plist.parent.mkdir(parents=True)
    plist.write_text("x")
    calls = []
    assert service.stop("kaptyorka-hub", run=fake_run(calls), platform="darwin") == [
        "kaptyorka-hub: stopped"]
    target = f"gui/{os.getuid()}/io.kaptyorka.kaptyorka-hub"
    assert calls == [["launchctl", "bootout", target], ["launchctl", "disable", target]]


def test_stop_darwin_without_plist_is_not_running(home):
    assert service.stop("kaptyorka-hub", run=fake_run([]), platform="darwin") == [
        "kaptyorka-hub: not running"]


def test_install_darwin_enables_before_bootstrap(home):
    calls = []
    install("kaptyorka-hub", ["/k", "serve"], run=fake_run(calls), platform="darwin")
    verbs = [c[1] for c in calls]
    assert verbs.index("enable") < verbs.index("bootstrap")
    assert calls[0] == ["launchctl", "enable", f"gui/{os.getuid()}/io.kaptyorka.kaptyorka-hub"]


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


def test_systemd_unit_has_short_stop_timeout():
    assert "TimeoutStopSec=10" in unit_text("kaptyorka-hub", ["/k"], "linux")


def _fake(calls, results):
    def run(cmd, **kw):
        calls.append(list(cmd))
        return subprocess.CompletedProcess(cmd, results.get(" ".join(cmd[:3]), 0), "", "boom")
    return run


def _install_unit(home, name="kaptyorka-hub"):
    u = home / ".config/systemd/user" / f"{name}.service"
    u.parent.mkdir(parents=True, exist_ok=True)
    u.write_text("x")


def test_restart_linux_restarts_enabled_unit(home):
    _install_unit(home)
    calls = []
    assert service.restart("kaptyorka-hub", run=_fake(calls, {}), platform="linux") == [
        "kaptyorka-hub: restarted"]
    assert calls[-1] == ["systemctl", "--user", "restart", "kaptyorka-hub"]


def test_restart_linux_starts_disabled_unit(home):
    _install_unit(home)
    calls = []
    service.restart("kaptyorka-hub", run=_fake(calls, {"systemctl --user is-enabled": 1}), platform="linux")
    assert calls[-1] == ["systemctl", "--user", "enable", "--now", "kaptyorka-hub"]


def test_restart_linux_not_installed(home):
    calls = []
    assert service.restart("kaptyorka-hub", run=_fake(calls, {}), platform="linux") == [
        "kaptyorka-hub: not installed"]
    assert calls == []


def test_restart_linux_failure_raises(home):
    _install_unit(home)
    with pytest.raises(service.ServiceError, match="boom"):
        service.restart("kaptyorka-hub", run=_fake([], {"systemctl --user restart": 1}), platform="linux")


def _plist(home):
    p = home / "Library/LaunchAgents/io.kaptyorka.kaptyorka-hub.plist"
    p.parent.mkdir(parents=True)
    p.write_text("x")
    return p


def test_restart_darwin_kickstarts_loaded_job(home):
    _plist(home)
    calls = []
    assert service.restart("kaptyorka-hub", run=_fake(calls, {}), platform="darwin") == [
        "kaptyorka-hub: restarted"]
    assert calls[-1] == ["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/io.kaptyorka.kaptyorka-hub"]


def test_restart_darwin_bootstraps_job_that_stop_unloaded(home):
    plist = _plist(home)
    calls = []
    service.restart("kaptyorka-hub", run=_fake(calls, {"launchctl print gui/%d/io.kaptyorka.kaptyorka-hub" % os.getuid(): 1}),
                    platform="darwin")
    assert [c[1] for c in calls] == ["print", "enable", "bootstrap"]
    assert calls[-1] == ["launchctl", "bootstrap", f"gui/{os.getuid()}", str(plist)]


def test_restart_darwin_not_installed(home):
    assert service.restart("kaptyorka-hub", run=_fake([], {}), platform="darwin") == [
        "kaptyorka-hub: not installed"]


def test_hub_exits_fast_on_sigterm_with_open_connection(home):
    import signal
    import socket as sk
    import threading
    import time
    import urllib.request

    with sk.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    (home / ".kaptyorka").mkdir()
    (home / ".kaptyorka/hub-token").write_text("T\n")
    exe = os.path.join(os.path.dirname(sys.executable), "kaptyorka")
    proc = subprocess.Popen([exe, "serve", "--foreground", "--port", str(port)],
                            stderr=subprocess.PIPE, text=True)
    try:
        deadline = time.monotonic() + 15
        while True:
            try:
                sk.create_connection(("127.0.0.1", port), timeout=1).close()
                break
            except OSError:
                assert proc.poll() is None and time.monotonic() < deadline, "hub did not start"
                time.sleep(0.05)
        req = urllib.request.Request(f"http://127.0.0.1:{port}/bridge/poll?machine=x&wait=25",
                                     headers={"Authorization": "Bearer T"})
        def poll():
            try:
                urllib.request.urlopen(req, timeout=30).read()
            except OSError:  # the hub going away ends the poll
                pass

        threading.Thread(target=poll, daemon=True).start()
        time.sleep(0.5)
        t0 = time.monotonic()
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=6)
        print(f"stop took {time.monotonic() - t0:.2f}s")
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait()
        proc.stderr.close()


def _pipx_prefix(tmp_path, monkeypatch):
    venv = tmp_path / "venv"
    venv.mkdir()
    (venv / "pipx_metadata.json").write_text("{}")
    monkeypatch.setattr(sys, "prefix", str(venv))


def test_update_reinstalls_then_restarts_only_running_units(tmp_path, monkeypatch, capsys):
    _pipx_prefix(tmp_path, monkeypatch)
    calls = []

    def run(cmd, **kw):
        calls.append(cmd[:2])
        return subprocess.CompletedProcess(cmd, 0, "kaptyorka 9.9.9\n", "")
    state = {"kaptyorka-bridge": True, "kaptyorka-hub": False}
    monkeypatch.setattr(service, "is_active", lambda name, **k: (calls.append(["active?", name]), state[name])[1])
    monkeypatch.setattr(service, "restart", lambda name, **k: [f"{name}: restarted"])
    assert cli._update(None, run=run) == 0
    assert calls[:3] == [["active?", "kaptyorka-bridge"], ["active?", "kaptyorka-hub"], ["pipx", "reinstall"]]
    out = capsys.readouterr().out.splitlines()
    assert out[0].endswith("-> 9.9.9")
    assert out[1:] == ["kaptyorka-bridge: restarted", "kaptyorka-hub: stopped, left off"]


@pytest.mark.parametrize("platform,unit,cmd", [
    ("linux", ".config/systemd/user/kaptyorka-hub.service", ["systemctl", "--user", "is-active"]),
    ("darwin", "Library/LaunchAgents/io.kaptyorka.kaptyorka-hub.plist", ["launchctl", "print"])])
def test_is_active(home, platform, unit, cmd):
    calls = []
    assert service.is_active("kaptyorka-hub", run=fake_run(calls), platform=platform) is None
    (home / unit).parent.mkdir(parents=True)
    (home / unit).write_text("x")
    assert service.is_active("kaptyorka-hub", run=fake_run(calls), platform=platform) is True
    assert service.is_active("kaptyorka-hub", run=fake_run(calls, fail=(cmd[0],)), platform=platform) is False
    assert calls[0][:len(cmd)] == cmd


def test_update_failed_reinstall_does_not_restart(tmp_path, monkeypatch, capsys):
    _pipx_prefix(tmp_path, monkeypatch)
    monkeypatch.setattr(service, "restart", lambda *a, **k: pytest.fail("restarted"))
    run = lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, "", "no network")
    assert cli._update(None, run=run) == 1
    assert "no network" in capsys.readouterr().err


def test_update_needs_pipx_install(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(sys, "prefix", str(tmp_path))
    assert cli._update(None, run=lambda *a, **k: pytest.fail("ran")) == 1
    assert "not installed with pipx" in capsys.readouterr().err


def test_start_again_uses_saved_port_and_host(home, capsys, monkeypatch):
    monkeypatch.setattr(service, "install", lambda name, argv, **k: seen.append(argv) or [])
    seen = []
    for args in (["--port", "9999", "--host", "10.0.0.5"], []):
        with pytest.raises(SystemExit):
            run_cli(["start", *args])
    out = capsys.readouterr().out.splitlines()
    assert out[0] == out[3] == "hub:   http://10.0.0.5:9999"
    assert seen[0] == seen[1] and seen[1][-2:] == ["--port", "9999"]


def test_join_without_flags_when_never_joined(home, capsys):
    with pytest.raises(SystemExit) as e:
        run_cli(["join"])
    assert e.value.code == 1
    assert "not joined yet: run kaptyorka join --hub <address> --token <token>" in capsys.readouterr().err


def test_join_without_flags_reruns_join_with_saved_arguments(home, monkeypatch):
    paths.save_config({"hub": "http://h:1", "token": "T", "machine": "pc", "tools": ["codex"]})
    from kaptyorka import join
    seen = []
    monkeypatch.setattr(join, "main", lambda *a, **k: seen.append((a, k)) or 0)
    with pytest.raises(SystemExit) as e:
        run_cli(["join"])
    assert e.value.code == 0 and seen == [(("http://h:1", "T", "pc"), {"only": ["codex"]})]


def test_join_with_flags_joins(home, monkeypatch):
    from kaptyorka import join
    seen = []
    monkeypatch.setattr(join, "main", lambda *a, **k: seen.append((a, k)) or 0)
    with pytest.raises(SystemExit) as e:
        run_cli(["join", "--hub", "h:1", "--token", "T", "--name", "pc", "--only", "cursor"])
    assert e.value.code == 0 and seen == [(("h:1", "T", "pc"), {"only": ["cursor"]})]


def test_join_hub_without_token_is_an_error(home):
    with pytest.raises(SystemExit) as e:
        run_cli(["join", "--hub", "h"])
    assert e.value.code == 2


@pytest.mark.parametrize("platform", ["linux", "darwin"])
def test_start_without_saved_settings_keeps_port_of_older_hub_unit(home, capsys, monkeypatch, platform):
    exe_argv = ["/opt/x/kaptyorka", "serve", "--foreground", "--port", "9001"]
    unit = (home / ".config/systemd/user/kaptyorka-hub.service" if platform == "linux"
            else home / "Library/LaunchAgents/io.kaptyorka.kaptyorka-hub.plist")
    unit.parent.mkdir(parents=True)
    unit.write_text(unit_text("kaptyorka-hub", exe_argv, platform))
    seen = []
    monkeypatch.setattr(service, "install", lambda name, argv, **k: seen.append(argv) or [])
    with pytest.raises(SystemExit):
        run_cli(["start"])
    assert seen[0][-2:] == ["--port", "9001"]
