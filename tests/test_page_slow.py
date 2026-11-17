"""The page in a real headless Chrome: the admin's Team (add a person and copy the link, give a
project, answer a request, remove), and the chat. Run with: pytest -m slow tests/test_page_slow.py"""
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
            assert page.js('document.getElementById("tab-team").hidden')
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


def test_member_sees_only_home(member_page):
    """A member's page: Home only (an admin view in the link falls back to it), their own
    sessions and the manager, and no admin route fetched at all."""
    store, page, url = member_page
    bob = store.authenticate(url.split("#t=")[1])
    store.seen("b1", "box-1", "codex", "proj-a", principal=bob)
    page.go(url + "&view=notes")
    page.wait('document.querySelectorAll("#agents .row").length === 2')
    assert page.js('document.getElementById("tab-team").hidden') and page.js('document.getElementById("team").hidden')
    assert page.js('[...document.querySelectorAll("#agents .row .name")].map(n => n.textContent)') == ["manager", "proj-a"]
    assert page.js('document.querySelector("#agents .mgr .n").textContent') == "off"
    assert page.js('document.getElementById("text").placeholder') == "Message manager"
    time.sleep(2.5)  # a poll or two
    fetched = page.js('[...new Set(performance.getEntriesByType("resource").map(e => new URL(e.name).pathname))]')
    assert set(fetched) <= {"/api/me", "/api/feed", "/fonts/archivo-latin.woff2", "/favicon.ico"}, fetched


def test_manager_asleep_on_home(tmp_path, monkeypatch):
    """A manager Claude retired reads `asleep` on its Home line."""
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
    assert page.wait('[...document.querySelectorAll("#log .msg:not(.out) .text")].pop()?.textContent') == "may I have proj-b?"
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
    his page, the row says Requested, the admin says Yes under Waiting for you, and the
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
                admin.go(base + f"#t={atoken}&view=team")
                admin.wait('!document.getElementById("waiting").hidden')
                assert admin.js('document.getElementById("n-team").textContent') == "1"
                text = admin.js('document.getElementById("waiting-rows").textContent')
                assert text.startswith("bob wants to talk to carol's session")
                assert "a question about billing" in text
                admin.screenshot(shots / "admin-requests.png")
                click(admin, "#waiting-rows", "Yes")
                admin.wait('document.getElementById("waiting").hidden')
            page.wait(f'!!({billing}) && !{billing}.classList.contains("locked")')
            assert page.js('document.querySelector("#agents .others-head + .row .name").textContent') == \
                "codex session"  # carol's other session, its project hidden
            page.wait('!document.getElementById("compose").hidden')
            assert page.js('document.getElementById("text").placeholder') == "Message billing"
            page.js('document.getElementById("text").value = "hello carol"')
            page.js('document.querySelector("#compose button[type=submit]").click()')
            page.wait('[...document.querySelectorAll("#log .msg:not(.out) .text")].some(t => t.textContent === "hello carol")')
            assert page.js('document.getElementById("send-err").hidden')
            assert [m.text for m in store.inbox("k:c")][-1] == "hello carol"
            store.send("k:c", "bob", "hi bob")
            page.wait('[...document.querySelectorAll("#log .msg .text")].some(t => t.textContent === "hi bob")')
            page.screenshot(shots / "approved-desktop.png")
    finally:
        stop()
        store.close()




def team_row(name):
    return '[...document.querySelectorAll("#people-rows .person")].find(r => r.dataset.person === %s)' % json.dumps(name)


def test_team_add_person_give_project_remove(admin_page):
    """A first-time admin: Add person, type a name, Add, copy the link in place; open the
    person, tick a project seen only on a session (it is registered on that tick) and untick
    it; Remove asks inline, holds through refreshes, and signs them out."""
    store, page, url = admin_page
    _, bob_t = person(store, "bob")
    store.heartbeat("box-1", [{"key": "k:b", "tool": "claude-code", "project": "proj-a"}],
                    principal=store.authenticate(bob_t), facts={"host": "box-1", "os_user": "u"})
    page.go(url)
    page.wait(f'!!{team_row("bob")}')
    assert page.js('document.getElementById("waiting").hidden')  # nothing waits: no box
    assert page.js(f'{team_row("alice")}.textContent') == "aliceyou"
    assert page.js(f'{team_row("alice")}.querySelector("button")') is None  # nothing to do on yourself
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
    assert page.js(f'{team_row("carol")}.querySelector(".p-act").textContent') == "Copy invite link"
    page.js('document.getElementById("add-done").click()')
    assert page.js('document.getElementById("add-box").hidden && !document.getElementById("add-person-btn").hidden')
    # bob: open, tick proj-a (only a session had it), Saved; it is registered and he may write
    page.js(f'{team_row("bob")}.querySelector(".p-main").click()')
    page.wait(f'!!{team_row("bob")}.querySelector(".p-projects")')
    assert page.js(f'[...{team_row("bob")}.querySelectorAll(".proj")].map(l => l.textContent)') == ["proj-a"]
    page.js(f'{team_row("bob")}.querySelector(".proj input").click()')
    page.wait(f'{team_row("bob")}.querySelector(".proj small")?.textContent === "Saved"')
    bob = store.person_by_name("bob")["id"]
    assert store.project_role("proj-a", bob) == "write"
    assert page.wait(f'{team_row("bob")}.querySelector(".p-sum").textContent') == "proj-a"
    page.js(f'{team_row("bob")}.querySelector(".proj input").click()')
    end = time.monotonic() + 10
    while store.project_role("proj-a", bob) is not None:
        assert time.monotonic() < end, "untick did not take the project away"
        time.sleep(0.1)
    page.wait(f'!{team_row("bob")}.querySelector(".proj input").checked')
    # Remove: the question holds through refreshes; Esc cancels; Remove removes
    click(page, "#people-rows [data-person=bob]", "Remove")
    assert page.wait('document.querySelector("#people-rows .confirm")?.textContent').startswith(
        "Remove bob? They are signed out everywhere.")
    time.sleep(4.5)
    assert page.js('!!document.querySelector("#people-rows .confirm")')
    page.js('document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" }))')
    page.wait('!document.querySelector("#people-rows .confirm")')
    click(page, "#people-rows [data-person=bob]", "Remove")
    page.wait('!!document.querySelector("#people-rows .confirm")')
    click(page, "#people-rows .confirm", "Remove")
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
    """A message to the manager while it sleeps: "waking the manager…" under it."""
    import os
    shots = Path(os.environ.get("KAPTYORKA_SHOTS") or tmp_path)
    monkeypatch.setenv("KAPTYORKA_HOME", str(tmp_path / "kh"))
    store = Store(tmp_path / "hub.db")
    alice, token = person(store, "alice", "admin")
    mtoken = ManagerStore(store).ensure_account("box-1", "alice")
    store.heartbeat("box-1", [{"key": "k:mgr", "tool": "claude-code", "project": "manager",
                               "label": "manager"}], principal=store.authenticate(mtoken),
                    facts={"host": "box-1", "os_user": "u"})
    with store.lock:  # its session stopped after an idle hour: asleep
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
            assert page.wait('document.querySelector("#agents .mgr .n")?.textContent') == "asleep"
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
