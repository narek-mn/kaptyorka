"""OpenCode works without the shell alias: the wrapper, doctor's check, real session ids,
pushes that fail visibly, the login check and the machine's current name."""
import json
import os
import subprocess
import sys
from types import SimpleNamespace

import pytest

from helpers import person
from kaptyorka import doctor, http, identity, paths
from kaptyorka import shim as shim_mod
from kaptyorka.adapters import opencode
from kaptyorka.adapters.base import DeliveryError
from kaptyorka.adapters.opencode import ALIAS, OpenCodeAdapter
from kaptyorka.bridge import Bridge
from kaptyorka.launch import Launcher
from kaptyorka.store import Store
from test_bridge import FakeAdapter

BASH_RC = ".bash_profile" if sys.platform == "darwin" else ".bashrc"  # add_alias's pick

REAL_LOGIN_PATH = opencode.login_path  # conftest fakes it


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "hub.db")
    yield s
    s.close()


@pytest.fixture
def bins(home, tmp_path, monkeypatch):
    """A fake real `opencode` (prints its arguments) in its own folder, and ~/.local/bin."""
    monkeypatch.setenv("SHELL", "/bin/bash")
    real = tmp_path / "realbin"
    real.mkdir()
    (real / "opencode").write_text('#!/bin/sh\necho "real $*"\n')
    (real / "opencode").chmod(0o755)
    local = home / ".local" / "bin"
    local.mkdir(parents=True)
    return SimpleNamespace(real=real, local=local)


def wired(home, path, monkeypatch):
    monkeypatch.setattr(opencode, "login_path", lambda run=None: path)
    return OpenCodeAdapter(home, log=lambda line: None)


def wrap(w, *args, path):
    r = subprocess.run(["sh", str(w), *args], capture_output=True, text=True,
                       env={"PATH": path, "HOME": os.environ["HOME"]})
    return r.stdout.strip()


def test_the_wrapper_adds_port_0_to_the_tui_only(home, bins, monkeypatch):
    path = f"{bins.local}:{bins.real}:/usr/bin:/bin"
    a = wired(home, path, monkeypatch)
    (home / ".bashrc").write_text(ALIAS + "\n")
    lines = a._server_on(False)
    w = bins.local / "opencode"
    assert os.access(w, os.X_OK) and any("wrapper written" in line for line in lines)
    assert ALIAS not in (home / ".bashrc").read_text()  # the wrapper covers new and old shells
    assert wrap(w, path=path) == "real --port 0"
    assert wrap(w, "-c", path=path) == "real --port 0 -c"
    assert wrap(w, str(home), path=path) == f"real --port 0 {home}"  # opencode <folder>
    assert wrap(w, "--port", "4096", path=path) == "real --port 4096"
    assert wrap(w, "--mini", path=path) == "real --mini"  # --port is refused there
    assert wrap(w, "auth", "list", path=path) == "real auth list"
    assert a._server_on(False) == []  # once
    # the real binary moved: the next one on PATH, never the wrapper itself
    (bins.real / "opencode").unlink()
    other = bins.real.parent / "otherbin"
    other.mkdir()
    (other / "opencode").write_text('#!/bin/sh\necho "moved $*"\n')
    (other / "opencode").chmod(0o755)
    assert wrap(w, path=f"{bins.local}:{other}:/usr/bin:/bin") == "moved --port 0"
    r = subprocess.run(["sh", str(w)], capture_output=True, text=True,
                       env={"PATH": f"{bins.local}:/usr/bin:/bin"})
    assert r.returncode == 127 and "not found" in r.stderr


def test_no_wrapper_where_it_would_not_come_first(home, bins, monkeypatch):
    for path in (f"{bins.real}:{bins.local}:/usr/bin:/bin", f"{bins.real}:/usr/bin:/bin"):
        (home / BASH_RC).write_text("")
        wired(home, path, monkeypatch)._server_on(False)
        assert not (bins.local / "opencode").exists()
        assert ALIAS in (home / BASH_RC).read_text()  # the fallback


def test_a_real_opencode_in_local_bin_is_never_touched(home, bins, monkeypatch):
    mine = bins.local / "opencode"
    mine.write_text("#!/bin/sh\necho mine\n")
    wired(home, f"{bins.local}:/usr/bin:/bin", monkeypatch)._server_on(False)
    assert mine.read_text() == "#!/bin/sh\necho mine\n"
    assert ALIAS in (home / BASH_RC).read_text()


def test_configure_unconfigure_v2_and_bridge_start(home, bins, monkeypatch):
    path = f"{bins.local}:{bins.real}:/usr/bin:/bin"
    a = wired(home, path, monkeypatch)
    w = bins.local / "opencode"
    assert a.install_command("/k") == []  # not joined yet: nothing written at bridge start
    a.configure("/k")
    assert opencode._ours(w)
    w.unlink()
    assert any("wrapper written" in line for line in a.install_command("/k"))  # older joins
    monkeypatch.setattr(opencode, "major", lambda: 2)
    assert any("wrapper removed" in line for line in a.install_command("/k"))  # no --port on V2
    monkeypatch.setattr(opencode, "major", lambda: 1)
    a.configure("/k")
    lines = a.unconfigure()
    assert not w.exists() and any("wrapper removed" in line for line in lines)
    assert (bins.real / "opencode").exists()


def test_a_worker_starts_through_the_wrapper(home, bins, monkeypatch):
    from test_launch import order as launch_order
    order = launch_order(tool="opencode")
    launcher = Launcher({"machine": "box-1"}, home)
    assert launcher.command(order, "/w")[7] == "opencode"
    wired(home, f"{bins.local}:{bins.real}:/usr/bin:/bin", monkeypatch)._server_on(False)
    assert launcher.command(order, "/w")[7] == str(bins.local / "opencode")


def test_login_path_asks_a_login_shell(monkeypatch):
    monkeypatch.setenv("SHELL", "/bin/zsh")
    seen = []

    def run(argv, **kw):
        seen.append(argv)
        return subprocess.CompletedProcess(argv, 0, "motd\n/a:/b", "")
    assert REAL_LOGIN_PATH(run) == "/a:/b" and seen[0][:2] == ["/bin/zsh", "-lc"]
    monkeypatch.setenv("PATH", "/fallback")
    assert REAL_LOGIN_PATH(lambda *a, **k: (_ for _ in ()).throw(OSError())) == "/fallback"


def test_doctor_fails_for_a_tui_without_a_server(monkeypatch):
    monkeypatch.setattr(opencode, "_tuis", lambda: [11, 12])
    monkeypatch.setattr(opencode, "_listen_ports", lambda pid: [4096] if pid == 12 else [])
    assert opencode.deaf_tuis() == [11]
    c = doctor._opencode_server()
    assert (c.ok, c.detail, c.fix) == (
        False, "OpenCode is running without its local server, so the hub can't reach it",
        "close it and start it again from a new terminal")
    monkeypatch.setattr(opencode, "service", lambda: {"url": "http://127.0.0.1:1"})
    assert opencode.deaf_tuis() == [] and doctor._opencode_server().ok  # V2: its service
    monkeypatch.setattr(opencode, "service", lambda: None)
    monkeypatch.setattr(opencode, "_listen_ports", lambda pid: [4096])
    assert doctor._opencode_server().ok


def test_doctor_lists_the_check_and_fix_kills_nothing(home, monkeypatch):
    monkeypatch.setattr(opencode, "deaf_tuis", lambda v2=None: [11])
    monkeypatch.setattr(http, "request", lambda *a, **k: (200, {}))
    kills = []
    monkeypatch.setattr(os, "kill", lambda *a: kills.append(a))

    class Installed(OpenCodeAdapter):
        def installed(self):
            return True
    a = Installed(home)
    results = doctor.checks({"hub": "http://h", "token": "t"}, [a], home,
                            run=lambda *a, **k: subprocess.CompletedProcess(a, 0, "", ""),
                            platform="linux")
    assert [c.ok for c in results if c.name == "opencode server"] == [False]
    assert kills == []


def test_made_up_session_ids_are_refused(monkeypatch):
    monkeypatch.setenv("KAPTYORKA_OPENCODE_SESSION", "probe-hi")
    assert identity.identify("pc", pid=1) is None
    monkeypatch.setenv("KAPTYORKA_OPENCODE_SESSION", "ses_2fA9")
    assert identity.identify("pc", pid=1).key == "opencode:ses_2fA9"
    assert not opencode.valid_id("ses_") and not opencode.valid_id("ses_a b")
    with pytest.raises(DeliveryError, match="not an OpenCode session id"):
        OpenCodeAdapter().deliver("opencode:probe-hi", "hi")


def test_a_failed_push_shows_with_its_reason_until_read(store):
    pid, _ = person(store, "alice")
    p = store.authenticate(store.device_add(pid, "box-1"))
    store.seen("opencode:ses_a", "box-1", "opencode", "proj-a", principal=p)
    store.seen("claude:b", "box-1", "claude-code", "proj-a", principal=p)
    store.send("claude:b", store.agent("opencode:ses_a").name, "hi")
    [item] = store.offer("box-1", "9", p)
    assert store.ack(item["id"], False, p, "the session is off the hub")
    [m] = [m for m in store.messages_for(p) if m["text"] == "hi"]
    assert m["failed"] == "the session is off the hub" and not m["read"]
    assert store.offer("box-1", "9", p) == []  # not offered again and again
    store.inbox("opencode:ses_a")  # pulled after all
    [m] = [m for m in store.messages_for(p) if m["text"] == "hi"]
    assert m["failed"] == "" and m["read"]


def test_a_failed_push_never_shows_the_recipients_key_or_paths(store):
    from kaptyorka import hub_feed

    alice, _ = person(store, "alice", "admin")
    bob, _ = person(store, "bob")
    pa = store.authenticate(store.device_add(alice, "box-1"))
    pb = store.authenticate(store.device_add(bob, "box-2"))
    store.seen("opencode:ses_a", "box-1", "opencode", "proj-a", principal=pa)
    store.send_from_web("bob", store.agent("opencode:ses_a").name, "hi")  # bob on the page
    [item] = store.offer("box-1", "9", pa)
    raw = ("opencode session opencode:ses_a: [Errno 13] Permission denied:"
           " '/home/alice/.local/share/opencode/x'")
    assert store.ack(item["id"], False, pa, raw)
    for who in (pb, pa):  # the sender, and even the recipient's admin person
        [m] = [m for m in store.messages_for(who) if m["text"] == "hi"]
        assert m["failed"] == paths.UNREACHABLE
        [m] = [m for m in hub_feed.feed(store, who)["messages"] if m["text"] == "hi"]
        assert m["failed"] == paths.UNREACHABLE  # what the page's Not delivered tooltip shows
        assert "ses_a" not in m["failed"] and "/home" not in m["failed"]


def test_push_reasons_are_short_and_fixed():
    assert paths.push_reason("claude session claude:x: that session is off the hub") == \
        "the session is off the hub"
    assert paths.push_reason("cursor: pull only") == "this tool only pulls messages"
    assert paths.push_reason("claude session claude:x refused: busy") == \
        "the agent tool refused the message"
    assert paths.push_reason("codex session codex:x: not loaded in the app-server") == \
        paths.UNREACHABLE
    assert paths.push_reason("") == paths.UNREACHABLE
    assert all(paths.push_reason(r) == r for r in paths.PUSH_REASONS)


def test_the_bridge_sends_why_a_push_failed(home, monkeypatch):
    acks = []

    def request(method, url, token, body=None, **kw):
        if "/bridge/poll" in url:
            return 200, [{"kind": "message", "id": 5, "key": "codex:x", "text": "hi"}]
        acks.append(body)
        return 204, None
    monkeypatch.setattr(http, "request", request)
    b = Bridge({"hub": "http://h", "token": "t", "machine": "box-1"},
               [FakeAdapter(fail=True)], lambda line: None)
    assert b.poll_once(0) == 0
    assert acks == [{"id": 5, "ok": False, "error": paths.UNREACHABLE}]  # never the raw text


def test_logged_in_reads_auth_json_and_the_auth_list(home, monkeypatch):
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    a = OpenCodeAdapter(home)
    out = {"text": ""}
    monkeypatch.setattr(opencode, "login_output", lambda *a, **k: out["text"])
    out["text"] = "\x1b[2m┌  Credentials\n│\n└  0 credentials\n"
    assert a.logged_in(None) is None  # free models and local providers need no login
    out["text"] += "┌  Environment\n│\n●  Anthropic ANTHROPIC_API_KEY\n└  1 environment variable\n"
    assert a.logged_in(None) is True
    out["text"] = "└  2 credentials\n"
    assert a.logged_in(None) is True
    out["text"] = None
    assert a.logged_in(None) is None
    auth = home / ".local" / "share" / "opencode" / "auth.json"
    auth.parent.mkdir(parents=True)
    auth.write_text(json.dumps({"anthropic": {"type": "oauth"}}))
    assert a.logged_in(None) is True


def test_a_session_takes_the_machines_new_name(store):
    pid, _ = person(store, "alice")
    p = store.authenticate(store.device_add(pid, "box-1"))
    store.seen("opencode:ses_a", "old-name", "opencode", "proj-a", principal=p)
    assert store.agent("opencode:ses_a").name == "old-name/opencode/proj-a"
    store.seen("opencode:ses_a", "box-1", "opencode", "proj-a", principal=p)
    a = store.agent("opencode:ses_a")
    assert (a.machine, a.name) == ("box-1", "box-1/opencode/proj-a")
    store.heartbeat("box-2", [{"key": "opencode:ses_a", "tool": "opencode",
                               "project": "proj-a"}], principal=p)
    assert store.agent("opencode:ses_a").name == "box-2/opencode/proj-a"


def test_the_shim_names_the_machine_as_its_config_says_now(home):
    paths.save_config({"hub": "http://h", "token": "t", "machine": "box-new"})
    assert shim_mod._machine({"machine": "box-old"}) == "box-new"
    paths.save_config({"hub": "http://h", "token": "t"})
    assert shim_mod._machine({"machine": "box-old"}) == "box-old"
