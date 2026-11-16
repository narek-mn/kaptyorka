import importlib.resources
import re

from kaptyorka import http


def page(hub) -> str:
    status, html = http.request("GET", hub.url + "/", "")
    assert status == 200
    return html


def test_page_served_without_token(hub):
    s, html = http.request("GET", hub.url + "/", "")
    assert s == 200 and all(v in html for v in ['id="home"', 'id="admin"', 'id="messages"', 'id="notes"'])


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
    assert 'data-view="rules"' in html and 'id="rules"' in html
    # an Admin section: between Machines and Notes, and fetched only for the admin
    assert re.search(r'ADMIN_VIEWS = \[[^\]]*"rules"', html)
    assert html.index('data-view="machines"') < html.index('data-view="rules"') < html.index('data-view="notes"')
    assert "if (!isAdmin() || (!force && view !== \"rules\"" in html
    assert '"/api/rules"' in html and '"/api/proposals"' in html and '"/api/conflicts"' in html
    assert '"/api/proposals/"' in html and 'id="n-rules"' in html
    code = html[html.index("function loadRules("):html.index("function busy(")]
    assert not re.search(r"\b(prompt|alert|confirm)\(", code)  # reject reason is an inline input
    assert "innerHTML" not in code and 'placeholder: "Reason"' in code


def test_page_shows_manager_badge(hub):
    html = page(hub)
    assert 'a.role === "manager"' in html and "the team's manager session" in html
    # the manager is the last line on Home, with its state word from the feed
    assert 'mgr ? "up" : feed.manager || "off"' in html
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
    # only members get it, behind one "Ask for access" link on Home, fetched while it is open
    assert 'if (!me || isAdmin() || view !== "home" || !askOpen) return;' in html
    assert 'id="ask-link"' in html and ">Ask for access<" in html
    assert '$("ask-link").hidden = m.role === "admin";' in html


def test_home_two_pane_chat(hub):
    html = page(hub)
    home = re.search(r'<section id="home".*?\n    </section>', html, re.S).group(0)
    assert 'id="agents"' in home and 'id="log"' in home and 'id="compose"' in home
    assert 'id="to"' not in home and "<select" not in home.split('id="compose"')[1]  # no select: a click sets the target
    assert 'placeholder="Message manager"' in home
    assert 'id="chat-back"' in home and 'id="chat-name"' in home  # the chat's header; Back on a phone
    # detail on hover: the full name, person, folder, tool and doing
    assert "var tip = [a.label ? a.name : null, a.mine ? null : a.person, a.project, a.tool, a.status," in html
    # the admin sees whose a line is and an Activity chat of every message; no "all" line
    assert "who: isAdmin() && !a.mine ? a.person : null" in html and 'sessionRow("all"' not in html
    assert 'if (admin) list.push({ key: "activity", name: "Activity"' in html
    assert "isAdmin() ? harness(a.harness) : null" in html  # what the tool loaded, on hover
    assert '$("text").placeholder = "Message " + (e.name || "manager");' in html  # whom you write to
    # messages in full, grouped by sender, a line per day; very long ones fold at 12 lines
    assert 'function notice(m) { return m.kind !== "message" || m.sender === "hub"; }' in html
    assert '(showNotices ? "hide notices" : "show notices") + " (" + notices + ")"' in html
    assert 'el("b", { "class": fromMe(m) ? "mine" : null, text: senderName(m) })' in html
    assert "-webkit-line-clamp: 12" in html and '"Show more"' in html and "dayLabel(m.created)" in html
    assert "overflow-wrap: anywhere" in html


def test_home_warns_only_what_blocks(hub):
    html = page(hub)
    assert 'var rows = (isAdmin() ? fleet.warnings : feed.warnings || []).filter(function (w) { return w.blocks; });' in html
    assert 'id="warns"' in html and 'id="warn-banner"' not in html


def test_member_page_makes_no_admin_fetch(hub):
    html = page(hub)
    for fn in ("loadFleet", "loadRules", "loadSettings", "loadInvites", "loadKinds"):
        body = html[html.index(f"function {fn}("):]
        assert "isAdmin()" in body[:body.index("\n  }")], fn


def test_fonts_served_open_and_nothing_else(hub):
    import urllib.request
    with urllib.request.urlopen(hub.url + "/fonts/archivo-latin.woff2") as r:
        assert r.status == 200 and r.headers["Content-Type"] == "font/woff2"
        assert r.read()[:4] == b"wOF2"
    for bad in ("/fonts/OFL.txt", "/fonts/..%2Fweb%2Findex.html", "/fonts/%2e%2e%2fhub.db", "/fonts/nope.woff2"):
        assert http.request("GET", hub.url + bad, "")[0] == 404, bad
    assert (importlib.resources.files("kaptyorka") / "web/fonts/OFL.txt").is_file()  # the license ships too


def test_page_csp_allows_own_font(hub):
    import urllib.request
    with urllib.request.urlopen(hub.url + "/") as r:
        csp = r.headers["Content-Security-Policy"]
    assert "font-src 'self'" in csp and "default-src 'none'" in csp
    assert "url(/fonts/archivo-latin.woff2)" in page(hub)


def test_feed_carries_the_manager_state(hub):
    status, f = http.request("GET", hub.url + "/api/feed", hub.token)
    assert status == 200 and f["manager"] == "off"  # the hub fixture switches it off


def test_phone_layouts(hub):
    html = page(hub)
    phone = html[html.index("@media (max-width: 720px)"):]
    assert "min-width: 560px" not in html  # admin tables are cards on a phone, not cut off
    # Home stacks; the Admin sub-menu is one row that scrolls sideways
    assert "#home { display: flex; flex-direction: column;" in phone
    assert "#home.open .side, #home:not(.open) .chat { display: none; }" in phone  # the list, or one chat
    assert ".sub { flex: none; flex-direction: row; overflow-x: auto;" in phone
    assert "#people td.c-act, #access td.c-act, #settings td.c-act { display: flex; flex-wrap: wrap;" in phone
    assert "#audit td.l-what" in phone
    assert ".stack td.nil { display: none; }" in phone and '"sw-label", text: "Auto-update"' in html


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
    doors = html[html.index("function renderDoors("):html.index("function openDoor(")]
    assert "box.hidden = !feed.doors.length" in doors and "isAdmin()" not in doors  # the admin's Access section only


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
    assert '"No proposals yet"' in html


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
    sub = re.search(r'<nav class="sub".*?</nav>', html, re.S).group(0)
    assert re.findall(r'data-view="(\w+)"', sub) == ["people", "access", "machines", "rules", "notes", "audit", "settings"]
    top = re.search(r'<nav role="tablist">.*?</nav>', html, re.S).group(0)
    assert re.findall(r'data-view="(\w+)"', top) == ["home", "admin"]
    assert re.search(r'<button class="tab admin-only" role="tab" data-view="admin" id="tab-admin" hidden>', top)
    sec = re.search(r'<section id="settings".*?</section>', html, re.S).group(0)
    assert 'class="admin"' in sec
    assert 'id="retention"' in sec and 'id="ret-days"' in sec
    assert "Keep messages for" in sec and "<span>days</span>" in sec
    assert 'title="Older messages and their files are deleted every night."' in sec
    assert ">Manager<" in sec and 'id="legacy-panel"' not in sec
    access = re.search(r'<section id="access".*?</section>', html, re.S).group(0)
    assert 'id="legacy-panel"' in access and 'title="Machines that joined before personal tokens">Shared token' in access
    assert "settings.manager_state" in html and "settings.manager_reason" in html
    assert '"The manager session is starting."' in html and '"The manager session is running."' in html
    assert '"The manager session is on but cannot start: " + why + "."' in html
    assert '"The manager session is off."' in html
    assert '"Last start failed: " + settings.manager_error' in html
    assert "Turn it on with " in html and "kaptyorka start --manager" in html
    assert "Switch it off with " in html and "kaptyorka start --no-manager" in html
    assert ", or close the manager worker on Machines." in html
    assert '"/api/settings"' in html and "manager_state" in html
    people = re.search(r'<section id="people".*?</section>', html, re.S).group(0)
    assert "legacy" not in people and "Old machines" not in people
    assert 'ADMIN_VIEWS = ["people", "access", "machines", "rules", "notes", "audit", "settings"]' in html


def test_proposal_fields_have_visible_labels(hub):
    form = re.search(r'<form[^>]*id="propose".*?</form>', page(hub), re.S).group(0)
    assert '<label for="prop-text">' in form and '<label for="prop-why">' in form


def test_replacing_a_machine_owner_asks_first(hub):
    html = page(hub)
    assert '" the owner of " + m.host + " instead of " + m.owner + "?"' in html
    assert '"Leave " + m.host + " without an owner?"' in html


def test_machines_show_which_tools_are_logged_in(hub):
    html = page(hub)
    assert "logins = f.logins || {}" in html and "logins[t] === false" in html
    assert '"not logged in"' in html and '"class": "login-off"' in html
    assert '"No agent tool is logged in on this machine. Run "' in html and '" there."' in html
    # one logged-in tool is enough: the others' dots go grey and no help line shows
    assert 'return logins[t] === true;' in html and '(anyIn ? " quiet" : "")' in html
    assert "return !anyIn && logins[t] === false" in html and ".login-off.quiet" in html
    for name, cmd in [("Claude Code", "claude auth login"), ("Codex", "codex login"),
                      ("OpenCode", "opencode auth login")]:
        assert f'["{name}", "{cmd}"]' in html
