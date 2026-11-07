"""The admin's page in a real headless Chrome: an inline confirm survives the 2 s refresh, and
adding a person shows their invite link at once. Run with: pytest -m slow tests/test_page_slow.py"""
import json
import time

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
            assert page.js('document.getElementById("team-key").hidden')
    finally:
        stop()
        store.close()


def test_phone_more_menu(tmp_path):
    """At 390 px the admin sees five named slots; More lists the rest by name and opens them."""
    store = Store(tmp_path / "hub.db")
    ManagerStore(store).set_enabled(False, "test")
    stop = start_hub(store)
    _, token = person(store, "alice", "admin")
    try:
        with chrome(390, 844) as page:
            page.go(f"http://127.0.0.1:{stop.port}/#t={token}")
            page.wait('!document.getElementById("more").hidden')
            shown = page.js('[...document.querySelectorAll("nav .tab, nav .tab-more")]'
                            '.filter(b => b.offsetWidth > 0).map(b => b.textContent.replace(/\\d+/g, ""))')
            assert shown == ["Team", "Messages", "Notes", "Machines", "More"]
            assert page.js('document.getElementById("view-title").textContent') == "Team"
            page.js('document.getElementById("more").click()')
            items = page.wait('[...document.querySelectorAll("#more-menu button")].map(b => b.textContent)')
            assert [i.rstrip("0123456789") for i in items] == ["Rules", "People", "Access", "Log", "Settings"]
            page.js('document.body.click()')  # a click outside closes it
            assert page.js('document.getElementById("more-menu").hidden')
            page.js('document.getElementById("more").click()')
            page.js('[...document.querySelectorAll("#more-menu button")].find(b => b.textContent.startsWith("People")).click()')
            page.wait('!document.getElementById("people").hidden')
            assert page.js('document.getElementById("more-menu").hidden')
            assert page.js('document.getElementById("more-label").textContent') == "People"
            assert page.js('document.getElementById("view-title").textContent') == "People"
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
    page.go(url.replace("view=people", "view=messages"))
    page.wait('[...document.querySelectorAll("#to option")].some(o => o.value === "box-1/codex/proj-a")')
    page.js("""(() => { const dt = new DataTransfer(); dt.items.add(new File(["hello"], "a.txt"));
      const i = document.getElementById("file-in"); i.files = dt.files; i.dispatchEvent(new Event("change")); })()""")
    assert page.wait('!document.getElementById("file-chip").hidden && '
                     'document.getElementById("file-chip").textContent') == "a.txt· 5 B"
    page.js('document.getElementById("to").value = "box-1/codex/proj-a"')
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


def test_propose_from_page(member_page):
    store, page, url = member_page
    page.go(url + "&view=rules")
    assert page.wait('document.querySelector("#prop-rows .empty-row")?.textContent') == \
        "No proposals yet. Propose a rule when your agent keeps getting the same correction."
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
        [("bob", "it pushed a broken build twice")]


def test_member_sees_no_restore(member_page):
    store, page, url = member_page
    store.notes_write_from_web("alice", "plan", "first", "proj-a")
    store.notes_write_from_web("alice", "plan", "second", "proj-a")
    RulesStore(store).set_rules("old rule", "alice")
    RulesStore(store).set_rules("new rule", "alice")
    page.go(url + "&view=notes")
    page.wait('[...document.querySelectorAll("#topics .topic b")].some(b => b.textContent === "plan")')
    page.js('[...document.querySelectorAll("#topics .topic")]'
            '.find(t => t.querySelector("b").textContent === "plan").click()')
    page.wait('document.querySelector("#doc h2")?.textContent === "plan"')
    click(page, "#doc .tools", "History 2")
    page.wait('document.querySelectorAll("#doc .version").length === 2')
    page.js('document.querySelectorAll("#doc .version")[1].click()')
    assert page.wait('document.querySelector("#doc .byline .old")?.textContent') == "old version v1"
    buttons = page.js('[...document.querySelectorAll("#doc button")].map(b => b.textContent)')
    assert "Restore this version" not in buttons and "Delete" not in buttons
    page.js('document.querySelector(".tab[data-view=rules]").click()')
    page.wait('document.querySelector("#rules-doc .tag")?.textContent === "v2"')
    click(page, "#rules-doc .tools", "History 2")
    page.wait('document.querySelectorAll("#rules-doc .version").length === 2')
    page.js('document.querySelectorAll("#rules-doc .version")[1].click()')
    assert page.wait('document.querySelector("#rules-doc .byline .old")?.textContent') == "old version v1"
    assert page.js('document.querySelector("#rules-doc .doc-body").textContent') == "old rule"
    buttons = page.js('[...document.querySelectorAll("#rules-doc button")].map(b => b.textContent)')
    assert "Restore this version" not in buttons and "Edit" not in buttons


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
    page.js('document.querySelector(".tab[data-view=rules]").click()')
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
