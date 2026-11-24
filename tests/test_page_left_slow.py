"""The page in a real headless Chrome: a session another one started sits under it on a rail,
and one whose starter ended says "left by" (left.py). Run with: pytest -m slow
tests/test_page_left_slow.py. Screenshots go to KAPTYORKA_SHOTS."""
import json
import os
import time
from pathlib import Path

import pytest

from chrome import chrome, chrome_path
from conftest import start_hub
from helpers import person
from kaptyorka import left
from kaptyorka.manager import ManagerStore
from kaptyorka.store import Store

pytestmark = [pytest.mark.slow,
              pytest.mark.skipif(chrome_path() is None, reason="no Chrome or Chromium installed")]

ROW = '[...document.querySelectorAll("#agents .row")].find(r => r.querySelector(".name")?.textContent === %s)'


def row(name):
    return ROW % json.dumps(name)


def s(key, label, **kw):
    return {"key": key, "tool": "claude-code", "project": "proj-a", "label": label,
            "activity": "idle", **kw}


def test_children_under_their_starter_and_left_by(tmp_path):
    store = Store(tmp_path / "hub.db")
    now = [time.time()]
    store.clock = lambda: now[0]
    ManagerStore(store).set_enabled(False, "test")
    person(store, "alice", "admin")
    bob, token = person(store, "bob")
    store.project_register("proj-a", by="alice")
    store.grant_project("proj-a", bob, "write", "alice")
    sessions = [s("p1", "api-dev"), s("p2", "planner"),
                s("c1", "helper-tests", background=True, parent="p1", helper=True),
                s("c2", "helper-docs", parent="p1", helper=True),
                s("c3", "helper-old", background=True, parent="gone", helper=True, parent_gone=True)]
    for step in (left.FRESH_S + 1, 0):  # the second beat after FRESH_S: the children are listed
        store.heartbeat("box-2", sessions, principal=store.authenticate(token),
                        facts={"host": "box-2", "os_user": "bob"})
        now[0] += step
    stop = start_hub(store)
    out = Path(os.environ.get("KAPTYORKA_SHOTS") or tmp_path)
    try:
        for w, h, tag in ((1440, 900, "desktop"), (390, 844, "phone")):
            with chrome(w, h) as page:
                page.go(f"http://127.0.0.1:{stop.port}/#t={token}")
                page.wait(f'!!({row("helper-tests")})')
                names = page.js('[...document.querySelector("#agents .wt-rows").querySelectorAll(".row .name")]'
                                '.map(n => n.textContent)')
                assert names == ["api-dev", "helper-tests", "helper-docs"], names
                assert page.js(f'{row("helper-tests")}.classList.contains("wt-sub")')
                assert not page.js(f'{row("api-dev")}.classList.contains("wt-sub")')
                if not page.js(f'!!({row("helper-old")})'):  # in Background, folded at first
                    page.js('document.querySelector("#agents [data-key=\\"bg-head\\"]").click()')
                    page.wait(f'!!({row("helper-old")})')
                old = page.js(f'{row("helper-old")}.querySelector(".subline").textContent')
                assert old.startswith("left by another session"), old
                listed = page.js('[...document.querySelectorAll("#agents .row .name")].map(n => n.textContent)')
                assert len(listed) == len(set(listed)), listed
                assert page.js("document.documentElement.scrollWidth <= document.documentElement.clientWidth")
                page.screenshot(out / f"left-{tag}.png")
    finally:
        stop()
        store.close()
