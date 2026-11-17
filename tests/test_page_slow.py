"""The admin's page in a real headless Chrome: an inline confirm survives the 2 s refresh, and
adding a person shows their invite link at once. Run with: pytest -m slow tests/test_page_slow.py"""
import json
import time
from pathlib import Path

import pytest

from chrome import chrome, chrome_path
from conftest import start_hub
from helpers import person
from kaptyorka.manager import ManagerStore
from kaptyorka.rules import RulesStore
from kaptyorka.store import Store

pytestmark = [pytest.mark.slow,
              pytest.mark.skipif(chrome_path() is None, reason="no Chrome or Chromium installed")]


@pytest.fixture
def admin_page(tmp_path):
    store = Store(tmp_path / "hub.db")
    ManagerStore(store).set_enabled(False, "test")
    stop = start_hub(store)
    _, token = person(store, "alice", "admin")
    url = f"http://127.0.0.1:{stop.port}/#t={token}&view=people"
    try:
        with chrome() as page:
            yield store, page, url
    finally:
        stop()
        store.close()


@pytest.fixture
def member_page(tmp_path):
    """bob, a member with read access to proj-a; alice is the admin."""
    store = Store(tmp_path / "hub.db")
    ManagerStore(store).set_enabled(False, "test")
    stop = start_hub(store)
    person(store, "alice", "admin")
    bob, token = person(store, "bob")
    store.project_register("proj-a", by="alice")
    store.grant_project("proj-a", bob, "read", "alice")
    try:
        with chrome() as page:
            yield store, page, f"http://127.0.0.1:{stop.port}/#t={token}"
    finally:
        stop()
        store.close()


def rows(page):
    return page.js('[...document.querySelectorAll("#people-rows tr td:first-child b")].map(b => b.textContent)')


def click_in_row(page, name, label):
    page.js(f"""[...document.querySelectorAll("#people-rows tr")]
      .find(tr => tr.querySelector("b").textContent === {json.dumps(name)})
      .querySelectorAll("button").forEach(b => {{ if (b.textContent === {json.dumps(label)}) b.click(); }})""")


def test_confirm_survives_refresh(admin_page):
    store, page, url = admin_page
    person(store, "bob")
    page.go(url)
    page.wait('document.querySelectorAll("#people-rows tr").length >= 2')
    click_in_row(page, "bob", "Remove")
    text = page.wait('document.querySelector("#people .confirm")?.textContent')
    assert text.startswith("Remove bob? Their devices stop working at once; their messages and notes stay.")
    # the feed changes under the open confirm: the panel must not redraw for 2+ refreshes
    store.person_create("carol", "member", by="alice")
    time.sleep(4.5)
    assert page.js('!!document.querySelector("#people .confirm")')
    assert "carol" not in rows(page)
    page.js('document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" }))')  # Esc cancels
    page.wait('!document.querySelector("#people .confirm")')
    page.wait('[...document.querySelectorAll("#people-rows b")].some(b => b.textContent === "carol")')
    assert store.person_by_name("bob") is not None
    # and Remove in the confirm removes; bob's worker on his own machine may still run there
    bob = store.person_by_name("bob")["id"]
    with store.lock:
        dev = store.db.execute("SELECT id FROM devices WHERE person_id=?", (bob,)).fetchone()[0]
        store.db.execute(
            "INSERT INTO workers (name, project, task, boss_key, boss_person, device_id, host,"
            " tool, key, state, created) VALUES ('w4','proj-a','t','claude:b',?,?,'box-2',"
            " 'claude-code','claude:w4','up',1)", (bob, dev))
    click_in_row(page, "bob", "Remove")
    page.wait('!!document.querySelector("#people .confirm")')
    page.js('[...document.querySelectorAll("#people .confirm button")].find(b => b.textContent === "Remove").click()')
    page.wait('![...document.querySelectorAll("#people-rows b")].some(b => b.textContent === "bob")')
    assert store.person_by_name("bob") is None
    assert page.wait('!document.getElementById("people-ok").hidden && '
                     'document.getElementById("people-ok").textContent') == \
        "Removed bob. w4 may still run on box-2; close it on that machine."


def test_add_person_shows_link(admin_page):
    store, page, url = admin_page
    page.go(url)
    page.wait('document.querySelectorAll("#people-rows tr").length >= 1')
    page.js('document.getElementById("add-person-btn").click()')
    page.js('document.getElementById("new-name").value = "bob"')
    page.js('document.querySelector("#add-person button[type=submit]").click()')
    assert page.wait('!document.getElementById("invite").hidden && document.getElementById("invite-text").textContent') \
        == "Send this link to bob. It works for 7 days."
    link = page.js('document.querySelector("#invite input").value')
    assert "/i/" in link
    time.sleep(4.5)  # the panel stays through refreshes until closed
    assert page.js('!document.getElementById("invite").hidden')
    assert page.wait('document.getElementById("invites-toggle")?.textContent') == "Open invites (1)"
    page.js('document.getElementById("invites-toggle").click()')
    assert page.wait('document.querySelector("#invites-open li b")?.textContent') == "bob"
    assert page.js('document.getElementById("add-person").hidden')  # the form closed on success
    page.js('document.querySelector("#invite button[title=Close]").click()')
    assert page.js('document.getElementById("invite").hidden')


def test_member_asks_for_access(tmp_path):
    store = Store(tmp_path / "hub.db")
    ManagerStore(store).set_enabled(False, "test")
    stop = start_hub(store)
    person(store, "alice", "admin")
    _, token = person(store, "bob")
    store.project_register("proj-a", by="alice")
    try:
        with chrome() as page:
            page.go(f"http://127.0.0.1:{stop.port}/#t={token}")
            page.wait('document.getElementById("ask-link") && !document.getElementById("ask-link").hidden')
            assert page.js('document.getElementById("my-access").hidden')  # behind the link
            page.js('document.getElementById("ask-link").click()')
            page.wait('!document.getElementById("my-access").hidden')
            assert page.js('document.getElementById("my-access-rows").textContent').startswith(
                "ProjectsNone yet. Ask below.")
            # a wrong name: the hub's answer shows under the form
            page.js('document.getElementById("ask-target").value = "nope"')
            page.js('document.querySelector("#ask-access button[type=submit]").click()')
            assert page.wait('!document.getElementById("ask-err").hidden && '
                             'document.getElementById("ask-err").textContent') == "no project named nope"
            page.js('document.getElementById("ask-role").value = "write"')
            page.js('document.getElementById("ask-target").value = "proj-a"')
            page.js('document.getElementById("ask-why").value = "to review"')
            page.js('document.querySelector("#ask-access button[type=submit]").click()')
            asked = page.wait('[...document.querySelectorAll("#my-access-rows .tag")]'
                              '.map(t => t.textContent).find(t => t.includes("proj-a"))')
            assert asked.startswith("write access to project proj-a") and asked.endswith("pending")
            assert page.js('document.getElementById("ask-err").hidden')
            assert page.js('document.getElementById("ask-target").value') == ""
            assert [(r["person"], r["target"], r["role"], r["why"]) for r in store.requests()] == \
                [("bob", "proj-a", "write", "to review")]
            # the admin-only parts stay away
            assert page.js('document.getElementById("tab-admin").hidden')
    finally:
        stop()
        store.close()


def test_phone_admin_submenu(tmp_path):
    """At 390 px the Admin sub-menu is one row that scrolls sideways; the page never does."""
    store = Store(tmp_path / "hub.db")
    ManagerStore(store).set_enabled(False, "test")
    stop = start_hub(store)
    _, token = person(store, "alice", "admin")
    try:
        with chrome(390, 844) as page:
            page.go(f"http://127.0.0.1:{stop.port}/#t={token}")
            page.wait('!document.getElementById("tab-admin").hidden')
            assert page.js('document.getElementById("home").offsetWidth') <= 390
            page.js('document.getElementById("tab-admin").click()')
            page.wait('!document.getElementById("people").hidden')
            sub = page.js('(() => { const s = document.getElementById("sub"); '
                          'return [s.scrollWidth > s.clientWidth, getComputedStyle(s).flexDirection]; })()')
            assert sub == [True, "row"]
            assert page.js('[...document.querySelectorAll("#sub button")].map(b => b.textContent.replace(/\\d+/g, ""))') == \
                ["People", "Access", "Machines", "Rules", "Notes", "Log", "Settings"]
            page.js('document.querySelector(\'#sub button[data-view="settings"]\').click()')
            page.wait('!document.getElementById("settings").hidden')
            assert page.js('document.querySelector("#sub button[aria-current=page]").textContent') == "Settings"
            assert page.js("document.documentElement.scrollWidth") == 390
            page.js('document.getElementById("tab-home").click()')
            page.wait('!document.getElementById("home").hidden')
            assert page.js("document.documentElement.scrollWidth") == 390
    finally:
        stop()
        store.close()


def click(page, scope, label):
    page.js(f"""[...document.querySelectorAll({json.dumps(scope + " button")})]
      .find(b => b.textContent.trim() === {json.dumps(label)}).click()""")


def test_attach_sends_file(admin_page):
    store, page, url = admin_page
    _, bob_t = person(store, "bob")
    store.seen("b1", "box-1", "codex", "proj-a", principal=store.authenticate(bob_t))
    page.go(url.replace("&view=people", ""))
    # a click on bob's line writes to it; no select
    page.wait('[...document.querySelectorAll("#agents .row")].some(r => r.title.startsWith("box-1/codex/proj-a") '
              '|| r.querySelector(".name").textContent === "proj-a")')
    page.js('[...document.querySelectorAll("#agents .row")].find(r => r.querySelector(".name").textContent === "proj-a").click()')
    assert page.wait('document.getElementById("text").placeholder') == "Message proj-a"
    assert page.js('document.querySelector("#agents .row.on .who").textContent') == "bob"  # the admin sees whose
    page.js("""(() => { const dt = new DataTransfer(); dt.items.add(new File(["hello"], "a.txt"));
      const i = document.getElementById("file-in"); i.files = dt.files; i.dispatchEvent(new Event("change")); })()""")
    assert page.wait('!document.getElementById("file-chip").hidden && '
                     'document.getElementById("file-chip").textContent') == "a.txt· 5 B"
    page.js('document.getElementById("text").value = "see the file"')
    page.js('document.querySelector("#compose button[type=submit]").click()')
    assert page.wait('document.querySelector("#log .msg .file")?.textContent') == "a.txt5 B"
    assert page.js('document.getElementById("file-chip").hidden')
    assert [(m.text, m.file["name"]) for m in store.messages() if m.file] == [("see the file", "a.txt")]


def test_note_delete_undo(admin_page):
    store, page, url = admin_page
    for v in ("one", "two", "three"):
        store.notes_write_from_web("alice", "plan", v)
    page.go(url.replace("view=people", "view=notes"))
    page.wait('document.querySelector("#doc h2")?.textContent === "plan"')
    click(page, "#doc .tools", "Delete")
    text = page.wait('document.querySelector("#doc .confirm")?.textContent')
    assert text.startswith("Delete plan? All 3 versions go.")
    time.sleep(2.5)  # the confirm holds through a refresh
    assert page.js('!!document.querySelector("#doc .confirm")')
    click(page, "#doc .confirm", "Delete")
    assert page.wait('document.querySelector("#topics .toast")?.textContent') == "Deleted plan.Undo"
    assert all(n["topic"] != "plan" for n in store.notes_list(None))
    time.sleep(2.5)  # no "This note was deleted." for the page's own delete
    assert page.js('document.querySelector("#topics .toast")?.textContent') == "Deleted plan.Undo"
    click(page, "#topics .toast", "Undo")
    page.wait('document.querySelector("#doc h2")?.textContent === "plan"')
    assert page.wait('[...document.querySelectorAll("#doc .tools button")]'
                     '.map(b => b.textContent).includes("History 3")')
    assert page.js('!document.querySelector("#topics .toast")')
    assert len(store.note_history("plan")) == 3


def test_propose_from_page(admin_page):
    store, page, url = admin_page
    page.go(url.replace("view=people", "view=rules"))
    assert page.wait('document.querySelector("#prop-rows .empty-row")?.textContent') == \
        "No proposals"
    page.js('document.getElementById("propose-btn").click()')
    page.wait('!document.getElementById("propose").hidden')
    page.js('document.querySelector("#propose button[type=submit]").click()')  # empty: nothing sent
    assert RulesStore(store).proposals() == []
    page.js('document.getElementById("prop-text").value = "run the tests before a commit"')
    page.js('document.getElementById("prop-why").value = "it pushed a broken build twice"')
    page.js('document.querySelector("#propose button[type=submit]").click()')
    assert page.wait('document.querySelector("#prop-rows tr .state")?.textContent') == "open"
    assert page.js('document.querySelector("#prop-rows .prop b").textContent') == "run the tests before a commit"
    assert page.js('document.getElementById("propose").hidden')
    assert [(p["person"], p["evidence"]) for p in RulesStore(store).proposals()] == \
        [("alice", "it pushed a broken build twice")]


def test_member_sees_only_home(member_page):
    """A member's page: Home only (an admin view in the link falls back to it), their own
    sessions and the manager, and no admin route fetched at all."""
    store, page, url = member_page
    bob = store.authenticate(url.split("#t=")[1])
    store.seen("b1", "box-1", "codex", "proj-a", principal=bob)
    page.go(url + "&view=notes")
    page.wait('document.querySelectorAll("#agents .row").length === 2')
    assert page.js('document.getElementById("tab-admin").hidden') and page.js('document.getElementById("admin").hidden')
    assert page.js('[...document.querySelectorAll("#agents .row .name")].map(n => n.textContent)') == ["manager", "proj-a"]
    assert page.js('document.querySelector("#agents .mgr .n").textContent') == "off"
    assert page.js('document.getElementById("text").placeholder') == "Message manager"
    time.sleep(2.5)  # a poll or two
    fetched = page.js('[...new Set(performance.getEntriesByType("resource").map(e => new URL(e.name).pathname))]')
    assert set(fetched) <= {"/api/me", "/api/feed", "/fonts/archivo-latin.woff2", "/favicon.ico"}, fetched


def test_admin_restores_old_versions(admin_page):
    store, page, url = admin_page
    store.notes_write_from_web("alice", "plan", "first")
    store.notes_write_from_web("alice", "plan", "second")
    RulesStore(store).set_rules("old rule", "alice")
    RulesStore(store).set_rules("new rule", "alice")
    page.go(url.replace("view=people", "view=notes"))
    page.wait('document.querySelector("#doc h2")?.textContent === "plan"')
    click(page, "#doc .tools", "History 2")
    page.wait('document.querySelectorAll("#doc .version").length === 2')
    page.js('document.querySelectorAll("#doc .version")[1].click()')
    page.wait('!!document.querySelector("#doc .byline .old")')
    click(page, "#doc .byline", "Restore this version")
    page.wait('document.querySelectorAll("#doc .version").length === 3')
    assert page.js('document.querySelector("#doc .doc-body").textContent') == "first"
    assert page.js('!document.querySelector("#doc .byline .old")')
    assert store.note_history("plan")[0].text == "first"
    page.js('document.querySelector("#sub button[data-view=rules]").click()')
    page.wait('document.querySelector("#rules-doc .tag")?.textContent === "v2"')
    click(page, "#rules-doc .tools", "History 2")
    page.wait('document.querySelectorAll("#rules-doc .version").length === 2')
    page.js('document.querySelectorAll("#rules-doc .version")[1].click()')
    page.wait('!!document.querySelector("#rules-doc .byline .old")')
    click(page, "#rules-doc .byline", "Restore this version")
    page.wait('document.querySelector("#rules-doc .tag")?.textContent === "v3"')
    assert page.wait('document.querySelectorAll("#rules-doc .version").length === 3')
    assert page.js('document.querySelector("#rules-doc .doc-body").textContent') == "old rule"
    assert RulesStore(store).current()["text"] == "old rule"


def test_manager_asleep_on_settings_and_machines(tmp_path, monkeypatch):
    """A manager Claude retired reads Asleep on Settings and `asleep` on Machines, with no
    close button. Screenshots: settings-asleep.png and machines-asleep.png in tmp_path."""
    monkeypatch.setenv("KAPTYORKA_HOME", str(tmp_path / "kh"))
    store = Store(tmp_path / "hub.db")
    alice, token = person(store, "alice", "admin")
    ms = ManagerStore(store)
    ms.ensure_account("box-1", "alice")
    with store.lock:
        store.db.execute(
            "INSERT INTO workers (name, project, task, boss_key, boss_person, device_id, host,"
            " tool, folder, state, created, closed_at, error, manager) VALUES ('manager',"
            " 'manager', 'the team''s manager', 'web:alice', ?, 0, 'box-1', 'claude-code',"
            " '/home/u/.kaptyorka/manager', 'closed', ?, ?, 'session gone', 1)",
            (alice, time.time() - 4000, time.time() - 60))
    stop = start_hub(store)
    base = f"http://127.0.0.1:{stop.port}/#t={token}"
    try:
        with chrome() as page:
            page.go(base + "&view=settings")
            page.wait('document.getElementById("manager-state").textContent.includes("Asleep")')
            assert page.js('document.querySelector("#manager-state b").textContent') \
                == "Asleep: it wakes when someone sends it a message."
            page.screenshot(tmp_path / "settings-asleep.png")
            page.js('document.querySelector(\'#sub button[data-view="machines"]\').click()')
            page.wait('document.querySelector("#worker-rows .state") !== null')
            time.sleep(0.3)  # let the tab paint
            assert page.js('document.querySelector("#worker-rows .state").textContent') == "asleep"
            assert page.js('document.querySelector("#worker-rows .c-act button")') is None
            assert page.js('document.querySelector("#worker-rows .werr")') is None
            page.screenshot(tmp_path / "machines-asleep.png")
            # Home: the manager's line, with its state word
            page.js('document.getElementById("tab-home").click()')
            assert page.wait('document.querySelector("#agents .mgr .n")?.textContent') == "asleep"
    finally:
        stop()
        store.close()


def test_chat_reads_in_full_and_phone_goes_back(tmp_path):
    """A long message folds at 12 lines behind Show more; who it went to shows when it is not
    between this page and the chat; a phone shows the list, a tapped chat, then Back. No
    screen scrolls sideways."""
    store = Store(tmp_path / "hub.db")
    ManagerStore(store).set_enabled(False, "test")
    stop = start_hub(store)
    alice, token = person(store, "alice", "admin")
    me = store.authenticate(token)
    for i in range(9):
        store.seen(f"k{i}", "box-1", "codex", f"proj-{i}", principal=me)
    name = store.agent("k0").name
    store.send_from_web("alice", name, "first", alice)
    store.send("k0", "alice", "\n".join(f"line {i}" for i in range(30)))
    store.send("k0", store.agent("k1").name, "x" * 300)  # one long word, to another session
    wide = "document.documentElement.scrollWidth <= document.documentElement.clientWidth"
    try:
        for w, h in ((1440, 900), (390, 844)):
            with chrome(w, h) as page:
                page.go(f"http://127.0.0.1:{stop.port}/#t={token}")
                page.wait('document.querySelectorAll("#agents .row").length === 11')  # Activity, manager, 9
                assert page.js(wide)
                if w < 720:
                    assert page.js('getComputedStyle(document.getElementById("messages")).display') == "none"
                page.js('[...document.querySelectorAll("#agents .row")].find(r => r.querySelector(".name").textContent === "proj-0").click()')
                assert page.wait('document.getElementById("text").placeholder') == "Message proj-0"
                page.wait('!!document.querySelector("#log .more")')
                assert page.js('document.querySelector("#log .more").textContent') == "Show more"
                assert page.js('[...document.querySelectorAll("#log .head .to")].map(t => t.textContent)') == ["proj-1"]
                assert page.js(wide)
                page.js('document.querySelector("#log .more").click()')
                assert page.wait('document.querySelector("#log .more")?.textContent') == "Show less"
                if w < 720:
                    assert page.js('getComputedStyle(document.querySelector("#home .side")).display') == "none"
                    page.js('document.getElementById("chat-back").click()')
                    page.wait('getComputedStyle(document.querySelector("#home .side")).display !== "none"')
                    assert page.js(wide)
    finally:
        stop()
        store.close()


def test_member_writes_to_the_manager(member_page):
    """A member's page sees the manager's session masked, so the manager chat sends to its
    label "manager", as the real launcher sets it; the message reaches the manager."""
    store, page, url = member_page
    mtoken = ManagerStore(store).ensure_account("box-1", "alice")
    store.heartbeat("box-1", [{"key": "k:mgr", "tool": "claude-code", "project": "manager",
                               "label": "manager"}], principal=store.authenticate(mtoken),
                    facts={"host": "box-1", "os_user": "u"})
    page.go(url)
    page.wait('document.querySelector("#agents .row.mgr .dot.on") !== null')
    assert page.js('document.getElementById("text").placeholder') == "Message manager"
    page.js('document.getElementById("text").value = "may I have proj-b?"')
    page.js('document.querySelector("#compose button[type=submit]").click()')
    assert page.wait('[...document.querySelectorAll("#log .msg .text")].pop()?.textContent') == "may I have proj-b?"
    assert page.js('document.getElementById("send-err").hidden')
    assert [m.text for m in store.inbox("k:mgr")] == ["may I have proj-b?"]


def test_member_manager_chat_while_it_sleeps(member_page):
    """The manager asleep: its name is masked for a member and its row is gone from the
    feed, yet its messages stay in the manager chat and a new reply counts as unread."""
    store, page, url = member_page
    mtoken = ManagerStore(store).ensure_account("box-1", "alice")
    store.heartbeat("box-1", [{"key": "k:mgr", "tool": "claude-code", "project": "manager",
                               "label": "manager"}], principal=store.authenticate(mtoken),
                    facts={"host": "box-1", "os_user": "u"})
    mgr = store.agent("k:mgr").name

    def reply(text):
        with store.lock:
            store.db.execute("INSERT INTO messages (sender_key, sender_name, to_key, to_name, machine, tool,"
                             " project, text, created) VALUES ('k:mgr',?,'web:bob','bob','','','',?,?)",
                             (mgr, text, time.time()))
    reply("first answer")
    store.seen("b1", "box-1", "codex", "proj-a", principal=store.authenticate(url.split("#t=")[1]))
    with store.lock:
        store.db.execute("UPDATE agents SET last_seen=last_seen-100 WHERE key='k:mgr'")  # asleep
    page.go(url)
    page.wait('document.querySelector("#agents .row.mgr") !== null')
    assert page.js('document.querySelector("#agents .row.mgr .dot.on")') is None
    page.wait('[...document.querySelectorAll("#log .msg .text")].some(t => t.textContent === "first answer")')
    page.js('document.querySelector("#agents .row:not(.mgr)").click()')  # another chat
    reply("second answer")
    assert page.wait('document.querySelector("#agents .row.mgr .count")?.textContent') == "1"
    page.js('document.querySelector("#agents .row.mgr").click()')
    page.wait('[...document.querySelectorAll("#log .msg .text")].some(t => t.textContent === "second answer")')
    assert page.wait('document.querySelector("#agents .row.mgr .count") === null')
    assert page.js('[...document.querySelectorAll("#log .head b")].map(b => b.textContent)') == ["manager"]


def test_background_job_marked_and_empty_chat(tmp_path):
    """Background jobs fold into one Background group under the chats, closed at first; the
    viewer's open or closed is kept through a reload. Inside, a job has a clock, and the chat
    header says "background job" with the empty-chat line that says it works on its own; the
    composer stays. The manager, though it runs as a background job too, keeps its own look."""
    import os
    shots = Path(os.environ.get("KAPTYORKA_SHOTS") or tmp_path)
    store = Store(tmp_path / "hub.db")
    ManagerStore(store).set_enabled(False, "test")
    stop = start_hub(store)
    _, token = person(store, "alice", "admin")
    mtoken = ManagerStore(store).ensure_account("box-1", "alice")
    facts = {"host": "box-1", "os_user": "u"}
    store.heartbeat("box-1", [{"key": "k:mgr", "tool": "claude-code", "project": "manager",
                               "label": "manager", "background": True}],
                    principal=store.authenticate(mtoken), facts=facts)
    store.heartbeat("box-1", [
        {"key": "k:job", "tool": "claude-code", "project": "proj-a", "label": "nightly-check",
         "background": True},
        {"key": "k:term", "tool": "claude-code", "project": "proj-b", "label": "api-dev"}],
        principal=store.authenticate(token), facts=facts)
    row = '[...document.querySelectorAll("#agents .row")].find(r => r.querySelector(".name").textContent === %s)'
    job, term = row % '"nightly-check"', row % '"api-dev"'
    try:
        for w, h, tag in ((1440, 900, "desktop"), (390, 844, "phone")):
            with chrome(w, h) as page:
                page.go(f"http://127.0.0.1:{stop.port}/#t={token}")
                head = 'document.querySelector("#agents .bg-head")'
                page.wait(f'!!({term}) && !!{head}')
                assert page.js(f'{head}.textContent') == "Background1"
                assert page.js(f'{head}.getAttribute("aria-expanded")') == "false" and page.js(f'!({job})')
                assert page.js(f'!!{term}.querySelector(".dot.on") && !{term}.querySelector(".bg-lead")')
                assert page.js(f'!{term}.closest(".bg-rows")')
                mgr = 'document.querySelector("#agents .row.mgr")'
                assert page.js(f'!!{mgr}.querySelector(".dot.on") && !{mgr}.querySelector(".bg-lead")')
                assert "background" not in page.js(f'{mgr}.textContent')
                page.screenshot(shots / f"list-closed-{tag}.png")
                page.js(f'{head}.click()')
                page.wait(f'!!({job})')
                assert page.js(f'{head}.getAttribute("aria-expanded")') == "true"
                assert page.js(f'!!{job}.closest(".bg-rows")')
                assert page.js(f'!!{job}.querySelector(".bg-lead.on use[href=\\"#i-clock\\"]")')
                assert page.js(f'{job}.querySelector(".subline").textContent') == "box-1"
                time.sleep(0.3)
                page.screenshot(shots / f"list-open-{tag}.png")
                page.js("window.before = 1; location.reload()")  # this viewer's choice holds
                page.wait(f'!window.before && !!({job})')
                page.js(f'{job}.click()')
                assert page.wait('document.getElementById("text").placeholder') == "Message nightly-check"
                assert page.wait('document.querySelector("#log .blank")?.textContent') == \
                    "No messages yet. This job works on its own; write to it below."
                assert page.js('document.getElementById("chat-sub").textContent').startswith("background job")
                assert page.js('!document.getElementById("compose").hidden')
                time.sleep(0.4)  # a phone chat slides in
                page.screenshot(shots / f"bg-chat-{tag}.png")
                if w < 720:
                    page.js('document.getElementById("chat-back").click()')
                    page.wait('getComputedStyle(document.querySelector("#home .side")).display !== "none"')
                page.js(f'{term}.click()')
                assert page.wait('document.getElementById("text").placeholder') == "Message api-dev"
                assert page.wait('document.querySelector("#log .blank")?.textContent') == "No messages yet."
                assert "background" not in page.js('document.getElementById("chat-sub").textContent')
    finally:
        stop()
        store.close()


def test_member_requests_a_connection_and_admin_approves(tmp_path):
    """bob sees carol's session under Others, locked, with no content; he asks to connect
    his page, the row says Requested, the admin approves under Access > Requests, and the
    row becomes a normal chat he can write in. Desktop and phone."""
    import os
    shots = Path(os.environ.get("KAPTYORKA_SHOTS") or tmp_path)
    store = Store(tmp_path / "hub.db")
    ManagerStore(store).set_enabled(False, "test")
    stop = start_hub(store)
    _, atoken = person(store, "alice", "admin")
    bob, btoken = person(store, "bob")
    carol, ctoken = person(store, "carol")
    store.project_register("proj-a", by="alice")
    store.grant_project("proj-a", bob, "write", "alice")
    facts = {"host": "box-1", "os_user": "u"}
    store.heartbeat("box-1", [{"key": "k:b", "tool": "claude-code", "project": "proj-a", "label": "api-dev"}],
                    principal=store.authenticate(btoken), facts=facts)
    store.heartbeat("box-2", [{"key": "k:c", "tool": "codex", "project": "proj-c", "label": "billing"},
                              {"key": "k:c2", "tool": "codex", "project": "proj-d"}],
                    principal=store.authenticate(ctoken), facts={"host": "box-2", "os_user": "u"})
    store.set_status("k:c", "secret status")
    base = f"http://127.0.0.1:{stop.port}/"
    row = '[...document.querySelectorAll("#agents .row")].find(r => r.querySelector(".name").textContent === %s)'
    billing = row % '"billing"'
    wide = "document.documentElement.scrollWidth <= document.documentElement.clientWidth"
    try:
        with chrome(390, 844) as page:
            page.go(base + f"#t={btoken}")
            page.wait(f'!!({billing})')
            assert page.js('document.querySelector("#agents .others-head").textContent') == "Others"
            assert page.js(wide)
            page.screenshot(shots / "others-phone.png")
            page.js(f'{billing}.click()')
            page.wait('!document.getElementById("connect-pane").hidden')
            time.sleep(0.4)
            assert page.js(wide)
            page.screenshot(shots / "request-phone.png")
        with chrome() as page:
            page.go(base + f"#t={btoken}")
            page.wait(f'!!({billing})')
            assert page.js(f'{billing}.classList.contains("locked") && !!{billing}.querySelector("use[href=\\"#i-lock\\"]")')
            assert "secret status" not in page.js('document.body.innerText')
            assert "proj-c" not in page.js('document.body.innerText')
            page.screenshot(shots / "others-desktop.png")
            page.js(f'{billing}.click()')
            page.wait('!document.getElementById("connect-pane").hidden')
            assert page.js('document.getElementById("compose").hidden && document.getElementById("log").hidden')
            assert page.js('document.getElementById("connect-from").value') == store.agent("k:b").name  # her one session
            assert page.js('[...document.getElementById("connect-from").options].map(o => o.textContent)') == \
                ["this page", "my session api-dev (box-1)"]
            page.js('document.getElementById("connect-from").value = "page"')
            page.js('document.getElementById("connect-why").value = "a question about billing"')
            page.screenshot(shots / "request-desktop.png")
            page.js('document.querySelector("#connect-form button[type=submit]").click()')
            page.wait(f'{billing}.querySelector(".ask-state")?.textContent === "Requested"')
            page.wait('!document.getElementById("connect-state").hidden')
            assert page.js('document.getElementById("connect-form").hidden')
            page.screenshot(shots / "requested-desktop.png")
            with chrome() as admin:
                admin.go(base + f"#t={atoken}&view=access")
                admin.wait('!document.getElementById("req-panel").hidden')
                assert admin.js('document.getElementById("n-admin").textContent') == "1"
                text = admin.js('document.getElementById("req-rows").textContent')
                assert text.startswith("bob wants a connection from their page to carol's session billing on box-2")
                assert "a question about billing" in text
                admin.screenshot(shots / "admin-requests.png")
                admin.js('[...document.querySelectorAll("#req-rows button")].find(b => b.textContent === "Approve").click()')
                admin.wait('document.getElementById("req-panel").hidden')
            page.wait(f'!!({billing}) && !{billing}.classList.contains("locked")')
            assert page.js('document.querySelector("#agents .others-head + .row .name").textContent') == \
                "codex session"  # carol's other session, its project hidden
            page.wait('!document.getElementById("compose").hidden')
            assert page.js('document.getElementById("text").placeholder') == "Message billing"
            page.js('document.getElementById("text").value = "hello carol"')
            page.js('document.querySelector("#compose button[type=submit]").click()')
            page.wait('[...document.querySelectorAll("#log .msg .text")].some(t => t.textContent === "hello carol")')
            assert page.js('document.getElementById("send-err").hidden')
            assert [m.text for m in store.inbox("k:c")][-1] == "hello carol"
            store.send("k:c", "bob", "hi bob")
            page.wait('[...document.querySelectorAll("#log .msg .text")].some(t => t.textContent === "hi bob")')
            page.screenshot(shots / "approved-desktop.png")
    finally:
        stop()
        store.close()


def test_people_devices_line_up_and_add_form(tmp_path):
    """Admin > People: one primary Add person in the header opens the add form (Esc closes it);
    every device action sits in one right-aligned column; Invite then Remove at the row's top
    right; the shared token links to Access; revoking the admin's hub device says what breaks.
    Desktop and phone."""
    import hashlib
    import os
    shots = Path(os.environ.get("KAPTYORKA_SHOTS") or tmp_path)
    store = Store(tmp_path / "hub.db")
    mm = ManagerStore(store)
    mm.set_enabled(False, "test")
    stop = start_hub(store)
    alice, token = person(store, "alice", "admin")
    bob, _ = person(store, "bob")
    person(store, "carol")
    store.device_add(alice, "hub")
    store.device_add(bob, "desk-2")
    mm.ensure_account("box-1", "alice")
    now = store.clock()
    with store.lock:
        store.db.execute("INSERT INTO devices (person_id, name, token_hash, created, legacy) VALUES (?, 'legacy', ?, ?, 1)",
                         (alice, hashlib.sha256(b"old").hexdigest(), now))
        for name, host, seen in (("hub", "box-1", now), ("alice-laptop", "box-2", now - 7200),
                                 ("bob-laptop", "box-3", now - 300)):
            store.db.execute("UPDATE devices SET host=?, last_used=? WHERE name=?", (host, seen, name))
    acts = '[...document.querySelectorAll("#people-rows .dline .d-act")]'
    try:
        for w, h, tag in ((1440, 900, "desktop"), (390, 844, "phone")):
            with chrome(w, h) as page:
                page.go(f"http://127.0.0.1:{stop.port}/#t={token}&view=people")
                page.wait('document.querySelectorAll("#people-rows tr").length >= 4')
                # the form is behind the button; the button is the header's one primary action
                assert page.js('document.getElementById("add-person").hidden')
                assert page.js('document.querySelectorAll("#people .phead .btn.primary").length') == 1
                # device actions share one right edge; person actions read Invite, Remove
                rights = page.js(f'{acts}.map(a => Math.round(a.getBoundingClientRect().right))')
                assert len(rights) >= 5 and len(set(rights)) == 1, rights
                bob_acts = page.js('[...document.querySelectorAll("#people-rows tr")].find(tr => tr.querySelector("b").textContent === "bob")'
                                   '.querySelectorAll(".p-acts .btn")')
                assert page.js('[...[...document.querySelectorAll("#people-rows tr")].find(tr => tr.querySelector("b").textContent === "bob")'
                               '.querySelectorAll(".p-acts .btn")].map(b => b.textContent + ":" + b.classList.contains("quiet-danger"))') \
                    == ["Invite:false", "Remove:true"], bob_acts
                assert page.js('[...document.querySelectorAll("#people-rows .d-act .link")].map(b => b.textContent)') == ["Manage in Access"]
                assert "Its switch is in" not in page.js('document.getElementById("people-rows").textContent')
                page.screenshot(shots / f"people-{tag}.png")
                # the hub device's confirm names what breaks, on its own line
                page.js('[...document.querySelectorAll("#people-rows .dline")].find(d => d.querySelector(".d-name").textContent === "hub")'
                        '.querySelector(".d-act button").click()')
                text = page.wait('document.querySelector("#people .dline .confirm")?.textContent')
                assert text.startswith("Revoke hub? The admin link and the hub machine's sessions stop working.")
                page.screenshot(shots / f"people-revoke-{tag}.png")
                page.js('document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" }))')
                page.wait('!document.querySelector("#people .confirm")')
                # Add person: focus in the name, Esc puts the button back
                page.js('document.getElementById("add-person-btn").click()')
                assert page.js('document.activeElement.id') == "new-name"
                assert page.js('document.getElementById("add-person-btn").hidden')
                page.screenshot(shots / f"people-add-{tag}.png")
                page.js('document.getElementById("new-name").dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }))')
                assert page.js('document.getElementById("add-person").hidden && !document.getElementById("add-person-btn").hidden')
    finally:
        stop()
        store.close()
