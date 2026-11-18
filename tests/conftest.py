import os
import shlex
import subprocess
import threading
import time
from types import SimpleNamespace

import pytest

# Commands that touch the real machine: services, the pipx install, agent tools.
FENCED = {"nvidia-smi", "systemctl", "launchctl", "loginctl", "systemd-run", "pipx",
          "claude", "codex", "opencode", "cursor"}


@pytest.fixture(autouse=True)
def opencode_v1(monkeypatch):
    """OpenCode V1 unless a test says otherwise: no `opencode --version`, no V2 service."""
    from kaptyorka.adapters import opencode

    monkeypatch.setattr(opencode, "major", lambda: 1)
    monkeypatch.setattr(opencode, "service", lambda: None)


@pytest.fixture(autouse=True)
def fence_real_commands(request, monkeypatch):
    """Fail any test (except `slow` ones) that would run a fenced command for real.

    Guards Popen itself, which run/check_output/call use, so a default argument
    bound to the real `subprocess.run` at import time is still caught.
    """
    if request.node.get_closest_marker("slow"):
        return
    real_init = subprocess.Popen.__init__

    def guarded(self, args, *a, **kw):
        argv = shlex.split(args) if isinstance(args, str) else list(args)
        name = os.path.basename(os.fsdecode(argv[0])) if argv else ""
        if name in FENCED:
            pytest.fail(f"test tried to run the real `{name}` ({argv!r}); inject a fake `run`",
                        pytrace=False)
        real_init(self, args, *a, **kw)

    monkeypatch.setattr(subprocess.Popen, "__init__", guarded)


class _Every:
    """Holds every project name; lists none."""

    def __contains__(self, name):
        return True

    def __iter__(self):
        return iter(())


@pytest.fixture(autouse=True)
def every_folder_is_a_team_project(request, monkeypatch):
    """Tests written before the team-project gate use any folder name. They keep working as if
    each were a registered team project; tests marked `team_only` get the real gate."""
    if request.node.get_closest_marker("team_only"):
        return
    from kaptyorka import offhub
    from kaptyorka.bridge import Bridge
    from kaptyorka.store import Store

    monkeypatch.setattr(Store, "team_project", lambda self, project: True)
    real = Bridge._team
    monkeypatch.setattr(Bridge, "_team", lambda self: (_Every(), real(self)[1]))
    monkeypatch.setattr(offhub, "personal", lambda *a, **k: False)
    monkeypatch.setattr(offhub, "save_team", lambda *a, **k: None)  # a shim runs as its own process


@pytest.fixture(autouse=True)
def no_real_gpu(monkeypatch):
    """The bridge's use reading never finds the real nvidia-smi; tests give a fake one."""
    from kaptyorka import usage

    monkeypatch.setattr(usage, "_nvidia_smi", lambda: None)


@pytest.fixture(autouse=True)
def hub_home(tmp_path, monkeypatch):
    """A hub's tick writes the manager's files under KAPTYORKA_HOME: never the real one."""
    monkeypatch.setenv("KAPTYORKA_HOME", str(tmp_path / "kaptyorka-home"))


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setenv("KAPTYORKA_HOME", str(h / ".kaptyorka"))
    return h


def start_hub(store, port: int = 0, **app_kw):
    """Run a hub on 127.0.0.1 in a thread; return a stop() that is safe to call twice."""
    import uvicorn

    from kaptyorka.hub import build_app

    app = build_app(store, **app_kw)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port,
                                           log_level="warning", timeout_graceful_shutdown=1))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        assert thread.is_alive(), "hub server failed to start"
        assert time.monotonic() < deadline, "hub server did not start within 10 s"
        time.sleep(0.01)

    def stop():
        server.should_exit = True
        thread.join()

    stop.port = server.servers[0].sockets[0].getsockname()[1]
    stop.app = app
    return stop


@pytest.fixture
def hub(tmp_path):
    from kaptyorka.store import Store

    from kaptyorka.manager import ManagerStore

    store = Store(tmp_path / "hub.db", legacy_token="T")
    ManagerStore(store).set_enabled(False, "test")  # no manager files racing the test's home
    stop = start_hub(store)
    yield SimpleNamespace(url=f"http://127.0.0.1:{stop.port}", token="T", store=store)
    stop()
    store.close()
