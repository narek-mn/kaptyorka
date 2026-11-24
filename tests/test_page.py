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
    assert "var tip = [a.label || a.title ? a.name : null, a.mine ? null : a.person, a.project, a.tool, a.status," in html
    # the admin sees whose a line is (a member too for a hidden project's session) and an
    # Activity chat of every message; no "all" line
    assert "who: !a.mine && (isAdmin() || hidden(a)) ? a.person : null" in html and 'sessionRow("all"' not in html
    assert 'if (admin) list.push({ key: "activity", name: "Activity"' in html
    assert "isAdmin() ? harness(a.harness) : null" in html  # what the tool loaded, on hover
    assert '$("text").placeholder = "Message " + (e.name || "manager");' in html  # whom you write to
    # messages in full, grouped by sender, a line per day; very long ones fold at about 20 lines, never the newest
    assert 'function notice(m) { return m.kind !== "message" || m.sender === "hub"; }' in html
    assert '(showNotices ? "hide notices" : "show notices") + " (" + notices + ")"' in html
    assert 'el("b", { "class": p[0] === ME ? "mine" : null, text: senderName(m) })' in html
    assert ".msg .text.clamp { max-height: 32em;" in html and '"Show more"' in html and "dayLabel(m.created)" in html
    assert "if (m.id !== newest && " in html and "var text = md(m.text);" in html  # markdown, built from text nodes
    assert "overflow-wrap: anywhere" in html


def test_home_warns_only_what_blocks(hub):
    html = page(hub)
    assert 'var rows = (isAdmin() ? fleet.warnings : feed.warnings || []).filter(function (w) { return w.blocks; });' in html
    assert 'id="warns"' in html and 'id="warn-banner"' not in html
    # one line over the chat, only for its own machine; the rest behind the top bar's sign
    assert "var w = mine[0], fix = warnFix(w), more = rows.length - 1;" in html and 'id="warn-ind"' in html and 'id="warn-pop"' in html
    assert 'attrs.title = "A team machine: shared by the team" + (d.projects.length ?' in html
    assert "kaptyorka.warn-dismissed" in html and "#warns { flex: none; position: relative; z-index: 4; height: 0; }" in html  # no layout jump


def test_home_names_a_session_by_its_title(hub):
    html = page(hub)
    assert 'a.label || (hidden(a) ? a.tool + " session" : a.title || plain)' in html  # label, title, folder
    assert 'shortName(a.name) + " \\u00b7 "' in html  # the folder moves to the grey line


def test_empty_session_chat_says_where_the_typing_stays(hub):
    html = page(hub)
    assert "function emptyText(e)" in html and "stays on " in html
    assert '"Messages you send here show up here. What "' in html
    assert 'e.who, e.note]' in html  # the session's own status line in the chat header


def test_no_function_name_is_declared_twice(hub):
    """A second declaration silently replaces the first (both hoist): two `tick`s once made every
    poll throw, so the live dot said the hub was unreachable."""
    import collections
    import re

    names = collections.Counter(re.findall(r"\bfunction ([A-Za-z_$][\w$]*)\(", page(hub)))
    assert [n for n, c in names.items() if c > 1] == []


def test_member_page_makes_no_admin_fetch(hub):
    html = page(hub)
    # loadWarnings is everyone's: /api/warnings answers a member with their own only
    for fn in ("loadProposals", "loadGitlab"):
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
    assert '"class": "bg-head", "aria-expanded"' in code and 'foldGroup(box, "bg", ' in code
    assert 'name: "Background", tip: "Background jobs (claude --bg)"' in html
    assert 's.setItem(f.save, f.open ? "1" : "0")' in code
    assert "var open = f.open || inIt;" in code and "unseen ?" in code
    assert 'store(function (s) { return s.getItem("kaptyorka.bg-open"); }) === "1"' in html
    assert '.list .bg-head[aria-expanded="true"] > svg { transform: rotate(90deg); }' in html
    assert '"class": "bg", text: "background"' not in html  # the clock says it in the row


def test_idle_sessions_fold_at_the_end(hub):
    """A session idle over a day goes to one Idle group after Background, folded at first, kept
    per viewer; never the manager (its own entry), a worker or a background job."""
    html = page(hub)
    code = html[html.index("function renderSessions()"):html.index("function lead(e)")]
    assert code.index('foldGroup(box, "bg", ') < code.index('foldGroup(box, "idle", ')
    assert 'shown.filter(function (e) { return !e.bg && !e.idle; })' in code
    assert 'shown.filter(function (e) { return e.sess && !e.bg && e.idle; })' in code
    # a session still busy or waiting on a background task never folds away (a stuck one)
    assert 'var idle = a.worker || (a.activity && a.activity !== "idle") ? "" : idleText(a.idle_s);' \
        in html
    assert 'store(function (s) { return s.getItem("kaptyorka.idle-open"); }) === "1"' in html
    assert 'sub: (idle ? idle + " \\u00b7 " : "") + (a.title' in html  # first on the grey line


def team_code(html):
    return html[html.index("/* Team: the admin's one screen."):html.index("/* redraw only the parts whose data changed")]


def test_settings_one_flat_sidebar(hub):
    """The top bar keeps Home; the name button goes straight to Settings (no menu). Sign out is
    at the bottom of the Settings sidebar, and at the end of Profile on a phone. Settings is one area with one flat sidebar, no headings: Profile,
    Machines, Signed-in browsers, then People (with the waiting count) and Projects for an
    admin, each at its own address; the old #settings/account/... and #settings/team/...
    addresses still land there. One Machines page, with filter chips; the old All machines
    address lands on it with All. What left the page long ago stays gone."""
    html = page(hub)
    top = re.search(r'<nav role="tablist">.*?</nav>', html, re.S).group(0)
    assert re.findall(r'data-view="(\w+)"', top) == ["home"]
    btn = re.search(r'<button type="button" class="acct" id="viewer-name"[^\n]*', html).group(0)
    assert 'aria-label="Settings"' in btn and "aria-haspopup" not in btn and 'id="n-acct"' in btn
    assert 'id="acct-menu"' not in html and 'role="menu' not in html and "menuitem" not in html
    assert '$("viewer-name").addEventListener("click", function () { show("settings", true, "profile"); });' in html
    nav = re.search(r'<nav class="set-nav".*?</nav>', html, re.S).group(0)
    assert nav.index('class="set-out"') > nav.index('href="#settings/projects"') and 'id="signout"' in nav
    assert re.findall(r'href="#settings/([a-z/]+)"', nav) == ["profile", "machines", "browsers", "people", "projects"]
    assert "set-group" not in nav and "Your account" not in nav and ">Team<" not in nav
    for old, new in (("account/profile", "profile"), ("account/machines", "machines"), ("account/browsers", "browsers"),
                     ("team/people", "people"), ("team/projects", "projects")):
        assert f'"{old}": "{new}"' in html
    assert 'id="pane-team-machines"' not in html and ">All machines<" not in html
    pane = re.search(r'id="pane-machines".*?id="a-machines-err"', html, re.S).group(0)
    assert 'id="m-filter"' in pane and 'id="add-team-btn"' not in pane
    assert '{ mFilter = "all"; return ["settings", "machines"]; }' in html
    assert 'id="n-team"' in nav and 'id="n-acct"' in html  # the waiting count, twice in sight
    people = re.search(r'id="pane-people".*?id="team-err"', html, re.S).group(0)
    assert people.index("Waiting for you") < people.index('id="people-rows"')
    assert ">Add person<" in people and "<select" not in people  # no role picker
    assert 'return ["settings", "people"];' in html  # an old #view=team link
    assert 'if (v === "account") return ["settings", "profile"];' in html
    for gone in ('id="people"', 'id="access"', 'id="rules"', 'id="notes"', 'id="audit"',
                 'id="legacy-panel"', 'id="doors"', '"Revoke"', '"/api/legacy', '"/api/doors',
                 '"/api/log', '"/api/settings"', '"/api/workers', '"/auto_update"', '"/shared"', '"/owner"',
                 '"/api/rules', '"/api/notes', "Decided", "Shared token", "Machine use"):
        assert gone not in html, gone


def test_one_add_machine_button(hub):
    """One Add machine: an admin answers whose account it is (two options, a line each); a member
    goes straight to their own join command."""
    html = page(hub)
    pane = re.search(r'id="pane-machines".*?id="a-machines-err"', html, re.S).group(0)
    assert pane.count(">Add machine<") == 1 and "Add a machine" not in html and "Add team machine" not in html
    choice = re.search(r'id="add-choice".*?id="add-choice-cancel"', pane, re.S).group(0)
    assert ">My own account<" in choice and ">Shared team account<" in choice
    assert "Its sessions are yours." in choice and "Its sessions are visible to everyone in the project." in choice
    assert 'if (isAdmin()) { addPanels("choice"); $("add-own").focus(); return; }' in html
    assert '$("add-own").addEventListener("click", ownJoin);' in html


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


def test_team_people_rows_and_person_page(hub):
    code = team_code(page(hub))
    # a clean row (name, short status, chevron) opens the person's own page at its address
    assert '"hasn\'t joined yet"' in code and 'v.self ? "you"' in code
    assert 'href: "#settings/people/" + encodeURIComponent(v.name)' in code and "openPersonPage(v.name)" in code
    assert "return !isManagerAccount(p) && !p.team;" in code  # nor the manager account, nor team machines
    # the page: profile, projects, machines, then the actions
    for words in ('["Role", ', '["GitLab", ', '["Last seen", ', '"Changes save automatically"', '"Projects"', '"Machines"',
                  '"New invite link"', 'button("Make admin"', 'button("Remove admin"', 'button("Remove", "quiet-danger"'):
        assert words in code, words
    assert '"Remove " + v.name + "? They are signed out everywhere."' in code
    # a tick gives write; a project seen only on a session is registered on its first tick;
    # "Saved" for 2 s next to that box, an error inline next to it
    assert 'on ? { person: v.name, role: "write" } : { person: v.name }' in code
    assert 'api("/api/projects", { name: project })' in code
    assert 'flash("saved:" + k, 2000)' in code and 'text: "Saved ✓"' in code and "boxErr[k] = e.message" in code
    # a team machine's switch: on by default, off hides it and its sessions from them; none for admins
    assert 'api(apiPath("/api/people/", v.name, "/team-machines/" + encodeURIComponent(team)), { visible: on })' in code
    assert 'role: "switch"' in code and '"Turn a team machine off to hide it and its sessions from them."' in code
    assert "var sw = v.admin ? null :" in code and "shared accounts" not in code and "machine_access" not in code
    # the page may run on plain http: the copy falls back to a hidden field
    assert 'document.execCommand("copy")' in code and "window.isSecureContext" in code


def test_no_make_team_machine_button(hub):
    html = page(hub)
    assert "Make team machine" not in html and '"/team")' not in html
    assert 'd.emergency ? chip("This hub"' in html


def test_machines_grouped_with_filter_chips(hub):
    """Machines: no Mine / Everyone switch and no Give to…; four chips, one choice, kept per
    browser behind try/catch; one card per computer, something online first; inside, team,
    yours, then others; the empty sentences."""
    html = page(hub)
    pane = re.search(r'id="pane-machines".*?id="a-machines-err"', html, re.S).group(0)
    chips = re.search(r'id="m-filter".*?</div>', pane, re.S).group(0)
    assert re.findall(r'data-f="(\w+)"', chips) == ["all", "team", "mine", "online", "warnings"]
    assert re.findall(r'>(\w+)</button>', chips) == ["All", "Team", "Mine", "Online", "Warnings"]  # Warnings shows only with some
    assert 'id="m-scope"' not in html and 'id="team-machine-rows"' not in html and "mScope" not in html
    assert "Give to" not in html and '"/move"' not in html and "giveMachine" not in html
    code = html[html.index("function machineHosts()"):html.index("function renderAccount()")]
    assert 'store(function (s) { s.setItem("kaptyorka.machines-filter", f); });' in code
    assert 'store(function (s) { return s.getItem("kaptyorka.machines-filter"); })' in html
    assert "function store(fn) { try { return fn(window.localStorage); } catch (e) { return null; } }" in html
    assert "var rank = { team: 0, mine: 1, other: 2 };" in code
    assert "(b.on - a.on) || (a.h.host < b.h.host" in code
    assert "if (!isAdmin()) account.team_machines.forEach" in code  # a member: what the hub lets them use
    assert '"No machines online right now."' in code
    assert '"No machines yet. Click Add machine and run the command on your computer."' in code
    assert 'chip("Team", "active", "a-tag")' in code and 'chip("Mine", "", "a-tag")' in code


def test_one_name_for_a_machine(hub):
    html = page(hub)
    assert 'function machineName(d) { return d.label || (d.os_user && d.host ? d.os_user + "@" + d.host : d.name); }' in html
    assert "+ (a.where || a.machine)," in html and "sub: mgr ? mgr.where || mgr.machine" in html


def test_home_mine_and_everyone(hub):
    """An admin's Home switches Mine (what a member sees) and Everyone (a header per person and
    per team machine, folded but your own); the choice and the folds are kept per browser."""
    html = page(hub)
    side = re.search(r'<div class="side">.*?id="agents"', html, re.S).group(0)
    assert 'id="h-scope"' in side and "admin-only" in side and ">Mine<" in side and ">Everyone<" in side
    code = html[html.index("function homeAll()"):html.index("function lead(e) {")]
    assert "function inMine(e) { return !e.sess || e.mine || e.team; }" in code
    assert 'st.setItem("kaptyorka.home-scope", hScope)' in code and '"kaptyorka.home-groups"' in code
    assert "var open = groupOpen[g.key] != null ? !!groupOpen[g.key] : g.own || !!g.machine;" in code
    assert 'el("small", { text: String(g.rows.length) })' in code and "unseen ?" in code


def test_open_views_do_not_blink(hub):
    """A poll redraws a part only when its data changed: each part has its own key (the session
    list and the people list once shared one, so every poll redrew both), online things drop
    their ever-moving last seen, and a press on a list holds its redraw until the click lands."""
    html = page(hub)
    keys = re.findall(r'changed\("([a-z-]+)"', html)
    assert len(keys) == len(set(keys)), keys
    assert 'changed("sessions"' in html and 'changed("people"' in html and 'changed("person", calm(' in html
    assert 'changed("account", calm(' in html and 'changed("hosts", calm(' in html
    assert "function pressing(box)" in html and "if (pressing(box)) return;" in html


def test_old_admin_links_open_team(hub):
    html = page(hub)
    assert 'return ["settings", "people"];' in html


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
    assert 'read: !!m.read' in html and 'mineLast.m.read ? "delivered" : mineLast.m.failed ? "undelivered" : "sent"' in html
    assert 'a.activity === "busy" ? "is working"' in html and 'more: a.status' in html
    assert '"waking the manager\\u2026"' in html
    # no moving dots for those who asked for less motion
    reduced = html[html.index("@media (prefers-reduced-motion: reduce)"):]
    assert ".working .dots, .tick .spin { display: none; }" in reduced[:400]


def test_markdown_is_built_from_text_nodes(hub):
    """Message markdown never goes through HTML: the renderer makes elements and text nodes, and
    links only to http or https (test_page_reading_slow.py tries scripts and javascript: links)."""
    html = page(hub)
    code = html[html.index("var MD_LINK"):html.index("  function renderMessages(jump) {")]
    assert "innerHTML" not in code and "insertAdjacentHTML" not in code and "outerHTML" not in code
    assert "var MD_LINK = /^https?:\\/\\/[^\\s<>]+$/i;" in code and 'rel: "noopener noreferrer nofollow"' in code
    assert "innerHTML" not in html  # nowhere on the page
