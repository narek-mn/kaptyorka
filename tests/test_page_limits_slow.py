"""The page in a real headless Chrome: the manager row while its account is at its usage limit,
and the amber limit line on Machines (limits.py). Desktop and phone. Run with: pytest -m slow
tests/test_page_limits_slow.py. Screenshots go to KAPTYORKA_SHOTS (docs/screens/limits-*.png)."""
import json
import os
import time
from pathlib import Path

import pytest

import kaptyorka
from chrome import chrome, chrome_path
from conftest import start_hub
from helpers import person
from kaptyorka import identity, limits
from kaptyorka.manager import ManagerStore
from kaptyorka.store import Store

pytestmark = [pytest.mark.slow,
              pytest.mark.skipif(chrome_path() is None, reason="no Chrome or Chromium installed")]

SIZES = ((1440, 900, "desktop"), (390, 844, "phone"))
WIDE = "document.documentElement.scrollWidth <= document.documentElement.clientWidth"


@pytest.fixture
def team(tmp_path, monkeypatch):
    """alice (admin) with the hub's own account u on box-1, where the manager runs and which is
    at its Claude Code limit; a team machine jet-1 whose account u is at its Codex limit."""
    monkeypatch.setenv("KAPTYORKA_HOME", str(tmp_path / "kh"))
    monkeypatch.setattr(identity, "facts", lambda: {"host": "box-1", "os_user": "u",
                                                    "version": kaptyorka.__version__})
    store = Store(tmp_path / "hub.db")
    mm = ManagerStore(store)
    mm.set_enabled(True, "test")
    alice, _ = person(store, "alice", "admin")
    atoken, _ = store.invite_redeem(store.invite_create(alice), "box-1", host="box-1", os_user="u")
    store.heartbeat("box-1", [], principal=store.authenticate(atoken),
                    facts={"host": "box-1", "os_user": "u"})
    jet, _ = store.team_machine_add("jet-1", "alice")
    jtoken, _ = store.invite_redeem(store.invite_create(jet), "jet-1", host="jet-1", os_user="u")
    store.heartbeat("jet-1", [], principal=store.authenticate(jtoken),
                    facts={"host": "jet-1", "os_user": "u"})
    now = store.clock()
    resets = now + 2 * 3600 + 17 * 60
    limits.mark(store, "box-1", "u", "claude-code", resets, "session", now)
    limits.mark(store, "jet-1", "u", "codex", None, "reading", now)
    stop = start_hub(store, manager_url="http://127.0.0.1:9999")
    try:
        yield store, f"http://127.0.0.1:{stop.port}/#t={atoken}", resets
    finally:
        stop()
        store.close()


def shots(tmp_path):
    return Path(os.environ.get("KAPTYORKA_SHOTS") or tmp_path)


def test_manager_row_waits_for_its_reset(team, tmp_path):
    store, url, resets = team
    hm = limits.hm(resets)
    for w, h, tag in SIZES:
        with chrome(w, h) as page:
            page.go(url)
            row = 'document.querySelector("#agents [data-key=\\"mgr\\"]")'
            page.wait(f'!!{row} && {row}.textContent.includes("Usage limit")')
            text = page.js(f"{row}.innerText")
            assert f"limit resets {hm}" in text, text  # the chip says Usage limit: the time fits
            sub = f"{row}.querySelector(\".subline span\")"
            assert page.js(f"{sub}.scrollWidth <= {sub}.clientWidth"), "the reset time is cut off"
            assert "usage limit" in page.js(f"{row}.title")
            chip = page.js(f'{row}.querySelector(".chip").className')
            assert "warn" in chip, chip
            assert page.js(WIDE)
            page.screenshot(shots(tmp_path) / f"limits-manager-{tag}.png")


def test_machines_show_accounts_at_their_limit(team, tmp_path):
    store, url, resets = team
    hm = limits.hm(resets)
    for w, h, tag in SIZES:
        with chrome(w, h) as page:
            page.go(url + "&view=machines")
            page.wait('document.querySelectorAll("#a-machines .m-limit").length >= 2')
            lines = page.js('[...document.querySelectorAll("#a-machines .m-limit")].map(n => n.textContent)')
            assert sorted(lines) == sorted([f"u: at its Claude Code usage limit until {hm}",
                                            "u: at its Codex usage limit (reset time unknown)"]), lines
            assert page.js(WIDE)
            page.screenshot(shots(tmp_path) / f"limits-machines-{tag}.png")


MD = ("## Status: proj-a build\n\n- tests pass on box-1\n- **two** flaky ones left\n\n"
      "```\npython3 -m pytest tests -q\n```\n\n| step | minutes |\n|---|---:|\n| build | 4 |\n"
      "| tests | 11 |\n")


def test_everything_tonight_together(team, tmp_path):
    """The manager waiting, a session with the helper it started on the rail and a helper
    left by its ended starter, a request in Waiting for you, and a markdown chat in the reading
    column, all at once: each still renders and nothing scrolls sideways."""
    store, url, _ = team
    bob, _ = person(store, "bob")
    store.project_register("proj-a", by="alice")
    store.request_access(bob, "project", "proj-a", "to fix the login", role="write")
    principal = store.authenticate(url.split("#t=")[1])
    sessions = [{"key": "p1", "tool": "claude-code", "project": "proj-a", "label": "api-dev",
                 "activity": "idle"},
                {"key": "c1", "tool": "claude-code", "project": "proj-a", "label": "helper-tests",
                 "activity": "busy", "background": True, "parent": "p1", "helper": True},
                {"key": "c2", "tool": "claude-code", "project": "proj-a", "label": "helper-old",
                 "activity": "idle", "parent": "gone", "helper": True, "parent_gone": True}]
    real = store.clock
    store.heartbeat("box-1", sessions, principal=principal, facts={"host": "box-1", "os_user": "u"})
    store.clock = lambda: real() + left_s()  # the helpers are past their first two minutes
    store.heartbeat("box-1", sessions, principal=principal, facts={"host": "box-1", "os_user": "u"})
    store.send_from_web("alice", store.agent("p1").name, MD, store.authenticate(url.split("#t=")[1]).person_id)
    for w, h, tag in SIZES:
        with chrome(w, h) as page:
            page.go(url)
            row = '[...document.querySelectorAll("#agents .row")].find(r => r.querySelector(".name")?.textContent === %s)'
            page.wait(f'!!({row % json.dumps("helper-tests")}) && !!({row % json.dumps("helper-old")})')
            assert page.js(f'({row % json.dumps("helper-tests")}).classList.contains("wt-sub")')
            assert page.js(f'({row % json.dumps("helper-old")}).innerText').count("left by another session") == 1
            assert "Usage limit" in page.js('document.querySelector("#agents [data-key=\\"mgr\\"]").innerText')
            page.js(f'({row % json.dumps("api-dev")}).click()')
            page.wait('!!document.querySelector("#log .md table")')
            assert page.js('!!document.querySelector("#log .md-pre pre code")')
            assert page.js(WIDE)
            page.screenshot(shots(tmp_path) / f"limits-together-{tag}.png")
            page.go(url + "&view=team")
            page.wait('document.querySelectorAll("#waiting-rows .w-row").length === 1')
            assert page.js(WIDE)


def left_s():
    from kaptyorka import left
    return left.FRESH_S + 1
