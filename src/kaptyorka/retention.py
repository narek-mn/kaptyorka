import sys
import threading
import time

from kaptyorka import admin_ops, memory


def _stderr(line: str) -> None:
    print(line, file=sys.stderr, flush=True)


def prune_loop(store, stop: threading.Event, interval_s: float = 86400, report=_stderr) -> None:
    """Prune old messages and memory once now, then every `interval_s`, until `stop` is set."""
    while not stop.is_set():
        try:
            out = store.prune(time.time())
            if out["messages"] or out["files"]:
                report(f"retention: deleted {out['messages']} messages, {out['files']} files")
        except Exception as e:  # a failed prune must not stop the next one
            report(f"retention: prune failed: {e}")
        try:
            admin_ops.purge_deleted_notes(store, time.time())
        except Exception as e:
            report(f"retention: note purge failed: {e}")
        try:
            out = memory.prune(store, time.time())
            if any(out.values()):
                report(f"retention: deleted {out['git_facts']} git facts,"
                       f" {out['done_checks']} done checks, {out['state_writes']} state writes")
        except Exception as e:
            report(f"retention: memory prune failed: {e}")
        stop.wait(interval_s)
