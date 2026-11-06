"""Per-machine bridge: reports running sessions and pushes messages through each tool's door."""
import subprocess
import sys
import threading
import time
from dataclasses import asdict
from pathlib import Path
from typing import Callable, Dict, List, Optional
from urllib.parse import urlencode

from kaptyorka import facts, http, identity, paths
from kaptyorka.adapters.base import Adapter

HEARTBEAT_S = 10
WAIT_S = 25
RETRY_S = 5
FACTS_S = 60
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
    def __init__(self, cfg: dict, adapters: List[Adapter], log: Callable[[str], None], run=None):
        self.cfg, self.adapters, self.log = cfg, adapters, log
        self.runner = run or subprocess.run
        self.stop = threading.Event()
        self._problems: Dict[str, Optional[str]] = {}
        self.pending_confirm = False  # a new token is saved but the hub has not confirmed it
        self.hub: dict = {}  # the hub's last JSON answer to a heartbeat; {} before one or on 204
        self._facts: dict = {}
        self._facts_at = 0.0

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
                                      Path.home() / "workspace" / "projects")})
        self.hub = answer if status == 200 and isinstance(answer, dict) else {}
        self._report("heartbeat", None if status in (200, 204)
                     else f"hub answered {status} to heartbeat")

    def _ack(self, msg_id: int, ok: bool) -> None:
        url, body = self.cfg["hub"] + "/bridge/ack", {"id": msg_id, "ok": ok}
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
            self.poll_once(WAIT_S)
        except (http.HubUnreachable, RuntimeError) as e:
            problem = (f"kaptyorka hub unreachable at {self.cfg['hub']}"
                       if isinstance(e, http.HubUnreachable) else str(e))
            self._report("poll", problem)
            return RETRY_S
        self._report("poll", None)
        return 0

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
