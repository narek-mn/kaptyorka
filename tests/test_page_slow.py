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
    """A background job shows a clock and "background" in the list, "background job" in the
    chat header and the empty-chat line that says it works on its own; the composer stays.
    The manager, though it runs as a background job too, keeps its own look."""
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
                page.wait(f'!!({job})')
                assert page.js(f'!!{job}.querySelector(".bg-lead.on use[href=\\"#i-clock\\"]")')
                assert page.js(f'{job}.querySelector(".subline").textContent') == "backgroundbox-1"
                assert page.js(f'!!{term}.querySelector(".dot.on") && !{term}.querySelector(".bg-lead")')
                mgr = 'document.querySelector("#agents .row.mgr")'
                assert page.js(f'!!{mgr}.querySelector(".dot.on") && !{mgr}.querySelector(".bg-lead")')
                assert "background" not in page.js(f'{mgr}.textContent')
                page.screenshot(shots / f"list-{tag}.png")
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
