"""The page in a real headless Chrome: two sessions swap names between polls (as two OpenCode
sessions in one folder did) and each keeps its own chat: history, draft, unread count, the open
chat and its "Working with" chips go by the session's handle, never its name.
Run with: pytest -m slow tests/test_page_names_slow.py"""
import json
import time

import pytest

from chrome import chrome, chrome_path
from conftest import start_hub
from helpers import person
from kaptyorka.manager import ManagerStore
from kaptyorka.store import Store, session_id
from test_chat_ids import to_page
from test_page_together_slow import FAKE
from test_working_together import both

pytestmark = [pytest.mark.slow,
              pytest.mark.skipif(chrome_path() is None, reason="no Chrome or Chromium installed")]


def row(key):
    return f'document.querySelector("#agents .row[data-key=\\"{session_id(key)}\\"]")'


def log():
    return 'document.getElementById("log").textContent'


def test_two_sessions_that_swap_names_keep_their_own_chats(tmp_path):
    store = Store(tmp_path / "hub.db")
    t0 = time.time()
    store.clock = lambda: t0
    ManagerStore(store).set_enabled(False, "test")
    person(store, "alice", "admin")
    bob, btoken = person(store, "bob")
    store.project_register("proj-a", by="alice")
    store.grant_project("proj-a", bob, "write", "alice")
    store.heartbeat("box-2", [{"key": "opencode:o1", "tool": "opencode", "project": "proj-a"},
                              {"key": "opencode:o2", "tool": "opencode", "project": "proj-a"},
                              {"key": "codex:c3", "tool": "codex", "project": "proj-a", "label": "api"}],
                    principal=store.authenticate(btoken), facts={"host": "box-2", "os_user": "bob"})
    both(store, "opencode:o1", "codex:c3", ago=60)  # o1 works with api: a group
    store.send_from_web("bob", store.agent("opencode:o1").name, "first for one", bob)
    to_page(store, "opencode:o1", "bob", "reply from one")
    store.send_from_web("bob", store.agent("opencode:o2").name, "first for two", bob)
    to_page(store, "opencode:o2", "bob", "reply from two")
    one, two = store.agent("opencode:o1").name, store.agent("opencode:o2").name
    assert one != two
    stop = start_hub(store)
    try:
        with chrome() as page:
            page.call("Page.addScriptToEvaluateOnNewDocument", source=FAKE)
            page.go(f"http://127.0.0.1:{stop.port}/#t={btoken}")
            page.wait(f"!!{row('opencode:o1')}")
            page.js('window.__base = null; fetch("/api/feed", { headers: { Authorization: "Bearer " + localStorage.getItem("kaptyorka.token") } })'
                    '.then(r => r.json()).then(f => { window.__base = f; })')
            page.wait("!!window.__base")
            page.js(f"{row('opencode:o1')}.click()")
            page.wait(f'{log()}.includes("reply from one")')
            assert "reply from two" not in page.js(log())
            page.wait('!document.getElementById("chat-with").hidden')
            assert page.js('[...document.querySelectorAll("#chat-with .with")].map(c => c.textContent)') == ["api"]
            page.js('document.getElementById("text").value = "draft for one"')
            # the names swap; then o2, now named as o1 was, writes to the page
            page.js("""window.__script = n => {
              const f = JSON.parse(JSON.stringify(window.__base));
              const a = f.agents.find(a => a.id === %s), b = f.agents.find(a => a.id === %s);
              [a.name, b.name] = [b.name, a.name];
              if (n >= 2) {
                const m = f.messages.filter(m => m.sender_id === b.id).pop();
                f.messages.push(Object.assign({}, m, { id: 100000, text: "new from two", sender: %s, read: false }));
              }
              return f;
            }""" % (json.dumps(session_id("opencode:o1")), json.dumps(session_id("opencode:o2")), json.dumps(one)))
            page.wait("window.__polls >= 3", timeout=15)
            time.sleep(0.3)
            assert page.js('document.querySelector("#agents .row.on").dataset.key') == session_id("opencode:o1")
            text = page.js(log())
            assert "reply from one" in text and "first for one" in text
            assert "new from two" not in text and "reply from two" not in text
            assert page.js('document.getElementById("text").value') == "draft for one"
            assert page.js('[...document.querySelectorAll("#chat-with .with")].map(c => c.textContent)') == ["api"]
            assert page.js(f"{row('opencode:o2')}.querySelector('.count')?.textContent") == "1"
            assert page.js(f"!{row('opencode:o1')}.querySelector('.count')")
            # the other chat: its own history and an empty draft; back again, the draft returns
            page.js(f"{row('opencode:o2')}.click()")
            page.wait(f'{log()}.includes("new from two")')
            text = page.js(log())
            assert "reply from two" in text and "reply from one" not in text
            assert page.js('document.getElementById("text").value') == ""
            assert page.js('document.getElementById("chat-with").hidden')  # o2 works with no one
            page.js(f"{row('opencode:o1')}.click()")
            page.wait(f'{log()}.includes("reply from one")')
            assert page.js('document.getElementById("text").value') == "draft for one"
            assert page.js("window.__errs") == []
    finally:
        stop()
        store.close()
