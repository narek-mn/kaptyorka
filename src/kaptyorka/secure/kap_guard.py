#!/usr/bin/python3 -I
"""guard (red team round 3): keeps two things current the moment the box changes them.

1. The covers in each person's view of a team folder (kaproot.hide_view): when the box makes,
   renames, removes or rewrites a tool folder or file, or one of git's (.opencode, .claude,
   opencode.json, .git/hooks, .git/config, ...), the view gets its cover again, so the person's
   own OpenCode, Claude Code, Codex, Cursor, editor or git started in her view loads only an
   empty folder or file of her own, never code the box wrote (finding 1; round 4, findings 1
   and 3). At the top of the folder and in its .git within milliseconds (a quick pass that looks
   only there); anywhere below after at least DEEP_WAIT and three times the last full walk, so a
   busy box cannot keep the guard walking. Up to kc.WATCH_MAX folders are watched per team
   folder; deeper changes beyond that are seen at the next RESYNC.
2. The relay's copy of each box's sign-in values (kaproot.write_box_secrets), rewritten when a
   tool in the box signs in or refreshes its sign-in, so the relay refuses to send them away in
   any form it knows (finding 2).

Root, from kaptyorka-guard.service, in the computer's own mount namespace (its covers must be
the person's). It watches with inotify (never following a link), looks at everything again every
RESYNC seconds and on SIGHUP (hubchat add sends one), and after an overflow of events. Box-
written names are only ever compared with fixed lists, never run or followed. Installed as
/usr/local/libexec/kaptyorka/guard. Standard library only.
"""
import ctypes
import os
import select
import signal
import struct
import sys
import time
from typing import Dict, List, Tuple

if __package__:
    from . import kapcommon as kc, kaproot as kr
else:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import kapcommon as kc
    import kaproot as kr

RESYNC = 10.0
SETTLE = 0.02     # gather a burst of events, then act once
DEEP_WAIT = 0.1   # least time between two full walks of one folder
IN_MODIFY, IN_ATTRIB, IN_CLOSE_WRITE, IN_MOVED_FROM, IN_MOVED_TO = 0x2, 0x4, 0x8, 0x40, 0x80
IN_CREATE, IN_DELETE, IN_DELETE_SELF, IN_MOVE_SELF, IN_Q_OVERFLOW, IN_IGNORED = 0x100, 0x200, 0x400, 0x800, 0x4000, 0x8000
IN_ONLYDIR, IN_DONT_FOLLOW, IN_NONBLOCK, IN_CLOEXEC = 0x01000000, 0x02000000, 0o4000, 0o2000000
DIR_EVENTS = IN_CREATE | IN_DELETE | IN_MOVED_FROM | IN_MOVED_TO | IN_DELETE_SELF | IN_MOVE_SELF
FILE_EVENTS = DIR_EVENTS | IN_CLOSE_WRITE | IN_MODIFY | IN_ATTRIB
TOP_EVENTS = DIR_EVENTS | IN_CLOSE_WRITE           # the top of a team folder: a tool file rewritten in place
GIT_EVENTS = DIR_EVENTS | IN_CLOSE_WRITE | IN_MODIFY   # a .git: config rewritten in place (round 4, finding 1)


def log(msg: str) -> None:
    print(kc.term(f"guard: {msg}"), flush=True)


class Inotify:
    def __init__(self):
        self.libc = ctypes.CDLL(None, use_errno=True)
        self.libc.inotify_add_watch.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
        self.fd = self.libc.inotify_init1(IN_NONBLOCK | IN_CLOEXEC)
        if self.fd < 0:
            raise OSError(ctypes.get_errno(), "inotify_init1")
        self.wd: Dict[int, Tuple[str, str]] = {}     # watch -> (kind, key)

    def add(self, path: str, mask: int, kind: str, key: str) -> None:
        w = self.libc.inotify_add_watch(self.fd, path.encode(), mask | IN_ONLYDIR | IN_DONT_FOLLOW)
        if w >= 0:
            self.wd[w] = (kind, key)

    def reset(self) -> None:
        for w in list(self.wd):
            self.libc.inotify_rm_watch(self.fd, w)
        self.wd.clear()

    def read(self) -> Tuple[List[Tuple[str, str]], bool]:
        """The (kind, key) of every watch that saw an event, and whether events were lost."""
        hits, lost = [], False
        while True:
            try:
                buf = os.read(self.fd, 65536)
            except BlockingIOError:
                break
            i = 0
            while i + 16 <= len(buf):
                w, mask, _, ln = struct.unpack_from("iIII", buf, i)
                i += 16 + ln
                if mask & IN_Q_OVERFLOW:
                    lost = True
                elif mask & IN_IGNORED:
                    self.wd.pop(w, None)     # the folder is gone
                elif w in self.wd:
                    hits.append(self.wd[w])
        return hits, lost


def plan() -> Tuple[Dict[str, Tuple[str, str, str]], Dict[str, str]]:
    """(folder key -> (person, name, box), box -> person) from every person's folders.json."""
    folders: Dict[str, Tuple[str, str, str]] = {}
    boxes: Dict[str, str] = {}
    try:
        people = sorted(p for p in os.listdir(kc.PEOPLE) if kc.PERSON_RX.fullmatch(p))
    except FileNotFoundError:
        people = []
    for person in people:
        for b in kr.boxes_of(person):
            boxes[b] = person
        try:
            for name, e in kr.load_folders(person).items():
                folders[f"{person}/{name}"] = (person, name, e["box"])
        except (kc.Refuse, ValueError, KeyError, OSError) as e:
            log(f"{person}: folders.json: {e}")
    return folders, boxes


def watch_folder(ino: Inotify, key: str, top: str, walked: List[str]) -> None:
    """The top and its .git (quick pass), and the folders below it (full walk)."""
    ino.add(top, TOP_EVENTS, "top", key)
    ino.add(top + "/.git", GIT_EVENTS, "top", key)
    for rel in walked[:kc.WATCH_MAX]:
        if rel != ".git":
            ino.add(f"{top}/{rel}", GIT_EVENTS if rel.endswith(".git") else DIR_EVENTS, "deep", key)


def watch_boxes(ino: Inotify, boxes) -> None:
    for box in boxes:
        home = kc.box_home(box)
        for d in sorted({os.path.dirname(f"{home}/{rel}") for rel in kc.SIGNIN_FILES}):
            ino.add(d, FILE_EVENTS, "box", box)
        for d in ("state/opencode", "state/opencode/data", ".config"):    # a sign-in folder made later
            ino.add(f"{home}/{d}", DIR_EVENTS, "box", box)


def do_folder(ino: Inotify, folders, key: str, deep: bool = True) -> None:
    person, name, box = folders[key]
    info: dict = {}
    try:
        done = kr.hide_view(person, name, box, deep=deep, info=info)
        if done:
            log(f"{person}/{name}: {' '.join(done)}")
    except (kc.Refuse, OSError, KeyError) as e:
        log(f"{person}/{name}: {e}")
    if deep:
        watch_folder(ino, key, f"{kc.box_projects(box)}/{name}", info.get("walked", []))


def do_box(boxes, box: str) -> None:
    try:
        kr.write_box_secrets(boxes[box], box)
    except (kc.Refuse, OSError, KeyError, ValueError) as e:
        log(f"box {box}: sign-ins: {e}")


def main() -> int:
    if os.geteuid() != 0:
        raise SystemExit("guard: run as root (kaptyorka-guard.service)")
    hup = [True]
    signal.signal(signal.SIGHUP, lambda *_: hup.__setitem__(0, True))
    ino = Inotify()
    folders: Dict[str, Tuple[str, str, str]] = {}
    boxes: Dict[str, str] = {}
    pending: Dict[str, float] = {}     # folder key -> when its full walk may run
    took: Dict[str, float] = {}        # folder key -> how long its last full walk took
    last = 0.0
    log("up")

    def full(key: str) -> None:
        t0 = time.monotonic()
        do_folder(ino, folders, key)
        took[key] = time.monotonic() - t0

    while True:
        if hup[0] or time.monotonic() - last > RESYNC:
            hup[0] = False
            folders, boxes = plan()
            ino.reset()
            pending.clear()
            for key in folders:
                full(key)
            watch_boxes(ino, boxes)
            for b in boxes:
                do_box(boxes, b)
            last = time.monotonic()
        now = time.monotonic()
        for key in [k for k, when in pending.items() if when <= now]:
            del pending[key]
            if key in folders:
                full(key)
        wait = min([1.0] + [max(0.0, when - time.monotonic()) for when in pending.values()])
        try:
            ready, _, _ = select.select([ino.fd], [], [], wait)
        except InterruptedError:
            continue
        if not ready:
            continue
        time.sleep(SETTLE)
        hits, lost = ino.read()
        if lost:
            hup[0] = True
            continue
        now = time.monotonic()
        for kind, key in sorted(set(hits)):
            if kind in ("top", "deep") and key in folders:
                if kind == "top":
                    do_folder(ino, folders, key, deep=False)     # at once: the top and its .git
                pending.setdefault(key, now + max(DEEP_WAIT, 3 * took.get(key, 0.0)))
            elif kind == "box" and key in boxes:
                do_box(boxes, key)


if __name__ == "__main__":
    sys.exit(main())
