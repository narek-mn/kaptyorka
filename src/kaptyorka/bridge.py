"""Per-machine bridge: reports running sessions and pushes messages through each tool's door."""
import sys
import threading
import time
from dataclasses import asdict
from typing import Callable, List, Optional
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
        self._problem: Optional[str] = None

    def heartbeat(self) -> None:
        sessions = [asdict(s) for a in self.adapters for s in a.sessions()]
        http.request("POST", self.cfg["hub"] + "/bridge/heartbeat", self.cfg["token"],
                     {"machine": self.cfg["machine"], "sessions": sessions})

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
            http.request("POST", self.cfg["hub"] + "/bridge/ack", self.cfg["token"],
                         {"id": m["id"], "ok": ok})
            delivered += ok
        return delivered

    def _step(self) -> float:
        """One poll; returns how long to wait before the next. Logs each problem once."""
        try:
            self.poll_once(WAIT_S)
        except (http.HubUnreachable, RuntimeError) as e:
            problem = (f"kaptyorka hub unreachable at {self.cfg['hub']}"
                       if isinstance(e, http.HubUnreachable) else str(e))
            if problem != self._problem:
                self._problem = problem
                self.log(problem)
            return RETRY_S
        self._problem = None
        return 0

    def _beat(self) -> None:
        while not self.stop.is_set():
            try:
                self.heartbeat()
            except http.HubUnreachable:
                pass  # the poll loop reports the outage
            except Exception as e:
                self.log(f"heartbeat failed: {e}")
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
