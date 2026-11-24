"""Warnings on the page in a real headless Chrome: at most one line over the chat (only for the
open chat's own machine), every warning in full under the top bar's warning sign, dismiss and
find again, nothing in the chat moving, a phone. Run with:
pytest -m slow tests/test_page_warnings_slow.py   (screenshots to KAPTYORKA_SHOTS)"""
import json
import os
import time
from pathlib import Path

import pytest

from chrome import chrome, chrome_path
from conftest import start_hub
from helpers import person
from kaptyorka.manager import ManagerStore
from kaptyorka.store import Store

pytestmark = [pytest.mark.slow,
              pytest.mark.skipif(chrome_path() is None, reason="no Chrome or Chromium installed")]

LONG = ("worker parser-tests on box-2 failed to start: the folder /home/bob/proj-a has uncommitted"
        " changes in 14 files and the worktree could not be created because a branch with that"
        " name already exists")


def w(host, user, text, short, team=False):
    return {"host": host, "os_user": user, "person": user, "text": text, "short": short,
            "blocks": True, "team": team}


OWN = w("box-1", "alice", "alice@box-1: no agent tool is logged in. Run `claude /login` there.",
        "no tool logged in")
MANY = [OWN,
        w("box-2", "bob", "bob@box-2: no agent tool is logged in. Run `codex login` there.", "no tool logged in"),
        w("box-3", "u", "u@box-3: no agent tool is logged in. Run `claude /login` there.", "no tool logged in", True),
        w("box-4", "carol", "bridge on carol@box-4 has been offline for 127 minutes", "bridge offline"),
        w("box-5", "u", "bridge on u@box-5 has been offline for 41 minutes", "bridge offline", True),
        w("box-2", "bob", LONG, "worker parser-tests failed")]
FAKE = """(() => { const real = window.fetch;
  window.fetch = function (u, o) {
    if (String(u) === "/api/warnings" && window.__warns) return Promise.resolve(new Response(JSON.stringify(window.__warns), { status: 200 }));
    return real.apply(this, arguments); }; })()"""
API_DEV = '[...document.querySelectorAll("#agents .row")].find(r => r.textContent.includes("api-dev"))'
LINE = 'document.querySelector("#warns .warn-line")'
WIDE = "document.documentElement.scrollWidth <= document.documentElement.clientWidth"


@pytest.fixture
def hub(tmp_path):
    """alice (admin) with one session on alice@box-1, a message each way."""
    store = Store(tmp_path / "hub.db")
    t0 = time.time()
    store.clock = lambda: t0
    ManagerStore(store).set_enabled(False, "test")
    alice, tok = person(store, "alice", "admin")
    store.heartbeat("box-1", [{"key": "a1", "tool": "claude-code", "project": "proj-a", "label": "api-dev"}],
                    principal=store.authenticate(tok), facts={"host": "box-1", "os_user": "alice"})
    store.send_from_web("alice", store.agent("a1").name, "Please run the migration check.", alice)
    store.send("a1", "alice", "Running it now. 3 of 12 checks done.")
    stop = start_hub(store)
    try:
        yield f"http://127.0.0.1:{stop.port}/#t={tok}"
    finally:
        stop()
        store.close()


def open_page(page, url, warns):
    page.call("Page.addScriptToEvaluateOnNewDocument", source=FAKE + f";window.__warns = {json.dumps(warns)};")
    page.go(url)
    page.wait(f"!!({API_DEV})")
    page.js(f"{API_DEV}.click()")
    page.wait('document.getElementById("chat-name").textContent === "api-dev"')


def test_one_line_and_the_list(hub, tmp_path):
    shots = Path(os.environ.get("KAPTYORKA_SHOTS") or tmp_path)
    for wd, h, tag in ((1440, 900, "desktop"), (390, 844, "phone")):
        with chrome(wd, h) as page:
            open_page(page, hub, MANY)
            page.wait(f"!!{LINE}")
            # one line for this chat's own machine; the rest are a count
            assert page.js(f"{LINE}.getBoundingClientRect().height") <= 40
            assert page.js('document.querySelectorAll("#warns .warn-line").length') == 1
            assert page.js(f'{LINE}.querySelector(".warn-text").textContent') == "alice@box-1: no agent tool is logged in"
            assert page.js(f'{LINE}.querySelector("code").textContent') == "claude /login"
            assert page.js(f'{LINE}.querySelector(".warn-more").textContent') == "+5 more"
            assert page.js('document.getElementById("warn-n").textContent') == "6"
            # the line floats: the messages do not move when it comes or goes
            assert page.js('document.getElementById("warns").getBoundingClientRect().height') == 0
            assert page.js(WIDE)
            time.sleep(0.3)
            page.screenshot(shots / f"warnings-after-{tag}.png")
            # +5 more opens the list: each in full, its command and Copy
            page.js(f'{LINE}.querySelector(".warn-more").click()')
            page.wait('!document.getElementById("warn-pop").hidden && document.querySelectorAll("#warn-list li").length === 6')
            assert page.js('document.activeElement.id') == "warn-pop"
            texts = page.js('[...document.querySelectorAll("#warn-list li p")].map(p => p.textContent)')
            assert LONG in texts and "bridge on u@box-5 has been offline for 41 minutes" in texts
            assert page.js('[...document.querySelectorAll("#warn-list code")].map(c => c.textContent)') == \
                ["claude /login", "codex login", "claude /login"]
            box = page.js('(() => { const r = document.getElementById("warn-pop").getBoundingClientRect(); return [r.left, r.right]; })()')
            assert box[0] >= 0 and box[1] <= wd
            assert page.js(WIDE)
            time.sleep(0.3)
            page.screenshot(shots / f"warnings-list-{tag}.png")
            # Escape closes it and gives the focus back to the sign
            page.call("Input.dispatchKeyEvent", type="keyDown", key="Escape", code="Escape", windowsVirtualKeyCode=27)
            page.wait('document.getElementById("warn-pop").hidden')
            assert page.js('document.activeElement.id') == "warn-ind"
            # dismiss: the line goes; the sign keeps it, Show again brings it back
            page.js(f'{LINE}.querySelector("button[aria-label=Dismiss]").click()')
            page.wait(f"!{LINE}")
            assert page.js('document.getElementById("warn-n").textContent') == "6"
            page.js("window.before = 1; location.reload()")  # remembered in this browser
            page.wait(f"!window.before && !!({API_DEV}) && !document.getElementById('warn-ind').hidden")
            page.js(f"{API_DEV}.click()")
            time.sleep(0.5)
            assert page.js(f"!{LINE}")
            page.js('document.getElementById("warn-ind").click()')
            page.wait('document.querySelectorAll("#warn-list li").length === 6')
            page.js('[...document.querySelectorAll("#warn-list button")].find(b => b.textContent === "Show again").click()')
            page.wait(f"!!{LINE}")
            page.js("document.body.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true }))")  # a click outside
            page.wait('document.getElementById("warn-pop").hidden')


def test_other_machines_only_in_the_sign_and_long_text(hub):
    with chrome() as page:
        open_page(page, hub, MANY[1:])  # nothing about this chat's machine
        page.wait('!document.getElementById("warn-ind").hidden')
        assert page.js('document.getElementById("warn-n").textContent') == "5"
        assert page.js(f"!{LINE}") and page.js('document.getElementById("warns").hidden')
    with chrome() as page:
        own_long = dict(OWN, text="alice@box-1: " + LONG.split(": ", 1)[1] + ". Run `claude /login` there.")
        open_page(page, hub, [own_long])
        page.wait(f"!!{LINE}")
        assert page.js(f"{LINE}.getBoundingClientRect().height") <= 40  # cut with an ellipsis, full on hover
        assert page.js(f'{LINE}.title') == own_long["text"]
        assert page.js(f'!{LINE}.querySelector(".warn-more")')  # one warning: no "+N more"
        assert page.js('document.getElementById("warn-n").textContent') == "1"
    with chrome() as page:
        open_page(page, hub, [])
        time.sleep(2.5)
        assert page.js('document.getElementById("warn-ind").hidden') and page.js(f"!{LINE}")
