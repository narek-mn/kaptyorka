import importlib.resources
import re

from kaptyorka import http


def page(hub) -> str:
    status, html = http.request("GET", hub.url + "/", "")
    assert status == 200
    return html


def test_page_served_without_token(hub):
    s, html = http.request("GET", hub.url + "/", "")
    assert s == 200 and all(v in html for v in ['id="team"', 'id="messages"', 'id="notes"'])


def test_page_has_no_external_urls(hub):
    assert re.findall(r'(?:src|href|url\()=?"?https?://', page(hub)) == []


def test_page_is_packaged():
    assert (importlib.resources.files("kaptyorka") / "web/index.html").is_file()


def test_notes_show_project_state_and_git_activity(hub):
    html = page(hub)
    assert '"/api/projects/"' in html and '"/activity"' in html
    assert html.index('id="project-info"') < html.index('id="notes-grid"')  # above the topics
    assert 'id="project-info"' in html and "hidden" in re.search(r'<div[^>]*id="project-info"[^>]*>', html).group(0)
    assert '"State"' in html and '"Recent git activity"' in html
    assert '"No recent activity"' in html and "No commits in the last" not in html
    # the state note is text nodes only: headings in bold, no Markdown library, no innerHTML
    assert "innerHTML" not in html and "marked" not in html
    assert re.search(r'charAt\(0\) === "#"', html)
    assert "\\u00b7" in html or "·" in html


def test_activity_branch_lookup_is_own_property(hub):
    # a branch named "constructor" or "toString" must not pick up Object.prototype
    html = page(hub)
    assert "Object.prototype.hasOwnProperty.call(d.branches, g.branch)" in html
    assert "d.branches[g.branch]" in html.split("hasOwnProperty.call(d.branches, g.branch) ?")[1].split(";")[0]
    assert html.count("d.branches[") == 1


def test_page_has_rules_tab(hub):
    html = page(hub)
    assert 'data-view="rules"' in html and 'id="rules"' in html and 'id="i-rules"' in html
    assert re.search(r'VIEWS = \[[^\]]*"rules"', html)
    # between Notes and Machines, visible to everyone
    assert html.index('data-view="notes"') < html.index('data-view="rules"') < html.index('data-view="machines"')
    assert "admin-only" not in re.search(r'<button[^>]*data-view="rules"[^>]*>', html).group(0)
    assert '"/api/rules"' in html and '"/api/proposals"' in html and '"/api/conflicts"' in html
    assert '"/api/proposals/"' in html and 'id="n-rules"' in html
    code = html[html.index("function loadRules("):html.index("function busy(")]
    assert not re.search(r"\b(prompt|alert|confirm)\(", code)  # reject reason is an inline input
    assert "innerHTML" not in code and 'placeholder: "Reason"' in code
