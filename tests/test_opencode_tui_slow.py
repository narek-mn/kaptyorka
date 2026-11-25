"""Real OpenCode terminal windows (TUIs) against a hub and a bridge from this checkout, end to
end: what a window shows must be what the hub shows. Every OpenCode bug found so far was here
(a name never shown, a closed window's chat kept for 30 minutes, a switched window's old chat
left behind), and no test drove a real window.

All in a sandbox: HOME, XDG_* and KAPTYORKA_HOME in a temp folder, `kaptyorka join --only
opencode` there (its MCP entry, plugin and, on OpenCode 2, the TUI plugin that notes which chat
each window shows), a hub on a free port, `kaptyorka bridge`, on OpenCode 2 a background service
of its own on a free port, and windows in tmux on a private socket. No model and no login:
chats are made and renamed through OpenCode's own API and opened with `opencode -s <id>`; a
pushed message is read back from the chat. Only processes this test started are stopped (the
sweep at the end matches the sandbox HOME in a process's environment).

Run with: pytest -m slow tests/test_opencode_tui_slow.py (needs `opencode` and `tmux` on PATH;
one at a time, like the other slow tests; a few minutes)."""
import base64
import contextlib
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

from kaptyorka.adapters import opencode
from kaptyorka.store import session_id

SRC = Path(__file__).resolve().parents[1] / "src"
BEAT_S = 10  # bridge.HEARTBEAT_S
SOON_S = BEAT_S + 8  # "within one beat": a beat, plus the time a beat and the hub take


def _major() -> int:
    if shutil.which("opencode") is None:
        return 0
    try:
        r = subprocess.run(["opencode", "--version"], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return 0
    m = re.search(r"(\d+)\.\d+", r.stdout)
    return int(m.group(1)) if m else 0


MAJOR = _major()
V2 = MAJOR >= 2
pytestmark = [pytest.mark.slow,
              pytest.mark.skipif(MAJOR == 0, reason="no `opencode` on PATH"),
              pytest.mark.skipif(shutil.which("tmux") is None, reason="no tmux on PATH"),
              pytest.mark.skipif(not sys.platform.startswith("linux"), reason="reads /proc")]
v2_only = pytest.mark.skipif(not V2, reason="OpenCode 2 only")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait(what: str, seconds: float, check, info=lambda: ""):
    """check()'s first true answer within `seconds`, else fail with info()."""
    deadline = time.monotonic() + seconds
    while True:
        try:
            got = check()
        except (OSError, ValueError, KeyError, TypeError):
            got = None
        if got:
            return got
        if time.monotonic() > deadline:
            raise AssertionError(f"{what}: not within {seconds:.0f} s\n{info()}")
        time.sleep(0.5)


def call(url: str, method: str = "GET", body=None, headers=None, timeout: float = 10):
    """(status, decoded JSON, text or None)."""
    data = json.dumps(body).encode() if body is not None else None
    h = dict(headers or {})
    if data is not None:
        h["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            status = r.status
    except urllib.error.HTTPError as e:
        raw, status = e.read(), e.code
    try:
        return status, json.loads(raw) if raw else None
    except ValueError:
        return status, raw.decode(errors="replace")


class Window:
    def __init__(self, world, target, pid, folder):
        self.world, self.target, self.pid, self.folder = world, target, pid, folder

    def alive(self) -> bool:
        return not opencode._ended(self.pid)

    def screen(self) -> str:
        r = self.world.tmux("capture-pane", "-p", "-t", self.target, check=False)
        return r.stdout[-3000:]

    def keys(self, *keys):
        self.world.tmux("send-keys", "-t", self.target, *keys)

    def close(self):
        """The person closes the terminal: its pane goes, the window process with it."""
        self.world.tmux("kill-window", "-t", self.target, check=False)
        wait("the window process ended", 20, lambda: not self.alive())


class World:
    """The sandbox: a hub, this machine joined to it for OpenCode, its bridge, and on OpenCode 2
    the background service; windows are added by the tests."""

    def __init__(self, root: Path):
        self.root, self.procs, self.logs = root, [], []
        self.home = root / "home"
        self.home.mkdir()
        (root / "bin").mkdir()
        self.exe = root / "bin" / "kaptyorka"  # this checkout's code, as join records it
        self.exe.write_text(f"#!{sys.executable}\nimport sys\nsys.path.insert(0, {str(SRC)!r})\n"
                            "from kaptyorka.cli import entry\nsys.exit(entry())\n")
        self.exe.chmod(0o755)
        keep = {k: v for k, v in os.environ.items()
                if not k.startswith(("OPENCODE", "KAPTYORKA", "CLAUDE", "XDG_", "TMUX"))}
        self.env = {**keep, "HOME": str(self.home), "TERM": "xterm-256color",
                    "XDG_CONFIG_HOME": str(self.home / ".config"),
                    "XDG_STATE_HOME": str(self.home / ".local/state"),
                    "XDG_DATA_HOME": str(self.home / ".local/share"),
                    "XDG_CACHE_HOME": str(self.home / ".cache"),
                    "KAPTYORKA_HOME": str(self.home / ".kaptyorka"),
                    "KAPTYORKA_SKIP_SERVICE": "1",
                    # the wrapper join writes for OpenCode 1 comes first, as on a real machine
                    "PATH": f"{self.home / '.local/bin'}:{root / 'bin'}:{os.environ.get('PATH', '')}"}
        self.sock = f"kap-oc-{os.getpid()}-{int(time.time())}"
        self.folders = {}
        for name in ("proj-a", "proj-b"):
            self.folders[name] = root / "w" / name
            self.folders[name].mkdir(parents=True)
        self.n, self.made = 0, 0.0

    # --- processes -------------------------------------------------------------------
    def spawn(self, name, argv, cwd, env=None):
        log = open(self.root / f"{name}.log", "w")
        self.logs.append(log)
        p = subprocess.Popen(argv, cwd=cwd, env=env or self.env, stdout=log,
                             stderr=subprocess.STDOUT, start_new_session=True)
        self.procs.append(p)
        return p

    def log(self, name, lines=40) -> str:
        f = self.home / ".kaptyorka/bridge.log" if name == "bridge" else self.root / f"{name}.log"
        text = f.read_text(errors="replace") if f.exists() else ""
        return f"--- {name}.log\n" + "\n".join(text.splitlines()[-lines:])

    def kaptyorka(self, *args, env=None, timeout=90):
        return subprocess.run([str(self.exe), *args], env=env or self.env, cwd=self.home,
                              capture_output=True, text=True, timeout=timeout)

    def tmux(self, *args, check=True):
        r = subprocess.run(["tmux", "-L", self.sock, "-f", "/dev/null", *args], env=self.env,
                           capture_output=True, text=True, timeout=20)
        if check:
            assert r.returncode == 0, f"tmux {args}: {r.stderr}"
        return r

    def start(self):
        # the hub, its own home, a free port, no manager
        self.port = free_port()
        self.hub = f"http://127.0.0.1:{self.port}"
        hub_env = {**self.env, "KAPTYORKA_HOME": str(self.root / "hub")}
        self.spawn("hub", [str(self.exe), "serve", "--foreground", "--no-manager", "--port",
                           str(self.port)], self.root, hub_env)
        token = self.root / "hub" / "hub-token"
        wait("the hub's token", 30, token.exists, lambda: self.log("hub"))
        self.token = token.read_text().strip()
        wait("the hub answering", 30, lambda: self.api("GET", "/api/feed")[0] == 200,
             lambda: self.log("hub"))
        for p in self.folders:  # team projects: their sessions go on the hub
            assert self.api("POST", "/api/projects", {"name": p})[0] == 204
        if V2:  # its own service, on a free port (the account's own one may run: never that)
            r = subprocess.run(["opencode", "service", "set", "port", str(free_port())],
                               env=self.env, cwd=self.home, capture_output=True, text=True,
                               timeout=60)
            assert r.returncode == 0, r.stdout + r.stderr
        # alice joins with an invite, as a person does: the page's close is for her own device
        # token (the hub's own token is the old shared one, which closes nothing)
        assert self.api("POST", "/api/people", {"name": "alice"})[0] == 200
        status, invite = self.api("POST", "/api/invites", {"person": "alice"})
        assert status == 200, invite
        r = self.kaptyorka("join", invite["url"], "--name", "box-1", "--only", "opencode")
        assert r.returncode == 0, r.stdout + r.stderr
        self.token = json.loads((self.home / ".kaptyorka/config.json").read_text())["token"]
        conf = self.home / ".config/opencode"
        assert (conf / "plugins/kaptyorka.js").exists(), r.stdout
        if V2:
            assert (conf / "plugins/kaptyorka-hub/tui.js").exists(), r.stdout
            self.spawn("service", ["opencode", "serve", "--service"], self.home)
            self.svc = wait("the OpenCode service", 90, self._service, lambda: self.log("service"))
        else:
            assert (self.home / ".local/bin/opencode").exists(), \
                "join wrote no OpenCode wrapper (its PATH check):\n" + r.stdout + r.stderr
        self.spawn("bridge", [str(self.exe), "bridge"], self.home)
        self.tmux("new-session", "-d", "-s", "base", "-x", "200", "-y", "50", "--", "cat")

    def _service(self):
        f = self.home / ".local/state/opencode/service.json"
        info = json.loads(f.read_text())
        token = base64.b64encode(f"opencode:{info.get('password') or ''}".encode()).decode()
        svc = {"url": info["url"].rstrip("/"), "Authorization": f"Basic {token}"}
        return svc if call(svc["url"] + "/api/info", headers={"Authorization": svc["Authorization"]})[0] == 200 else None

    def stop(self):
        with contextlib.suppress(Exception):
            self.tmux("kill-server", check=False)
        with contextlib.suppress(OSError):  # tmux leaves its socket file behind
            os.unlink(Path(os.environ.get("TMUX_TMPDIR") or "/tmp") / f"tmux-{os.getuid()}" / self.sock)
        for p in reversed(self.procs):
            with contextlib.suppress(OSError):
                os.killpg(p.pid, signal.SIGTERM)
        for p in reversed(self.procs):
            try:
                p.wait(10)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(OSError):
                    os.killpg(p.pid, signal.SIGKILL)
                p.wait(5)
        for pid in self.leftovers():  # what OpenCode started by itself (MCP servers, a pty daemon)
            with contextlib.suppress(OSError):
                os.kill(pid, signal.SIGKILL)
        for log in self.logs:
            log.close()

    def leftovers(self):
        """Live processes whose HOME is this sandbox's: only ever ones this test started."""
        want, out = f"HOME={self.home}".encode(), []
        for d in os.listdir("/proc"):
            if not d.isdigit() or int(d) == os.getpid():
                continue
            with contextlib.suppress(OSError):
                if want in Path(f"/proc/{d}/environ").read_bytes().split(b"\0"):
                    out.append(int(d))
        return out

    # --- the hub ---------------------------------------------------------------------
    def api(self, method, path, body=None):
        return call(self.hub + path, method, body, {"Authorization": f"Bearer {self.token}"})

    def rows(self):
        status, feed = self.api("GET", "/api/feed")
        assert status == 200, feed
        return [a for a in feed["agents"] if a["tool"] == "opencode"]

    def row(self, sid):
        want = session_id(f"opencode:{sid}")
        return next((a for a in self.rows() if a["id"] == want), None)

    def why(self, *windows) -> str:
        rows = [{k: a.get(k) for k in ("id", "name", "title", "project", "activity", "close",
                                       "closing", "close_error")} for a in self.rows()]
        notes = {f.name: f.read_text() for f in (self.home / ".kaptyorka/opencode-open").glob("*.json")} \
            if (self.home / ".kaptyorka/opencode-open").is_dir() else {}
        seen = subprocess.run([sys.executable, "-c", "from kaptyorka.adapters.opencode import "
                               "OpenCodeAdapter as A\nfor s in A().sessions(): print(s)"],
                              env={**self.env, "PYTHONPATH": str(SRC)}, capture_output=True,
                              text=True, timeout=30)
        return "\n".join([f"hub rows: {json.dumps(rows, indent=1)}", f"window notes: {notes}",
                          f"the adapter sees: {seen.stdout}{seen.stderr[-1500:]}",
                          self.log("bridge", 25)] + [f"--- screen\n{w.screen()}" for w in windows])

    def shows(self, sid, title=None, seconds=SOON_S, *windows):
        return wait(f"{sid} on the hub" + (f" as {title!r}" if title else ""), seconds,
                    lambda: (lambda r: r if r and (title is None or r["title"] == title) else None)(self.row(sid)),
                    lambda: self.why(*windows))

    def gone(self, sid, seconds=SOON_S, *windows):
        wait(f"{sid} gone from the hub", seconds, lambda: self.row(sid) is None,
             lambda: self.why(*windows))

    def close(self, sid, confirm=True):
        return self.api("POST", "/api/sessions/" + urllib.parse.quote(session_id(f"opencode:{sid}")) + "/close",
                        {"confirm": confirm})

    # --- OpenCode ----------------------------------------------------------------------
    def oc(self, method, path, body=None, server=None):
        """OpenCode's own API: the V2 service, or a V1 server (a window's, or `serve`)."""
        if V2:
            return call(self.svc["url"] + path, method, body,
                        {"Authorization": self.svc["Authorization"]})
        return call(server + path, method, body)

    def chat(self, folder, title):
        """A new chat in this folder with this title, made through OpenCode's API: its id."""
        d = str(self.folders[folder])
        self.made = time.time()
        if V2:
            status, made = self.oc("POST", "/api/session", {"location": {"directory": d}, "title": title})
            assert status == 200, made
            return made["data"]["id"]
        port = free_port()  # V1: a short-lived `opencode serve`, then only the windows' servers
        p = self.spawn(f"serve-{port}", ["opencode", "serve", "--port", str(port)], self.home)
        try:
            url = f"http://127.0.0.1:{port}"
            wait("opencode serve", 60, lambda: call(url + "/session")[0] == 200,
                 lambda: self.log(f"serve-{port}"))
            status, made = call(url + "/session?" + urllib.parse.urlencode({"directory": d}),
                                "POST", {"title": title})
            assert status == 200, made
            return made["id"]
        finally:
            os.killpg(p.pid, signal.SIGTERM)
            p.wait(10)

    def rename(self, sid, title, window=None):
        if V2:
            status, got = self.oc("PATCH", f"/api/session/{sid}", {"title": title})
        else:
            status, got = call(f"{self.server(window)}/session/{sid}", "PATCH", {"title": title})
        assert status in (200, 204), got

    def server(self, window) -> str:
        ports = wait("the window's own server", 30, lambda: opencode._listen_ports(window.pid))
        return f"http://127.0.0.1:{ports[0]}"

    def messages(self, sid, window=None) -> str:
        if V2:
            return json.dumps([self.oc("GET", f"/api/session/{sid}/{part}")[1]
                               for part in ("message", "inbox")])
        return json.dumps(call(f"{self.server(window)}/session/{sid}/message")[1])

    def window(self, folder, sid=None) -> Window:
        """A terminal window running OpenCode in this folder, showing this chat."""
        self.n += 1
        # a chat made a moment ago, as a person opens one from earlier: never in the same second
        time.sleep(max(0.0, self.made + 1.5 - time.time()))
        argv = ["opencode"] + (["-s", sid] if sid else [])
        r = self.tmux("new-window", "-d", "-P", "-F", "#{session_name}:#{window_index} #{pane_pid}",
                      "-c", str(self.folders[folder]), "--", *argv)
        target, pid = r.stdout.split()
        w = Window(self, target, int(pid), folder)
        assert open(f"/proc/{w.pid}/comm").read().strip() == "opencode", "not OpenCode itself"
        if V2:  # the TUI plugin has noted what it shows
            wait("the window's note", 60, lambda: self._noted(w, sid), lambda: self.why(w))
        else:
            self.server(w)
        return w

    def switch(self, w, sid, title):
        """The person switches this window to another chat: OpenCode 1's own /tui/select-session
        route; on OpenCode 2 (no server in the window) its session list, as a person picks one."""
        if not V2:
            status, got = call(f"{self.server(w)}/tui/select-session", "POST", {"sessionID": sid})
            assert status == 200, got
            return
        w.keys("/sessions")
        time.sleep(0.5)
        w.keys("Enter")
        wait("the session list", 15, lambda: "first chat" in w.screen() or title in w.screen(),
             lambda: w.screen())
        w.keys("-l", title)
        time.sleep(1)
        w.keys("Enter")
        wait("the window's note of the new chat", 20, lambda: self._noted(w, sid),
             lambda: self.why(w))

    def _noted(self, w, sid):
        f = self.home / ".kaptyorka/opencode-open" / f"{w.pid}.json"
        return json.loads(f.read_text()).get("session") == (sid or "")


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    w = World(tmp_path_factory.mktemp("oc-tui"))
    try:
        w.start()
        yield w
    finally:
        w.stop()
    assert not w.leftovers(), "processes of the sandbox left running"


def test_a_window_shows_its_chat_by_name_and_a_rename_within_a_beat(world):
    sid = world.chat("proj-a", "first chat")
    w = world.window("proj-a", sid)
    try:
        row = world.shows(sid, "first chat", SOON_S, w)
        assert row["project"] == "proj-a"
        world.rename(sid, "renamed chat", w)
        world.shows(sid, "renamed chat", SOON_S, w)
    finally:
        w.close()


def test_a_window_switched_to_another_chat_shows_the_new_one_and_drops_the_old(world):
    old, new = world.chat("proj-b", "switch from"), world.chat("proj-b", "switch to")
    w = world.window("proj-b", old)
    try:
        world.shows(old, "switch from", SOON_S, w)
        world.switch(w, new, "switch to")
        if not V2:  # OpenCode 1 cannot say which chat a window shows: from its first new
            # step (a rename moves no time.updated there): the person types in it
            status, got = call(f"{world.server(w)}/session/{new}/prompt_async", "POST",
                               {"parts": [{"type": "text", "text": "hello"}]})
            assert status in (200, 204), got
        world.shows(new, "switch to", SOON_S, w)
        world.gone(old, SOON_S, w)
    finally:
        w.close()


def test_two_windows_in_one_folder_are_two_sessions(world):
    a, b = world.chat("proj-a", "left window"), world.chat("proj-a", "right window")
    wa = world.window("proj-a", a)
    wb = world.window("proj-a", b)
    try:
        ra = world.shows(a, "left window", SOON_S, wa, wb)
        rb = world.shows(b, "right window", SOON_S, wa, wb)
        assert ra["name"] != rb["name"]
        world.rename(a, "left renamed", wa)  # one window's chat moves: the other keeps its own
        world.shows(a, "left renamed", SOON_S, wa, wb)
        assert world.row(b) is not None, world.why(wa, wb)
    finally:
        wa.close()
        wb.close()


def test_a_closed_window_s_chat_leaves_at_once(world):
    sid = world.chat("proj-a", "closed window")
    w = world.window("proj-a", sid)
    world.shows(sid, "closed window", SOON_S, w)
    w.close()
    world.gone(sid, SOON_S)


def test_the_page_s_close_ends_the_window_and_only_it(world):
    sid = world.chat("proj-b", "close me")
    w = world.window("proj-b", sid)
    try:
        world.shows(sid, "close me", SOON_S, w)
        # the x, asking first (it is recent), once the bridge has proven its window
        wait("the x on its row", SOON_S, lambda: world.row(sid).get("close") == "ask",
             lambda: world.why(w))
        status, got = world.close(sid, confirm=True)
        assert status == 202, got
        wait("the window process ended", 2 * SOON_S, lambda: not w.alive(), lambda: world.why(w))
        world.gone(sid)
        assert "closed session close me as asked on the page" in world.log("bridge", 200)
        if V2:
            assert world._service(), "the OpenCode service stopped"
    finally:
        if w.alive():
            w.close()


@v2_only
def test_a_chat_in_no_window_has_no_x_and_its_close_is_refused(world):
    sid = world.chat("proj-a", "app chat")
    row = world.shows(sid, "app chat")
    assert "close" not in row, row
    status, got = world.close(sid, confirm=True)
    assert status == 409 and "terminal window" in str(got), (status, got)
    assert world.row(sid) is not None


def test_a_message_from_the_hub_reaches_the_open_chat(world):
    sid = world.chat("proj-a", "listening chat")
    w = world.window("proj-a", sid)
    try:
        row = world.shows(sid, "listening chat", SOON_S, w)
        text = f"hello from the hub {os.getpid()}-{int(time.time())}"
        status, got = world.api("POST", "/api/send", {"to": row["name"], "text": text})
        assert status == 200 and got["sent"], got
        wait("the message in the chat", 60, lambda: text in world.messages(sid, w),
             lambda: world.why(w))
    finally:
        w.close()


@v2_only
def test_a_chat_used_only_through_the_service_shows_while_recent(world):
    sid = world.chat("proj-b", "service only")
    world.shows(sid, "service only")
    world.rename(sid, "service renamed")
    world.shows(sid, "service renamed")
