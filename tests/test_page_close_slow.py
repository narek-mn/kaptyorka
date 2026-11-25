"""The page in a real headless Chrome: the close x on a session row (left.py Closing). A member
sees it on their own sessions, an admin on everyone's, never on the manager; an idle one
closes on the click, a newer one asks first in the row; the x never opens the chat, and the
row says Closing... until its bridge answers. Run with: pytest -m slow
tests/test_page_close_slow.py. Screenshots go to KAPTYORKA_SHOTS."""
import json
import os
from pathlib import Path

import pytest

from chrome import chrome, chrome_path
from conftest import start_hub
from helpers import person
from kaptyorka import http
from kaptyorka.manager import ManagerStore
from kaptyorka.store import Store

pytestmark = [pytest.mark.slow,
              pytest.mark.skipif(chrome_path() is None, reason="no Chrome or Chromium installed")]

ROW = '[...document.querySelectorAll("#agents .row")].find(r => r.querySelector(".name")?.textContent === %s)'
DAY = 86400


def row(name):
    return ROW % json.dumps(name)


def s(key, label, idle_s):
    return {"key": key, "tool": "claude-code", "project": "proj-a", "label": label,
            "status": "idle", "idle_s": idle_s}


def open_idle(page, name):
    if not page.js(f'!!({row(name)})'):  # in Idle, folded at first
        page.js('document.querySelector("#agents [data-key=\\"idle-head\\"]").click()')
        page.wait(f'!!({row(name)})')


def test_who_sees_the_x_and_what_it_does(tmp_path):
    store = Store(tmp_path / "hub.db")
    ManagerStore(store).set_enabled(False, "test")
    _, admin = person(store, "alice", "admin")
    bob, token = person(store, "bob")
    store.project_register("proj-a", by="alice")
    store.grant_project("proj-a", bob, "write", "alice")
    beat = [s("claude:b1", "old-term", 3 * DAY), s("claude:b2", "busy-one", 60)]
    store.heartbeat("box-2", beat, principal=store.authenticate(token),
                    facts={"host": "box-2", "os_user": "bob"})
    store.heartbeat("box-1", [s("claude:a1", "alice-own", 60)], principal=store.authenticate(admin),
                    facts={"host": "box-1", "os_user": "alice"})
    stop = start_hub(store)
    url = f"http://127.0.0.1:{stop.port}"
    out = Path(os.environ.get("KAPTYORKA_SHOTS") or tmp_path)
    x = '.querySelector(".row-x")'
    try:
        with chrome(1440, 900) as page:  # the admin, Everyone: bob's sessions have the x too
            page.go(f"{url}/#t={admin}")
            page.wait(f'!!({row("alice-own")})')
            page.js('document.querySelector("#h-scope [data-scope=\\"everyone\\"]").click()')
            page.js('[...document.querySelectorAll("#agents .g-head")].forEach(h => h.getAttribute("aria-expanded") === "false" && h.click())')
            page.wait(f'!!({row("busy-one")})')
            assert page.js(f'!!{row("busy-one")}{x} && !!{row("alice-own")}{x}')
            assert page.js('!document.querySelector("#agents [data-key=\\"mgr\\"] .row-x")')
        for w, h, tag in ((1440, 900, "desktop"), (390, 844, "phone")):
            with chrome(w, h) as page:  # bob: his own two
                page.go(f"{url}/#t={token}")
                page.wait(f'!!({row("busy-one")})')
                assert page.js(f'{row("busy-one")}{x}.getAttribute("title")') == "Close session"
                # in use: asks once, in the row; the x never opens the chat
                page.js(f'{row("busy-one")}{x}.click()')
                page.wait(f'{row("busy-one")}.textContent.includes("Close this session? It is in use.")')
                assert not page.js(f'{row("busy-one")}.classList.contains("on")')
                assert page.js("document.documentElement.scrollWidth <= document.documentElement.clientWidth")
                page.screenshot(out / f"close-ask-{tag}.png")
                page.js(f'[...{row("busy-one")}.querySelectorAll(".row-ask [role=button]")].find(b => b.textContent === "Cancel").click()')
                page.wait(f'!{row("busy-one")}.textContent.includes("Close this session?")')
                assert page.js(f'!!{row("busy-one")}{x}')
                if tag == "phone":
                    page.js(f'{row("busy-one")}.click()')  # the row itself still opens the chat
                    page.wait(f'{row("busy-one")}.classList.contains("on")')
                    continue
                # idle over a day: closes on the click
                open_idle(page, "old-term")
                page.js(f'{row("old-term")}{x}.click()')
                page.wait(f'{row("old-term")}.textContent.includes("Closing\\u2026")')
                assert not page.js(f'{row("old-term")}.classList.contains("on")')
                assert not page.js(f'!!{row("old-term")}{x}')
                page.screenshot(out / f"close-closing-{tag}.png")
                # its bridge says no: the row says why, the x is back
                status, _ = http.request("POST", url + "/bridge/heartbeat", token, {
                    "machine": "box-2", "sessions": beat,
                    "close_done": {"claude:b1": "in use again, not closed"}})
                assert status == 200
                page.wait(f'{row("old-term")}.textContent.includes("in use again, not closed")', 15)
                page.wait(f'!!{row("old-term")}{x}')
                page.screenshot(out / f"close-refused-{tag}.png")
                page.js(f'{row("old-term")}{x}.click()')
                page.wait(f'{row("old-term")}.textContent.includes("Closing\\u2026")')
                # this time it closed: the row goes
                http.request("POST", url + "/bridge/heartbeat", token, {
                    "machine": "box-2", "sessions": [beat[1]], "close_done": {"claude:b1": ""}})
                page.wait(f'!({row("old-term")})', 20)
    finally:
        stop()
        store.close()
