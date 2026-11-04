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
