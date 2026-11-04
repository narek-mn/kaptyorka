import os
import shlex
import subprocess
import threading
import time
from types import SimpleNamespace

import pytest

# Commands that touch the real machine: services, the pipx install, agent tools.
FENCED = {"systemctl", "launchctl", "loginctl", "systemd-run", "pipx",
          "claude", "codex", "opencode", "cursor"}


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


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setenv("KAPTYORKA_HOME", str(h / ".kaptyorka"))
    return h


def start_hub(store, port: int = 0):
    """Run a hub on 127.0.0.1 in a thread; return a stop() that is safe to call twice."""
    import uvicorn

    from kaptyorka.hub import build_app

    server = uvicorn.Server(uvicorn.Config(build_app(store, "T"), host="127.0.0.1", port=port,
                                           log_level="warning", timeout_graceful_shutdown=1))
    thread = threading.Thread(target=server.run)
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
    return stop


@pytest.fixture
def hub(tmp_path):
    from kaptyorka.store import Store

    store = Store(tmp_path / "hub.db")
    stop = start_hub(store)
    yield SimpleNamespace(url=f"http://127.0.0.1:{stop.port}", token="T", store=store)
    stop()
    store.close()
