"""The page's Machines tab: workers, machines, the admin's warnings and auto-update switch."""
import importlib.resources
import re

import pytest


@pytest.fixture(scope="module")
def html() -> str:
    return (importlib.resources.files("kaptyorka") / "web/index.html").read_text()


def test_machines_tab_between_notes_and_people(html):
    tabs = re.findall(r'class="tab[^"]*" role="tab" data-view="(\w+)"', html)
    assert tabs.index("machines") == tabs.index("notes") + 1
    assert tabs.index("people") == tabs.index("machines") + 1
    tab = re.search(r'<button class="([^"]*)" role="tab" data-view="machines"[^>]*>', html)
    assert "admin-only" not in tab.group(1) and "hidden" not in tab.group(0)
    assert 'id="machines" role="tabpanel"' in html
    assert re.search(r'VIEWS = \[[^\]]*"machines"', html)


def test_routes_referenced(html):
    for route in ('"/api/machines"', '"/api/workers"', '"/api/warnings"',
                  '"/api/workers/"', '"/close"', '"/adopt"', '"/auto_update"', '"/api/devices/"'):
        assert route in html, route


def test_all_worker_states_have_badges(html):
    states = re.search(r"var STATES = \[([^\]]*)\]", html).group(1)
    assert re.findall(r'"(\w+)"', states) == ["starting", "up", "unattended", "abandoned",
                                               "failed", "closed"]
    for s in ("starting", "up", "unattended", "abandoned", "failed", "closed"):
        assert f".state.{s}" in html, s


def test_warnings_banner_and_attach_code(html):
    banner = re.search(r'<div[^>]*id="warn-banner"[^>]*>', html).group(0)
    assert "admin-only" not in banner and "hidden" in banner  # shown by script, admin only
    assert html.index('id="warn-banner"') < html.index('<nav role="tablist">')
    assert "more" in html and "slice(0, 3)" in html
    assert 'el("code"' in html and "w.attach" in html


def test_auto_update_switch_sends_on(html):
    assert 'role: "switch"' in html or "role=\"switch\"" in html
    assert re.search(r'"/auto_update", \{ on: !', html)
