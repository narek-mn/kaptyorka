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
    # the state note is text nodes only: headings in bold, no Markdown library, no innerHTML
    assert "innerHTML" not in html and "marked" not in html
    assert re.search(r'charAt\(0\) === "#"', html)
    assert "\\u00b7" in html or "·" in html
