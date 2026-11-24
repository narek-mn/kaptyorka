"""The page in a real headless Chrome: the admin's Team (add a person and copy the link, give a
project, answer a request, remove), and the chat. Run with: pytest -m slow tests/test_page_slow.py"""
import base64
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
    url = f"http://127.0.0.1:{stop.port}/#t={token}&view=team"
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
            assert page.js('document.querySelector("#set-nav [data-sec=\\"people\\"]").hidden')
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
    page.go(url.replace("&view=team", ""))
    # Everyone, bob's group: a click on bob's line writes to it; no select
    page.wait('!document.getElementById("h-scope").hidden')
    page.js('document.querySelector("#h-scope [data-scope=everyone]").click()')
    page.wait('!!document.querySelector("#agents .g-head[data-key=\\"g:p:bob\\"]")')
    page.js('document.querySelector("#agents .g-head[data-key=\\"g:p:bob\\"]").click()')
    page.wait('[...document.querySelectorAll("#agents .row")].some(r => r.title.startsWith("box-1/codex/proj-a") '
              '|| r.querySelector(".name").textContent === "proj-a")')
    page.js('[...document.querySelectorAll("#agents .row")].find(r => r.querySelector(".name").textContent === "proj-a").click()')
    assert page.wait('document.getElementById("text").placeholder') == "Message proj-a"
    assert page.js('document.querySelector("#agents .row.on").closest(".bg-rows").getAttribute("aria-label")') == "bob"  # the admin sees whose
    page.js("""(() => { const dt = new DataTransfer(); dt.items.add(new File(["hello"], "a.txt"));
      const i = document.getElementById("file-in"); i.files = dt.files; i.dispatchEvent(new Event("change")); })()""")
    assert page.wait('!document.getElementById("file-chip").hidden && '
                     'document.getElementById("file-chip").textContent') == "a.txt· 5 B"
    page.js('document.getElementById("text").value = "see the file"')
    page.js('document.querySelector("#compose button[type=submit]").click()')
    assert page.wait('document.querySelector("#log .msg .file")?.textContent') == "a.txt5 B"
    assert page.js('document.getElementById("file-chip").hidden')
    assert [(m.text, m.file["name"]) for m in store.messages() if m.file] == [("see the file", "a.txt")]


def test_member_sees_only_home(member_page):
    """A member's page: Home only (an admin view in the link falls back to it), their own
    sessions and the manager, and no admin route fetched at all."""
    store, page, url = member_page
    bob = store.authenticate(url.split("#t=")[1])
    store.seen("b1", "box-1", "codex", "proj-a", principal=bob)
    page.go(url + "&view=notes")
    page.wait('document.querySelectorAll("#agents .row").length === 2')
    assert page.js('document.querySelector("#set-nav [data-sec=\\"people\\"]").hidden') and page.js('document.getElementById("settings").hidden')
    assert page.js('[...document.querySelectorAll("#agents .row .name")].map(n => n.textContent)') == ["manager", "proj-a"]
    assert page.js('document.querySelector("#agents .mgr .n").textContent') == "off"
    assert page.js('document.getElementById("text").placeholder') == "Message manager"
    time.sleep(2.5)  # a poll or two
    assert page.js('document.getElementById("live").className') == "live"  # the hub answers
    fetched = page.js('[...new Set(performance.getEntriesByType("resource").map(e => new URL(e.name).pathname))]')
    # /api/warnings is the member's own scoped list (the Machines count), not an admin route
    assert set(fetched) <= {"/api/me", "/api/feed", "/api/warnings", "/fonts/archivo-latin.woff2", "/favicon.ico"}, fetched


def test_manager_between_runs_on_home(tmp_path, monkeypatch):
    """A manager whose run ended, with no admin bridge to start it, reads `blocked` on Home."""
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
    try:
        with chrome() as page:
            page.go(f"http://127.0.0.1:{stop.port}/#t={token}")
            assert page.wait('document.querySelector("#agents .mgr .n")?.textContent') == "blocked"
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
    assert page.wait('[...document.querySelectorAll("#log .msg:not(.out) .text")].pop()?.textContent') == "may I have proj-b?"
    assert page.js('document.getElementById("send-err").hidden')
    assert [m.text for m in store.inbox("k:mgr")] == ["may I have proj-b?"]


def test_member_manager_chat_between_runs(member_page):
    """The manager between runs: its name is masked for a member and its row is gone from the
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
        store.db.execute("UPDATE agents SET last_seen=last_seen-100 WHERE key='k:mgr'")  # offline
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
                assert page.js(f'{job}.querySelector(".subline").textContent') == "u@box-1"  # account@computer
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
                assert page.wait('document.querySelector("#log .blank")?.textContent') == \
                        "Messages you send here show up here. What you type to Claude stays on your computer."
                assert "background" not in page.js('document.getElementById("chat-sub").textContent')
    finally:
        stop()
        store.close()


def test_member_page_shows_no_other_person(tmp_path):
    """bob's page lists only his session and the manager: carol's sessions, their labels and
    status are nowhere; once the admin opens a door, carol's session shows as a normal chat."""
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
    store.heartbeat("box-1", [{"key": "k:b", "tool": "claude-code", "project": "proj-a", "label": "api-dev"}],
                    principal=store.authenticate(btoken), facts={"host": "box-1", "os_user": "u"})
    store.heartbeat("box-2", [{"key": "k:c", "tool": "codex", "project": "proj-c", "label": "billing"}],
                    principal=store.authenticate(ctoken), facts={"host": "box-2", "os_user": "u"})
    store.set_status("k:c", "secret status")
    base = f"http://127.0.0.1:{stop.port}/"
    names = '[...document.querySelectorAll("#agents .row .name")].map(n => n.textContent)'
    try:
        with chrome() as page:
            page.go(base + f"#t={btoken}")
            page.wait(f'{names}.includes("api-dev")')
            text = page.js('document.body.innerText')
            for secret in ("billing", "carol", "box-2", "secret status", "proj-c", "Others"):
                assert secret not in text, secret
            assert page.js('document.getElementById("connect-pane")') is None
            page.screenshot(shots / "member-alone-desktop.png")
            store.open_door_group(bob, carol, "alice")
            page.wait(f'{names}.includes("billing")')
            page.screenshot(shots / "member-door-desktop.png")
    finally:
        stop()
        store.close()


def team_row(name):
    return '[...document.querySelectorAll("#people-rows .person")].find(r => r.dataset.person === %s)' % json.dumps(name)


PERSON = 'document.getElementById("person-body")'


def open_person(page, name):
    """Click a person's row in People; wait for their page."""
    page.js(f'{team_row(name)}.querySelector(".p-main").click()')
    page.wait(f'!document.getElementById("pane-person").hidden && document.getElementById("person-name").textContent === {json.dumps(name)}'
              f' && !!{PERSON}.querySelector(".pp-acts, .pp-boxes")')


def test_team_add_person_give_project_remove(admin_page):
    """A first-time admin: Add person, type a name, Add, copy the link in place; open the
    person's page (its own address, Back returns), tick a project seen only on a session (it is
    registered on that tick, "Saved ✓" shows beside it) and untick it; Remove asks inline,
    holds through refreshes, signs them out and returns to People."""
    store, page, url = admin_page
    _, bob_t = person(store, "bob")
    store.heartbeat("box-1", [{"key": "k:b", "tool": "claude-code", "project": "proj-a"}],
                    principal=store.authenticate(bob_t), facts={"host": "box-1", "os_user": "u"})
    page.go(url)
    page.wait(f'!!{team_row("bob")}')
    assert page.js('document.getElementById("waiting").hidden')  # nothing waits: no box
    assert page.js(f'{team_row("alice")}.textContent') == "aliceAdminyou"
    assert page.js(f'{team_row("alice")}.querySelector("button")') is None  # a clean row: no buttons
    # Add person: the name field has the focus; Add shows the link with Copy in the same place
    page.js('document.getElementById("add-person-btn").click()')
    assert page.js('document.activeElement.id') == "new-name"
    page.js('document.getElementById("new-name").value = "carol"')
    page.js('document.querySelector("#add-person button[type=submit]").click()')
    page.wait('!document.getElementById("add-link").hidden')
    assert page.js('document.getElementById("add-who").textContent') == "carol's invite link"
    link = page.js('document.querySelector("#add-link input").value')
    assert "/i/" in link and store.person_by_name("carol")["role"] == "member"
    page.call("Browser.grantPermissions", permissions=["clipboardReadWrite", "clipboardSanitizedWrite"],
              origin=url.split("/#")[0])
    page.call("Page.bringToFront")  # the clipboard needs a focused page
    page.js('document.getElementById("add-copy").click()')
    assert page.wait('document.getElementById("add-copy").textContent === "Copied" && navigator.clipboard.readText()') == link
    page.wait(f'!!{team_row("carol")}')
    assert page.js(f'{team_row("carol")}.querySelector(".p-sum").textContent') == "hasn't joined yet"
    page.js('document.getElementById("add-done").click()')
    assert page.js('document.getElementById("add-box").hidden && !document.getElementById("add-person-btn").hidden')
    # carol's page: profile, no machines yet, New invite link and Make admin; Back returns
    open_person(page, "carol")
    assert page.js("location.hash") == "#settings/people/carol"
    assert page.js(f'{PERSON}.querySelector(".pp-acts").textContent') == "New invite linkMake adminRemove"
    assert "hasn't joined yet" in page.js(f'{PERSON}.querySelector(".a-profile").textContent')
    page.js('document.getElementById("person-back").click()')
    page.wait('!document.getElementById("pane-people").hidden')
    assert page.js("location.hash") == "#settings/people"
    # bob: tick proj-a (only a session had it), Saved; it is registered and he may write
    open_person(page, "bob")
    assert page.js(f'[...{PERSON}.querySelectorAll(".proj")].map(l => l.textContent)') == ["proj-a"]
    assert "Changes save automatically" in page.js(f'{PERSON}.textContent')
    page.js(f'{PERSON}.querySelector(".proj input").focus(); {PERSON}.querySelector(".proj input").click()')
    page.wait(f'{PERSON}.querySelector(".pp-cue")?.textContent === "Saved ✓"')
    assert page.js(f'document.activeElement === {PERSON}.querySelector(".proj input")')  # focus kept through the redraw
    bob = store.person_by_name("bob")["id"]
    assert store.project_role("proj-a", bob) == "write"
    page.wait(f'!{PERSON}.querySelector(".pp-cue")', timeout=5)  # about 2 seconds
    page.js(f'{PERSON}.querySelector(".proj input").click()')
    end = time.monotonic() + 10
    while store.project_role("proj-a", bob) is not None:
        assert time.monotonic() < end, "untick did not take the project away"
        time.sleep(0.1)
    page.wait(f'!{PERSON}.querySelector(".proj input").checked')
    # Remove: the question holds through refreshes; Esc cancels; Remove removes
    click(page, "#person-body .pp-acts", "Remove")
    assert page.wait(f'{PERSON}.querySelector(".confirm")?.textContent').startswith(
        "Remove bob? They are signed out everywhere.")
    time.sleep(4.5)
    assert page.js(f'!!{PERSON}.querySelector(".confirm")')
    page.js('document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" }))')
    page.wait(f'!{PERSON}.querySelector(".confirm")')
    click(page, "#person-body .pp-acts", "Remove")
    page.wait(f'!!{PERSON}.querySelector(".confirm")')
    click(page, "#person-body .confirm", "Remove")
    page.wait('!document.getElementById("pane-people").hidden')
    page.wait(f'!{team_row("bob")}')
    assert store.person_by_name("bob") is None and store.authenticate(bob_t) is None
    assert page.wait('!document.getElementById("team-ok").hidden && document.getElementById("team-ok").textContent') == "Removed bob."


def test_team_answers_yes_and_no_on_a_phone(tmp_path):
    """Waiting for you, at 390 px: a project request and a rule proposal, one line each with Yes
    and No; the tab counts them; Yes gives what was asked, No closes it; the page never
    scrolls sideways."""
    store = Store(tmp_path / "hub.db")
    ManagerStore(store).set_enabled(False, "test")
    stop = start_hub(store)
    _, token = person(store, "alice", "admin")
    bob, bob_t = person(store, "bob")
    store.project_register("proj-a", by="alice")
    store.request_access(bob, "project", "proj-a", "to fix the login", role="write")
    RulesStore(store).propose(store.authenticate(bob_t), "", "run the tests before a commit", "")
    wide = "document.documentElement.scrollWidth <= document.documentElement.clientWidth"
    try:
        with chrome(390, 844) as page:
            page.go(f"http://127.0.0.1:{stop.port}/#t={token}&view=team")
            page.wait('document.querySelectorAll("#waiting-rows .w-row").length === 2')
            assert page.js('document.getElementById("n-team").textContent') == "2"
            lines = page.js('[...document.querySelectorAll("#waiting-rows .w-text")].map(t => t.textContent)')
            assert lines == ["bob wants to work on proj-ato fix the login", "bob suggests a rulerun the tests before a commit"]
            assert page.js(wide)
            click(page, "#waiting-rows .w-row:first-child", "Yes")
            page.wait('document.querySelectorAll("#waiting-rows .w-row").length === 1')
            assert store.project_role("proj-a", bob) == "write"  # what was asked
            click(page, "#waiting-rows .w-row:first-child", "No")
            page.wait('document.getElementById("waiting").hidden')
            assert page.js('document.getElementById("n-team").hidden')
            assert [p["status"] for p in RulesStore(store).proposals()] == ["rejected"]
            assert page.js(wide)
    finally:
        stop()
        store.close()


HOLD_SEND = """(() => { const f = window.fetch; window._mode = "hold";
  window._hold = new Promise(r => { window._release = r; });
  window.fetch = (u, o) => !String(u).includes("/api/send") || window._mode === "pass" ? f(u, o)
    : window._mode === "fail" ? Promise.reject(new Error("hub unreachable")) : window._hold.then(() => f(u, o)); })()"""
LAST_TICK = '[...document.querySelectorAll("#log .tick")].pop()?.textContent'


def test_message_states_and_working_row(tmp_path):
    """Send: Sending while the hub has not answered, Sent, Delivered once the session has it;
    the working row while the session is busy, with its status text, gone on its reply. A
    failed send says Not sent with Retry. On a phone too; screenshots to KAPTYORKA_SHOTS."""
    import os
    shots = Path(os.environ.get("KAPTYORKA_SHOTS") or tmp_path)
    store = Store(tmp_path / "hub.db")
    ManagerStore(store).set_enabled(False, "test")
    stop = start_hub(store)
    _, token = person(store, "alice", "admin")
    me = store.authenticate(token)

    def beat(activity):
        store.heartbeat("box-1", [{"key": "k1", "tool": "claude-code", "project": "proj-a",
                                   "activity": activity}], tools=("claude-code",), principal=me)
    try:
        for w, h in ((1440, 900), (390, 844)):
            beat("idle")
            with chrome(w, h) as page:
                page.go(f"http://127.0.0.1:{stop.port}/#t={token}")
                page.wait('[...document.querySelectorAll("#agents .row")].some(r => r.querySelector(".name").textContent === "proj-a")')
                page.js('[...document.querySelectorAll("#agents .row")].find(r => r.querySelector(".name").textContent === "proj-a").click()')
                assert page.wait('document.getElementById("text").placeholder') == "Message proj-a"
                # Back to the list only on a phone
                assert page.js('getComputedStyle(document.getElementById("chat-back")).display !== "none"') == (w < 720)
                page.js(HOLD_SEND)
                page.js(f'document.getElementById("text").value = "please run the tests {w}"')
                page.js('document.querySelector("#compose button[type=submit]").click()')
                assert page.wait('document.querySelector("#log .msg.out.sending .tick")?.textContent') == "Sending…"
                assert page.js('document.getElementById("text").value') == ""  # free for the next one
                page.screenshot(shots / f"sending-{w}.png")
                page.js("window._release()")
                page.wait('!!document.querySelector("#log .msg:not(.out) .tick")')
                assert page.js(LAST_TICK) == "Sent"
                assert page.js('document.querySelectorAll("#log .msg.out").length') == 0
                assert [m.text for m in store.inbox("k1")] == [f"please run the tests {w}"]  # it has it
                assert page.wait(LAST_TICK + ' === "Delivered"')
                page.screenshot(shots / f"delivered-{w}.png")
                assert page.js('document.querySelector("#log .working")') is None  # idle: no row
                beat("busy")
                store.set_status("k1", "running the tests")
                row = page.wait('document.querySelector("#log .working")?.textContent')
                assert row == "proj-ais working· running the tests"
                page.screenshot(shots / f"working-{w}.png")
                store.send("k1", "alice", "all green")
                page.wait('[...document.querySelectorAll("#log .msg .text")].some(t => t.textContent === "all green")')
                assert page.wait('document.querySelector("#log .working") === null')
                assert page.js('document.querySelectorAll("#log .tick").length') == 0  # nothing after a reply
                # the hub cannot be reached: Not sent, Retry sends it
                page.js('window._mode = "fail"')
                page.js('document.getElementById("text").value = "one more"')
                page.js('document.querySelector("#compose button[type=submit]").click()')
                assert page.wait('document.querySelector("#log .msg.out.failed .tick")?.textContent') == \
                    "Not sent · Retry · Edit"
                assert page.js('document.getElementById("send-err").textContent') == "hub unreachable"
                page.js('window._mode = "pass"')
                page.js('[...document.querySelectorAll("#log .tick button")].find(b => b.textContent === "Retry").click()')
                page.wait('[...document.querySelectorAll("#log .msg:not(.out) .text")].some(t => t.textContent === "one more")')
                assert page.js('document.getElementById("send-err").hidden')
                assert page.js("document.documentElement.scrollWidth <= document.documentElement.clientWidth")
                store.inbox("k1")
                store.send("k1", "alice", "ok")
    finally:
        stop()
        store.close()


@pytest.mark.parametrize("w,h", [(1440, 900), (390, 844)])
def test_manager_waking_row(tmp_path, monkeypatch, w, h):
    """A message to the manager while it starts: "waking the manager…" under it."""
    import os
    shots = Path(os.environ.get("KAPTYORKA_SHOTS") or tmp_path)
    monkeypatch.setenv("KAPTYORKA_HOME", str(tmp_path / "kh"))
    store = Store(tmp_path / "hub.db")
    alice, token = person(store, "alice", "admin")
    mtoken = ManagerStore(store).ensure_account("box-1", "alice")
    store.heartbeat("box-1", [{"key": "k:mgr", "tool": "claude-code", "project": "manager",
                               "label": "manager"}], principal=store.authenticate(mtoken),
                    facts={"host": "box-1", "os_user": "u"})
    with store.lock:  # its run ended; the hub starts it again
        store.db.execute("UPDATE agents SET last_seen=last_seen-100 WHERE key='k:mgr'")
        store.db.execute(
            "INSERT INTO workers (name, project, task, boss_key, boss_person, device_id, host,"
            " tool, folder, state, created, closed_at, error, manager) VALUES ('manager',"
            " 'manager', 'the team''s manager', 'web:alice', ?, 0, 'box-1', 'claude-code',"
            " '/home/u/.kaptyorka/manager', 'closed', ?, ?, 'session gone', 1)",
            (alice, time.time() - 4000, time.time() - 60))
    stop = start_hub(store)
    host = ManagerStore(store)._host_of_account()  # the hub machine, as the hub set it up
    with store.lock:  # the admin's bridge there is up, so a message may wake the manager
        store.db.execute("INSERT INTO devices (person_id, name, token_hash, secret_hash, created,"
                         " host, last_used) VALUES (?, 'hub-box', 'h', 's', ?, ?, ?)",
                         (alice, time.time(), host, time.time()))
    try:
        with chrome(w, h) as page:
            page.go(f"http://127.0.0.1:{stop.port}/#t={token}")
            assert page.wait('document.querySelector("#agents .mgr .n")?.textContent') == "starting"
            if w < 720:
                page.js('document.querySelector("#agents .row.mgr").click()')
            page.js('document.getElementById("text").value = "are you there?"')
            page.js('document.querySelector("#compose button[type=submit]").click()')
            page.wait('!!document.querySelector("#log .msg:not(.out) .tick")')
            assert page.js(LAST_TICK) == "Sent"  # the manager has not read it yet
            assert page.wait('document.querySelector("#log .working")?.textContent') == "waking the manager\u2026"
            time.sleep(2.5)  # a poll later it still says so
            assert page.js('document.querySelector("#log .working")?.textContent') == "waking the manager\u2026"
            page.screenshot(shots / f"waking-{w}.png")
    finally:
        stop()
        store.close()


@pytest.mark.parametrize("w,h", [(1440, 900), (390, 844)])
def test_team_projects_from_gitlab(tmp_path, w, h):
    """Team > Projects, for a first-time admin: every GitLab project and every project on the hub,
    with who has access. Add to hub registers a GitLab project with its description and repo;
    Edit changes the description in place; Remove asks first and says what goes; Add project
    takes a name for a project that is not in GitLab. A member sees none of it. Screenshots to
    KAPTYORKA_SHOTS."""
    import os
    from helpers import FakeGitLab, gitlab_project
    from kaptyorka import paths
    from kaptyorka.gitlab import GitLabSource

    shots = Path(os.environ.get("KAPTYORKA_SHOTS") or tmp_path)
    shots.mkdir(parents=True, exist_ok=True)
    tag = "phone" if w < 720 else "desktop"
    lab = FakeGitLab([gitlab_project("proj-a", "the web shop"),
                      gitlab_project("proj-b", "billing service that sends the monthly invoices"),
                      gitlab_project("proj-c")], "glpat-test-token")
    tok = paths.home() / "gitlab-token"
    tok.write_text("glpat-test-token")
    os.chmod(tok, 0o600)
    store = Store(tmp_path / "hub.db")
    ManagerStore(store).set_enabled(False, "test")
    GitLabSource(store).set(lab.url, "team", "alice")
    stop = start_hub(store)
    _, token = person(store, "alice", "admin")
    bob, bob_t = person(store, "bob")
    carol, _ = person(store, "carol")
    store.project_register("proj-a", "the web shop", by="alice")
    store.project_register("proj-x", "notes only", by="alice")
    store.grant_project("proj-a", bob, "write", "alice")
    store.grant_project("proj-a", carol, "read", "alice")
    row = 'document.querySelector("#project-rows [data-project=\\"{}\\"]")'.format
    wide = "document.documentElement.scrollWidth <= document.documentElement.clientWidth"
    try:
        with chrome(w, h) as page:
            page.go(f"http://127.0.0.1:{stop.port}/#t={token}&view=projects")
            page.wait(f'!!{row("proj-c")}')
            names = page.js('[...document.querySelectorAll("#project-rows .pj-name")].map(n => n.textContent)')
            assert names == ["proj-a", "proj-x", "proj-b", "proj-c"]  # on the hub first
            assert page.js(f'{row("proj-a")}.querySelector(".pj-who").textContent') == "Access: bob, carol (read)"
            assert page.js(f'{row("proj-x")}.querySelector(".pj-who").textContent') == "Nobody has access yet"
            assert page.js(f'{row("proj-b")}.querySelector(".pj-tag").textContent') == "Not on hub"
            assert page.js(f'{row("proj-b")}.querySelector(".pj-acts").textContent') == "Add to hub"
            assert page.js(f'{row("proj-a")}.querySelector(".pj-acts").textContent') == "EditRemove"
            assert page.js('document.getElementById("pj-note").hidden')
            assert page.js(wide)
            page.screenshot(shots / f"projects-{tag}.png")
            # Add to hub: registered with GitLab's description and repo, nobody has access yet
            click(page, '#project-rows [data-project="proj-b"]', "Add to hub")
            page.wait(f'{row("proj-b")}.querySelector(".pj-tag").textContent === "On hub"')
            p = next(x for x in store.projects_for(store.authenticate(token)) if x["name"] == "proj-b")
            assert (p["description"], p["repo"]) == ("billing service that sends the monthly invoices",
                                                     "ssh://git@gitlab.example:2222/team/proj-b.git")
            # the person ticks list it now
            page.js('location.hash = "#settings/people/bob"')
            page.wait('[...document.querySelectorAll("#person-body .proj")].map(l => l.textContent).join() === "proj-a,proj-b,proj-x"')
            page.js('history.back()')
            page.wait('!document.getElementById("pane-projects").hidden')
            # Edit: the description in place
            click(page, '#project-rows [data-project="proj-a"]', "Edit")
            page.wait('!!document.querySelector("#project-rows .confirm input")')
            assert page.js('document.querySelector("#project-rows .confirm input").value') == "the web shop"
            assert page.js('document.activeElement === document.querySelector("#project-rows .confirm input")')
            page.js('document.querySelector("#project-rows .confirm input").value = "the web shop, v2"')
            page.screenshot(shots / f"projects-edit-{tag}.png")
            click(page, "#project-rows .confirm", "Save")
            page.wait(f'{row("proj-a")}.querySelector(".pj-desc").textContent === "the web shop, v2"')
            # Remove asks first, plainly, and holds through refreshes
            click(page, '#project-rows [data-project="proj-x"]', "Remove")
            assert page.wait('document.querySelector("#project-rows .confirm")?.textContent') == (
                "Remove proj-x? Its sessions leave the hub and their chats are deleted. Its notes are kept.RemoveCancel")
            assert page.js(wide)
            page.screenshot(shots / f"projects-remove-{tag}.png")
            time.sleep(2.5)
            assert page.js('!!document.querySelector("#project-rows .confirm")')
            click(page, "#project-rows .confirm", "Remove")
            page.wait(f'!{row("proj-x")}')
            assert "proj-x" not in store.team_projects()
            # Add project: a name and a description, for one not in GitLab
            page.js('document.getElementById("add-project-btn").click()')
            assert page.js('document.activeElement.id') == "new-project"
            page.js('document.getElementById("new-project").value = "proj-d"')
            page.js('document.getElementById("new-project-desc").value = "a side tool"')
            page.screenshot(shots / f"projects-add-{tag}.png")
            page.js('document.querySelector("#add-project button[type=submit]").click()')
            page.wait(f'!!{row("proj-d")} && document.getElementById("add-project-box").hidden')
            assert "proj-d" in store.team_projects()
            # a member: no Team tab and no project list
            page.go("about:blank")
            page.wait('location.href === "about:blank"')
            page.go(f"http://127.0.0.1:{stop.port}/#t={bob_t}&view=team")
            page.wait('document.getElementById("viewer-name").textContent === "bob"')
            time.sleep(1)
            assert page.js('document.querySelector("#set-nav [data-sec=\\"people\\"]").hidden && !document.querySelector("#project-rows .pj-row")')
    finally:
        stop()
        store.close()
        lab.close()


def test_team_projects_gitlab_refuses(tmp_path, monkeypatch):
    """GitLab refuses the token: one plain line over the list, the hub's projects still there."""
    import os
    from helpers import FakeGitLab
    from kaptyorka import paths
    from kaptyorka.gitlab import GitLabSource

    shots = Path(os.environ.get("KAPTYORKA_SHOTS") or tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "h"))
    monkeypatch.setenv("KAPTYORKA_HOME", str(tmp_path / "h" / ".kaptyorka"))
    lab = FakeGitLab([], "glpat-right")
    tok = paths.home() / "gitlab-token"
    tok.write_text("glpat-wrong")
    os.chmod(tok, 0o600)
    store = Store(tmp_path / "hub.db")
    ManagerStore(store).set_enabled(False, "test")
    GitLabSource(store).set(lab.url, "team", "alice")
    stop = start_hub(store)
    _, token = person(store, "alice", "admin")
    store.project_register("proj-a", "the web shop", by="alice")
    try:
        with chrome(390, 844) as page:
            page.go(f"http://127.0.0.1:{stop.port}/#t={token}&view=team")
            note = page.wait('!document.getElementById("pj-note").hidden && document.getElementById("pj-note").textContent')
            assert note == ("GitLab refused the token (401): it is wrong or expired;"
                            " put a new one in ~/.kaptyorka/gitlab-token")
            assert page.js('[...document.querySelectorAll("#project-rows .pj-name")].map(n => n.textContent)') == ["proj-a"]
            page.screenshot(shots / "projects-error-phone.png")
    finally:
        stop()
        store.close()
        lab.close()


@pytest.mark.parametrize("w,h", [(1440, 900), (390, 844)])
def test_sign_in_with_gitlab(tmp_path, w, h):
    """Sign in with GitLab, as a first-time person: the lock screen has one button; GitLab sends
    the browser back signed in. The admin sets a GitLab username on a person's row; an unknown GitLab user is told to ask the admin; Sign out shows the button again; the
    admin's private link still works. Screenshots to KAPTYORKA_SHOTS."""
    import os
    from helpers import FakeGitLabOAuth
    from kaptyorka import paths, signin
    from kaptyorka.gitlab import GitLabSource

    shots = Path(os.environ.get("KAPTYORKA_SHOTS") or tmp_path)
    shots.mkdir(parents=True, exist_ok=True)
    tag = "phone" if w < 720 else "desktop"
    lab = FakeGitLabOAuth(users={"alice-gl": {"id": 11, "state": "active"},
                                 "dave-gl": {"id": 44, "state": "active"}})
    app_file = paths.home() / "gitlab-oauth"
    app_file.write_text("app-id\napp-secret-value\n")
    os.chmod(app_file, 0o600)
    store = Store(tmp_path / "hub.db")
    ManagerStore(store).set_enabled(False, "test")
    GitLabSource(store).set(lab.url, "team", "alice")
    stop = start_hub(store)
    _, admin_t = person(store, "alice", "admin")
    _, bob_t = person(store, "bob")
    carol = store.person_create("carol", by="alice")  # invited; signed in on the page before any join
    lab.users["carol-gl"] = {"id": 55, "state": "active"}
    signin.set_user(store, None, "alice", "alice-gl", store.authenticate(admin_t))
    signin.set_user(store, None, "carol", "carol-gl", store.authenticate(admin_t))
    lab.who = "carol-gl"
    with chrome(w, h) as page:
        page.go(f"http://127.0.0.1:{stop.port}/")
        page.wait('document.getElementById("lock").dataset.mode === "gitlab"')
        page.js('document.getElementById("gitlab-signin").click()')
        page.wait('document.getElementById("viewer-name")?.textContent === "carol"')
    lab.who = "alice-gl"
    base = f"http://127.0.0.1:{stop.port}"
    wide = "document.documentElement.scrollWidth <= document.documentElement.clientWidth"
    gl = 'document.querySelector("#person-body .p-gl")'
    try:
        with chrome(w, h) as page:
            page.go(base + "/")
            page.wait('document.getElementById("lock").dataset.mode === "gitlab"')
            assert page.js('getComputedStyle(document.getElementById("unlock")).display') == "none"
            assert page.js('document.getElementById("gitlab-signin").textContent') == "Sign in with GitLab"
            assert page.js('document.querySelector("#lock .lock-name").textContent') == "kaptyorka"
            assert page.js(wide)
            page.screenshot(shots / f"signin-{tag}.png")
            page.js('document.getElementById("gitlab-signin").click()')
            page.wait('document.getElementById("viewer-name")?.textContent === "alice"')
            assert "t=" not in page.js("location.href")
            assert page.js('localStorage.getItem("kaptyorka.token")')
            # People, then each person's page: the GitLab username, or a quiet way to add one
            page.js('document.querySelector("#set-nav [data-sec=\\"people\\"]").click()')
            carol_row = 'document.querySelector("#people-rows [data-person=carol]")'
            page.wait(f'!!{carol_row}')
            assert page.js(f'{carol_row}.querySelector(".p-sum").textContent') == "hasn't joined yet"
            assert carol
            open_person(page, "alice")
            page.wait(f'!!{gl}')
            assert page.js(f'{gl}.textContent') == "@alice-gl"
            page.js('document.getElementById("person-back").click()')
            page.wait('!document.getElementById("pane-people").hidden')
            open_person(page, "bob")
            page.wait(f'!!{gl}')
            assert page.js(f'{gl}.textContent') == "Add GitLab name"
            assert page.js(wide)
            page.screenshot(shots / f"team-gitlab-{tag}.png")
            page.js(f'{gl}.click()')
            page.wait('!!document.querySelector("#person-body .confirm input")')
            assert page.js('document.activeElement.placeholder') == "GitLab username"
            page.js('document.querySelector("#person-body .confirm input").value = "bob-gl"')
            page.screenshot(shots / f"team-gitlab-edit-{tag}.png")
            click(page, "#person-body .confirm", "Save")
            page.wait(f'{gl} && {gl}.textContent === "@bob-gl"')
            page.js('document.getElementById("person-back").click()')
            page.wait('!document.getElementById("pane-people").hidden')
            assert store.person_by_name("bob") and [p["gitlab_user"] for p in store.people()
                                                    if p["name"] == "bob"] == ["bob-gl"]
            # Add person is a name only: people link their own GitLab from the invite
            page.js('document.getElementById("add-person-btn").click()')
            assert page.js('document.getElementById("new-gitlab")') is None
            page.js('document.getElementById("new-name").value = "dave"')
            page.js('document.querySelector("#add-person button[type=submit]").click()')
            page.wait('!document.getElementById("add-link").hidden')
            assert [p["gitlab_user"] for p in store.people() if p["name"] == "dave"] == [None]
            # Sign out ends this sign-in; the button is back
            page.js('document.getElementById("signout").click()')
            page.wait('!document.getElementById("lock").hidden && document.getElementById("lock").dataset.mode === "gitlab"')
            assert not [d for d in store.devices(1) if d["name"] == "gitlab-web" and d["revoked_at"] is None]
            # an unknown GitLab user is told plainly
            lab.who = "erin-gl"
            lab.users["erin-gl"] = {"id": 66, "state": "active"}
            page.js('document.getElementById("gitlab-signin").click()')
            text = page.wait('document.querySelector("h1") && document.querySelector("h1").textContent === "Sign in"'
                             ' && document.body.innerText')
            assert "ask the admin to add you" in text
            page.screenshot(shots / f"signin-unknown-{tag}.png")
            # a member's device link no longer opens the page: it says to sign in with GitLab
            page.go(f"{base}/#t={bob_t}")
            page.wait('!document.getElementById("lock-note").hidden && document.getElementById("lock").dataset.mode === "gitlab"')
            assert page.js('document.getElementById("app").hidden')
            assert page.js('localStorage.getItem("kaptyorka.token")') is None
            assert page.js(wide)
            page.screenshot(shots / f"device-link-{tag}.png")
            # the admin token (kaptyorka page on the hub machine) still works, with GitLab down
            (paths.home() / "admin-token").write_text(admin_t + "\n")
            lab.down = True
            page.go("about:blank")  # a new hash alone would not load the page again
            page.go(f"{base}/#t={admin_t}")
            page.wait('document.getElementById("viewer-name")?.textContent === "alice"')
    finally:
        stop()
        store.close()
        lab.close()


@pytest.mark.parametrize("w,h", [(1440, 900), (390, 844)])
def test_people_link_their_own_gitlab(tmp_path, w, h):
    """dave opens his invite link, clicks Sign in with GitLab and lands on his page, linked; the
    invite still joins a machine. erin, who joined before, opens her device link: it says to
    sign in with GitLab, and that sign-in links her. Screenshots to KAPTYORKA_SHOTS."""
    import os
    from helpers import FakeGitLabOAuth
    from kaptyorka import paths, signin
    from kaptyorka.gitlab import GitLabSource

    shots = Path(os.environ.get("KAPTYORKA_SHOTS") or tmp_path)
    shots.mkdir(parents=True, exist_ok=True)
    tag = "phone" if w < 720 else "desktop"
    lab = FakeGitLabOAuth(users={"alice-gl": {"id": 11, "state": "active"},
                                 "dave-gl": {"id": 44, "state": "active"},
                                 "erin-gl": {"id": 55, "state": "active"}})
    app_file = paths.home() / "gitlab-oauth"
    app_file.write_text("app-id\napp-secret-value\n")
    os.chmod(app_file, 0o600)
    store = Store(tmp_path / "hub.db")
    ManagerStore(store).set_enabled(False, "test")
    GitLabSource(store).set(lab.url, "team", "alice")
    stop = start_hub(store)
    _, admin_t = person(store, "alice", "admin")
    signin.set_user(store, None, "alice", "alice-gl", store.authenticate(admin_t))
    dave = store.person_create("dave", by="alice")
    code = store.invite_create(dave, by="alice")
    _, erin_t = person(store, "erin")
    base = f"http://127.0.0.1:{stop.port}"
    wide = "document.documentElement.scrollWidth <= document.documentElement.clientWidth"
    try:
        with chrome(w, h) as page:
            page.go(f"{base}/i/{code}")
            page.wait('!!document.querySelector("form button")')
            assert page.js('document.querySelector("form button").textContent') == "Sign in with GitLab"
            assert "This invite is for dave." in page.js("document.body.innerText")
            assert f"kaptyorka join {base}/i/{code}" in page.js("document.body.innerText")
            assert page.js(wide)
            page.screenshot(shots / f"invite-gitlab-{tag}.png")
            lab.who = "dave-gl"
            page.js('document.querySelector("form button").click()')
            page.wait('document.getElementById("viewer-name")?.textContent === "dave"')
            assert [p["gitlab_user"] for p in store.people() if p["name"] == "dave"] == ["dave-gl"]
            assert store.invite_valid(code)  # join still works on his machine
            page.js('document.getElementById("viewer-name").click()')
            page.wait('document.getElementById("a-profile").textContent.includes("@dave-gl")')
            assert "Connect GitLab" not in page.js('document.getElementById("settings").innerText')
        with chrome(w, h) as page:
            page.go(f"{base}/#t={erin_t}")
            page.wait('!document.getElementById("lock-note").hidden')
            assert page.js('document.getElementById("gitlab-signin").textContent') == "Sign in with GitLab"
            assert page.js(wide)
            page.screenshot(shots / f"connect-gitlab-{tag}.png")
            lab.who = "erin-gl"
            page.js('document.getElementById("gitlab-signin").click()')
            page.wait('document.getElementById("viewer-name")?.textContent === "erin"'
                      ' && localStorage.getItem("kaptyorka.token") !== null')
            assert [p["gitlab_user"] for p in store.people() if p["name"] == "erin"] == ["erin-gl"]
            page.js('document.getElementById("viewer-name").click()')
            page.wait('document.getElementById("a-profile").textContent.includes("@erin-gl")')
            assert "Connect GitLab" not in page.js('document.getElementById("settings").innerText')
            page.screenshot(shots / f"connected-{tag}.png")
    finally:
        stop()
        store.close()
        lab.close()


@pytest.mark.parametrize("w,h", [(1440, 900), (390, 780)])
def test_image_opens_large_and_video_plays(tmp_path, w, h):
    import os
    shots = os.environ.get("KAPTYORKA_SHOTS")
    store = Store(tmp_path / "hub.db")
    ManagerStore(store).set_enabled(False, "test")
    stop = start_hub(store)
    alice, token = person(store, "alice", "admin")
    # a 1x1 png is enough to open; a real picture for the screenshot
    import base64
    import struct
    import zlib
    W, H = 640, 400
    rows = b"".join(b"\x00" + bytes(v for x in range(W) for v in (40 + x * 200 // W, 90 + y * 120 // H, 160)) for y in range(H))
    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d))
    png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", W, H, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b"")
    pic = store.file_add("photo.png", png, alice)
    vid = store.file_add("clip.mp4", b"not really a video", alice)
    store.seen("b1", "box-1", "codex", "proj-a", principal=store.authenticate(token))
    store.send_from_web("alice", "box-1/codex/proj-a", "a picture", person_id=alice, file_id=pic)
    store.send_from_web("alice", "box-1/codex/proj-a", "a video", person_id=alice, file_id=vid)
    try:
        with chrome(w, h) as page:
            page.go(f"http://127.0.0.1:{stop.port}/#t={token}")
            page.wait('[...document.querySelectorAll("#agents .row")].some(r => r.querySelector(".name")?.textContent === "proj-a")')
            page.js('[...document.querySelectorAll("#agents .row")].find(r => r.querySelector(".name").textContent === "proj-a").click()')
            page.wait('document.querySelector("#log img.att.zoom")?.complete && document.querySelector("#log img.att.zoom").naturalWidth')
            assert page.js('document.querySelector("#log video.att").controls')
            assert page.js('document.querySelector("#log video.att").preload') == "metadata"
            assert page.js('document.querySelector("#log .vid .file")') is not None
            page.js('document.querySelector("#log img.att.zoom").click()')
            page.wait('document.querySelector(".lightbox img")?.naturalWidth')
            if shots:
                time.sleep(0.4)
                page.screenshot(Path(shots) / f"viewer-{w}.png")
            page.js('document.dispatchEvent(new KeyboardEvent("keydown", {key: "Escape", bubbles: true}))')
            assert page.js('!document.querySelector(".lightbox")')
            page.js('document.querySelector("#log img.att.zoom").dispatchEvent(new KeyboardEvent("keydown", {key: "Enter", bubbles: true}))')
            assert page.js('!!document.querySelector(".lightbox")')
            page.js('document.querySelector(".lightbox").click()')  # the backdrop
            assert page.js('!document.querySelector(".lightbox")')
    finally:
        stop()
        store.close()


TINY_WEBM = base64.b64decode(
    "GkXfo59ChoEBQveBAULygQRC84EIQoKEd2VibUKHgQJChYECGFOAZwEAAAAAAAIwEU2bdLpNu4tTq4QVSalmU6yBoU27i1OrhBZUrmtTrIHYTbuMU6uEElTDZ1OsggEeTbuMU6uEHFO7a1OsggIa7AEAAAAAAABZAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAVSalmsirXsYMPQkBNgI1MYXZmNjAuMTYuMTAwV0GNTGF2ZjYwLjE2LjEwMESJiEBywAAAAAAAFlSua8GuAQAAAAAAADjXgQFzxYjmh3qhIsItHZyBACK1nIN1bmSIgQCGhVZfVlA4g4EBI+ODhAX14QDgibCBQLqBMJqBAhJUw2f8c3OgY8CAZ8iaRaOHRU5DT0RFUkSHjUxhdmY2MC4xNi4xMDBzc9ZjwItjxYjmh3qhIsItHWfIoUWjh0VOQ09ERVJEh5RMYXZjNjAuMzEuMTAyIGxpYnZweGfIoUWjiERVUkFUSU9ORIeTMDA6MDA6MDAuMzAwMDAwMDAwAB9DtnX254EAo8GBAACAUAMAnQEqQAAwAABHCIWFiIWEiAICAnWqA/gCBwjVZ5mPN9IA/v9NEv/8WFfxYV/FhX/xYV/8/M7txfzmAKOWgQBkANEBAAEQEAAYABhYL/QACIwAAKOWgQDIANEBAAEQEAAYABhYL/QACIwAABxTu2uRu4+zgQC3iveBAfGCAZ/wgQM=")


def _open_chat(page, stop, token):
    page.go(f"http://127.0.0.1:{stop.port}/#t={token}")
    page.wait('[...document.querySelectorAll("#agents .row")].some(r => r.querySelector(".name")?.textContent === "proj-a")')
    page.js('[...document.querySelectorAll("#agents .row")].find(r => r.querySelector(".name").textContent === "proj-a").click()')


def test_video_loads_under_the_page_policy(tmp_path):
    store = Store(tmp_path / "hub.db")
    ManagerStore(store).set_enabled(False, "test")
    stop = start_hub(store)
    alice, token = person(store, "alice", "admin")
    vid = store.file_add("clip.webm", TINY_WEBM, alice)
    store.seen("b1", "box-1", "codex", "proj-a", principal=store.authenticate(token))
    store.send_from_web("alice", "box-1/codex/proj-a", "a video", person_id=alice, file_id=vid)
    try:
        with chrome() as page:
            page.go(f"http://127.0.0.1:{stop.port}/#t={token}")
            page.js("window.__csp = []; document.addEventListener('securitypolicyviolation', e => window.__csp.push(e.violatedDirective))")
            _open_chat(page, stop, token)
            page.wait('document.querySelector("#log video.att")?.readyState >= 1')
            assert page.js("window.__csp") == []
            assert page.js('document.querySelector("#log video.att").duration') > 0
    finally:
        stop()
        store.close()


def test_viewer_keeps_focus_on_its_two_buttons(tmp_path):
    store = Store(tmp_path / "hub.db")
    ManagerStore(store).set_enabled(False, "test")
    stop = start_hub(store)
    alice, token = person(store, "alice", "admin")
    pic = store.file_add("p.png", b"\x89PNG", alice)
    store.seen("b1", "box-1", "codex", "proj-a", principal=store.authenticate(token))
    store.send_from_web("alice", "box-1/codex/proj-a", "pic", person_id=alice, file_id=pic)
    try:
        with chrome() as page:
            _open_chat(page, stop, token)
            page.wait('!!document.querySelector("#log img.att.zoom")')
            page.js('document.querySelector("#log img.att.zoom").click()')
            tab = ('document.dispatchEvent(new KeyboardEvent("keydown", {key: "Tab", shiftKey: @@S@@, bubbles: true, cancelable: true}));'
                   'document.activeElement.className + "|" + document.activeElement.textContent')
            assert page.js("document.activeElement.className") == "lb-close"
            assert page.js(tab.replace("@@S@@", "false")) == "btn|Download"  # Close is last: wraps to Download
            assert page.js(tab.replace("@@S@@", "true")) == "lb-close|"      # Download is first: wraps back to Close
    finally:
        stop()
        store.close()


@pytest.mark.parametrize("w,h", [(1440, 900), (390, 844)])
def test_account_page_add_machine_and_new_invite_link(tmp_path, w, h):
    """bob signs in with GitLab and opens his name: the Account page (its own address; Back and
    reload work) with his profile, machines (rename, remove, online or when last seen, the OS
    user), this browser, his projects, Add machine with the join command and Copy, Sign out. The admin's
    person page has New invite link, which copies a fresh link and kills the old one. Screenshots to
    KAPTYORKA_SHOTS."""
    import os
    from helpers import FakeGitLabOAuth
    from kaptyorka import paths, signin
    from kaptyorka.gitlab import GitLabSource

    shots = Path(os.environ.get("KAPTYORKA_SHOTS") or tmp_path)
    shots.mkdir(parents=True, exist_ok=True)
    tag = "phone" if w < 720 else "desktop"
    lab = FakeGitLabOAuth(users={"alice-gl": {"id": 11, "state": "active"},
                                 "bob-gl": {"id": 22, "state": "active"}})
    app_file = paths.home() / "gitlab-oauth"
    app_file.write_text("app-id\napp-secret-value\n")
    os.chmod(app_file, 0o600)
    store = Store(tmp_path / "hub.db")
    ManagerStore(store).set_enabled(False, "test")
    GitLabSource(store).set(lab.url, "team", "alice")
    stop = start_hub(store)
    _, admin_t = person(store, "alice", "admin")
    bob, _ = person(store, "bob")
    by = store.authenticate(admin_t)
    signin.set_user(store, None, "alice", "alice-gl", by)
    signin.set_user(store, None, "bob", "bob-gl", by)
    for name in ("proj-a", "proj-b"):
        store.project_register(name, by="alice")
    store.grant_project("proj-a", bob, "write", "alice")
    store.grant_project("proj-b", bob, "read", "alice")
    store.device_add(bob, "bob@box-2")
    with store.lock:
        store.db.execute("UPDATE devices SET name='bob@box-1', os_user='bob', last_used=? WHERE name='bob-laptop'",
                         (store.clock(),))
        store.db.execute("UPDATE devices SET os_user='bob', last_used=? WHERE name='bob@box-2'",
                         (store.clock() - 3 * 3600,))
    old = store.invite_create(bob, by="alice")
    base = f"http://127.0.0.1:{stop.port}"
    wide = "document.documentElement.scrollWidth <= document.documentElement.clientWidth"
    acct = 'document.getElementById("settings")'
    try:
        with chrome(w, h) as page:
            lab.who = "bob-gl"
            page.go(base + "/")
            page.wait('document.getElementById("lock").dataset.mode === "gitlab"')
            page.js('document.getElementById("gitlab-signin").click()')
            page.wait('document.getElementById("viewer-name")?.textContent === "bob"')
            # the name goes straight to Settings > Profile, with its own address; no menu
            assert page.js('document.getElementById("viewer-name").getAttribute("aria-label")') == "Settings"
            assert page.js('document.querySelector("#set-nav [data-sec=\\"people\\"]").hidden')  # a member
            page.js('document.getElementById("viewer-name").click()')
            page.wait('!document.getElementById("pane-profile").hidden && document.querySelectorAll("#a-projects .a-row").length === 2')
            assert page.js("location.hash") == "#settings/profile"
            assert page.js('document.getElementById("home").hidden && !document.getElementById("acct-menu")')
            text = page.js(f"{acct}.innerText")
            for want in ("Profile", "bob", "Member", "@bob-gl", "Only an admin changes your role.", "proj-a", "Write",
                         "proj-b", "Read", "Machines", "Signed-in browsers"):
                assert want in text, want
            assert "alice" not in text and "gitlab-web" not in text and "All machines" not in text
            assert page.js(wide)
            page.screenshot(shots / f"settings-profile-{tag}.png")
            page.js('document.querySelector("#set-nav [data-sec=\\"browsers\\"]").click()')
            page.wait('!document.getElementById("pane-browsers").hidden')
            text = page.js('document.getElementById("pane-browsers").innerText')
            assert "This browser" in text and "Sign out everywhere else" not in text  # one browser
            page.screenshot(shots / f"settings-browsers-{tag}.png")
            page.js('document.querySelector("#set-nav [data-sec=\\"machines\\"]").click()')
            page.wait('document.querySelectorAll("#a-machines .a-row").length === 2')
            assert page.js("location.hash") == "#settings/machines"
            text = page.js('document.getElementById("pane-machines").innerText')
            for want in ("bob@box-1", "online", "bob@box-2", "3h ago", "Rename", "Remove", "Add machine"):
                assert want in text, want
            page.screenshot(shots / f"settings-machines-{tag}.png")
            # Back walks the sections back to Home; Forward and a reload come back
            page.js("history.back()")
            page.wait('!document.getElementById("pane-browsers").hidden')
            page.js("history.back()")
            page.wait('!document.getElementById("pane-profile").hidden')
            page.js("history.back()")
            page.wait('!document.getElementById("home").hidden && document.getElementById("settings").hidden')
            for _ in range(3):
                page.js("history.forward()")
                time.sleep(0.2)
            page.wait('!document.getElementById("pane-machines").hidden')
            page.js("location.reload()")
            page.wait('!document.getElementById("pane-machines").hidden && document.querySelectorAll("#a-machines .a-row").length === 2')
            # rename a machine inline
            box2 = 'document.querySelector("#a-machines [data-host=\\"bob@box-2\\"] [data-machine]")'
            click(page, '#a-machines [data-host="bob@box-2"] [data-machine]', "Rename")
            page.wait('!!document.querySelector("#a-machines .confirm input")')
            page.js('document.querySelector("#a-machines .confirm input").value = "build box"')
            click(page, "#a-machines .confirm", "Save")
            page.wait(f'{box2} && {box2}.querySelector("b").textContent === "build box"')
            # remove asks first
            click(page, '#a-machines [data-host="bob@box-2"] [data-machine]', "Remove")
            q = page.wait('document.querySelector("#a-machines .confirm") && document.querySelector("#a-machines .confirm").innerText')
            assert "Remove build box? It stops reaching the hub until it joins again." in q
            page.screenshot(shots / f"account-remove-{tag}.png")
            click(page, "#a-machines .confirm", "Cancel")
            # Add machine: a member goes straight to the join command with a fresh invite for bob
            page.js('document.getElementById("add-machine").click()')
            cmd = page.wait('!document.getElementById("a-join").hidden && document.getElementById("join-cmd").textContent')
            assert cmd.startswith(f"kaptyorka join {base}/i/")
            assert signin.invite_person(store, cmd.rsplit("/", 1)[1])["name"] == "bob"
            page.call("Browser.grantPermissions", permissions=["clipboardReadWrite", "clipboardSanitizedWrite"],
                      origin=base)
            page.call("Page.bringToFront")  # the clipboard needs a focused page
            page.js('document.getElementById("join-copy").click()')
            page.wait('document.getElementById("join-copy").classList.contains("copied")')
            assert page.js(wide)
            page.screenshot(shots / f"add-machine-{tag}.png")
            # shown again, not made again
            page.js('document.getElementById("add-machine").click()')
            page.js('document.getElementById("add-machine").click()')
            assert page.js('document.getElementById("join-cmd").textContent') == cmd
            # Sign out: the visible one (sidebar on desktop, end of Profile on a phone)
            page.js('document.getElementById("viewer-name").click()')
            page.wait('!document.getElementById("pane-profile").hidden')
            page.js('[...document.querySelectorAll(".signout")].find(b => b.offsetParent).click()')
            page.wait('!document.getElementById("lock").hidden')
        with chrome(w, h) as page:
            lab.who = "alice-gl"
            page.go(base + "/")
            page.wait('document.getElementById("lock").dataset.mode === "gitlab"')
            page.js('document.getElementById("gitlab-signin").click()')
            page.wait('document.getElementById("viewer-name")?.textContent === "alice"')
            page.js('document.querySelector("#set-nav [data-sec=\\"people\\"]").click()')
            page.wait(f'!!{team_row("bob")}')
            page.wait('document.getElementById("live").className === "live"')
            open_person(page, "bob")
            row = 'document.querySelector("#person-body .pp-acts")'
            page.call("Browser.grantPermissions", permissions=["clipboardReadWrite", "clipboardSanitizedWrite"],
                      origin=base)
            page.call("Page.bringToFront")
            assert page.js('document.getElementById("add-choice").hidden')
            page.js(f'[...{row}.querySelectorAll("button")].find(b => b.textContent === "New invite link").click()')
            link = page.wait(f'!!{row}.querySelector(".btn.copied") && navigator.clipboard.readText()')
            assert link.startswith(f"{base}/i/") and old not in link
            assert not store.invite_valid(old)
            assert page.js(wide)
            page.screenshot(shots / f"team-row-{tag}.png")
    finally:
        stop()
        store.close()
        lab.close()


@pytest.mark.parametrize("w,h", [(1440, 900), (390, 844)])
def test_team_make_and_remove_admin(tmp_path, w, h):
    """Make admin on a member's row asks first, then the row carries the Admin pill and Remove
    admin; your own row offers Remove admin only while another admin exists. Screenshots to
    KAPTYORKA_SHOTS."""
    import os
    shots = Path(os.environ.get("KAPTYORKA_SHOTS") or tmp_path)
    shots.mkdir(parents=True, exist_ok=True)
    tag = "phone" if w < 720 else "desktop"
    store = Store(tmp_path / "hub.db")
    ManagerStore(store).set_enabled(False, "test")
    stop = start_hub(store)
    _, token = person(store, "alice", "admin")
    bob, bob_t = person(store, "bob")
    person(store, "carol")
    store.seen("b1", "box-1", "codex", "proj-a", principal=store.authenticate(bob_t))
    row = 'document.querySelector("#people-rows [data-person=\\"{}\\"]")'.format
    acts = 'document.querySelector("#person-body .pp-acts")'
    wide = "document.documentElement.scrollWidth <= document.documentElement.clientWidth"
    try:
        with chrome(w, h) as page:
            page.go(f"http://127.0.0.1:{stop.port}/#t={token}&view=team")
            page.wait(f'!!{row("bob")}')
            assert page.js(f'{row("alice")}.querySelector(".p-role").textContent') == "Admin"
            assert page.js(f'!{row("bob")}.querySelector(".p-role")')
            assert page.js(wide)
            page.screenshot(shots / f"admin-rows-{tag}.png")
            open_person(page, "alice")
            assert "Remove admin" not in page.js(f'{PERSON}.textContent')  # the only admin
            assert "Admins work in every project." in page.js(f'{PERSON}.textContent')
            page.js('document.getElementById("person-back").click()')
            page.wait('!document.getElementById("pane-people").hidden')
            open_person(page, "bob")
            click(page, "#person-body .pp-acts", "Make admin")
            page.wait(f'!!{acts}.querySelector(".confirm")')
            assert page.js(f'{acts}.querySelector(".confirm span").textContent') == \
                "Make bob an admin? They can change everything on the hub."
            assert page.js(wide)
            page.screenshot(shots / f"admin-confirm-{tag}.png")
            click(page, "#person-body .confirm", "Make admin")
            page.wait('document.getElementById("person-sub").textContent === "Admin"')
            assert store.person_by_name("bob")["role"] == "admin"
            assert page.js('document.getElementById("team-ok").textContent') == "bob is now an admin."
            page.js('document.getElementById("person-back").click()')
            page.wait(f'!document.getElementById("pane-people").hidden && !!{row("bob")}.querySelector(".p-role")')
            open_person(page, "alice")
            assert "Remove admin" in page.js(f'{acts}.textContent')  # now there are two
            page.screenshot(shots / f"admin-two-{tag}.png")
            page.js('history.back()')
            page.wait('!document.getElementById("pane-people").hidden')
            open_person(page, "bob")
            click(page, "#person-body .pp-acts", "Remove admin")
            page.wait(f'!!{acts}.querySelector(".confirm")')
            page.screenshot(shots / f"admin-remove-confirm-{tag}.png")
            click(page, "#person-body .confirm", "Remove admin")
            page.wait('document.getElementById("person-sub").textContent === "Member"')
            assert store.person_by_name("bob")["role"] == "member"
    finally:
        stop()
        store.close()


@pytest.mark.parametrize("w,h", [(1440, 900), (390, 844)])
def test_machines_add_a_shared_team_account(tmp_path, w, h):
    """Machines for an admin: Add machine asks whose account it is; Shared team account takes a
    name and shows the join command; once it joins, the new account shows under its own
    computer with a Team tag. The old All machines link lands on Machines with All. Rows have
    no Give to… (the command line keeps `machine move`). Screenshots to KAPTYORKA_SHOTS."""
    import os
    shots = Path(os.environ.get("KAPTYORKA_SHOTS") or tmp_path)
    shots.mkdir(parents=True, exist_ok=True)
    tag = "phone" if w < 720 else "desktop"
    store = Store(tmp_path / "hub.db")
    ManagerStore(store).set_enabled(False, "test")
    stop = start_hub(store)
    _, token = person(store, "alice", "admin")
    bob, bob_t = person(store, "bob")
    b = store.authenticate(bob_t)
    store.heartbeat("box-1", [], principal=b, facts={"host": "box-1", "os_user": "bob"})
    with store.lock:
        store.db.execute("INSERT OR REPLACE INTO device_facts (device_id, facts, updated) VALUES (?,?,?)",
                         (b.device_id, json.dumps({"tools": {"claude-code": "2.1", "codex": "0.9"},
                                                   "logins": {"claude-code": True, "codex": False}, "use": {
                             "age": 0, "cpus": 16, "mem_mb": 64000,
                             "gpus": [{"index": 0, "util": 90.0, "mem": 0, "total": 24576}],
                             "users": {"bob": {"cpu": 6.0, "mem": 20000}}}}), store.clock()))
    card = 'document.querySelector("#a-machines [data-host=\\"{}\\"]")'.format
    wide = "document.documentElement.scrollWidth <= document.documentElement.clientWidth"
    try:
        with chrome(w, h) as page:
            page.go(f"http://127.0.0.1:{stop.port}/#t={token}&view=machines")
            page.wait(f'!!{card("box-1")}')
            assert page.js("location.hash") == "#settings/machines"
            assert page.js('document.querySelector("#m-filter [aria-pressed=\\"true\\"]").textContent') == "All"
            assert page.js(f'{card("box-1")}.querySelector(".m-host b").textContent') == "box-1"
            assert page.js(f'{card("box-1")}.querySelector(".m-kind").textContent') == "GPU 24 GB · 16 CPUs · 62 GB RAM"
            assert page.js(f'{card("box-1")}.querySelector(".m-use").textContent') == "GPU 90% · CPU 38% · memory 31%"
            assert page.js(f'{card("box-1")}.querySelector(".a-top").innerText').split() == ["bob@box-1", "bob"]
            assert page.js(f'{card("box-1")}.querySelector(".m-tools").textContent') == "Claude CodeCodex · not logged in"
            assert "Give" not in page.js('document.getElementById("pane-machines").innerText')
            # Add machine: whose account? Shared team account: a name, then the command to run there
            page.js('document.getElementById("add-machine").click()')
            page.wait('!document.getElementById("add-choice").hidden')
            assert page.js('document.activeElement.id') == "add-own"
            assert page.js(wide)
            page.screenshot(shots / f"add-machine-choice-{tag}.png")
            page.js('document.getElementById("add-shared").click()')
            page.wait('!document.getElementById("add-team-box").hidden && document.getElementById("add-choice").hidden')
            assert page.js('document.activeElement.id') == "new-team"
            page.js('document.getElementById("new-team").value = "jetson-1"')
            page.js('document.querySelector("#add-team button[type=submit]").click()')
            page.wait('!document.getElementById("add-team-link").hidden')
            cmd = page.js('document.querySelector("#add-team-link input").value')
            assert cmd.startswith("kaptyorka join http://127.0.0.1:")
            assert page.js(wide)
            page.screenshot(shots / f"team-machine-added-{tag}.png")
            t, _ = store.invite_redeem(cmd.rsplit("/", 1)[1], "jetson-1", host="jetson-1", os_user="nvidia")
            store.heartbeat("jetson-1", [], principal=store.authenticate(t),
                            facts={"host": "jetson-1", "os_user": "nvidia"})
            page.js('[...document.querySelectorAll("#add-team-link button")].find(b => b.textContent === "Done").click()')
            page.wait(f'!!{card("jetson-1")}')
            assert page.js(f'{card("jetson-1")}.querySelector(".a-top").innerText').split() == ["nvidia@jetson-1", "Team"]
            assert page.js(f'!{card("jetson-1")}.querySelector(".m-tools") && !{card("jetson-1")}.querySelector(".m-kind")')  # never reported
            assert "jetson-1" not in page.js('document.getElementById("people-rows").textContent')
    finally:
        stop()
        store.close()


@pytest.mark.parametrize("w,h", [(1440, 900), (390, 844)])
def test_settings_sections_and_account_button(tmp_path, w, h):
    """The name button opens Settings > Profile (no menu); Sign out sits under the sidebar (end of Profile on a phone); each Settings
    section opens from the sidebar at its own address, with the waiting count on People and
    on the account button. Screenshots of each to KAPTYORKA_SHOTS."""
    import json as _json
    import os
    shots = Path(os.environ.get("KAPTYORKA_SHOTS") or tmp_path)
    shots.mkdir(parents=True, exist_ok=True)
    tag = "phone" if w < 720 else "desktop"
    store = Store(tmp_path / "hub.db")
    ManagerStore(store).set_enabled(False, "test")
    stop = start_hub(store)
    alice, token = person(store, "alice", "admin")
    bob, bob_t = person(store, "bob")
    person(store, "carol", "admin")
    store.project_register("proj-a", "the web shop", by="alice")
    store.project_register("proj-b", "billing", by="alice")
    store.grant_project("proj-a", bob, "write", "alice")
    store.request_access(bob, "project", "proj-b", "to fix invoices")
    a = store.authenticate(token)
    store.heartbeat("box-1", [], principal=a, facts={"host": "box-1", "os_user": "alice"})
    b = store.authenticate(bob_t)
    store.heartbeat("gpu-1", [], principal=b, facts={"host": "gpu-1", "os_user": "bob"})
    with store.lock:
        store.db.execute("INSERT INTO device_facts (device_id, facts, updated) VALUES (?,?,?)",
                         (b.device_id, _json.dumps({"use": {
                             "age": 0, "cpus": 16, "mem_mb": 64000,
                             "gpus": [{"index": 0, "util": 90.0, "mem": 0, "total": 0}],
                             "users": {"bob": {"cpu": 6.0, "mem": 20000}}}}), store.clock()))
    _, code = store.team_machine_add("jet-1", "alice")
    t, _ = store.invite_redeem(code, "jet-1", host="jet-1", os_user="nvidia")
    store.heartbeat("jet-1", [], principal=store.authenticate(t), facts={"host": "jet-1", "os_user": "nvidia"})
    wide = "document.documentElement.scrollWidth <= document.documentElement.clientWidth"
    base = f"http://127.0.0.1:{stop.port}"
    try:
        with chrome(w, h) as page:
            page.go(f"{base}/#t={token}")
            page.wait('document.getElementById("n-acct").textContent === "1"')  # one request waits
            assert page.js('document.getElementById("viewer-name").getAttribute("aria-label")') == "Settings"
            page.js('document.getElementById("viewer-name").click()')
            page.wait('!document.getElementById("pane-profile").hidden')
            assert page.js("location.hash") == "#settings/profile"
            assert page.js(wide)
            # Sign out: one visible, quiet; under the sidebar on desktop, at the end of Profile on a phone
            assert page.js('[...document.querySelectorAll(".signout")].filter(b => b.offsetParent).length') == 1
            sel = '.set-out-phone' if w < 720 else '.set-out button'
            assert page.js(f'!!document.querySelector("{sel}").offsetParent')
            time.sleep(0.4)
            page.screenshot(shots / f"topbar-settings-signout-{tag}.png")
            # one flat sidebar: no headings
            assert page.js('document.querySelectorAll("#set-nav .set-group").length') == 0
            assert page.js('[...document.querySelectorAll("#set-nav a")].filter(a => !a.hidden).map(a => a.textContent)') == \
                ["Profile", "Machines", "People1", "Projects"]
            for sec, ready in [("people", '#people-rows [data-person="bob"]'),
                               ("projects", '#project-rows [data-project="proj-b"]'),
                               ("profile", '#a-profile dd'),
                               ("machines", '#a-machines .a-row')]:
                page.js(f'document.querySelector("#set-nav [data-sec=\\"{sec}\\"]").click()')
                page.wait(f'!document.getElementById("pane-{sec}").hidden'
                          f' && !!document.querySelector({_json.dumps(ready)})')
                assert page.js("location.hash") == f"#settings/{sec}"
                assert page.js(f'document.querySelector("#set-nav [data-sec=\\"{sec}\\"]").getAttribute("aria-current")') == "page"
                assert page.js(wide), sec
                time.sleep(0.4)  # the section's rise has ended
                page.screenshot(shots / f"settings-{sec.replace('/', '-')}-{tag}.png")
            assert page.js('document.getElementById("n-team").textContent') == "1"
            # a deep link opens its section after a reload; the old addresses land on the new ones
            page.go(f"{base}/#settings/projects")
            page.wait('!document.getElementById("pane-projects").hidden')
            for old, new in (("team/projects", "projects"), ("team/people", "people"), ("account/profile", "profile"),
                             ("account/machines", "machines")):
                page.go(f"{base}/#settings/{old}")
                page.wait(f'!document.getElementById("pane-{new}").hidden')
                assert page.js("location.hash") == f"#settings/{new}"
            # a person's own address opens their page; Back goes to People
            page.go(f"{base}/#settings/people/bob")
            page.wait('!document.getElementById("pane-person").hidden && document.getElementById("person-name").textContent === "bob"')
            assert page.js('document.querySelector("#set-nav [data-sec=\\"people\\"]").getAttribute("aria-current")') == "page"
            page.go(f"{base}/#settings/people/nobody")
            page.wait('document.getElementById("person-body").textContent.includes("No one named nobody is on the hub.")')
            # the old All machines address: Machines, with every account
            page.go(f"{base}/#settings/team/machines")
            page.wait('!document.getElementById("pane-machines").hidden'
                      ' && !!document.querySelector(\'#a-machines [data-host="jet-1"]\')')
            assert page.js("location.hash") == "#settings/machines"
            assert page.js('document.querySelector("#m-filter [aria-pressed=\\"true\\"]").dataset.f') == "all"
    finally:
        stop()
        store.close()


def machines_demo(store):
    """A hub for the Machines page: box-1 with three accounts (lab, a team machine; alice, the
    admin; bob, a member) and a GPU queue; two Jetsons (team machines), jetson-2 offline and
    hidden from bob; carol, a member signed in only in a browser, with one offline account on
    box-2; dave, with no machine at all. Returns ({name: token}, keep_online)."""
    from kaptyorka.workers import WorkerStore
    _, alice_t = person(store, "alice", "admin")
    bob, bob_t = person(store, "bob")
    carol, carol_t = person(store, "carol")
    _, dave_t = person(store, "dave")
    store.project_register("proj-a", by="alice")
    store.grant_project("proj-a", bob, "write", "alice")
    a, b = store.authenticate(alice_t), store.authenticate(bob_t)
    store.heartbeat("box-1", [], principal=a, facts={"host": "box-1", "os_user": "alice"})
    store.heartbeat("box-1", [], principal=b, facts={"host": "box-1", "os_user": "bob"})
    team = {}
    for name, host, user in (("lab", "box-1", "lab"), ("jetson-1", "jetson-1", "nvidia"), ("jetson-2", "jetson-2", "nvidia")):
        _, code = store.team_machine_add(name, "alice")
        t, _ = store.invite_redeem(code, name, host=host, os_user=user)
        team[name] = store.authenticate(t)
        store.heartbeat(host, [], principal=team[name], facts={"host": host, "os_user": user})
    store.team_machine_set_visible("jetson-2", "bob", False, "alice")
    c = store.authenticate(store.device_add(carol, "carol@box-2"))
    store.heartbeat("box-2", [], principal=c, facts={"host": "box-2", "os_user": "carol"})
    WorkerStore(store).set_gpu_queue("box-1", "/srv/gpu.lock", 10, "alice")
    big = {"age": 0, "cpus": 32, "mem_mb": 131072,
           "gpus": [{"index": 0, "util": 97.0, "mem": 20000, "total": 24576},
                    {"index": 1, "util": 40.0, "mem": 8000, "total": 24576}],
           "users": {"alice": {"cpu": 6.0, "mem": 30000}, "bob": {"cpu": 4.0, "mem": 20000}}}
    jet = {"age": 0, "cpus": 8, "mem_mb": 16384, "gpus": [{"index": 0, "util": 40.0, "mem": None, "total": None}],
           "users": {"nvidia": {"cpu": 2.0, "mem": 4000}}}
    cc = {"tools": {"claude-code": "2.1"}, "logins": {"claude-code": True}}
    facts = {a.device_id: {**cc, "use": big, "gpu_queue": {"age": 0, "shares": 10, "holders": {"alice": 4, "bob": 3},
                                                           "whole": "", "waiting": 1, "waiters": {"bob": 1}}},
             b.device_id: {"tools": {"claude-code": "2.1"}, "logins": {"claude-code": False}},
             team["lab"].device_id: {"tools": {}, "logins": {}},
             team["jetson-1"].device_id: {**cc, "use": jet},
             team["jetson-2"].device_id: {**cc, "use": jet}}
    with store.lock:
        for dev, f in facts.items():  # jetson-2 last reported two days ago
            store.db.execute("INSERT OR REPLACE INTO device_facts (device_id, facts, updated) VALUES (?,?,?)",
                             (dev, json.dumps(f), store.clock() - (2 * 86400 if dev == team["jetson-2"].device_id else 0)))
        # carol's own token is a browser sign-in that ends, so her only machine is carol@box-2
        for who in ("carol", "dave"):
            store.db.execute("UPDATE devices SET expires=? WHERE person_id=(SELECT id FROM people WHERE name=?)"
                             " AND host=''", (store.clock() + 3600, who))
        old = store.clock() - 2 * 86400
        store.db.execute("UPDATE devices SET last_used=? WHERE id IN (?,?)", (old, team["jetson-2"].device_id, c.device_id))
        store.db.execute("UPDATE devices SET last_used=? WHERE id=?", (store.clock() - 3 * 3600, team["lab"].device_id))

    def online():
        store.seen("a1", "box-1", "claude", "proj-a", principal=a)
        store.seen("a2", "box-1", "claude", "proj-a", principal=a)
        store.seen("b1", "box-1", "claude", "proj-a", principal=b)
        store.seen("j1", "jetson-1", "claude", "proj-a", principal=team["jetson-1"])
        with store.lock:  # lab and jetson-2 stay offline; their sessions only make them bob's
            store.db.execute("UPDATE devices SET last_used=? WHERE id=?", (store.clock(), team["jetson-1"].device_id))
    online()
    for name, key in (("lab", "l1"), ("jetson-2", "j2")):
        store.seen(key, name, "claude", "proj-a", principal=team[name])
    with store.lock:
        store.db.execute("UPDATE agents SET last_seen=? WHERE key IN ('l1', 'j2')", (store.clock() - 3600,))
        store.db.execute("UPDATE devices SET last_used=? WHERE id=?", (store.clock() - 3 * 3600, team["lab"].device_id))
        store.db.execute("UPDATE devices SET last_used=? WHERE id=?", (store.clock() - 2 * 86400, team["jetson-2"].device_id))
    return {"alice": alice_t, "bob": bob_t, "carol": carol_t, "dave": dave_t}, online


@pytest.mark.parametrize("w,h", [(1440, 900), (390, 844)])
def test_machines_grouped_by_computer_with_filters(tmp_path, w, h):
    """Machines: one card per computer (something online first, then by name) with its size,
    use and GPU queue; under it one row per account (team, then yours, then others) with its
    dot or last seen, its tag, its sessions and tools. All / Team / Mine / Online filter it and
    stay on reload; a computer with nothing left goes. An admin sees every account; bob, a
    member, his own and the team machines he can use (not the one hidden from him), no use or
    size, queue counts only; carol's Online is empty; dave has no machine yet. Screenshots to
    KAPTYORKA_SHOTS."""
    import os
    shots = Path(os.environ.get("KAPTYORKA_SHOTS") or tmp_path)
    shots.mkdir(parents=True, exist_ok=True)
    tag = "phone" if w < 720 else "desktop"
    store = Store(tmp_path / "hub.db")
    ManagerStore(store).set_enabled(False, "test")
    stop = start_hub(store)
    tokens, online = machines_demo(store)
    base = f"http://127.0.0.1:{stop.port}"
    wide = "document.documentElement.scrollWidth <= document.documentElement.clientWidth"
    hosts = '[...document.querySelectorAll("#a-machines .m-group:not(.row-out)")].map(g => g.dataset.host)'
    rows = 'document.querySelector(\'#a-machines [data-host="{}"]\').querySelectorAll(".m-acct")'.format
    card = 'document.querySelector(\'#a-machines [data-host="{}"]\')'.format

    def tops(host):
        return page.js(f'[...{rows(host)}].map(r => r.querySelector(".a-top").innerText.split(/\\s+/).join(" "))')

    def text(host):
        return page.js(f'{card(host)}.innerText')

    def pick(f):
        page.js(f'document.querySelector(\'#m-filter [data-f="{f}"]\').click()')
        assert page.js('document.querySelector("#m-filter [aria-pressed=\\"true\\"]").dataset.f') == f

    def open_as(name):  # a fresh load, so the page takes the new token
        page.go("about:blank")
        page.go(f"{base}/#t={tokens[name]}")
        page.wait(f'document.getElementById("viewer-label").textContent === {json.dumps(name)}')
        online()
        page.go(f"{base}/#settings/machines")
        page.wait('!document.getElementById("pane-machines").hidden && !document.querySelector("#a-machines .skel")')

    try:
        with chrome(w, h) as page:
            # alice, the admin, All: every account on the hub
            open_as("alice")
            page.wait(f'{hosts}.length === 4')
            assert page.js('[...document.querySelectorAll("#m-filter button:not([hidden])")].map(b => b.textContent.replace(/ \\d+$/, ""))') == ["All", "Team", "Mine", "Online", "Warnings"]
            assert page.js('document.querySelector("#m-filter [aria-pressed=\\"true\\"]").dataset.f') == "all"
            assert page.js(hosts) == ["box-1", "jetson-1", "box-2", "jetson-2"]  # online first, then by name
            assert tops("box-1") == ["lab@box-1 Team", "alice@box-1 Mine", "bob@box-1 bob"]  # team, yours, others
            assert page.js(f'{card("box-1")}.querySelector(".m-kind").textContent') == "2 GPUs 24 GB · 32 CPUs · 128 GB RAM"
            assert page.js(f'{card("box-1")}.querySelector(".m-use").textContent').startswith("GPU 97% · CPU")
            assert page.js(f'{card("box-1")}.querySelector(".m-queue").textContent') == "GPU queue: alice 4/10 · bob 3/10 · 1 waiting"
            assert page.js(f'{card("jetson-2")}.querySelector(".m-kind").textContent') == "Jetson · 8 CPUs · 16 GB RAM"  # offline, still known
            assert page.js(f'{card("jetson-2")}.querySelector(".m-use")') is None  # nothing fresh
            box = text("box-1")
            assert "online · 2 sessions" in box and "online · 1 session" in box and "3h ago" in box
            assert "Claude Code · not logged in" in box and "No agent tool" in box
            assert tops("box-2") == ["carol@box-2 carol"] and "2d ago" in text("box-2")
            own = page.js(f'[...{rows("box-1")}].map(r => r.querySelector(".a-acts") ? r.querySelector(".a-acts").innerText : "")')
            assert "Rename" in own[1] and own[0] == "" and own[2] == ""  # only your own; not the page's own token
            assert "Remove" not in own[1] and "Give" not in page.js('document.getElementById("pane-machines").innerText')
            assert page.js(wide)
            time.sleep(0.4)
            page.screenshot(shots / f"machines-admin-all-{tag}.png")
            # Team: only team accounts; box-2 (no team account) goes
            pick("team")
            assert page.js(hosts) == ["box-1", "jetson-1", "jetson-2"]
            assert tops("box-1") == ["lab@box-1 Team"]
            assert page.js(wide)
            time.sleep(0.4)
            page.screenshot(shots / f"machines-admin-team-{tag}.png")
            pick("mine")
            assert page.js(hosts) == ["box-1"] and tops("box-1") == ["alice@box-1 Mine"]
            pick("online")
            assert page.js(hosts) == ["box-1", "jetson-1"] and tops("box-1") == ["alice@box-1 Mine", "bob@box-1 bob"]
            # kept on reload, in this browser
            page.js("window.before = 1; location.reload()")
            page.wait(f'!window.before && {hosts}.length === 2')
            assert page.js('document.querySelector("#m-filter [aria-pressed=\\"true\\"]").dataset.f') == "online"
            assert page.js('localStorage.getItem("kaptyorka.machines-filter")') == "online"
            # the old All machines / Everyone address opens Machines with All
            page.go(f"{base}/#settings/team/machines")
            page.wait(f'{hosts}.length === 4')
            assert page.js("location.hash") == "#settings/machines"
            assert page.js('document.querySelector("#m-filter [aria-pressed=\\"true\\"]").dataset.f') == "all"
            # Add machine asks whose; Shared team account from Mine moves to All, where it shows;
            # a second click closes; Esc closes the question; My own account from Team moves to All
            pick("mine")
            page.js('document.getElementById("add-machine").click()')
            assert not page.js('document.getElementById("add-choice").hidden')
            page.js('document.getElementById("add-choice").dispatchEvent(new KeyboardEvent("keydown", {key: "Escape", bubbles: true}))')
            assert page.js('document.getElementById("add-choice").hidden')
            page.js('document.getElementById("add-machine").click()')
            page.js('document.getElementById("add-shared").click()')
            assert not page.js('document.getElementById("add-team-box").hidden')
            assert page.js('document.querySelector("#m-filter [aria-pressed=\\"true\\"]").dataset.f') == "all"
            page.js('document.getElementById("add-machine").click()')
            assert page.js('document.getElementById("add-team-box").hidden && document.getElementById("add-machine").getAttribute("aria-expanded") === "false"')
            pick("team")
            page.js('document.getElementById("add-machine").click()')
            page.js('document.getElementById("add-own").click()')
            page.wait('!document.getElementById("a-join").hidden && document.getElementById("join-cmd").textContent !== ""')
            assert page.js('document.querySelector("#m-filter [aria-pressed=\\"true\\"]").dataset.f') == "all"
            page.js('document.getElementById("join-done").click()')

            # bob, a member: his own and the team machines he can use, never jetson-2
            open_as("bob")
            pick("all")
            page.wait(f'{hosts}.length === 2')
            assert page.js(hosts) == ["box-1", "jetson-1"]
            assert tops("box-1") == ["lab@box-1 Team", "bob@box-1 Mine"]
            assert tops("jetson-1") == ["nvidia@jetson-1 Team"]
            pane = page.js('document.getElementById("pane-machines").innerText')
            assert "jetson-2" not in pane and "alice" not in pane and "carol" not in pane
            assert page.js('!document.querySelector("#a-machines .m-use") && !document.querySelector("#a-machines .m-kind")')
            assert page.js(f'{card("box-1")}.querySelector(".m-queue").textContent') == "GPU queue: 7/10 in use · 1 waiting"
            assert page.js('[...document.querySelectorAll("#set-nav a:not([hidden])")].map(a => a.querySelector("span").textContent)') == ["Profile", "Machines"]
            assert page.js('document.getElementById("n-machines").textContent') == "2"  # lab offline, his own login
            assert page.js(wide)
            time.sleep(0.4)
            page.screenshot(shots / f"machines-member-{tag}.png")
            pick("team")
            assert page.js(hosts) == ["box-1", "jetson-1"] and tops("box-1") == ["lab@box-1 Team"]
            pick("mine")
            assert page.js(hosts) == ["box-1"] and tops("box-1") == ["bob@box-1 Mine"]
            # Add machine: no question for a member, straight to the command
            page.js('document.getElementById("add-machine").click()')
            page.wait('!document.getElementById("a-join").hidden && document.getElementById("join-cmd").textContent !== ""')
            assert page.js('document.getElementById("add-choice").hidden')
            page.js('document.getElementById("join-done").click()')

            # carol: box-2 (offline) and the team machines she may use, no sessions needed
            open_as("carol")
            pick("online")
            page.wait(f'{hosts}.length === 1')
            assert page.js(hosts) == ["jetson-1"] and tops("jetson-1") == ["nvidia@jetson-1 Team"]
            assert page.js(f'{rows("jetson-1")}[0].title') == "A team machine: shared by the team"  # no project of hers: no tail
            assert page.js(wide)
            time.sleep(0.4)
            page.screenshot(shots / f"machines-online-{tag}.png")
            pick("all")
            assert page.js(hosts) == ["jetson-1", "box-1", "box-2", "jetson-2"]

            # dave: no machine of his own, only the team machines; Mine is empty and says so
            open_as("dave")
            pick("mine")
            msg = page.wait('document.querySelector("#a-machines .a-none")?.textContent')
            assert msg and "Add machine" not in msg
            assert page.js(wide)
            time.sleep(0.4)
            page.screenshot(shots / f"machines-member-empty-{tag}.png")
            pick("all")
            assert page.js(hosts) == ["jetson-1", "box-1", "jetson-2"]
    finally:
        stop()
        store.close()


@pytest.mark.parametrize("w,h", [(1440, 900), (390, 844)])
def test_machines_warnings(tmp_path, w, h):
    """Machines shows each warning as an amber chip on its account's row (the full text on
    hover, a click opens it with the fix and Copy); a warning of no account (a worker with no
    OS user) on its computer's card. A Warnings chip filters to them and stays on reload; the
    count shows beside Machines in Settings. An admin sees all; bob only his own and the team
    machines he can use, no other person's name. Screenshots to KAPTYORKA_SHOTS."""
    import os
    shots = Path(os.environ.get("KAPTYORKA_SHOTS") or tmp_path)
    shots.mkdir(parents=True, exist_ok=True)
    tag = "phone" if w < 720 else "desktop"
    store = Store(tmp_path / "hub.db")
    ManagerStore(store).set_enabled(False, "test")
    stop = start_hub(store)
    tokens, online = machines_demo(store)
    with store.lock:
        ids = {r["os_user"]: r["id"] for r in store.db.execute(
            "SELECT id, os_user FROM devices WHERE host='box-1'")}
        f = json.loads(store.db.execute("SELECT facts FROM device_facts WHERE device_id=?",
                                        (ids["alice"],)).fetchone()["facts"])
        store.db.execute("UPDATE device_facts SET facts=? WHERE device_id=?", (json.dumps(
            {**f, "version": "0.9.0", "update_failed": "auto-update failed"}), ids["alice"]))
        bob = store.db.execute("SELECT id FROM people WHERE name='bob'").fetchone()["id"]
        cols = {"name": "proj-a-w1", "project": "proj-a", "task": "t", "boss_key": "web:bob",
                "boss_person": bob, "device_id": 9999, "host": "box-1", "tool": "codex",
                "state": "failed", "created": store.clock(), "error": "no tmux there"}
        store.db.execute(f"INSERT INTO workers ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                         list(cols.values()))
    base = f"http://127.0.0.1:{stop.port}"
    wide = "document.documentElement.scrollWidth <= document.documentElement.clientWidth"
    hosts = '[...document.querySelectorAll("#a-machines .m-group:not(.row-out)")].map(g => g.dataset.host)'
    card = 'document.querySelector(\'#a-machines [data-host="{}"]\')'.format
    chips = '[...document.querySelectorAll("#a-machines .m-warns button")].map(b => b.textContent)'
    # no tool logged in has no chip of its own: the tool tag that says so opens it (counted once a row)
    tagged = 'document.querySelectorAll("#a-machines .m-acct:has(.m-tools button.chip.warn)").length'
    bob_row = 'document.querySelector(\'#a-machines [data-machine="%d"]\')'

    def open_as(name):
        page.go("about:blank")
        page.go(f"{base}/#t={tokens[name]}")
        page.wait(f'document.getElementById("viewer-label").textContent === {json.dumps(name)}')
        online()
        page.go(f"{base}/#settings/machines")
        page.wait('!document.getElementById("pane-machines").hidden && !document.querySelector("#a-machines .skel")')
        page.wait('!document.getElementById("m-warn-chip").hidden')
        page.js('document.querySelector(\'#m-filter [data-f="all"]\').click()')

    def count():
        n = page.js('document.getElementById("n-machines").textContent')
        assert page.js('document.getElementById("m-warn-chip").textContent') == f"Warnings {n}"
        return int(n)

    try:
        with chrome(w, h) as page:
            open_as("alice")
            page.wait(f'{chips}.length > 0')
            got = page.js(chips)
            assert count() == len(got) + page.js(tagged)
            assert "kaptyorka 0.9.0 \u00b7 auto-update failed" in got
            assert "bridge offline" in got and "worker proj-a-w1 failed" in got and "no tool logged in" not in got
            ttag = (bob_row % ids["bob"]) + '.querySelector(".m-tools button.chip.warn")'
            assert page.js(f'{ttag}.textContent') == "Claude Code · not logged in"
            page.js(f'{ttag}.click()')
            page.wait(f'!!{bob_row % ids["bob"]}.querySelector(".m-warn-text code")')
            assert page.js(f'{bob_row % ids["bob"]}.querySelector(".m-warn-text code").textContent') == "claude auth login"
            # lab reported no tool at all: its one "No agent tool" tag opens what to do
            lab = bob_row % ids["lab"]
            assert page.js(f'{lab}.querySelectorAll(".m-tools .chip").length') == 1
            page.js(f'{lab}.querySelector(".m-tools button.chip.bad").click()')
            page.wait(f'!!{lab}.querySelector(".m-warn-text")')
            assert "has no agent tool" in page.js(f'{lab}.querySelector(".m-warn-text").innerText')
            row = 'document.querySelector(\'#a-machines [data-machine="%d"]\')' % ids["alice"]
            b = f'{row}.querySelector(".m-warns button")'
            assert "auto-update failed). Run `kaptyorka update` there." in page.js(f'{b}.title')
            # the worker has no OS user: it sits on its computer's card
            assert page.js(f'{card("box-1")}.querySelector(".m-head .m-warns").innerText') == "worker proj-a-w1 failed"
            page.js(f'{b}.click()')
            page.wait(f'!!{row}.querySelector(".m-warn-text code")')
            assert page.js(f'{row}.querySelector(".m-warn-text code").textContent') == "kaptyorka update"
            assert page.js(f'{row}.querySelector(".m-warn-text").innerText').startswith(
                "alice@box-1 runs kaptyorka 0.9.0; the hub runs")
            assert page.js(f'{row}.querySelector(".m-warns button").getAttribute("aria-expanded")') == "true"
            assert page.js(wide)
            time.sleep(0.4)
            page.screenshot(shots / f"machines-warnings-admin-{tag}.png")
            # Warnings: only the accounts with one (and box-1's card for its worker); kept on reload
            page.js('document.getElementById("m-warn-chip").click()')
            assert page.js('localStorage.getItem("kaptyorka.machines-filter")') == "warnings"
            assert "box-2" not in page.js(hosts) and "jetson-1" not in page.js(hosts)
            assert page.js(f'[...{card("box-1")}.querySelectorAll(".m-acct")].every(r => r.querySelector(".m-warns"))')
            page.js("window.before = 1; location.reload()")
            page.wait('!window.before && document.querySelector("#m-filter [aria-pressed=\\"true\\"]")?.dataset.f === "warnings"')
            page.wait(f'{hosts}.length > 0')
            time.sleep(0.4)
            page.screenshot(shots / f"machines-warnings-admin-filter-{tag}.png")
            # the count shows beside Machines from any Settings section
            page.go(f"{base}/#settings/profile")
            page.wait('!document.getElementById("pane-profile").hidden')
            assert not page.js('document.getElementById("n-machines").hidden')

            open_as("bob")
            page.wait(f'{chips}.length > 0')
            got = page.js(chips)
            assert count() == len(got) + page.js(tagged) == 3
            assert sorted(got) == ["bridge offline", "worker proj-a-w1 failed"]
            pane = page.js('document.getElementById("pane-machines").innerText')
            assert "alice" not in pane and "jetson-2" not in pane and "0.9.0" not in pane
            page.js('document.getElementById("m-warn-chip").click()')
            page.wait(f'{hosts}.length > 0')
            assert page.js(hosts) == ["box-1"]
            page.js('document.querySelector("#a-machines .m-warns button").click()')
            assert page.js(wide)
            time.sleep(0.4)
            page.screenshot(shots / f"machines-warnings-member-{tag}.png")
    finally:
        stop()
        store.close()


def test_no_make_team_machine_button_and_this_hub(tmp_path):
    """The page has no Make team machine (the command line keeps `machine team --from`); the hub
    key's device on the Machines page says This hub. Screenshots to KAPTYORKA_SHOTS."""
    import os
    from kaptyorka import paths
    shots = Path(os.environ.get("KAPTYORKA_SHOTS") or tmp_path)
    shots.mkdir(parents=True, exist_ok=True)
    store = Store(tmp_path / "hub.db")
    ManagerStore(store).set_enabled(False, "test")
    stop = start_hub(store)
    _, token = person(store, "alice", "admin")
    (paths.home() / "admin-token").write_text(token + "\n")  # alice's device is the hub key
    bob, bob_t = person(store, "bob")
    store.seen("b1", "box-1", "codex", "proj-a", principal=store.authenticate(bob_t))
    try:
        with chrome(1440, 900) as page:
            page.go(f"http://127.0.0.1:{stop.port}/#t={token}&view=team")
            page.wait(f'!!{team_row("bob")}')
            open_person(page, "bob")
            assert "Make team machine" not in page.js('document.getElementById("settings").textContent')
            page.js('location.hash = "#settings/machines"')
            page.wait('!!document.querySelector("#a-machines .a-hub")')
            assert page.js('document.querySelector("#a-machines .a-hub").textContent') == "This hub"
            page.screenshot(shots / "machines-this-hub-desktop.png")
    finally:
        stop()
        store.close()


@pytest.mark.parametrize("w,h", [(1440, 900), (390, 844)])
def test_home_mine_everyone_and_the_machine_switch(tmp_path, w, h):
    """An admin's Home: Mine shows the manager, your own sessions and the team machine sessions
    in your projects (what a member sees); Everyone shows every session under a header per
    person and per team machine with its count, folded but your own; both kept on reload. A
    member gets no switch. A person's page: a switch per team machine, on by default; off hides
    it and its sessions from them; a poll does not redraw an open view whose data did not change. Screenshots to
    KAPTYORKA_SHOTS."""
    import os
    shots = Path(os.environ.get("KAPTYORKA_SHOTS") or tmp_path)
    shots.mkdir(parents=True, exist_ok=True)
    tag = "phone" if w < 720 else "desktop"
    store = Store(tmp_path / "hub.db")
    ManagerStore(store).set_enabled(False, "test")
    stop = start_hub(store)
    _, token = person(store, "alice", "admin")
    bob, bob_t = person(store, "bob")
    store.project_register("proj-a", by="alice")
    store.grant_project("proj-a", bob, "write", "alice")
    a, b = store.authenticate(token), store.authenticate(bob_t)
    store.heartbeat("box-1", [], principal=a, facts={"host": "box-1", "os_user": "alice"})
    store.heartbeat("box-2", [], principal=b, facts={"host": "box-2", "os_user": "bob"})
    _, code = store.team_machine_add("jet-1", "alice")
    t, _ = store.invite_redeem(code, "jet-1", host="jet-1", os_user="nvidia")
    j = store.authenticate(t)
    store.heartbeat("jet-1", [], principal=j, facts={"host": "jet-1", "os_user": "nvidia"})

    def online():
        store.seen("a1", "box-1", "codex", "proj-a", principal=a)
        store.seen("b1", "box-2", "codex", "proj-a", principal=b)
        store.seen("j1", "jet-1", "claude", "proj-a", principal=j)
    online()
    base = f"http://127.0.0.1:{stop.port}"
    wide = "document.documentElement.scrollWidth <= document.documentElement.clientWidth"
    rows = '[...document.querySelectorAll("#agents .row .subline span:first-child")].map(s => s.textContent)'
    heads = '[...document.querySelectorAll("#agents .g-head")].map(h => h.textContent + "|" + h.getAttribute("aria-expanded"))'
    try:
        with chrome(w, h) as page:
            page.go(f"{base}/#t={token}")
            page.wait('!document.getElementById("h-scope").hidden && document.querySelectorAll("#agents .row").length === 4')
            assert page.js('document.querySelector("#h-scope [aria-pressed=true]").textContent') == "Mine"
            assert page.js(rows) == ["alice@box-1", "nvidia@jet-1"]  # yours and the team machine's; not bob's
            assert page.js(wide)
            page.screenshot(shots / f"home-admin-mine-{tag}.png")
            page.js('document.querySelector("#h-scope [data-scope=everyone]").click()')
            page.wait('document.querySelectorAll("#agents .g-head").length === 3')
            assert page.js(heads) == ["You1|true", "bob1|false", "jet-1Team1|false"]
            page.js('[...document.querySelectorAll("#agents .g-head")].find(h => h.textContent.startsWith("bob")).click()')
            page.wait('document.querySelectorAll("#agents .row").length === 4')
            assert "bob@box-2" in page.js(rows)
            page.screenshot(shots / f"home-admin-everyone-{tag}.png")
            page.js("window.before = 1; location.reload()")  # the switch and the folds are kept in this browser
            page.wait('!window.before && document.querySelectorAll("#agents .g-head").length === 3')
            assert page.js(heads) == ["You1|true", "bob1|true", "jet-1Team1|false"]
            # bob's page: the team machine's switch, on by default
            page.go(f"{base}/#settings/people/bob")
            sw = 'document.querySelector("#person-body .sw")'
            page.wait(f'!!{sw}')
            assert page.js(f'{sw}.getAttribute("aria-checked")') == "true"
            assert "Turn a team machine off to hide it and its sessions from them." in page.js('document.getElementById("person-body").textContent')
            assert "nvidia@jet-1" in page.js('document.querySelector("#person-body [data-team=\\"jet-1\\"]").textContent')
            assert "bob@box-2" in page.js('document.getElementById("person-body").textContent')  # his own account, read only
            page.js(f'{sw}.focus(); {sw}.click()')
            page.wait(f'{sw}.getAttribute("aria-checked") === "false" && !!document.querySelector("#person-body .pp-cue")')
            assert store.team_hidden() == {"jet-1": ["bob"]}
            assert page.js(f'document.activeElement === {sw}')
            page.wait('!document.querySelector("#person-body .pp-cue")', timeout=5)
            # an open view is not redrawn by polls that bring nothing new
            page.js('window.__pp = document.querySelector("#person-body .a-profile")')
            for _ in range(3):
                online()
                time.sleep(2.2)
            assert page.js('window.__pp === document.querySelector("#person-body .a-profile")')
            page.screenshot(shots / f"person-{tag}.png")
        with chrome(w, h) as page:  # hidden: bob's Home has no jet-1 session
            page.go(f"{base}/#t={bob_t}")
            page.wait('document.querySelectorAll("#agents .row").length === 2')
            assert page.js('document.getElementById("h-scope").hidden')
            assert page.js(rows) == ["bob@box-2"]
        with chrome(w, h) as page:
            page.go(f"{base}/#t={token}")
            page.wait('!document.getElementById("h-scope").hidden')
            page.go(f"{base}/#settings/people/bob")
            page.wait(f'!!{sw}')
            page.js(f'{sw}.click()')
            page.wait(f'{sw}.getAttribute("aria-checked") === "true" && !!document.querySelector("#person-body .pp-cue")')
            assert store.team_hidden() == {}
            # admins: no switch, they see every team machine
            page.go(f"{base}/#settings/people/alice")
            page.wait('document.getElementById("person-body").textContent.includes("Admins see every team machine.")')
            assert page.js(f'!{sw}')
        with chrome(w, h) as page:  # shown again
            page.go(f"{base}/#t={bob_t}")
            page.wait('document.querySelectorAll("#agents .row").length === 3')
            assert page.js(rows) == ["bob@box-2", "nvidia@jet-1"]
            page.screenshot(shots / f"home-member-{tag}.png")
    finally:
        stop()
        store.close()


@pytest.mark.parametrize("w,h", [(1440, 900), (390, 844)])
def test_machines_gpu_queue_line(tmp_path, w, h):
    """The GPU queue line on Machines, once in the computer's header: names for the admin,
    counts only for a member. Screenshots to KAPTYORKA_SHOTS."""
    import json as _json
    import os
    from kaptyorka.workers import WorkerStore
    shots = Path(os.environ.get("KAPTYORKA_SHOTS") or tmp_path)
    shots.mkdir(parents=True, exist_ok=True)
    tag = "phone" if w < 720 else "desktop"
    store = Store(tmp_path / "hub.db")
    ManagerStore(store).set_enabled(False, "test")
    stop = start_hub(store)
    _, token = person(store, "alice", "admin")
    _, bob_t = person(store, "bob")
    carol, _ = person(store, "carol")
    b = store.authenticate(bob_t)
    store.heartbeat("box-1", [], principal=b, facts={"host": "box-1", "os_user": "bob"})
    c = store.authenticate(store.device_add(carol, "carol-box-1"))
    store.heartbeat("box-1", [], principal=c, facts={"host": "box-1", "os_user": "carol"})
    WorkerStore(store).set_gpu_queue("box-1", "/srv/gpu.lock", 10, "alice")
    with store.lock:
        store.db.execute("INSERT INTO device_facts (device_id, facts, updated) VALUES (?,?,?)",
                         (b.device_id, _json.dumps({
                             "version": "0.9.23", "tools": {"claude-code": "2.1"}, "logins": {"claude-code": True},
                             "use": {"age": 0, "cpus": 16, "mem_mb": 64000,
                                     "gpus": [{"index": 0, "util": 97.0, "mem": 0, "total": 0}],
                                     "users": {"bob": {"cpu": 6.0, "mem": 20000}}},
                             "gpu_queue": {"age": 0, "shares": 10, "holders": {"bob": 4, "carol": 6},
                                           "whole": "", "waiting": 1, "waiters": {"carol": 1}}}),
                          store.clock()))
    head = 'document.querySelector("#a-machines [data-host=\\"box-1\\"] .m-head")'
    wide = "document.documentElement.scrollWidth <= document.documentElement.clientWidth"
    base = f"http://127.0.0.1:{stop.port}"
    try:
        with chrome(w, h) as page:
            page.go(f"{base}/#t={token}&view=machines")
            page.wait(f'!!{head} && !!{head}.querySelector(".m-queue")')
            assert page.js('document.querySelectorAll("#a-machines .m-queue").length') == 1  # once per computer
            assert page.js(f'{head}.querySelector(".m-queue").textContent') == \
                "GPU queue: bob 4/10 · carol 6/10 · 1 waiting"
            assert page.js(f'{head}.querySelector(".m-use").textContent').startswith("GPU 97%")
            assert page.js(wide)
            page.screenshot(shots / f"machines-gpu-queue-admin-{tag}.png")
        with chrome(w, h) as page:
            page.go(f"{base}/#t={bob_t}&view=machines")
            page.wait('!!document.querySelector("#a-machines .m-queue")')
            assert page.js('document.querySelector("#a-machines .m-queue").textContent') == \
                "GPU queue: 10/10 in use · 1 waiting"
            assert "carol" not in page.js('document.getElementById("a-machines").textContent')
            assert page.js(wide)
            page.screenshot(shots / f"machines-gpu-queue-member-{tag}.png")
    finally:
        stop()
        store.close()


@pytest.mark.parametrize("w,h", [(1440, 900), (390, 844)])
def test_login_warnings_make_one_line(tmp_path, w, h):
    """Six accounts with no logged-in tool: one line over the chat with a link to Machines and a
    x that remembers the accounts; a chat on one of them still shows that account's own bar with
    the login command. One such account keeps its own bar. Screenshots to KAPTYORKA_SHOTS."""
    import json as _json
    import os
    shots = Path(os.environ.get("KAPTYORKA_SHOTS") or tmp_path)
    shots.mkdir(parents=True, exist_ok=True)
    tag = "phone" if w < 720 else "desktop"
    store = Store(tmp_path / "hub.db")
    ManagerStore(store).set_enabled(False, "test")
    stop = start_hub(store)
    aid, token = person(store, "alice", "admin")
    store.project_register("proj-a", by="alice")
    devs = []
    for i in range(1, 7):
        p = store.authenticate(store.device_add(aid, f"alice-box-{i}"))
        store.heartbeat(f"box-{i}", [], principal=p, facts={"host": f"box-{i}", "os_user": "alice"})
        with store.lock:
            store.db.execute("INSERT OR REPLACE INTO device_facts (device_id, facts, updated) VALUES (?,?,?)",
                             (p.device_id, _json.dumps({"host": f"box-{i}", "os_user": "alice", "version": "0.9.24",
                                                        "tools": {"claude-code": "2.1"}, "logins": {"claude-code": False}}),
                              store.clock()))
        devs.append(p)
    store.seen("s-1", "alice-box-1", "claude", "proj-a", principal=devs[0])
    warns = 'document.querySelectorAll("#warns li")'
    base = f"http://127.0.0.1:{stop.port}"
    try:
        with chrome(w, h) as page:
            page.go(f"{base}/#t={token}")
            page.wait(f'{warns}.length === 1')
            line = page.js('document.querySelector("#warns li").textContent')
            assert "6 accounts can't run agents (no tool logged in)" in line and "See machines" in line
            time.sleep(0.4)
            page.screenshot(shots / f"login-warn-one-line-{tag}.png")
            # a chat on one of them: that account's own bar, with the command and the group
            page.wait('[...document.querySelectorAll(".list .row")].some(r => r.textContent.includes("proj-a"))')
            page.js('[...document.querySelectorAll(".list .row")].find(r => r.textContent.includes("proj-a")).click()')
            page.wait(f'{warns}.length === 2')
            assert "alice@box-1" in page.js('document.querySelector("#warns li").textContent')
            assert "claude auth login" in page.js('document.getElementById("warns").textContent')
            page.js('localStorage.setItem("kaptyorka.login-warn-dismissed", JSON.stringify(["old@gone", "old@gone"]))')
            page.js('document.querySelector("#warns .login-group button").click()')
            assert page.js('JSON.parse(localStorage.getItem("kaptyorka.login-warn-dismissed"))') == \
                [f"alice@box-{i}" for i in range(2, 7)]  # exactly this group, no old names
            page.wait(f'{warns}.length === 1')
            assert "claude auth login" in page.js('document.getElementById("warns").textContent')
            page.js("location.reload()")
            page.wait('document.querySelectorAll(".list .row").length > 0')
            time.sleep(1.5)
            assert not page.js('document.querySelector("#warns .login-group")')  # stays dismissed
    finally:
        stop()
        store.close()


@pytest.mark.parametrize("w,h", [(1440, 900), (390, 844)])
def test_home_shows_claude_codes_title_else_the_folder(tmp_path, w, h):
    """A session with a title: the title is the name, the folder and account@computer the grey
    line, in the list and the chat header. Without one: the folder, as before."""
    import os
    shots = Path(os.environ.get("KAPTYORKA_SHOTS") or tmp_path)
    shots.mkdir(parents=True, exist_ok=True)
    tag = "phone" if w < 720 else "desktop"
    store = Store(tmp_path / "hub.db")
    ManagerStore(store).set_enabled(False, "test")
    stop = start_hub(store)
    aid, token = person(store, "alice", "admin")
    store.project_register("proj-a", by="alice")
    store.project_register("proj-b", by="alice")
    p = store.authenticate(store.device_add(aid, "alice-box-1"))
    store.heartbeat("alice-box-1", [
        {"key": "claude:s-1", "tool": "claude-code", "project": "proj-a", "title": "Fix the robot assembly"},
        {"key": "claude:s-2", "tool": "claude-code", "project": "proj-b"}],
        principal=p, facts={"host": "box-1", "os_user": "alice"})
    rows = '[...document.querySelectorAll(".list .row")].map(r => r.innerText.replace(/\\s+/g, " "))'
    store.set_status("claude:s-1", "Fixing the training loop")
    base = f"http://127.0.0.1:{stop.port}"
    try:
        with chrome(w, h) as page:
            page.go(f"{base}/#t={token}")
            page.wait(f'{rows}.some(t => t.includes("proj-b"))')
            got = page.js(rows)
            titled = [t for t in got if "Fix the robot assembly" in t][0]
            assert "proj-a" in titled and "alice@box-1" in titled and titled.index("Fix") < titled.index("proj-a")
            plain = [t for t in got if "proj-b" in t][0]
            assert "alice@box-1" in plain and "Fix" not in plain
            page.js('[...document.querySelectorAll(".list .row")].find(r => r.innerText.includes("Fix the robot")).click()')
            page.wait('document.getElementById("chat-name").textContent === "Fix the robot assembly"')
            head = page.js('document.getElementById("chat-sub").textContent')
            assert "proj-a" in head and "alice@box-1" in head and "Fixing the training loop" in head
            assert page.js('document.querySelector("#log .blank").textContent') == \
                "Messages you send here show up here. What you type to Claude stays on your computer."
            time.sleep(0.4)
            page.screenshot(shots / f"home-title-{tag}.png")
            page.js('[...document.querySelectorAll(".list .row")].find(r => r.innerText.includes("proj-b")).click()')
            page.wait('document.getElementById("chat-name").textContent === "proj-b"')
            assert page.js('document.querySelector("#log .blank").textContent').startswith("Messages you send here show up here.")
    finally:
        stop()
        store.close()


def test_idle_sessions_fold_and_names_match_claude(tmp_path):
    """Sessions idle over a day fold into one Idle group at the end, closed at first, each row
    saying how long; a worker idle as long stays in the list. A renamed session shows its name,
    an unnamed one Claude Code's title (the bridge drops the automatic name), else its folder.
    Screenshots to KAPTYORKA_SHOTS."""
    import os
    shots = Path(os.environ.get("KAPTYORKA_SHOTS") or tmp_path)
    store = Store(tmp_path / "hub.db")
    ManagerStore(store).set_enabled(False, "test")
    stop = start_hub(store)
    alice, token = person(store, "alice", "admin")
    facts = {"host": "box-1", "os_user": "alice"}
    day = 86400
    store.heartbeat("box-1", [
        {"key": "claude:ren", "tool": "claude-code", "project": "proj-a", "label": "api-fix",
         "title": "api-fix", "idle_s": 60},
        {"key": "claude:tit", "tool": "claude-code", "project": "proj-a", "label": "",
         "title": "Fix the login page", "idle_s": 600},
        {"key": "claude:new", "tool": "claude-code", "project": "proj-b", "label": "", "title": ""},
        {"key": "codex:w1", "tool": "codex", "project": "proj-a", "label": "", "idle_s": 5 * day},
        {"key": "claude:old", "tool": "claude-code", "project": "proj-c", "label": "",
         "title": "Notes on the old importer", "idle_s": 31 * day},
        {"key": "claude:day", "tool": "claude-code", "project": "proj-b", "label": "release-check",
         "idle_s": 30 * 3600}],
        principal=store.authenticate(token), facts=facts)
    with store.lock:
        store.db.execute(
            "INSERT INTO workers (name, project, task, boss_key, boss_person, device_id, host,"
            " tool, key, state, created) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            ("proj-a-w1", "proj-a", "t", "claude:ren", alice, 1, "box-1", "codex", "codex:w1",
             "up", store.clock()))
    row = '[...document.querySelectorAll("#agents .row")].find(r => r.querySelector(".name").textContent === %s)'
    head = 'document.querySelector("#agents [data-key=\\"idle-head\\"]")'
    try:
        for w, h, tag in ((1440, 900, "desktop"), (390, 844, "phone")):
            with chrome(w, h) as page:
                page.go(f"http://127.0.0.1:{stop.port}/#t={token}")
                page.wait(f'!!({row % json.dumps("api-fix")}) && !!{head}')
                assert page.js(f'{head}.textContent') == "Idle2"
                assert page.js(f'{head}.getAttribute("aria-expanded")') == "false"
                assert page.js(f'{head} === [...document.querySelectorAll("#agents > *")].slice(-2)[0]')  # at the end
                assert page.js(f'!({row % json.dumps("release-check")})')
                titled = row % json.dumps("Fix the login page")
                assert page.js(f'{titled}.querySelector(".subline").textContent').startswith("proj-a")
                assert page.js(f'!!({row % json.dumps("proj-b")})')  # no name, no title: its folder
                worker = row % json.dumps("proj-a")
                assert page.js(f'!{worker}.closest(".bg-rows")')
                assert page.js(f'{worker}.querySelector(".subline").textContent') == "alice@box-1"  # no idle word
                page.screenshot(shots / f"idle-folded-{tag}.png")
                page.js(f'{head}.click()')
                page.wait(f'!!({row % json.dumps("release-check")})')
                day_row, old = row % json.dumps("release-check"), row % json.dumps("Notes on the old importer")
                assert page.js(f'{day_row}.querySelector(".subline").textContent') == "idle 30 hours \u00b7 alice@box-1"
                assert page.js(f'{old}.querySelector(".subline").textContent') == \
                    "idle 31 days \u00b7 proj-c \u00b7 alice@box-1"
                assert page.js(f'{old}.closest(".bg-rows").id') == "idle-rows"
                time.sleep(0.3)
                page.screenshot(shots / f"idle-open-{tag}.png")
                page.js("window.before = 1; location.reload()")  # this viewer's choice holds
                page.wait(f'!window.before && !!({old})')
                page.js(f'{old}.click()')  # still a chat like any other
                assert page.wait('document.getElementById("text").placeholder') == "Message Notes on the old importer"
    finally:
        stop()
        store.close()


def test_background_task_and_stuck_chips(tmp_path):
    """A finished turn with a background task running shows "Background task", not "Working";
    a session silent half an hour or more shows an amber "Silent 1 h" (its task) or "Stuck 2 h"
    (busy with no new step). Screenshots to KAPTYORKA_SHOTS."""
    import os
    shots = Path(os.environ.get("KAPTYORKA_SHOTS") or tmp_path)
    store = Store(tmp_path / "hub.db")
    ManagerStore(store).set_enabled(False, "test")
    stop = start_hub(store)
    _, token = person(store, "alice", "admin")

    def task(silent):
        return [{"id": "bq1", "silent_s": silent, "command": "./run_suite.sh"}]
    store.heartbeat("box-1", [
        {"key": "k:sync", "tool": "claude-code", "project": "proj-a", "label": "data-sync",
         "activity": "shell", "write_s": 5, "tasks": task(40)},
        {"key": "k:night", "tool": "claude-code", "project": "proj-a", "label": "nightly-tests",
         "activity": "shell", "write_s": 4000, "tasks": task(3700)},
        {"key": "k:api", "tool": "claude-code", "project": "proj-b", "label": "api-dev",
         "activity": "busy", "write_s": 7300, "tasks": []},
        {"key": "k:ui", "tool": "claude-code", "project": "proj-b", "label": "ui-work",
         "activity": "busy", "write_s": 3, "tasks": []}],
        principal=store.authenticate(token), facts={"host": "box-1", "os_user": "u"})
    row = '[...document.querySelectorAll("#agents .row")].find(r => r.querySelector(".name").textContent === "%s")'
    chip = row + '.querySelector(".chip")'
    try:
        for w, h, tag in ((1440, 900, "desktop"), (390, 844, "phone")):
            with chrome(w, h) as page:
                page.go(f"http://127.0.0.1:{stop.port}/#t={token}")
                page.wait(f'!!({row % "ui-work"})')
                assert page.js(f'{chip % "data-sync"}.textContent') == "Background task"
                assert page.js(f'{chip % "data-sync"}.title') == "Waiting on a background task"
                assert page.js(f'{chip % "nightly-tests"}.textContent') == "Silent 1 h"
                assert page.js(f'{chip % "nightly-tests"}.classList.contains("warn")')
                assert page.js(f'{chip % "api-dev"}.textContent') == "Stuck 2 h"
                assert page.js(f'{chip % "ui-work"}.textContent') == "Working"
                assert page.js("document.documentElement.scrollWidth <= document.documentElement.clientWidth")
                page.screenshot(shots / f"stuck-{tag}.png")
    finally:
        stop()
        store.close()
