import importlib.resources
import re

from kaptyorka import http


def page(hub) -> str:
    status, html = http.request("GET", hub.url + "/", "")
    assert status == 200
    return html


def test_page_served_without_token(hub):
    s, html = http.request("GET", hub.url + "/", "")
    assert s == 200 and all(v in html for v in ['id="home"', 'id="settings"', 'id="messages"'])


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


def test_no_function_name_is_declared_twice(hub):
    """A second declaration silently replaces the first (both hoist): two `tick`s once made every
    poll throw, so the live dot said the hub was unreachable."""
    import collections
    import re

    names = collections.Counter(re.findall(r"\bfunction ([A-Za-z_$][\w$]*)\(", page(hub)))
    assert [n for n, c in names.items() if c > 1] == []


def test_member_page_makes_no_admin_fetch(hub):
    html = page(hub)
    for fn in ("loadWarnings", "loadProposals", "loadGitlab"):
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
    assert ".w-text { flex-basis: 100%;" in phone and ".set { grid-template-columns: minmax(0, 1fr);" in phone


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


def test_settings_split_account_and_team(hub):
    """The top bar keeps Home; the account button opens a menu (Account settings, Team settings
    for an admin, Sign out). Settings is one area with a sidebar: Your account (Profile,
    Machines, Signed-in browsers) and Team (People with the waiting count, Projects), each at
    its own address. One Machines page: an admin switches Mine / Everyone there, and the old
    All machines address lands on it. What left the page long ago stays gone."""
    html = page(hub)
    top = re.search(r'<nav role="tablist">.*?</nav>', html, re.S).group(0)
    assert re.findall(r'data-view="(\w+)"', top) == ["home"]
    menu = re.search(r'<div class="menu" id="acct-menu".*?id="signout"[^\n]*', html, re.S).group(0)
    assert 'role="menu"' in menu and 'aria-haspopup="menu"' in html
    assert re.findall(r'data-go="([a-z/]+)"', menu) == ["account/profile", "team/people"]
    assert ">Account settings<" in menu and ">Team settings<" in menu and 'id="signout"' in menu
    nav = re.search(r'<nav class="set-nav".*?</nav>', html, re.S).group(0)
    assert re.findall(r'href="#settings/([a-z/]+)"', nav) == [
        "account/profile", "account/machines", "account/browsers",
        "team/people", "team/projects"]
    assert 'id="pane-team-machines"' not in html and ">All machines<" not in html
    pane = re.search(r'id="pane-account-machines".*?id="machines-err"', html, re.S).group(0)
    assert 'id="m-scope"' in pane and ">Mine<" in pane and ">Everyone<" in pane
    assert 'id="add-team-btn"' in pane and 'id="team-machine-rows"' in pane
    assert '{ mScope = "everyone"; return ["settings", "account/machines"]; }' in html
    assert 'id="n-team"' in nav and 'id="n-acct"' in html  # the waiting count, twice in sight
    people = re.search(r'id="pane-team-people".*?id="team-err"', html, re.S).group(0)
    assert people.index("Waiting for you") < people.index('id="people-rows"')
    assert ">Add person<" in people and "<select" not in people  # no role picker
    assert 'return ["settings", "team/people"];' in html  # an old #view=team link
    assert 'if (v === "account") return ["settings", "account/profile"];' in html
    # the menu: arrows, Esc and a click outside
    assert 'e.key === "ArrowDown"' in html and 'e.key === "Escape"' in html
    assert '!$("acct-menu").contains(e.target)' in html
    for gone in ('id="people"', 'id="access"', 'id="rules"', 'id="notes"', 'id="audit"',
                 'id="legacy-panel"', 'id="doors"', '"Revoke"', '"/api/legacy', '"/api/doors',
                 '"/api/log', '"/api/settings"', '"/api/workers', '"/auto_update"', '"/shared"', '"/owner"',
                 '"/api/rules', '"/api/notes', "Decided", "Shared token", "Machine use"):
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
    # New invite link at any time, Remove once they joined; none on yourself
    assert '"hasn\'t joined yet"' in code and 'v.self ? "you"' in code
    assert '"New invite link"' in code and 'v.self ? null : button(' in code and 'button("Remove", "quiet-danger"' in code
    assert '"Remove " + v.name + "? They are signed out everywhere."' in code
    assert "return !isManagerAccount(p) && !p.team;" in code  # nor the manager account, nor team machines
    # a tick gives write; a project seen only on a session is registered on its first tick
    assert 'on ? { person: v.name, role: "write" } : { person: v.name }' in code
    assert 'api("/api/projects", { name: project })' in code
    assert 'flash("saved:" + k)' in code and 'text: "Saved"' in code
    # the page may run on plain http: the copy falls back to a hidden field
    assert 'document.execCommand("copy")' in code and "window.isSecureContext" in code


def test_team_machine_button_and_hub_label(hub):
    html = page(hub)
    code = team_code(html)
    assert 'button("Make team machine"' in code and "!v.self && !v.manager && !v.team && !v.linked" in code
    assert 'api(apiPath("/api/people/", v.name, "/team"))' in code and "teamErr(\"team-err\", e.message)" in code
    assert (" becomes a shared team machine: it leaves People, loses admin, and its sessions are"
            " visible to everyone with access to the project.") in code
    assert "confirm(" not in code
    assert 'd.emergency ? chip("This hub"' in html


def test_old_admin_links_open_team(hub):
    html = page(hub)
    assert 'return ["settings", "team/people"];' in html


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
