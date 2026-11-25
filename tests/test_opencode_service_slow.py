"""OpenCode 2's background service with no TUI on the machine, as the desktop app leaves it
(`opencode serve --service`, over SSH or locally): a session made through the service's own
API is listed by the adapter with its folder, and a push reaches it. Real `opencode` 2.x on
PATH, in a sandboxed home with no login: the pushed prompt is read back from the session, so
no model has to answer. Run with: pytest -m slow."""
import json
import os
import re
import shutil
import signal
import subprocess
import time

import pytest

from kaptyorka.adapters import opencode
from kaptyorka.adapters.opencode import OpenCodeAdapter

REAL_SERVICE, REAL_MAJOR = opencode.service, opencode.major  # conftest fakes both


def _v2() -> bool:
    if shutil.which("opencode") is None:
        return False
    r = subprocess.run(["opencode", "--version"], capture_output=True, text=True, timeout=10)
    m = re.search(r"(\d+)\.\d+", r.stdout)
    return bool(m) and int(m.group(1)) >= 2


pytestmark = [pytest.mark.slow,
              pytest.mark.skipif(not _v2(), reason="no OpenCode 2.x `opencode` on PATH")]


def test_service_session_with_no_tui_is_listed_and_gets_the_push(tmp_path, monkeypatch):
    home, project = tmp_path / "home", tmp_path / "proj-a"
    project.mkdir()
    home.mkdir()
    env = {**os.environ, "HOME": str(home), "XDG_CONFIG_HOME": str(home / ".config"),
           "XDG_STATE_HOME": str(home / ".local/state"), "XDG_DATA_HOME": str(home / ".local/share"),
           "XDG_CACHE_HOME": str(home / ".cache")}
    monkeypatch.setenv("XDG_STATE_HOME", env["XDG_STATE_HOME"])  # where service() looks
    monkeypatch.setenv("KAPTYORKA_HOME", str(home / ".kaptyorka"))  # no TUI notes: none here
    monkeypatch.setattr(opencode, "service", REAL_SERVICE)
    monkeypatch.setattr(opencode, "major", REAL_MAJOR)
    monkeypatch.setattr(opencode, "_tuis", lambda: [])  # this laptop's own OpenCode windows
    log = open(tmp_path / "serve.log", "w")
    srv = subprocess.Popen(["opencode", "serve", "--service"], cwd=home,
                           env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    try:
        svc, deadline = None, time.monotonic() + 60
        while time.monotonic() < deadline and svc is None:
            assert srv.poll() is None, (tmp_path / "serve.log").read_text()
            svc = opencode.service()
            if svc:
                try:
                    status, _ = opencode._v2_call(svc, "GET", "/api/info")
                    svc = svc if status == 200 else None
                except OSError:
                    svc = None
            time.sleep(0.3)
        assert svc, "the service never registered: " + (tmp_path / "serve.log").read_text()
        status, made = opencode._v2_call(svc, "POST", "/api/session",
                                         {"location": {"directory": str(project)}})
        assert status == 200, made
        sid = made["data"]["id"]
        got = [s for s in OpenCodeAdapter().sessions() if s.key == f"opencode:{sid}"]
        assert [(s.project, s.cwd) for s in got] == [("proj-a", str(project))]
        OpenCodeAdapter().deliver(f"opencode:{sid}", "hello from the hub")
        seen, deadline = "", time.monotonic() + 30
        while time.monotonic() < deadline and "hello from the hub" not in seen:
            seen = json.dumps([opencode._v2_call(svc, "GET", f"/api/session/{sid}/{part}")[1]
                               for part in ("message", "inbox")])
            time.sleep(0.5)
        assert "hello from the hub" in seen, seen
        print("service", opencode.service_version(svc), "session", sid, "listed and pushed")
    finally:
        os.killpg(srv.pid, signal.SIGTERM)
        try:
            srv.wait(10)
        except subprocess.TimeoutExpired:
            os.killpg(srv.pid, signal.SIGKILL)
            srv.wait()
        log.close()
