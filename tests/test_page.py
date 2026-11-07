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
    assert html.count("R rules · H hooks · S skills: what each session's tool has loaded") == 1
    assert 'id="team-key"' in html
    assert '$("team-key").hidden = !isAdmin() || !agents.length;' in html


def test_phone_layouts(hub):
    html = page(hub)
    phone = html[html.index("@media (max-width: 720px)"):html.index("@media (prefers-reduced-motion")]
    assert "min-width: 560px" not in html  # admin tables are cards on a phone, not cut off
    # tabs: a bottom bar with names under the icons; past five, More opens a menu of the rest
    assert "nav { position: fixed; left: 0; right: 0; bottom: 0;" in phone and ".tab .label { display: none; }" not in phone
    assert "nav .tab.overflow, nav .sep { display: none; }" in phone and "#view-title { display: block;" in phone
    assert 'var PHONE_BAR = ["team", "messages", "notes", "machines"];' in html
    assert 'id="more" type="button" aria-haspopup="menu"' in html and 'id="more-menu" role="menu"' in html
    assert '$("more-label").textContent = cur ? cur.title : "More";' in html
    assert 'n && !n.hidden ? el("span", { "class": "count", text: n.textContent }) : null' in html
    assert "/* a click outside closes the menu */" in html and "mask-image" not in phone
    assert "#people td.c-act, #access td.c-act, #settings td.c-act { display: flex; flex-wrap: wrap;" in phone
    assert "#audit td.l-what" in phone
    assert ".stack td.nil { display: none; }" in phone and '"sw-label", text: "Auto-update"' in html
    assert 'id="chips-toggle"' in html and ".chips.folded { display: none; }" in phone
    # the admin's door controls are behind the same fold, so its label names them
    assert '(withDoors ? "Filter and doors" : "Filter")' in html
    assert '"Filter: " + on.join(", ") + (withDoors ? ", doors" : "")' in html


def test_log_rows_are_sentences(hub):
    html = page(hub)
    code = html[html.index("function renderLog("):html.index("function loadLog(")]
    assert "r.sentence" in code and "timeEl(r.time)" in code
    assert "<th>When</th><th>Person</th><th>What</th></tr>" in html and "<th>Detail</th>" not in html


def test_shared_token_revoke_uses_plain_label(hub):
    html = page(hub)
    assert '"Revoke " + d.name + "? It stops working at once."' in html


def test_compose_attach_control(hub):
    html = page(hub)
    form = re.search(r'<form[^>]*id="compose".*?</form>', html, re.S).group(0)
    assert 'id="attach"' in form and 'aria-label="Attach"' in form and "#i-clip" in form
    assert '<input type="file" id="file-in" hidden>' in form and 'id="file-chip"' in form
    code = html[html.index("function upload("):html.index("/* notes: the team board")]
    assert '"X-File-Name": encodeURIComponent(' in code and 'fetch("/api/files"' in code
    assert "file_id: " in code


def test_doors_close_open_and_hint(hub):
    html = page(hub)
    assert '"Close the door between " + d.a_person + " and " + d.b_person + "? Their sessions stop talking directly."' in html
    assert '"/api/doors/" + d.id + "/close"' in html and '"/api/doors", { a_person:' in html
    assert '"Open door"' in html and '"Open a door between"' in html
    assert "A door lets two people's sessions talk directly, without the admin." in html
    assert '"kaptyorka.door-hint"' in html


def test_notes_delete_undo_restore(hub):
    html = page(hub)
    assert '"Delete " + n.topic + "?' in html and '"/api/notes?topic="' in html
    assert '"/api/notes/undo"' in html and '"Deleted " + ' in html and '"Undo"' in html
    assert '"This note was deleted."' in html
    assert html.count('"Restore this version"') == 2  # a note's old version, the rules' old version
    # after Delete and after Restore the feed is fetched at once, not at the next poll
    assert 'poll();  /* the topic list drops it now, not at the next tick */' in html
    assert "note.version = 0; seen.notes = null; renderNotes(true); poll(); })" in html


def test_rules_history_and_proposal_form(hub):
    html = page(hub)
    assert '"/api/rules/history"' in html and '"History " + ' in html
    form = re.search(r'<form[^>]*id="propose".*?</form>', html, re.S).group(0)
    assert 'aria-label="The rule"' in form and 'maxlength="2000"' in form
    assert 'aria-label="Why (what happened)"' in form and 'maxlength="4000"' in form
    assert ">Send<" in form and ">Cancel<" in form
    assert "Propose a rule" in html and 'act("prop-err", "/api/proposals", { text:' in html
    assert "No proposals yet. Propose a rule when your agent keeps getting the same correction." in html


def test_machine_cards_name_missing_tools(hub):
    assert '"No tools reported yet"' in page(hub)


def test_page_never_uses_browser_dialogs(hub):
    script = page(hub).split("<script>", 1)[1]
    assert not re.search(r"(?<![\w.])(prompt|alert|confirm)\(", script)
    assert not re.search(r"window\.(prompt|alert|confirm)\b", script)


def test_log_kind_filter_and_load_more(hub):
    html = page(hub)
    audit = re.search(r'<section id="audit".*?</section>', html, re.S).group(0)
    assert 'id="log-person"' in audit and 'id="log-kind" aria-label="Kind"' in audit
    assert 'id="log-more"' in audit and ">Load more<" in audit
    assert 'id="retention"' not in audit and "Keep messages" not in audit
    assert '"/api/log/kinds"' in html and '[["", "Everything"]]' in html
    assert '"&kind=" + encodeURIComponent(logKind)' in html and '"&before=" + ' in html


def test_settings_tab(hub):
    html = page(hub)
    assert '<symbol id="i-gear"' in html
    nav = re.search(r"<nav.*?</nav>", html, re.S).group(0)
    tabs = re.findall(r'data-view="(\w+)"', nav)
    assert tabs[-1] == "settings"
    for view, title in re.findall(r'data-view="(\w+)" title="([^"]+)"', nav):
        assert f'data-view="{view}" title="{title}" aria-label="{title}"' in nav
    assert re.search(r'<button class="tab admin-only" role="tab" data-view="settings" title="Settings" aria-label="Settings" hidden>'
                     r'<svg[^>]*><use href="#i-gear"/>', nav)
    sec = re.search(r'<section id="settings".*?</section>', html, re.S).group(0)
    assert 'class="admin"' in sec
    assert 'id="retention"' in sec and 'id="ret-days"' in sec
    assert "Keep messages for" in sec and "<span>days.</span>" in sec
    assert "Older messages and their files are deleted every night." in sec
    assert ">Manager<" in sec and 'id="legacy-panel"' in sec
    assert "Machines that joined before personal tokens" in sec
    assert '"The manager session is " + (on ? "on" : "off")' in html
    assert "Turn it on with " in html and "kaptyorka start --manager" in html
    assert "Switch it off with " in html and "kaptyorka start --no-manager" in html
    assert " on the hub machine, or close the manager worker on Machines." in html
    assert '"/api/settings"' in html and "manager_enabled" in html
    people = re.search(r'<section id="people".*?</section>', html, re.S).group(0)
    assert "legacy" not in people and "Old machines" not in people
    assert 'ADMIN_VIEWS = ["people", "access", "audit", "settings"]' in html


def test_proposal_fields_have_visible_labels(hub):
    form = re.search(r'<form[^>]*id="propose".*?</form>', page(hub), re.S).group(0)
    assert '<label for="prop-text">' in form and '<label for="prop-why">' in form


def test_replacing_a_machine_owner_asks_first(hub):
    html = page(hub)
    assert '" the owner of " + m.host + " instead of " + m.owner + "?"' in html
    assert '"Leave " + m.host + " without an owner?"' in html
