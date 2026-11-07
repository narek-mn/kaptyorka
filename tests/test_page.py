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


def test_page_shows_manager_badge(hub):
    html = page(hub)
    assert 'a.role === "manager"' in html and "the team's manager session" in html
    assert ".who .role" in html and "a.role]" in html  # the badge redraws when the role shows up
    assert 'w.manager ? el("span", { text: "manager" }) : bossEl' in html


def test_member_access_view_and_ask_form(hub):
    html = page(hub)
    assert 'id="my-access"' in html and ">My access<" in html and '"/api/me/access"' in html
    form = re.search(r'<form[^>]*id="ask-access".*?</form>', html, re.S).group(0)
    assert "Ask for" in form and 'id="ask-role"' in form and 'list="ask-names"' in form
    assert '<option value="project">access to project</option>' in form
    assert '<option value="machine">use of machine</option>' in form
    assert 'id="ask-why"' in form and "Send" in form
    assert 'api("/api/requests", { kind: kind' in html and 'id="ask-err"' in html
    assert '"Projects"' in html and '"Machines"' in html and '"Asked"' in html
    # only members get it, and it reloads with the poll while Team is open
    assert 'if (!me || isAdmin() || view !== "team") return;' in html
    assert 'if (v === "team") loadAccess();' in html


def test_team_second_line_and_admin_legend(hub):
    html = page(hub)
    assert '"class": "where", text: [a.person, a.machine, a.project].filter(Boolean).join(" \\u00b7 ")' in html
    assert "#team .who .where { display: block;" in html
    assert "isAdmin() ? harnessMarks(a.harness) : null" in html
    assert html.count("R rules text · H hooks · S skills") == 1 and 'id="team-key"' in html
    assert '$("team-key").hidden = !isAdmin() || !agents.length;' in html


def test_phone_layouts(hub):
    html = page(hub)
    phone = html[html.index("@media (max-width: 720px)"):html.index("@media (prefers-reduced-motion")]
    assert "min-width: 560px" not in html  # admin tables are cards on a phone, not cut off
    assert "nav.more-right" in phone and "mask-image" in phone and "function navFade()" in html
    assert "#people td.c-act, #access td.c-act { display: flex; flex-wrap: wrap;" in phone
    assert "#audit td.l-what" in phone
    assert ".stack td.nil { display: none; }" in phone and '"sw-label", text: "Auto-update"' in html
    assert 'id="chips-toggle"' in html and ".chips.folded { display: none; }" in phone


def test_log_rows_are_sentences(hub):
    html = page(hub)
    code = html[html.index("function renderLog("):html.index("function loadLog(")]
    assert "r.sentence" in code and "timeEl(r.time)" in code
    assert "<th>When</th><th>Person</th><th>What</th></tr>" in html and "<th>Detail</th>" not in html


def test_shared_token_revoke_uses_plain_label(hub):
    html = page(hub)
    assert '"Revoke " + d.name + "? It stops working at once."' in html
