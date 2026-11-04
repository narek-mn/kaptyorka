import threading
import time
from types import SimpleNamespace

import pytest


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
