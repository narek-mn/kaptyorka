#!/usr/bin/env python3
"""Soak test of the real manager, built and installed like a release. Run it before every
release that touches the manager:

    python3 tests/soak_manager.py [--minutes 16] [--out DIR] [--wheel PATH]

It takes about 18 minutes for 16 minutes of soak. Everything is sandboxed in a temp folder:
its own HOME (with a copy of this user's Claude Code login, deleted at the end), its own
KAPTYORKA_HOME, a free port, its own hub (`kaptyorka serve --foreground`), its own bridge
(`kaptyorka bridge`), its own tmux server (TMUX_TMPDIR), and the real `claude` CLI. It never
touches the real hub, ~/.kaptyorka, ~/.claude or the bridge service. The hub runs the manager
through the bridge of the hub key's person, a team machine, like the real hub.

What it checks, every 15 s and at the steps below:
 1. the manager stays reported (its agents row's last_seen keeps moving) and "up", never
    judged crashed;
 2. a page message (/api/send) is answered at minute ~1 and again at minute ~12;
 3. two people writing at the same moment both get an answer (minute ~3);
 4. the claude process killed on purpose (minute ~6) is started again by the hub on its own,
    and the new session answers (minute ~9);
 5. never two managers: at most one tmux session and one claude process in its folder;
 6. a hub restart (minute ~10) keeps the manager, and no launch order is refused with
    "belongs to None".
The time series and the result go to --out (default: a folder next to the sandbox).
"""
import argparse
import getpass
import glob
import json
import os
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
STEP_S = 15
ANSWER_S = 180


# The sandbox runs `claude` on a copy of this user's Claude Code login. The copy and the
# original share one refresh token: when the access token runs out, both renew at the same
# moment and whichever loses is logged out. Seen in a run: the sandbox's manager printed "Login
# expired"; had the sandbox won, the user's own sessions would have been logged out instead.
# So a run never starts when the login renews before it would end, and a run whose copy lost
# stops at once (nothing it measures after that counts).
LOGIN_MARGIN_S = 10 * 60


class LoginLost(Exception):
    """The sandbox's copy of the Claude Code login stopped working."""


def login_left(path: "Path | None" = None) -> "float | None":
    """Seconds until this user's Claude Code login (the one the sandbox copies) renews; None
    when unknown."""
    try:
        d = json.loads((path or Path.home() / ".claude" / ".credentials.json").read_text())
        return float(d["claudeAiOauth"]["expiresAt"]) / 1000 - time.time()
    except (OSError, ValueError, KeyError, TypeError):
        return None


def login_guard(run_s: float) -> None:
    """Exit before the sandbox starts when the login renews within run_s + LOGIN_MARGIN_S."""
    left = login_left()
    if left is not None and left < run_s + LOGIN_MARGIN_S:
        at = time.strftime("%H:%M", time.localtime(time.time() + max(left, 0) + 60))
        sys.exit(f"your Claude Code login renews in {max(left, 0) / 60:.0f} min, before this run"
                 " would end: the sandbox's copy of it would race your own sessions for the"
                 " renewal, and the loser is logged out. Run it again once it has renewed (after"
                 f" {at}; any claude session renews it when it runs out).")


def login_expired(pane: str) -> bool:
    """The manager's screen says its login is gone."""
    return "Please run /login" in pane or "Login expired" in pane


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Soak:
    def __init__(self, minutes: float, out: Path, wheel: str):
        self.minutes, self.wheel = minutes, wheel
        self.root = Path(tempfile.mkdtemp(prefix="kaptyorka-soak-"))
        self.out = out or Path(tempfile.mkdtemp(prefix="kaptyorka-soak-out-"))
        self.out.mkdir(parents=True, exist_ok=True)
        self.home = self.root / "home"
        self.khome = self.home / ".kaptyorka"
        self.folder = self.khome / "manager"
        self.bin = self.root / "bin"
        self.port = free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        self.procs = {}
        self.failures = []
        self.series = open(self.out / "series.log", "w")
        self.events = open(self.out / "events.log", "w")
        claude = shutil.which("claude")
        if claude is None:
            sys.exit("claude is not on PATH")
        self.env = {**os.environ, "HOME": str(self.home), "KAPTYORKA_HOME": str(self.khome),
                    "TMUX_TMPDIR": str(self.root / "tmux"), "KAPTYORKA_SKIP_SERVICE": "1",
                    "ANTHROPIC_MODEL": "haiku",
                    "PATH": f"{self.bin}:{Path(claude).parent}:/usr/bin:/bin"}
        for k in ("TMUX", "KAPTYORKA_CONFIG", "CLAUDE_CONFIG_DIR", "CLAUDECODE"):
            self.env.pop(k, None)
        (self.root / "tmux").mkdir(mode=0o700)
        self.t0 = time.monotonic()

    # output

    def stamp(self) -> str:
        return f"{time.strftime('%H:%M:%S')} +{(time.monotonic() - self.t0) / 60:5.2f}m"

    def event(self, text: str) -> None:
        line = f"{self.stamp()} {text}"
        print(line, flush=True)
        self.events.write(line + "\n")
        self.events.flush()

    def fail(self, text: str) -> None:
        if any(f.endswith(" " + text) for f in self.failures):
            return  # once each
        self.failures.append(f"{self.stamp()} {text}")
        self.event("FAIL " + text)

    # setup

    def run(self, argv, **kw):
        return subprocess.run(argv, env=self.env, capture_output=True, text=True, **kw)

    def install(self) -> None:
        dist = self.root / "dist"
        if self.wheel:
            whl = self.wheel
        else:
            r = subprocess.run([sys.executable, "-m", "pip", "wheel", "--no-deps", "-q", "-w",
                                str(dist), str(REPO)], capture_output=True, text=True)
            if r.returncode != 0:
                sys.exit(f"building the wheel failed: {r.stderr[-2000:]}")
            whl = glob.glob(str(dist / "kaptyorka-*.whl"))[0]
        env = {**self.env, "PIPX_HOME": str(self.root / "pipx"), "PIPX_BIN_DIR": str(self.bin)}
        r = subprocess.run(["pipx", "install", "--force", whl], env=env, capture_output=True,
                           text=True)
        if r.returncode != 0:
            sys.exit(f"pipx install failed: {r.stderr[-2000:]}")
        self.python = self.root / "pipx" / "venvs" / "kaptyorka" / "bin" / "python"
        v = self.run([str(self.bin / "kaptyorka"), "--version"])
        self.event(f"installed {Path(whl).name} ({(v.stdout or v.stderr).strip()}) with pipx")

    def sandbox_claude(self) -> None:
        """This user's Claude Code login, copied; the manager folder trusted, as the admin's
        one-time trust step does on a hub machine."""
        cdir = self.home / ".claude"
        cdir.mkdir(parents=True)
        src = Path.home() / ".claude" / ".credentials.json"
        if not src.exists():
            sys.exit("no Claude Code login to copy (~/.claude/.credentials.json)")
        shutil.copy(src, cdir / ".credentials.json")
        os.chmod(cdir / ".credentials.json", 0o600)
        (self.home / ".claude.json").write_text(json.dumps({
            "hasCompletedOnboarding": True,
            "projects": {str(self.khome): {"hasTrustDialogAccepted": True}}}))

    def setup_hub(self) -> None:
        """People like the real hub: the hub key belongs to the install person `svc`, now a
        team machine; `root` is the human admin; `alice` and `bob` write from the page."""
        self.khome.mkdir(parents=True, exist_ok=True)
        code = r'''
import hashlib, json, secrets, sys
from kaptyorka import paths
from kaptyorka.cli import _token, _admin_device
from kaptyorka.store import Store
tok = _token()
s = Store(paths.home() / "hub.db", legacy_token=tok)
def person(name, role):
    t = secrets.token_urlsafe(32)
    with s.lock:
        pid = s.db.execute("INSERT INTO people (name, role, created) VALUES (?,?,?)",
                           (name, role, s.clock())).lastrowid
        s.db.execute("INSERT INTO devices (person_id, name, token_hash, created) VALUES (?,?,?,?)",
                     (pid, name + "-page", hashlib.sha256(t.encode()).hexdigest(), s.clock()))
    return pid, t
# the install person (the first admin, as the hub's first start makes it) is `svc`
svc = next(x["id"] for x in s.people() if x["role"] == "admin")
with s.lock:
    s.db.execute("UPDATE people SET name='svc' WHERE id=?", (svc,))
s.close()
_admin_device(tok)  # the hub key, for the oldest admin: svc
s = Store(paths.home() / "hub.db", legacy_token=tok)
_, root = person("root", "admin")
_, alice = person("alice", "member")
_, bob = person("bob", "member")
with s.lock:
    s.db.execute("UPDATE people SET team=1, role='member' WHERE id=?", (svc,))
code = s.invite_create(svc)
print(json.dumps({"root": root, "alice": alice, "bob": bob, "invite": code}))
'''
        r = self.run([str(self.python), "-c", code])
        if r.returncode != 0:
            sys.exit(f"hub setup failed: {r.stderr[-3000:]}")
        self.tokens = json.loads(r.stdout.strip().splitlines()[-1])

    def start(self, name: str, argv) -> None:
        log = open(self.out / f"{name}.log", "a")
        self.procs[name] = subprocess.Popen(argv, env=self.env, stdout=log,
                                            stderr=subprocess.STDOUT, cwd=str(self.root),
                                            start_new_session=True)

    def stop(self, name: str) -> None:
        p = self.procs.pop(name, None)
        if p is not None and p.poll() is None:
            os.killpg(p.pid, signal.SIGTERM)
            try:
                p.wait(10)
            except subprocess.TimeoutExpired:
                os.killpg(p.pid, signal.SIGKILL)

    def start_hub(self) -> None:
        self.start("hub", [str(self.bin / "kaptyorka"), "serve", "--foreground", "--port",
                           str(self.port)])
        for _ in range(60):
            try:
                urllib.request.urlopen(self.url + "/", timeout=2)
                return
            except Exception:
                time.sleep(0.5)
        sys.exit("the hub did not come up")

    def join(self) -> None:
        r = self.run([str(self.bin / "kaptyorka"), "join", f"{self.url}/i/{self.tokens['invite']}",
                      "--only", "claude-code", "--name", "soak-box"], timeout=120)
        self.event(f"join: rc {r.returncode}: {(r.stdout + r.stderr).strip()[-300:]}")
        if r.returncode != 0:
            sys.exit("join failed")

    # observing

    def db(self):
        return sqlite3.connect(f"file:{self.khome / 'hub.db'}?mode=ro", uri=True, timeout=5)

    def manager_rows(self):
        with self.db() as c:
            c.row_factory = sqlite3.Row
            w = c.execute("SELECT state, error, key, created, up_at FROM workers"
                          " WHERE name='manager'").fetchone()
            a = c.execute("SELECT key, last_seen FROM agents WHERE key=?",
                          (w["key"],)).fetchone() if w is not None and w["key"] else None
            crashes = c.execute("SELECT value FROM settings WHERE key='manager_crashes'"
                                ).fetchone()
            launches = c.execute("SELECT COUNT(*) FROM launches WHERE worker='manager' AND"
                                 " kind='launch'").fetchone()[0]
        return w, a, json.loads(crashes[0]) if crashes else [], launches

    def managers_running(self):
        r = self.run(["tmux", "ls", "-F", "#{session_name}"])
        sessions = [s for s in r.stdout.split() if s]
        procs = []
        for d in glob.glob("/proc/[0-9]*"):
            try:
                if os.readlink(d + "/cwd") == str(self.folder) and \
                        "claude" in Path(d + "/cmdline").read_bytes().split(b"\0")[0].decode():
                    procs.append(int(d.split("/")[-1]))
            except OSError:
                continue
        return sessions, procs

    def check(self) -> None:
        pane = self.run(["tmux", "capture-pane", "-p", "-t", "=kaptyorka-manager"]).stdout or ""
        if login_expired(pane):
            raise LoginLost("the sandbox's Claude Code login expired: its copy lost the renewal"
                            " race with your own sessions; run it again")
        w, a, crashes, launches = self.manager_rows()
        sessions, procs = self.managers_running()
        now = time.time()
        seen = f"{now - a['last_seen']:5.1f}s ago" if a else "-"
        self.series.write(f"{self.stamp()} state={w['state'] if w else None} key="
                          f"{(w['key'] or '')[:15] if w else ''} last_seen={seen}"
                          f" crashes={len(crashes)} launches={launches} tmux={sessions}"
                          f" claude_pids={procs}\n")
        self.series.flush()
        if len([s for s in sessions if s == "kaptyorka-manager"]) > 1 or len(procs) > 1:
            self.fail(f"two managers: tmux {sessions}, claude pids {procs}")
        if crashes:
            self.fail(f"judged crashed: {crashes}")
        return w, a

    def wait_up(self, limit_s: float, after_key: str = "") -> str:
        end = time.monotonic() + limit_s
        while time.monotonic() < end:
            w, a = self.check()
            if w and w["state"] == "up" and w["key"] and w["key"] != after_key and a \
                    and time.time() - a["last_seen"] < 30:
                return w["key"]
            time.sleep(STEP_S / 3)
        self.fail(f"the manager was not up within {limit_s:.0f} s")
        return ""

    # messages

    def send(self, who: str, text: str) -> float:
        with self.db() as c:
            name = c.execute("SELECT a.name FROM agents a JOIN people p ON p.id=a.person_id"
                             " WHERE p.name='manager' ORDER BY a.last_seen DESC").fetchone()[0]
        req = urllib.request.Request(self.url + "/api/send", method="POST",
                                     data=json.dumps({"to": name, "text": text}).encode(),
                                     headers={"Authorization": f"Bearer {self.tokens[who]}",
                                              "Content-Type": "application/json"})
        sent = time.time()
        with urllib.request.urlopen(req, timeout=30) as r:
            self.event(f"{who} -> manager ({name}): {text!r}: HTTP {r.status}")
        return sent

    def answered(self, who: str, since: float):
        with self.db() as c:
            row = c.execute("SELECT m.created, m.text, m.sender_key FROM messages m JOIN agents a"
                            " ON a.key=m.sender_key JOIN people p ON p.id=a.person_id WHERE"
                            " p.name='manager' AND m.to_key=? AND m.created>? ORDER BY m.id",
                            (f"web:{who}", since)).fetchone()
        return row

    def ask(self, whos, label: str) -> None:
        sent = {}
        threads = [threading.Thread(target=lambda w=w: sent.__setitem__(
            w, self.send(w, f"{label}: please reply to me with one short line")))
            for w in whos]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        end = time.monotonic() + ANSWER_S
        left = set(whos)
        while left and time.monotonic() < end:
            for w in sorted(left):
                row = self.answered(w, sent[w])
                if row is not None:
                    self.event(f"answered {w} after {row[0] - sent[w]:.0f} s from {row[2][:15]}:"
                               f" {row[1][:120]!r}")
                    left.discard(w)
            self.check()
            time.sleep(5)
        for w in left:
            self.fail(f"{label}: no answer to {w} within {ANSWER_S} s")

    # the soak

    def soak(self) -> int:
        try:
            self.install()
            self.sandbox_claude()
            self.setup_hub()
            self.start_hub()
            self.join()
            self.start("bridge", [str(self.bin / "kaptyorka"), "bridge"])
            self.event(f"hub on port {self.port}, bridge started; waiting for the manager")
            key = self.wait_up(180)
            self.event(f"manager up as {key}")
            plan = [(1, "ask1"), (3, "ask2"), (6, "kill"), (10, "restart"), (12, "ask3")]
            done = []
            for minute, step in plan:
                if minute >= self.minutes:
                    break
                self.until(minute)
                done.append(step)
                if step == "ask1":
                    self.ask(["alice"], "check at minute 1")
                elif step == "ask2":
                    self.ask(["alice", "bob"], "two at once")
                elif step == "kill":
                    _, procs = self.managers_running()
                    self.event(f"killing claude pid {procs} on purpose")
                    for pid in procs:
                        os.kill(pid, signal.SIGKILL)
                    new = self.wait_up(300, after_key=key)
                    if new:
                        self.event(f"relaunched by the hub on its own as {new}")
                        key = new
                        w, _, crashes, launches = self.manager_rows()
                        self.event(f"after the kill: crashes={crashes} launches={launches}")
                        self.ask(["bob"], "after the kill")
                elif step == "restart":
                    self.event("restarting the hub")
                    self.stop("hub")
                    time.sleep(3)
                    self.start_hub()
                    self.event("hub back")
                elif step == "ask3":
                    self.ask(["alice"], "check at minute 12")
            self.until(self.minutes)
            w, a, crashes, launches = self.manager_rows()
            self.event(f"end: state={w['state']} crashes={crashes} launches={launches}")
            bridge_log = (self.out / "bridge.log").read_text()
            if "belongs to None" in bridge_log:
                self.fail("a launch order was refused with 'belongs to None'")
            waits = bridge_log.count("waits for the hub's heartbeat answer")
            self.event(f"orders that waited for a heartbeat answer: {waits}")
            want = 2 if "kill" in done else 1  # the start, and the one after the kill
            if launches != want:
                self.fail(f"{launches} launches; expected {want}")
        except LoginLost as e:
            self.fail(f"stopped early: {e}")
        finally:
            self.cleanup()
        result = "PASS" if not self.failures else "FAIL"
        summary = f"{result}: {len(self.failures)} failures\n" + "\n".join(self.failures)
        (self.out / "result.txt").write_text(summary + "\n")
        print(summary, f"\nlogs: {self.out}", flush=True)
        return 0 if not self.failures else 1

    def until(self, minute: float) -> None:
        while (time.monotonic() - self.t0) / 60 < minute:
            self.check()
            time.sleep(STEP_S)

    def cleanup(self) -> None:
        self.stop("bridge")
        self.stop("hub")
        self.run(["tmux", "kill-server"])
        time.sleep(1)
        sessions, procs = self.managers_running()
        for pid in procs:
            os.kill(pid, signal.SIGKILL)
        self.series.close()
        self.events.close()
        shutil.rmtree(self.root, ignore_errors=True)  # the login copy goes with it


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=16)
    ap.add_argument("--out", type=Path)
    ap.add_argument("--wheel", default="")
    a = ap.parse_args()
    if getpass.getuser() == "root":
        sys.exit("run as your own user")
    login_guard(a.minutes * 60 + 180)  # the soak, plus its install and setup
    return Soak(a.minutes, a.out, a.wheel).soak()


if __name__ == "__main__":
    sys.exit(main())
