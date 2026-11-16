"""The Admin > Machines section: workers, machines, warnings and the auto-update switch."""
import importlib.resources
import re

import pytest


@pytest.fixture(scope="module")
def html() -> str:
    return (importlib.resources.files("kaptyorka") / "web/index.html").read_text()


def test_machines_section_under_admin(html):
    sub = re.search(r'<nav class="sub".*?</nav>', html, re.S).group(0)
    views = re.findall(r'data-view="(\w+)"', sub)
    assert views.index("machines") == views.index("access") + 1 == views.index("people") + 2
    assert 'id="machines" role="tabpanel"' in html
    assert re.search(r'ADMIN_VIEWS = \[[^\]]*"machines"', html)
    # members have no Machines view: the fleet is fetched for the admin only
    assert "if (!isAdmin() || (!force && view !== \"machines\"" in html


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


def test_warnings_panel_and_attach_code(html):
    machines = re.search(r'<section id="machines".*?</section>', html, re.S).group(0)
    assert machines.index('id="warn-panel"') < machines.index('id="worker-rows"')  # every warning, first
    assert 'id="warns"' in re.search(r'<section id="home".*?</section>', html, re.S).group(0)  # Home: only blocking
    assert 'el("code"' in html and "w.attach" in html


def test_auto_update_switch_sends_on(html):
    assert 'role: "switch"' in html or "role=\"switch\"" in html
    assert re.search(r'"/auto_update", \{ on: !', html)


def test_machines_empty_state_is_short(html):
    assert '? "No machines yet" : "No machines yet. Run kaptyorka join with your invite link." })]));' in html
    assert '"No workers", title: "An agent starts one with work(project, task)"' in html
