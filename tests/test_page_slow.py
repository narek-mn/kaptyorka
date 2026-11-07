"""The admin's page in a real headless Chrome: an inline confirm survives the 2 s refresh, and
adding a person shows their invite link at once. Run with: pytest -m slow tests/test_page_slow.py"""
import json
import time

import pytest

from chrome import chrome, chrome_path
from conftest import start_hub
from helpers import person
from kaptyorka.manager import ManagerStore
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
    # and Remove in the confirm removes
    click_in_row(page, "bob", "Remove")
    page.wait('!!document.querySelector("#people .confirm")')
    page.js('[...document.querySelectorAll("#people .confirm button")].find(b => b.textContent === "Remove").click()')
    page.wait('![...document.querySelectorAll("#people-rows b")].some(b => b.textContent === "bob")')
    assert store.person_by_name("bob") is None


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
