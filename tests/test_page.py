import importlib.resources
import re

from kaptyorka import http


def page(hub) -> str:
    status, html = http.request("GET", hub.url + "/", "")
    assert status == 200
    return html


def test_page_served_without_token(hub):
    s, html = http.request("GET", hub.url + "/", "")
    assert s == 200 and all(v in html for v in ['id="home"', 'id="team"', 'id="messages"'])


def test_page_has_no_external_urls(hub):
    assert re.findall(r'(?:src|href|url\()=?"?https?://', page(hub)) == []


def test_page_is_packaged():
    assert (importlib.resources.files("kaptyorka") / "web/index.html").is_file()


def test_page_shows_manager_badge(hub):
    html = page(hub)
    assert 'a.role === "manager"' in html and "the team's manager session" in html
    # the manager is the last line on Home, with its state word from the feed
    assert 'mgr ? "up" : feed.manager || "off"' in html


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
    # the admin sees whose a line is (a member too for a hidden project's session) and an
    # Activity chat of every message; no "all" line
    assert "who: !a.mine && (isAdmin() || hidden(a)) ? a.person : null" in html and 'sessionRow("all"' not in html
    assert 'if (admin) list.push({ key: "activity", name: "Activity"' in html
    assert "isAdmin() ? harness(a.harness) : null" in html  # what the tool loaded, on hover
    assert '$("text").placeholder = "Message " + (e.name || "manager");' in html  # whom you write to
    # messages in full, grouped by sender, a line per day; very long ones fold at 12 lines
    assert 'function notice(m) { return m.kind !== "message" || m.sender === "hub"; }' in html
    assert '(showNotices ? "hide notices" : "show notices") + " (" + notices + ")"' in html
    assert 'el("b", { "class": p[0] === ME ? "mine" : null, text: senderName(m) })' in html
    assert "-webkit-line-clamp: 12" in html and '"Show more"' in html and "dayLabel(m.created)" in html
    assert "overflow-wrap: anywhere" in html


def test_home_warns_only_what_blocks(hub):
    html = page(hub)
    assert 'var rows = (isAdmin() ? fleet.warnings : feed.warnings || []).filter(function (w) { return w.blocks; });' in html
    assert 'id="warns"' in html and 'id="warn-banner"' not in html


def test_member_page_makes_no_admin_fetch(hub):
    html = page(hub)
    for fn in ("loadWarnings", "loadProposals", "loadInvites"):
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
    # Home stacks: the list, or one chat
    assert "#home { display: flex; flex-direction: column;" in phone
    assert "#home.open .side, #home:not(.open) .chat { display: none; }" in phone
    # Team: a waiting line puts Yes and No under its words
    assert ".w-text { flex-basis: 100%;" in phone and ".team { padding: 18px 16px 32px; }" in phone


def test_compose_attach_control(hub):
    html = page(hub)
    form = re.search(r'<form[^>]*id="compose".*?</form>', html, re.S).group(0)
    assert 'id="attach"' in form and 'aria-label="Attach"' in form and "#i-clip" in form
    assert '<input type="file" id="file-in" hidden>' in form and 'id="file-chip"' in form
    start = html.index("function upload(")
    code = html[start:html.index("\n  }\n", html.index("function deliver(", start))]
    assert '"X-File-Name": encodeURIComponent(' in code and 'fetch("/api/files"' in code
    assert "file_id: " in code


def test_page_never_uses_browser_dialogs(hub):
    script = page(hub).split("<script>", 1)[1]
    assert not re.search(r"(?<![\w.])(prompt|alert|confirm)\(", script)
    assert not re.search(r"window\.(prompt|alert|confirm)\b", script)


def test_background_sessions_fold_into_one_group(hub):
    """Background jobs leave the main list for one Background group, closed at first, kept per
    viewer, open when the open chat is one; its header carries the unread count."""
    html = page(hub)
    code = html[html.index("function renderSessions()"):html.index("function lead(e)")]
    assert '"class": "bg-head", "aria-expanded"' in code and '"Background"' in code
    assert 's.setItem("kaptyorka.bg-open", bgOpen ? "1" : "0")' in code
    assert "var open = bgOpen || inBg;" in code and "unseen ?" in code
    assert 'store(function (s) { return s.getItem("kaptyorka.bg-open"); }) === "1"' in html
    assert '.list .bg-head[aria-expanded="true"] > svg { transform: rotate(90deg); }' in html
    assert '"class": "bg", text: "background"' not in html  # the clock says it in the row


def team_code(html):
    return html[html.index("/* Team: the admin's one screen."):html.index("/* redraw only the parts whose data changed")]


def test_team_is_one_screen(hub):
    """The admin's tab is Team: no sub-tabs, no More; Waiting for you, then the people. What
    left the page (devices, Revoke, roles, machines, doors, shared token, rules, notes, log,
    settings, workers) has no markup or code here any more."""
    html = page(hub)
    top = re.search(r'<nav role="tablist">.*?</nav>', html, re.S).group(0)
    assert re.findall(r'data-view="(\w+)"', top) == ["home", "team"]
    assert '>Team<span class="count" id="n-team" hidden>0</span>' in top
    assert 'ADMIN_VIEWS = ["team"]' in html and 'class="sub"' not in html
    team = re.search(r'<section id="team".*?</section>', html, re.S).group(0)
    assert team.index("Waiting for you") < team.index(">Team<") < team.index('id="people-rows"')
    assert ">Add person<" in team and "<select" not in team  # no role picker
    for gone in ('id="people"', 'id="access"', 'id="machines"', 'id="rules"', 'id="notes"', 'id="audit"',
                 'id="settings"', 'id="legacy-panel"', 'id="doors"', '"Revoke"', '"/api/legacy', '"/api/doors',
                 '"/api/log', '"/api/settings"', '"/api/workers', '"/auto_update"', '"/shared"', '"/owner"',
                 '"/api/machines', '"/api/rules', '"/api/notes', "Decided", "Shared token", "Machine use"):
        assert gone not in html, gone


def test_team_waiting_lines_and_answers(hub):
    code = team_code(page(hub))
    # requests (projects, machines, connections), knocks and open rule proposals; nothing else
    assert '(r.kind === "session" ? "/api/connects/" : "/api/requests/") + r.id' in code
    assert '"/api/knocks/" + k.id + "/answer", { answer: "yes" }' in code
    assert '"/api/proposals/" + p.id, { accept: true, reason: "" }' in code
    assert "conflict" not in code and "warnings.filter" not in code
    for words in ('"wants to talk to "', '"wants to use "', '"wants to work on "', '"wants to see "', '"suggests a rule"'):
        assert words in code, words
    # Yes gives what was asked, with no role picker and no reason box
    assert 'yes: function () { return api(path, { approve: true, role: r.role === "write" ? "write" : "read" }); }' in code
    assert 'no: function () { return api(path, { approve: false }); }' in code
    assert 'button("Yes", "primary"' in code and 'button("No", ""' in code
    assert '$("waiting").hidden = !items.length;' in code


def test_team_people_rows(hub):
    code = team_code(page(hub))
    # one action at most: Copy invite link before they join, Remove after; none on yourself
    assert '"hasn\'t joined yet"' in code and 'v.admin ? "you"' in code
    assert '"Copy invite link"' in code and 'button("Remove", "quiet-danger"' in code
    assert '"Remove " + v.name + "? They are signed out everywhere."' in code
    assert "return !isManagerAccount(p);" in code  # the manager account is not listed
    # a tick gives write; a project seen only on a session is registered on its first tick
    assert 'on ? { person: v.name, role: "write" } : { person: v.name }' in code
    assert 'api("/api/projects", { name: project })' in code
    assert 'flash("saved:" + k)' in code and 'text: "Saved"' in code
    # the page may run on plain http: the copy falls back to a hidden field
    assert 'document.execCommand("copy")' in code and "window.isSecureContext" in code


def test_old_admin_links_open_team(hub):
    html = page(hub)
    assert 'else if (params.get("view")) view = "team";' in html


def test_chat_shows_sending_sent_delivered_and_working(hub):
    """My message shows at once as Sending, then Sent, Delivered once the session has it (the
    message's read flag), or Not sent with Retry and Edit. Under it, while the session is busy
    (its activity) and has not answered, a working row; the manager being woken first.
    Thoughts are never shown: only the busy word and the session's own status text."""
    html = page(hub)
    assert 'outbox.push(o);' in html and 'o.state = "sending"' in html
    assert 'o.state = "sent"' in html and 'o.state = "failed"' in html
    assert '"Sending\\u2026"' in html and '"Delivered"' in html and '"Not sent"' in html
    assert 'text: "Retry"' in html and 'text: "Edit"' in html
    assert 'read: !!m.read' in html and 'mineLast.m.read ? "delivered" : "sent"' in html
    assert 'a.activity === "busy" ? "is working"' in html and 'more: a.status' in html
    assert '"waking the manager\\u2026"' in html
    # no moving dots for those who asked for less motion
    reduced = html[html.index("@media (prefers-reduced-motion: reduce)"):]
    assert ".working .dots, .tick .spin { display: none; }" in reduced[:400]
