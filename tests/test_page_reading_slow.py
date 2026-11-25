"""The chat as something to read, in a real headless Chrome: two sides (what I sent on the right
on the accent surface, the other side on the left on a neutral one), 16 px text in lines under
80 characters (counted on the rendered lines), every head saying who and where, a time on
each later message of a run, markdown rendered (and never HTML), long messages folded but never
the newest, the scroll left alone while you read, and the manager's state and a late answer in
words. Screenshots to KAPTYORKA_SHOTS.
Run with: pytest -m slow tests/test_page_reading_slow.py"""
import json
import os
import time
from pathlib import Path

import pytest

from chrome import chrome, chrome_path
from reading_fixture import MEASURE, SIDES, build, manager_chat

pytestmark = [pytest.mark.slow,
              pytest.mark.skipif(chrome_path() is None, reason="no Chrome or Chromium installed")]

ROW = '[...document.querySelectorAll("#agents .row")].find(r => r.querySelector(".name")?.textContent === "api-dev")'
LOG = 'document.getElementById("log")'
EVIL = ('<script>window.__pwned = 1</script> <img src=x onerror="window.__pwned = 2"> '
        '[click](javascript:window.__pwned=3) [ok](https://example.com/a?b=1) '
        '`<b>bold?</b>` **<i>x</i>**\n\n```html\n<script>window.__pwned = 4</script>\n```\n'
        '| a | b |\n|---|---|\n| <u>1</u> | [y](data:text/html,x) |')


@pytest.fixture
def hub(tmp_path):
    store, url, stop = build(tmp_path)
    try:
        yield store, url
    finally:
        stop()
        store.close()


def open_chat(page, url):
    page.go(url)
    page.wait(f"!!({ROW})")
    page.js(f"{ROW}.click()")
    page.wait('document.querySelectorAll("#log .msg").length > 20')


def test_column_markdown_and_folding(hub, tmp_path):
    store, url = hub
    shots = Path(os.environ.get("KAPTYORKA_SHOTS") or tmp_path)
    for w, h, tag in ((1440, 900, "1440"), (1920, 1080, "1920"), (390, 844, "phone")):
        with chrome(w, h) as page:
            open_chat(page, url)
            m = page.js(MEASURE)
            assert m["font_px"] == 16 and m["line_height"] == 1.55 and m["contrast"] >= 4.5, m
            if w > 720:  # the pane is wide, the lines are not
                assert 60 <= (m["chars_per_line"] or 0) <= 80, (w, m)
            # who and where: the session's machine, project and tool beside its name, never cut
            head = '[...document.querySelectorAll("#log .group .head")].find(h => h.querySelector("b").textContent === "api-dev")'
            assert page.js(f'{head}.querySelector(".ctx").textContent') == "bob@box-2 \u00b7 proj-a \u00b7 Claude Code"
            assert page.js(f'{head}.scrollWidth <= {head}.clientWidth')
            # a later message of a run has its own time beside it
            assert page.js('[...document.querySelectorAll("#log .group .msg + .msg")].every(m => m.querySelector(":scope > time.at"))')
            assert page.js('document.querySelectorAll("#log .group .msg > time.at").length') > 0
            assert page.js("document.documentElement.scrollWidth <= document.documentElement.clientWidth")
            # markdown, not its marks: headings, lists, code, a table
            text = page.js(f"{LOG}.innerText")
            assert "## Status" not in text and "**214 passed**" not in text and "```" not in text and "|------|" not in text
            assert page.js('[...document.querySelectorAll("#log .md-h")].some(h => h.textContent === "Status: tokenizer migration")')
            assert page.js('document.querySelectorAll("#log .md ol > li").length') >= 4
            assert page.js('!!document.querySelector("#log .md-pre pre code")') and page.js('!!document.querySelector("#log .md-pre .md-copy")')
            assert page.js('document.querySelector("#log .md table th:nth-child(2)").style.textAlign') == "right"
            # wide code scrolls inside its box, never the page
            pre = 'document.querySelector("#log .md-pre pre")'
            assert page.js(f'getComputedStyle({pre}).overflowX') == "auto"
            # the last message is never folded; an older long one is, and stays open once opened
            assert page.js('!document.querySelector("#log .msg:last-of-type .text.clamp")')
        with chrome(w, h) as page:
            store.send("b1", "bob", "\n".join(f"{i}. step {i} of the long migration checklist, done" for i in range(1, 41)))
            store.send("b1", "bob", "That was the whole list.")
            open_chat(page, url)
            long = '[...document.querySelectorAll("#log .msg")].find(m => m.textContent.includes("step 40 of"))'
            page.wait(f"!!({long})")
            assert page.js(f'!!{long}.querySelector(".text.clamp")') and page.js(f'{long}.querySelector(".more").textContent') == "Show more"
            assert page.js(f'{long}.querySelector(".text").getBoundingClientRect().height') <= 16 * 32 + 2
            page.js(f'{long}.querySelector(".more").click()')
            page.wait(f'{long}.querySelector(".more").textContent === "Show less"')
            store.send("b1", "bob", "One more thing: CI is green.")  # a redraw of the chat
            page.wait('document.getElementById("log").textContent.includes("CI is green")')
            assert page.js(f'!{long}.querySelector(".text.clamp")')  # still open
            page.js(f'{long}.scrollIntoView()')
            time.sleep(0.3)
            page.screenshot(shots / f"reading-long-{tag}.png")


def test_two_sides(hub):
    """What I sent sits on the right in the accent bubble, of limited width, with no name over it;
    the session's answer on the left on a neutral surface under its who-and-where head; in the
    session's own chat another session writing to it sits on the right in a second neutral tone.
    In the manager's chat everybody but me is on the left. Text on every surface is at least
    4.5:1, at a desktop and a phone width."""
    store, url = hub
    alice = manager_chat(store)
    for w, h, share, gut in ((1366, 768, 0.70, 40), (390, 844, 0.85, 32)):
        with chrome(w, h) as page:
            open_chat(page, url)
            rows = page.js(SIDES)
            mine, left, peer = ([r for r in rows if r["side"] == s] for s in ("mine", "", "peer"))
            assert mine and left and peer, (len(mine), len(left), len(peer))
            col = rows[0]["col"]
            assert col == min(768, rows[0]["pane"]), rows[0]
            for r in mine:  # flush right, never the whole width, no head of its own
                assert r["right"] == 0 and r["left"] > 0 and r["width"] <= col * share + 1 and not r["head"], r
            for r in left:  # after the badge gutter, under a head
                assert r["left"] == gut and r["head"], r
            for r in peer:
                assert r["right"] == gut and r["head"], r  # one with code may take the whole width
            page_bg = page.js("getComputedStyle(document.body).backgroundColor")
            tones = [{r["bg"] for r in side} for side in (mine, left, peer)]
            assert all(len(t) == 1 for t in tones) and len(set.union(*tones) | {page_bg}) == 4, (tones, page_bg)
            assert page.js('getComputedStyle(document.querySelector("#log .group.mine .bub")).backgroundColor') == \
                page.js('getComputedStyle(document.querySelector(".chat")).getPropertyValue("--mine")'
                        '.replace(/^#(..)(..)(..)$/, (_, r, g, b) => `rgb(${parseInt(r, 16)}, ${parseInt(g, 16)}, ${parseInt(b, 16)})`)')
            assert min(r["contrast"] for r in rows) >= 4.5, sorted({r["contrast"] for r in rows})
            # a wide answer uses the column; my own time sits beside my bubble, inside the column
            assert max(r["width"] for r in left) >= (col - gut) * 0.9
            assert page.js('[...document.querySelectorAll("#log .group.mine .msg")].every(m => { const t = m.querySelector(":scope > time.at");'
                           ' return t && t.getBoundingClientRect().right <= m.querySelector(".bub").getBoundingClientRect().left; })')
            assert page.js("document.documentElement.scrollWidth <= document.documentElement.clientWidth")
        with chrome(w, h) as page:  # the manager's chat, read by the admin
            page.go(url.split("#")[0] + alice)
            page.wait('!!document.querySelector("#agents .row.mgr")')
            page.js('document.querySelector("#agents .row.mgr").click()')
            page.wait('document.querySelectorAll("#log .msg").length > 8')
            rows = page.js(SIDES)
            assert {r["side"] for r in rows} == {"mine", ""}, {r["side"] for r in rows}
            assert all(r["right"] == 0 for r in rows if r["side"] == "mine") and all(r["left"] == gut for r in rows if not r["side"])
            names = page.js('[...document.querySelectorAll("#log .head > b")].map(b => b.textContent)')
            assert "you" not in names and {"api-dev", "trainer", "parser-tests", "bob", "manager"} <= set(names), names
            # the receiver only where it is not the manager itself; my two in a row are one run
            assert page.js('[...document.querySelectorAll("#log .head .to .to-n")].map(t => t.textContent)') == ["api-dev", "bob", "parser-tests"]
            assert page.js('Math.max(...[...document.querySelectorAll("#log .group.mine")].map(g => g.querySelectorAll(".msg").length))') == 2
            tick = 'document.querySelector("#log .group.mine .msg:last-child .tick")'
            assert page.wait(f'{tick}?.textContent') == "No answer yet"
            assert page.js(f'Math.round({tick}.getBoundingClientRect().right) === Math.round({tick}.closest(".group").getBoundingClientRect().right)')
            assert page.js("document.documentElement.scrollWidth <= document.documentElement.clientWidth")


def test_never_html(hub):
    store, url = hub
    store.send("b1", "bob", EVIL)
    with chrome() as page:
        open_chat(page, url)
        page.wait(f'{LOG}.textContent.includes("bold?")')
        msg = '[...document.querySelectorAll("#log .msg")].find(m => m.textContent.includes("bold?"))'
        assert page.js("window.__pwned") is None
        assert page.js(f'{msg}.querySelectorAll("script, img, iframe, u, i, b").length') == 0
        hrefs = page.js(f'[...{msg}.querySelectorAll("a")].map(a => [a.getAttribute("href"), a.rel, a.target])')
        assert hrefs == [["https://example.com/a?b=1", "noopener noreferrer nofollow", "_blank"]]
        text = page.js(f"{msg}.textContent")
        assert "<script>window.__pwned = 1</script>" in text and "[click](javascript:window.__pwned=3)" in text
        assert page.js(f'{msg}.querySelector("code").textContent') == "<b>bold?</b>"
        assert "<script>window.__pwned = 4</script>" in page.js(f'{msg}.querySelector("pre code").textContent')
        assert "[y](data:text/html,x)" in page.js(f'{msg}.querySelector("table").textContent')


def test_scroll_is_left_alone_while_reading(hub):
    store, url = hub
    with chrome() as page:
        open_chat(page, url)
        time.sleep(0.5)
        assert page.js(f"{LOG}.scrollHeight - {LOG}.scrollTop - {LOG}.clientHeight") < 60  # opens at the newest
        page.js(f"{LOG}.scrollTop = 400")
        time.sleep(0.3)
        top = page.js(f"{LOG}.scrollTop")
        store.send("b1", "bob", "A new message while you read older ones.")
        page.wait(f'{LOG}.textContent.includes("while you read older")')
        assert abs(page.js(f"{LOG}.scrollTop") - top) < 2  # no jump
        assert page.wait('!document.getElementById("jump").hidden')
        page.js('document.getElementById("jump").click()')
        time.sleep(0.2)
        assert page.js(f"{LOG}.scrollHeight - {LOG}.scrollTop - {LOG}.clientHeight") < 60
        assert page.js('document.getElementById("jump").hidden')
        # at the bottom, a new message keeps you there and needs no button
        store.send("b1", "bob", "And another one at the bottom.")
        page.wait(f'{LOG}.textContent.includes("another one at the bottom")')
        time.sleep(0.2)
        assert page.js(f"{LOG}.scrollHeight - {LOG}.scrollTop - {LOG}.clientHeight") < 60
        assert page.js('document.getElementById("jump").hidden')
        # keyboard: a code block takes focus and scrolls sideways
        assert page.js('document.querySelector("#log .md-pre pre").tabIndex') == 0
        assert json.loads(json.dumps(page.js('document.querySelector("#jump").tagName'))) == "BUTTON"


def test_manager_state_and_late_answer(hub, tmp_path):
    """The manager says how it is in words (Ready with a session, Off without), and my message to
    it, a minute old with no answer, says "No answer yet" with the reason."""
    store, url = hub
    shots = Path(os.environ.get("KAPTYORKA_SHOTS") or tmp_path)
    from kaptyorka.manager import ManagerStore
    mtoken = ManagerStore(store).ensure_account("box-1", "alice")
    store.heartbeat("box-1", [{"key": "k:mgr", "tool": "claude-code", "project": "manager", "label": "manager"}],
                    principal=store.authenticate(mtoken), facts={"host": "box-1", "os_user": "u"})
    mgr = store.agent("k:mgr").name
    with store.lock:
        store.db.execute("INSERT INTO messages (sender_key, sender_name, to_key, to_name, machine, tool, project, text, created, read_at)"
                         " VALUES ('web:bob','bob','k:mgr',?,'','','','Who reviews tokenizer-v2?',?,?)",
                         (mgr, time.time() - 120, time.time() - 118))
    chip = 'document.querySelector("#agents .row.mgr .chip")'
    with chrome() as page:
        page.go(url)
        assert page.wait(f'{chip}?.textContent') == "Ready"
        page.js('document.querySelector("#agents .row.mgr").click()')
        assert page.wait('document.querySelector("#log .tick.late")?.textContent') == "No answer yet"
        assert page.js('document.querySelector("#chat-sub .chip").textContent') == "Ready"
        page.screenshot(shots / "manager-late.png")
    with store.lock:
        store.db.execute("UPDATE agents SET last_seen=last_seen-100 WHERE key='k:mgr'")  # gone: the hub has it off
    with chrome() as page:
        page.go(url)
        assert page.wait(f'{chip}?.textContent') == "Off"
        page.js('document.querySelector("#agents .row.mgr").click()')
        assert page.wait('document.querySelector("#log .tick.late .why")?.textContent') == "\u00b7 the manager is switched off"
        # the hub's reason (manager_why, test_refusal checks the feed sends it): under the name and on the message
        why = "reply blocked by the model's safety filter 4 times within an hour (last at 14:05); restarts stopped until 15:00"
        page.js("""(() => { const f = window.fetch; window.fetch = (u, o) => f(u, o).then(r => !String(u).startsWith("/api/feed") ? r :
          r.json().then(d => new Response(JSON.stringify(Object.assign(d, { manager: "blocked", manager_why: %s })), { status: 200 }))); })()""" % json.dumps(why))
        assert page.wait(f'{chip}?.textContent === "Can\'t start" && {chip}.textContent')
        assert page.js('document.querySelector("#agents .row.mgr .subline").textContent') == why
        assert page.wait('document.querySelector("#log .tick.late .why")?.textContent === %s' % json.dumps("\u00b7 " + why))
