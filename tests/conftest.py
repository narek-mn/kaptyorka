import pytest


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setenv("KUBRIK_HOME", str(h / ".kubrik"))
    return h


@pytest.fixture
def hub(tmp_path):
    import threading
    import time
    from types import SimpleNamespace

    import uvicorn

    from kubrik.hub import build_app
    from kubrik.store import Store

    store = Store(tmp_path / "hub.db")
    server = uvicorn.Server(uvicorn.Config(build_app(store, "T"), host="127.0.0.1", port=0,
                                           log_level="warning"))
    thread = threading.Thread(target=server.run)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        assert thread.is_alive(), "hub server failed to start"
        assert time.monotonic() < deadline, "hub server did not start within 10 s"
        time.sleep(0.01)
    port = server.servers[0].sockets[0].getsockname()[1]
    yield SimpleNamespace(url=f"http://127.0.0.1:{port}", token="T", store=store)
    server.should_exit = True
    thread.join()
    store.close()
