"""A machine joined over an earlier setup (its own `kaptyorka start`, or another hub): join
restarts the running bridge so it reads the new config, says so, names a hub this account
still runs, and "online" for a machine means its bridge reported, not any token use."""
import subprocess
from types import SimpleNamespace

import pytest

from kaptyorka import cli, http, paths, service
from kaptyorka.join import main
from test_hub_workers import alice, bridge, heartbeat, registered  # noqa: F401 (fixtures)
from test_join import FakeAdapter

pytestmark = pytest.mark.usefixtures("home")


def recording(calls):
    def install(name, argv, **kw):
        calls.append(kw.get("keep_running"))
        return []  # a unit already running with the same text: install leaves it alone
    return install


def test_join_to_another_hub_restarts_the_running_bridge(hub, capsys):
    paths.save_config({"hub": "http://127.0.0.1:8765", "token": "own", "machine": "pc"})
    calls = []
    assert main(hub.url, "T", "pc", adapters=[FakeAdapter("a")], install=recording(calls),
                local_hub=lambda: None) == 0
    assert calls == [False]  # not "keep it running as it is"
    assert f"bridge: restarted, so it reports to {hub.url}" in capsys.readouterr().out


def test_join_again_to_the_same_hub_leaves_it_running(hub, capsys):
    paths.save_config({"hub": hub.url, "token": "T", "machine": "pc"})
    calls = []
    assert main(hub.url, "T", "pc", adapters=[FakeAdapter("a")], install=recording(calls),
                local_hub=lambda: None) == 0
    assert calls == [True] and "restarted" not in capsys.readouterr().out


def test_after_leave_join_starts_it_as_a_first_join(hub, capsys):
    calls = []  # leave removed the config
    assert main(hub.url, "T", "pc", adapters=[FakeAdapter("a")], install=recording(calls),
                local_hub=lambda: None) == 0
    assert calls == [True] and "restarted" not in capsys.readouterr().out


def test_the_invite_flow_says_the_config_moved(monkeypatch, capsys):
    """The invite flow saves the new config before join.main: it passes moved itself."""
    paths.save_config({"hub": "http://127.0.0.1:8765", "token": "own", "machine": "pc"})
    from kaptyorka import join

    monkeypatch.setattr(join, "redeem", lambda link, name: ("http://h:1", "new", "s", "alice"))
    got = {}
    monkeypatch.setattr(join, "main", lambda *a, **k: got.update(k) or 0)
    args = SimpleNamespace(env_stdin=False, invite="http://h:1/i/abc", name="pc", only=None,
                           hub=None, token=None)
    assert cli._join(args) == 0
    assert got["moved"] is True


def test_a_real_unit_that_runs_unchanged_is_restarted(tmp_path, monkeypatch):
    unit_cmds = []

    def run(cmd, **kw):
        unit_cmds.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")  # every check says yes: it runs
    argv = ["/x/kaptyorka", "bridge"]
    service.install("kaptyorka-bridge", argv, run=run, platform="linux", keep_running=True)
    unit_cmds.clear()
    service.install("kaptyorka-bridge", argv, run=run, platform="linux", keep_running=True)
    assert ["systemctl", "--user", "restart", "kaptyorka-bridge"] not in unit_cmds
    service.install("kaptyorka-bridge", argv, run=run, platform="linux", keep_running=False)
    assert ["systemctl", "--user", "restart", "kaptyorka-bridge"] in unit_cmds


@pytest.mark.parametrize("state", [True, False])  # running, or installed and stopped
def test_join_elsewhere_stops_the_local_hub_and_keeps_its_data(hub, capsys, state):
    paths.save_config({"hub": "http://127.0.0.1:8765", "token": "own", "machine": "pc"})
    (paths.home() / "hub.db").write_text("hub data")
    stopped = []
    assert main(hub.url, "T", "pc", adapters=[FakeAdapter("a")], install=recording([]),
                local_hub=lambda: state, stop_hub=lambda: stopped.append(1)) == 0
    assert stopped == [1]
    assert (f"stopped this machine's own hub (kaptyorka start had been run here); its data is"
            f" kept in {paths.home()}") in capsys.readouterr().out
    assert (paths.home() / "hub.db").read_text() == "hub data"


def test_no_local_hub_nothing_to_stop(hub, capsys):
    assert main(hub.url, "T", "pc", adapters=[FakeAdapter("a")], install=recording([]),
                local_hub=lambda: None, stop_hub=lambda: pytest.fail("no hub here")) == 0


def test_joining_its_own_hub_keeps_it_running(hub, capsys):
    port = int(hub.url.rsplit(":", 1)[1])
    (paths.home() / "hub.json").write_text(f'{{"port": {port}, "host": "127.0.0.1"}}')
    assert main(hub.url, "T", "pc", adapters=[FakeAdapter("a")], install=recording([]),
                local_hub=lambda: True, stop_hub=lambda: pytest.fail("its own hub")) == 0
    assert "own hub" not in capsys.readouterr().out


def test_stop_hub_stops_only_the_hub(monkeypatch, capsys):
    stopped = []
    monkeypatch.setattr(service, "is_active", lambda unit, **k: True)
    monkeypatch.setattr(service, "stop", lambda unit, **k: stopped.append(unit) or [f"{unit}: stopped"])
    assert cli._stop(SimpleNamespace(hub=True)) == 0
    assert stopped == ["kaptyorka-hub"]
    assert "hub stopped" in capsys.readouterr().out
    assert cli._stop(SimpleNamespace(hub=False)) == 0 and stopped[1:] == [
        "kaptyorka-bridge", "kaptyorka-hub"]


# ---- online means the bridge reported ----

def machine_row(hub, token, device_id):
    status, machines = http.request("GET", hub.url + "/api/machines", token)
    assert status == 200
    return next(d for m in machines for d in m["devices"] if d["id"] == device_id)


def test_online_is_the_bridge_not_any_token_use(hub, alice):  # noqa: F811
    pid, client = alice
    code = hub.store.invite_create(pid)
    token, _ = hub.store.invite_redeem(code, "box-1", host="box-1", os_user="u")
    dev = hub.store.authenticate(token).device_id  # a token call: last_used is fresh now
    # a shim's heartbeat (no tools) and a one-off drop (no facts) never count
    assert http.request("POST", hub.url + "/bridge/heartbeat", token, {
        "machine": "box-1", "sessions": [], "facts": {"host": "box-1"}})[0] in (200, 204)
    assert http.request("POST", hub.url + "/bridge/heartbeat", token, {
        "machine": "box-1", "sessions": [], "tools": [], "gone": ["k"]})[0] in (200, 204)
    http.request("GET", hub.url + "/api/feed", token)
    assert machine_row(hub, client.token, dev)["online"] is False
    assert heartbeat(hub, token, "box-1")[0] == 200  # the bridge itself
    assert machine_row(hub, client.token, dev)["online"] is True


def test_a_bridge_says_it_is_one(hub, alice):  # noqa: F811
    pid, client = alice
    code = hub.store.invite_create(pid)
    token, _ = hub.store.invite_redeem(code, "box-2", host="box-2", os_user="u")
    dev = hub.store.authenticate(token).device_id
    assert http.request("POST", hub.url + "/bridge/heartbeat", token, {
        "machine": "box-2", "sessions": [], "bridge": True,
        "facts": {"host": "box-2", "os_user": "u"}})[0] in (200, 204)
    assert machine_row(hub, client.token, dev)["online"] is True
