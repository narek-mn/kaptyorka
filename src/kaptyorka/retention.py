import sys
import threading
import time

from kaptyorka import memory


def _stderr(line: str) -> None:
    print(line, file=sys.stderr, flush=True)


def prune_loop(store, stop: threading.Event, interval_s: float = 86400, report=_stderr) -> None:
    """Prune old messages once now, then every `interval_s`, until `stop` is set."""
    while not stop.is_set():
        try:
            out = store.prune(time.time())
            if out["messages"] or out["files"]:
                report(f"retention: deleted {out['messages']} messages, {out['files']} files")
        except Exception as e:  # a failed prune must not stop the next one
            report(f"retention: prune failed: {e}")
        try:
            git = memory.prune(store, time.time())
            if git:
                report(f"retention: deleted {git} git facts")
        except Exception as e:
            report(f"retention: git facts prune failed: {e}")
        stop.wait(interval_s)
