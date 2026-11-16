# Private by Default and a Simple Page (0.9.0) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A developer sees and messages only their own sessions and the manager; reaching anyone else goes through the manager and only the admin opens that door. The page becomes two tabs (Home, and Admin for the admin only) in the team design tokens, with very little text. Login warnings appear only when a machine has no logged-in tool.

**Architecture:** Privacy is enforced in the hub (store.py, hub_feed.py, hub.py tools and routes), never only on the page. The page (src/kaptyorka/web/index.html, one file) is restyled with the team design tokens and the Archivo font served by the hub, and reorganised: Home (sessions + messages) for everyone, Admin (People, Access, Machines, Rules, Notes, Log, Settings) for the admin only.

**Tech Stack:** Python 3.10, Starlette, SQLite, one HTML page with plain JS. No new dependencies. Archivo (SIL OFL 1.1) woff2 file bundled with the package.

**Spec:** Narek, 2026-11-16, after seeing the current page ("very much text, nothing intuitive, I get lost") and two mockups (the second approved: "yes, build it"): "admin panel is only for admin account; assume this page is for all developers; another developer must not be able to see other devs' sessions in his account or even send messages; if needed he just speaks with the hub manager session and asks for permission; after, only the admin should give it." And: "codex login or opencode login are not warnings; the developer can use only one; one is enough." Style: the team's own design tokens (warm stone and charcoal, square corners, no shadows, coral as the one accent, Archivo, dark default; source path given in the dispatch brief, not in the repo). Approved mockup: .superpowers/mock/home2.html and home2.png (git-ignored).

Assumption stated to Narek (no objection yet): when the admin opens a door between two developers, they see and may message each other's sessions until it closes.

## Global Constraints

- Python 3.10 floor; no new dependencies; `python3 -m pytest tests -q` green (slow page tests `-m slow tests/test_page_slow.py` too, at the end).
- Privacy is a hub rule: every route and MCP tool applies it; the page only reflects it. No team names or real addresses in code, tests, fixtures, screenshots.
- `store.py`: touches limited to the visibility/messaging rules named below; `Store.lock` is not reentrant.
- Page text: few words, every key fact, detail on hover; no explaining sentences; everyday words. Warn only about what truly blocks the person.
- The team design tokens exactly as given in the dispatch brief (dark theme values; light theme may follow `prefers-color-scheme` only if it costs nothing; otherwise dark only).
- Commit trailers as in `git log -1`. Version 0.9.0 in pyproject.toml, src/kaptyorka/__init__.py, tests/test_cli.py; `## 0.9.0` CHANGELOG.

## Review Focus

1. A member's `who`, page feed, brief "others online" line and send-error text never name or count another person's session (not even masked). Test: `test_member_sees_only_own_and_manager` across feed, MCP who, brief, error text.
2. A member cannot message, knock, or open a door to another person by any path (MCP send/knock/answer, page send/answer, send("all")). Tests per path.
3. The manager still reaches everyone, and a member still reaches the manager (and wakes it when asleep).
4. A door the admin opens lets the two people see and message each other's sessions; closing it hides them again. Test.
5. A member's page has no Admin tab and every admin route still answers 403 to a member (existing `test_member_routes_forbidden` stays green).

---

### Task 1: A member sees only their own sessions, the manager, and door partners

**Files:** `src/kaptyorka/hub_feed.py` (`visible` :16, `session_row` :24, `feed` :73), `src/kaptyorka/hub.py` (MCP `who` :164), `src/kaptyorka/brief.py` (`_you` :62), `src/kaptyorka/store.py` (`_shown` :1294 and the online list in `_targets`' "no session named" error :1321), tests.

**Rule:** for a viewer who is not the admin, a session is listed only when it is the viewer's own person's, or the manager account's, or its person has an open group door with the viewer, or it has an open session door with one of the viewer's sessions. Everything else is left out entirely (no "(private)" row, no count). The admin and the manager see everyone, as today. Project masking ("(private)" folder) stays for what is listed.

- [ ] Tests: `test_member_sees_only_own_and_manager` (feed agents, MCP `who`, brief others line, the "no session named X; online: ..." text), `test_member_sees_door_partner_until_closed`, `test_admin_and_manager_see_everyone`. Update by name the tests that assert the old masked rows: test_page_people.py `test_feed_filtered_for_member`, `test_feed_masks_private_sessions`, `test_knock_and_door_sessions_masked`; test_hub_people.py `test_who_hides_private_project`, `test_who_masks_name_of_private_session`; test_scoping.py `test_brief_and_errors_mask_private_folders`; test_hub_workers.py `test_who_marks_boss_and_worker` (check only).
- [ ] Implement in `visible`/`session_row` callers: filter before masking; one helper `hub_feed.listed(store, viewer, row) -> bool` used by feed, who and brief; the store error text lists only what `listed` allows (pass the viewer through as `_shown` already does).
- [ ] Run the touched test files. Commit `feat(hub): a member sees only own sessions, the manager and door partners`.

### Task 2: A member reaches only their own sessions and the manager; only the admin opens doors

**Files:** `src/kaptyorka/store.py` (`knock` :1627, `_answer` :1696, `answer_by_person` :1686), `src/kaptyorka/hub.py` (MCP `knock` :329 / `answer` :338, page `/api/knocks/{kid}/answer`), README "Groups and doors", tests.

**Rules:**
- `_may_message` is unchanged (own person, admin sender, open doors); `send_via_manager` / `send_web_via_manager` unchanged (manager and to-the-manager need no door).
- `knock` from a session or page of a non-admin, non-manager person: refused with `ask the manager: send it a message saying who you need and why`. The admin may knock as today (rarely used).
- `answer` (MCP and page): only the admin. The knocked person gets `only the admin opens doors`.
- The admin opens doors on the Admin page (existing `/api/doors` POST, `open_door_group`) or answers a knock. The manager cannot open doors (unchanged).
- Manager instructions (`manager.MANAGER_SECTION`): one added rule line: "When a person asks to reach someone else, tell the admin who, whom and why; the admin opens the door."
- README "Groups and doors": rewrite in four short bullets for the new rule.

- [ ] Tests: `test_member_knock_refused_points_to_manager`, `test_knocked_member_cannot_answer`, `test_admin_answer_opens_door`, `test_member_send_all_reaches_own_and_door_partners_only` (existing), `test_member_reaches_manager_without_door` (existing pair: keep green). Update by name: test_groups.py `test_knock_answer_yes_opens_door_and_send_works`, `test_only_knocked_person_or_admin_answers`, `test_tools_knock_answer_door_close`, `test_door_and_knock_routes`; test_page_people.py `test_knock_answer_route`.
- [ ] Implement. Run test_groups, test_page_people, test_hub_manager, test_scoping. Commit `feat(hub): only the admin opens doors; members ask the manager`.

### Task 3: Login warnings only when a machine has no logged-in tool

**Files:** `src/kaptyorka/workers.py` (`warnings` login rows), `src/kaptyorka/web/index.html` (Machines login dots/help line), `src/kaptyorka/brief.py` if it repeats the warning, tests.

**Rule:** a machine is fine when any tool with a known login state reports logged in. Only when every tool that reports a state is logged out: one warning `<host> (<user>): no agent tool is logged in. Run claude auth login there.` (Claude's command as the default suggestion). Per-tool red dots on the Admin Machines view stay as quiet detail (grey, not red, with the command on hover) when another tool is logged in.

- [ ] Tests: `test_no_warning_when_one_tool_logged_in`, `test_warning_when_no_tool_logged_in`. Update the 0.6.1 login warning tests by name (find with `grep -n "is not logged in" tests/`).
- [ ] Implement. Commit `fix: warn about logins only when no tool is logged in`.

### Task 4: The simple page in the team design tokens

**Files:** `src/kaptyorka/web/index.html`; new `src/kaptyorka/web/fonts/archivo-latin.woff2` and `OFL.txt` (source path in the dispatch brief); the static route in hub.py that serves the page (serve `/fonts/*` from the package); `pyproject.toml` package data if needed; `tests/test_page.py`, `tests/test_page_slow.py`, `tests/test_page_workers.py`, `tests/test_page_people.py`.

**Layout (match .superpowers/mock/home2.png):**
- Top bar 48 px: kaptyorka's own mark (`docs/assets/kaptyorka.svg`) + "kaptyorka"; tabs **Home** and **Admin** (Admin only for the admin; a member never gets it, not even hidden in markup that loads admin data); the person's name on the right, with Sign out on hover/click.
- **Home**, two columns (stack on narrow screens):
  - Left: one line per listed session: a 7 px dot (coral = busy, faint = idle), the session label (or short name), the machine on the right in faint 12 px. Full name, folder, tool and doing on hover. The manager is the last line with its state word (`asleep`, `up`, `off`). Above the list, only when there is something blocking for this viewer: one line per blocking warning, risk colour, the fix command on the right (admin only sees machine warnings; a member sees only their own blocking ones).
  - Right: messages, one line each: sender in bold (yours in coral), text, age on the right. Hub notices (sender `hub`) hidden; one faint "show notices" toggle at the end when any exist. Compose at the bottom: for a member the target is the manager (or a door partner chosen from a small select shown only when they have one); for the admin a select of everyone plus "all". Attach stays as an icon button.
- **Admin** (admin only): a left sub-menu: People, Access (requests, doors, shared token), Machines (machines and workers), Rules, Notes, Log, Settings. Each keeps today's function, restyled; trim each heading and help text to a few words.
- Members keep their "My access / Ask for" block as one small link on Home ("Ask for access"), opening the existing form.
- Style: the team dark tokens (`--bg #1b1a16`, `--surface #211f1a`, `--ink #e7e4dc`, `--muted #aaa498`, `--faint #79766c`, `--line rgba(231,228,220,.14)`, `--accent #da6947`, `--ok #699f08`, `--risk #c2942f`, `--danger #d59587`), Archivo 14 px, square corners, no shadows, thin lines for depth.

- [ ] Before coding, read .superpowers/mock/home2.html and the current index.html structure; keep every existing element id that tests use where the element survives, or update the tests that use it.
- [ ] Implement; check the page loads after each section (no test runs while shaping).
- [ ] At the end: update and run tests/test_page.py, test_page_people.py, test_page_workers.py, then `-m slow tests/test_page_slow.py`, saving screenshots of Home (admin), Home (member), and each Admin section to .superpowers/shots/ (git-ignored).
- [ ] Commit `feat(page): two tabs, team design tokens, far less text`.

### Task 5: Docs and version

README: Quick start and "The page" paragraph (two tabs; Admin only for the admin), "Groups and doors" (Task 2), the people section where it describes what members see; CHANGELOG `## 0.9.0` with four bullets (private by default; only the admin opens doors, members ask the manager; one logged-in tool is enough; new simple page). Whole suite once. Commit `docs: private by default, simple page; 0.9.0`.

### Release

After kursi's PASS (with screenshots) and green CI: tag v0.9.0, push, watch publish; update the hub machine and the laptop; check the real page as admin; with a temporary member invite in a temp hub (not the real one) check a member sees only their own sessions and the manager.

---

## Review changes (binding; they override the task text above where they differ)

Privacy paths (Tasks 1-2):
1. One rule in store.py: lock-held `_listed(viewer_person_id, row) -> bool` plus a public locking wrapper; true for the admin and the manager; feed, `who` and brief use the wrapper (no `hub_feed.listed`).
2. `_targets` (store.py:1300) for a member sender matches names and labels only among listed sessions: no "several sessions are named" list of others (another person's same label must not block the member's own send), no offline `SELECT ... WHERE name=?` fallback to others' sessions, no `; people: ...` tail naming others, online list only listed. One refusal text for not found and not allowed, naming no one else: `no session named X that you can reach; to reach someone else, message the manager`. Same for `_online_session` (knock, door_close).
3. The "no door to X: knock(to, about) first" texts in `send` (store.py:1223) and `send_from_web` (:1253) become the text from 2.
4. `projects_for` -> `working_now` (store.py:710) filtered with `_listed` (reaches members via MCP `projects`, `/api/projects`, brief.py:109).
5. `branches_with_sessions` (memory.py:297) filtered by the viewer (brief.py:129, `/api/projects/{name}/activity`); git lines naming a person in a shared project stay.
6. `store.machines` -> `online` (store.py:514): for a non-admin count only listed sessions.
7. Brief `_you`: no "Knocks for you ... answer" line for non-admins (brief.py:87); the no-person branch "N others online" (brief.py:73) does not count others.
8. Agent-facing text: hub.py `INSTRUCTIONS` (:56-61) no longer tells agents to knock; docstrings of `who`, `knock`, `answer`; README tools table (121-125), lines 256 and 267. CLI `who` needs no change (it reads `/api/feed`).
9. Knock is admin only, the manager included (it needs no door; it messages). `MANAGER_SECTION`: "knocks or messages" becomes "messages"; README 256 likewise. Old pending knocks stay answerable by the admin.
10. Member warnings: a `warnings` field in the feed for a non-admin, only rows whose person is the viewer; `/api/warnings` stays admin only.
11. Fonts: add `/fonts/` to auth.py open prefixes; CSP (hub.py:43) gets `font-src 'self'`; package-data `web/fonts/*`; ship `archivo-latin.woff2` and `OFL.txt` only; test: `/fonts/archivo-latin.woff2` 200 without a token, path traversal refused.

Page (Task 4):
12. Admin views: restyle only (tokens, moved under the Admin sub-menu, shorter text); no behaviour or markup-logic change. One index.html for everyone: a member's page makes no admin fetch, and the routes answer 403. On a phone the sub-menu is one scrolling row.
13. Compose: one way for everyone: clicking a row sets the target ("Message <name>"); default the manager; the admin also gets "all". No select. For the admin, another person's row shows that person's name faint. The "show notices" toggle shows the count.
14. Members lose the Notes, Machines and Workers views on the page (MCP and CLI keep them): say so in one README line.

Tests:
15. One sweep test: alice and bob (members), the admin and the manager, each with an online labelled session in a shared registered project on a shared machine. As alice, call every GET route and read tool (`who`, `projects`, `workers`, `inbox`, brief, feed, `/api/machines`, `/api/projects`, activity, `/api/knocks`, `/api/my-access`) and the send-error texts; bob's and the admin's session name, label and folder appear nowhere. Repeat with a group door open (bob appears, the admin does not) and after it closes. Also: a label shared by alice and bob still delivers alice's send to her own session; a send to bob's offline session name gives the same text as an unknown name.

Other:
16. Login suggestion (Task 3): the command of a tool installed there (the first that reports a state), not always Claude's. The launch-failure text (bridge.py:240) and the manager's hub-login check stay.
17. MCP send with a file to the manager: hub.py:321 skips `send_via_manager` when a file is attached; pass the file through with the same ownership check (or prove with a test it already works).
18. No team or company names in the public repo: kaptyorka's own mark in the top bar; tokens called "the team design tokens"; no company name, sibling project names or paths, or the team's machine names in committed files (this plan included).
19. Release: doors opened by members before 0.9.0 stay open (group doors do not expire); after the update the admin reviews Admin > Access and closes unwanted ones. No migration code.

Accepted consequence (one README line): a member's session cannot reply to a message from an admin's session except through the manager; replies to the admin's page still work, because the page send opens a door.
