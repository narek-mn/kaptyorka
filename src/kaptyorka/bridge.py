"""Per-machine bridge: reports running sessions and pushes messages through each tool's door."""
import sys
import threading
import time
from dataclasses import asdict
from typing import Callable, Dict, List, Optional
from urllib.parse import urlencode

from kaptyorka import http, paths
from kaptyorka.adapters.base import Adapter

HEARTBEAT_S = 10
WAIT_S = 25
RETRY_S = 5


def log(line: str) -> None:
    """Append to ~/.kaptyorka/bridge.log and print to stderr."""
    with open(paths.home() / "bridge.log", "a") as f:
        f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {line}\n")
    print(line, file=sys.stderr)


class Bridge:
    def __init__(self, cfg: dict, adapters: List[Adapter], log: Callable[[str], None]):
        self.cfg, self.adapters, self.log = cfg, adapters, log
        self.stop = threading.Event()
        self._problems: Dict[str, Optional[str]] = {}

    def _report(self, area: str, problem: Optional[str]) -> None:
        """Log a problem once per area until that area succeeds (problem None) again."""
        if problem is not None and problem != self._problems.get(area):
            self.log(problem)
        self._problems[area] = problem

    def heartbeat(self) -> None:
        sessions = []
        for a in self.adapters:
            try:
                found = [asdict(s) for s in a.sessions()]
                problem = None
            except Exception as e:  # one broken adapter must not hide the others
                found, problem = [], f"{a.tool} sessions failed: {e}"
            self._report("sessions:" + a.prefix, problem)
            sessions += found
        status, _ = http.request("POST", self.cfg["hub"] + "/bridge/heartbeat", self.cfg["token"],
                                 {"machine": self.cfg["machine"], "sessions": sessions})
        self._report("heartbeat", None if status == 204 else f"hub answered {status} to heartbeat")

    def _ack(self, msg_id: int, ok: bool) -> None:
        url, body = self.cfg["hub"] + "/bridge/ack", {"id": msg_id, "ok": ok}
        try:
            status, _ = http.request("POST", url, self.cfg["token"], body)
        except http.HubUnreachable:
            status, _ = http.request("POST", url, self.cfg["token"], body)
        self._report("ack", None if status == 204 else f"hub answered {status} to ack")

    def poll_once(self, wait: float = WAIT_S) -> int:
        query = urlencode({"machine": self.cfg["machine"], "wait": wait})
        status, items = http.request("GET", f"{self.cfg['hub']}/bridge/poll?{query}",
                                     self.cfg["token"], timeout=wait + 5)
        if status != 200:
            raise RuntimeError(f"hub answered {status} to poll")
        by_prefix = {a.prefix: a for a in self.adapters}
        delivered = 0
        for m in items:
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
