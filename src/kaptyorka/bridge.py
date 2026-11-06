"""Per-machine bridge: reports running sessions and pushes messages through each tool's door."""
import json
import re
import subprocess
import sys
import threading
import time
from dataclasses import asdict
from pathlib import Path
from typing import Callable, Dict, List, Optional
from urllib.parse import urlencode

from kaptyorka import __version__, facts, http, identity, orders, paths
from kaptyorka.adapters.base import Adapter, write_atomic
from kaptyorka.launch import LaunchError, Launcher

HEARTBEAT_S = 10
WAIT_S = 25
RETRY_S = 5
UPDATE_RETRY_S = 3600  # after a failed auto-update
FACTS_S = 60
SHOW_UP_S = 120  # a started worker's session must appear in the session list within this
SEEN_KEEP = 200  # order ids remembered against replay
NO_SECRET = "this machine has no signing secret; re-join with an invite"
COMPLETE = {"claude-code"}  # the only tool whose session list is complete; the rest may miss live ones


def log(line: str) -> None:
    """Append to ~/.kaptyorka/bridge.log and print to stderr."""
    with open(paths.home() / "bridge.log", "a") as f:
        f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {line}\n")
    print(line, file=sys.stderr)


def joined_adapters(cfg: dict, adapters: List[Adapter]) -> List[Adapter]:
    """The adapters for the tools join --only named; without --only, all of them."""
    tools = cfg.get("tools")
    return adapters if tools is None else [a for a in adapters if a.tool in tools]


class Bridge:
    def __init__(self, cfg: dict, adapters: List[Adapter], log: Callable[[str], None], run=None,
                 launcher: Optional[Launcher] = None):
        self.cfg, self.adapters, self.log = cfg, adapters, log
        self.runner = run or subprocess.run
        self.launcher = launcher or Launcher(cfg, Path.home(), run=self.runner, log=log)
        self.seen_ids: Dict[int, dict] = self._load_seen()
        self.pending: Dict[str, tuple] = {}  # worker -> (order, launch result, monotonic start)
        self._sessions: list = []  # the session objects of the last heartbeat
        self.stop = threading.Event()
        self._problems: Dict[str, Optional[str]] = {}
        self.pending_confirm = False  # a new token is saved but the hub has not confirmed it
        self.hub: dict = {}  # the hub's last JSON answer to a heartbeat; {} before one or on 204
        self._facts: dict = {}
        self._facts_at = 0.0
        self._updated_to = ""  # the hub version an update was last run for
        self._update_retry_at = 0.0

    def _report(self, area: str, problem: Optional[str]) -> None:
        """Log a problem once per area until that area succeeds (problem None) again."""
        if problem is not None and problem != self._problems.get(area):
            self.log(problem)
        self._problems[area] = problem

    def _machine_facts(self, sessions: list) -> dict:
        """Machine facts and git state per session folder, rebuilt at most once a minute."""
        if time.monotonic() - self._facts_at >= FACTS_S or not self._facts:
            repos = {}
            for cwd in {s.cwd for s in sessions if s.cwd}:
                info = facts.git(cwd, self.runner)
                if info:
                    repos[cwd] = info
            self._facts = {**identity.facts(), **facts.machine(self.adapters, self.runner), "git": repos}
            self._facts_at = time.monotonic()
        return self._facts

    def heartbeat(self) -> None:
        sessions, tools, objs = [], [], []
        for a in self.adapters:
            try:
                got = list(a.sessions())
                found = [asdict(s) for s in got]
                problem = None
            except Exception as e:  # one broken adapter must not hide the others
                found, problem = [], f"{a.tool} sessions failed: {e}"
            self._report("sessions:" + a.prefix, problem)
            sessions += found
            objs += got if problem is None else []
            if problem is None and a.tool in COMPLETE:
                tools.append(a.tool)  # this list is complete for it
        projects = self.hub.get("projects", [])
        status, answer = http.request(
            "POST", self.cfg["hub"] + "/bridge/heartbeat", self.cfg["token"],
            {"machine": self.cfg["machine"], "sessions": sessions, "tools": tools,
             "facts": self._machine_facts(objs),
             "folders": facts.folders(projects if isinstance(projects, list) else [], objs,
                                      Path.home())})
        self.hub = answer if status == 200 and isinstance(answer, dict) else {}
        self._sessions = objs
        self._report("heartbeat", None if status in (200, 204)
                     else f"hub answered {status} to heartbeat")
        self._check_pending(objs)

    # launch and close orders

    def _load_seen(self) -> Dict[int, dict]:
        """{order id: {"ok", "error"}} of the orders this bridge ran; ok None while running."""
        try:
            data = json.loads((paths.home() / "launched.json").read_text())
        except (OSError, ValueError):
            return {}
        if not isinstance(data, dict):
            return {}
        out = {}
        for k, v in list(data.items())[-SEEN_KEEP:]:
            if k.isdigit() and isinstance(v, dict):
                out[int(k)] = {"ok": v.get("ok"), "error": str(v.get("error") or "")}
        return out

    def _remember(self, order_id: int, ok: Optional[bool], error: str = "") -> None:
        self.seen_ids.pop(order_id, None)
        self.seen_ids[order_id] = {"ok": ok, "error": error}
        while len(self.seen_ids) > SEEN_KEEP:
            del self.seen_ids[next(iter(self.seen_ids))]
        try:
            write_atomic(paths.home() / "launched.json",
                         json.dumps({str(k): v for k, v in self.seen_ids.items()}))
        except OSError as e:
            self.log(f"could not save launched.json: {e}")

    def _order(self, m: dict) -> None:
        """Verify, run and ack one launch or close order. Every outcome is acked once; an
        order run before is acked again with its stored outcome (the first ack may be lost)."""
        order = {k: v for k, v in m.items() if k != "launch"}
        oid = order.get("id")
        name = order.get("worker")
        ran = False
        try:
            secret = self.cfg.get("secret")
            if not secret:
                raise LaunchError(NO_SECRET)
            problem = orders.verify(order, orders.signing_key(secret), time.time(),
                                    self.seen_ids)
            if problem == "order already run":
                done = self.seen_ids[oid]
                ok = done["ok"] is True
                error = done["error"] if done["ok"] is not None \
                    else "the bridge stopped while running this order"
                self._ack(oid, ok, True, "" if ok else error)
                return
            if problem:
                raise LaunchError(problem)
            kind = order.get("kind")
            if kind == "launch":
                you = self.hub.get("you")
                if order.get("person") != you and not self.hub.get("shared"):
                    raise LaunchError(f"this account belongs to {you}")
            elif kind != "close":
                raise LaunchError(f"unknown order kind {kind!r}")
            self._remember(oid, None)  # before running: a lost ack must never start it twice
            ran = True
            if kind == "launch":
                result = self.launcher.launch(order, self._sessions)
                self.pending[name] = (order, result, time.monotonic())
                self.log(f"started worker {name} in {result['folder']}")
            else:
                self.pending.pop(name, None)
                self.launcher.close(order)
                self.log(f"closed worker {name}")
            ok, error = True, ""
        except LaunchError as e:
            ok, error = False, str(e)
        except Exception as e:  # a bug in one order must not stop the poll loop
            ok, error = False, f"{order.get('kind') or 'order'} failed: {e}"
        if ran:
            self._remember(oid, ok, error)
        if not ok:
            self.log(f"order {oid} for worker {name} refused: {error}")
        if isinstance(oid, int) and not isinstance(oid, bool):
            self._ack(oid, ok, True, error)

    def _worker(self, body: dict) -> Optional[int]:
        try:
            status, _ = http.request("POST", self.cfg["hub"] + "/bridge/worker",
                                     self.cfg["token"], body)
        except http.HubUnreachable:
            return None
        return status

    def _stop(self, name: str, order: dict, why: str) -> None:
        try:
            self.launcher.close(order)
            self.log(f"worker {name} {why}; stopped its session")
        except LaunchError as e:
            self.log(f"worker {name} {why}; stopping its session failed: {e}")

    def _check_pending(self, sessions: list) -> None:
        """Tell the hub each started worker's session key once it shows up, or that it never
        did (then stop what was started, so nothing is left running unwatched)."""
        for name, (order, result, started) in list(self.pending.items()):
            try:
                key = self.launcher.find_key({**order, "folder": result["folder"],
                                              "key": result["key"]}, sessions)
                if key:
                    body = {"worker": name, "key": key}
                    if result.get("needs_brief"):
                        body["needs_brief"] = True
                    status = self._worker(body)
                    if status == 410:  # the worker was closed or failed meanwhile
                        self.pending.pop(name, None)
                        self._stop(name, order, "was closed while starting")
                    elif status == 404:
                        self.pending.pop(name, None)
                        self.log(f"worker {name}: the hub does not know it as starting here")
                    elif status == 204:
                        self.pending.pop(name, None)
                elif time.monotonic() - started > SHOW_UP_S:
                    status = self._worker({"worker": name, "error": (
                        "the session did not show up within 2 minutes;"
                        f" see ~/.kaptyorka/workers/{name}.log")})
                    if status in (204, 404, 410):
                        self.pending.pop(name, None)
                        self._stop(name, order, "did not show up within 2 minutes")
            except Exception as e:  # one worker must not block the others or the heartbeat
                self.pending.pop(name, None)
                self.log(f"worker {name}: {e}")

    def _ack(self, msg_id: int, ok: bool, launch: bool = False, error: str = "") -> None:
        url, body = self.cfg["hub"] + "/bridge/ack", {"id": msg_id, "ok": ok}
        if launch:
            body.update(launch=True, error=error)
        try:
            status, _ = http.request("POST", url, self.cfg["token"], body)
        except http.HubUnreachable:
            status, _ = http.request("POST", url, self.cfg["token"], body)
        self._report("ack", None if status == 204 else f"hub answered {status} to ack")

    def _rekey(self, m: dict) -> None:
        """The hub gave this legacy machine a token of its own: save it, switch to it, confirm."""
        self.cfg.update(token=m["token"], secret=m["secret"])
        paths.save_config(self.cfg)
        self.pending_confirm = True
        self._confirm()

    def _confirm(self) -> None:
        """Tell the hub the new token is in use; retried on each poll until it answers 204.
        The hub also drops the pending rekey on the first call made with the new token."""
        try:
            status, _ = http.request("POST", self.cfg["hub"] + "/bridge/rekeyed",
                                     self.cfg["token"], {"machine": self.cfg["machine"]})
        except http.HubUnreachable:
            status = None
        if status == 204:
            self.pending_confirm = False
            self._report("rekey", None)
            self.log("rekeyed: this machine now has its own token")
        else:
            self._report("rekey", f"hub answered {status} to rekeyed" if status
                         else "hub unreachable when confirming the new token")

    def poll_once(self, wait: float = WAIT_S) -> int:
        if self.pending_confirm:
            self._confirm()
        query = urlencode({"machine": self.cfg["machine"], "wait": wait})
        status, items = http.request("GET", f"{self.cfg['hub']}/bridge/poll?{query}",
                                     self.cfg["token"], timeout=wait + 5)
        if status != 200:
            raise RuntimeError(f"hub answered {status} to poll")
        by_prefix = {a.prefix: a for a in self.adapters}
        delivered = 0
        for m in items:
            if m.get("kind", "message") == "rekey":
                self._rekey(m)
                continue
            if m.get("launch") is True:
                self._order(m)
                continue
            key = m["key"]
            try:
                adapter = by_prefix.get(key.split(":", 1)[0])
                if adapter is None:
                    raise LookupError("no adapter for this tool")
                adapter.deliver(key, m["text"])
                ok = True
            except Exception as e:  # any failure leaves the message for pull
                self.log(f"push to {key} failed: {e}; message {m['id']} left for pull")
                ok = False
            self._ack(m["id"], ok)
            delivered += ok
        return delivered

    def _step(self) -> float:
        """One poll; returns how long to wait before the next. Logs each problem once."""
        try:
            delivered = self.poll_once(WAIT_S)
        except (http.HubUnreachable, RuntimeError) as e:
            problem = (f"kaptyorka hub unreachable at {self.cfg['hub']}"
                       if isinstance(e, http.HubUnreachable) else str(e))
            self._report("poll", problem)
            return RETRY_S
        self._report("poll", None)
        self._maybe_update(delivered)
        return 0

    @staticmethod
    def _numbers(version) -> tuple:
        return tuple(int(n) for n in re.findall(r"\d+", str(version or "")))

    def _maybe_update(self, delivered: int) -> None:
        """Run `kaptyorka update` once per hub version, only at a quiet moment."""
        new = self.hub.get("version")
        if (not self.hub.get("auto_update") or delivered or self.pending
                or new == self._updated_to
                or self._numbers(new) <= self._numbers(__version__)
                or time.monotonic() < self._update_retry_at):
            return
        argv0 = Path(sys.argv[0]).resolve() if sys.argv and sys.argv[0] else None
        exe = argv0 if argv0 and argv0.name == "kaptyorka" else Path(sys.executable).parent / "kaptyorka"
        self.log(f"updating kaptyorka {__version__} -> {new}")
        try:
            r = self.runner([str(exe), "update"], capture_output=True, text=True)
            error = "" if r.returncode == 0 else (r.stderr or r.stdout or "").strip() or f"exit {r.returncode}"
        except Exception as e:
            error = str(e)
        if error:
            self._report("update", f"auto-update failed: {error}")
            self._update_retry_at = time.monotonic() + UPDATE_RETRY_S
        else:
            self._report("update", None)
            self._updated_to = new

    def _beat(self) -> None:
        while not self.stop.is_set():
            try:
                self.heartbeat()
            except http.HubUnreachable:
                pass  # the poll loop reports the outage
            except Exception as e:
                self._report("heartbeat", f"heartbeat failed: {e}")
            self.stop.wait(HEARTBEAT_S)

    def run(self) -> None:
        beat = threading.Thread(target=self._beat, daemon=True)
        beat.start()
        try:
            while not self.stop.is_set():
                self.stop.wait(self._step())
        finally:
            self.stop.set()
            beat.join()
