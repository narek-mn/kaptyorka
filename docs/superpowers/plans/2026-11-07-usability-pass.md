# Usability pass (0.6.0) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A first-time admin can bring a teammate in, register a project, give and take access, change a role and remove the person, from the page or the command line, with inline confirms and no dead ends; a member can see what they may use and ask for more; every tab works on a phone.

**Architecture:** Store-side operations live in a new module `admin_ops.py` (same lock and connection as the other stores; `store.py` gets one `ensure_schema` line). The hub gains the missing routes; the CLI gains the same actions; the page gets the flows, inline confirms, empty states with a next step, phone cards, a member "My access" panel and sign-out. Source of findings: `.superpowers/ux-audit.md` (git-ignored; its ten most important items and the rest).

**Tech Stack:** Python 3.10, `mcp` SDK (Starlette), SQLite, one HTML page, no new dependency.

**Spec:** `docs/superpowers/specs/2026-11-06-team-brain-design.md` ("Part 1: People (0.2)", "Constraints", "Anti-goals": no manual step; the page stays one plain file) and the audit.

## Global Constraints

- Python 3.10 floor; no new dependency; the page stays one HTML file, dark, no build, no CDN.
- No team names in code, tests, fixtures or screenshots; use alice, bob, carol, proj-a, box-1.
- `Store.lock` is not reentrant; `admin_ops` follows the `MemoryStore` rule; `store.py` changes only by the one `ensure_schema` line plus `removed_at IS NULL` filters in `people()`, `person_by_name()` and the name lookups that feed grants and invites (each a one-line touch, listed in the task report).
- Removing a person is safe, never a raw delete: revoke their devices and open invites, close their workers and doors, drop their project and machine grants, pending knocks and requests; keep their name on old messages, notes and log rows; refuse the admin, the `manager` person and yourself; log it under the admin; the name can be reused by a new person.
- Removing a project unregisters it and keeps its notes, and the confirm text says so.
- No browser dialogs anywhere (`confirm`, `prompt`, `alert`); destructive actions confirm inline (button → "Remove bob? Yes / Cancel" in place, Esc cancels), as the Rules tab's Reject already does.
- Every empty state names the next step in plain English; every control has a visible label or a sentence around it; wrong input gets one plain sentence, never a traceback.
- Phone width 390 px with 16 px gutters and no horizontal page scroll on every tab, as admin and as member.
- Screenshots of every changed screen (desktop 1440 px and phone 390 px, admin and member, including empty states) go to `docs/screens/` with demo names; the reviewer looks at them.
- No version bump: fold the changes into the existing `## 0.6.0` CHANGELOG section.
- Commit trailers on every commit: the two lines exactly as in `git log -1 --format=%B` of HEAD; copy them, never retype.

## Review Focus

1. Removing a person who has a running worker and an open door must close both and leave no row pointing at a live session of theirs (Task 1 `test_remove_person_closes_workers_and_doors`).
2. A member must not be able to call any of the new admin routes, and a member's "ask for access" must not let them name a kind other than project or machine (Task 1 `test_member_routes_forbidden`, `test_request_kind_validated`).
3. Re-adding a removed person's name must create a fresh person with no old devices or grants (Task 1 `test_removed_name_can_be_reused`).
4. The inline confirm must survive the page's 2-second refresh without closing or re-rendering under the pointer (Task 2 `test_confirm_survives_refresh`, headless click test).
5. At 390 px every action button on People, Access and Log must be reachable without horizontal scroll (Task 3, phone screenshots checked by the reviewer).

---

## File map

| File | Responsibility |
|---|---|
| `src/kaptyorka/admin_ops.py` (new) | `ensure_schema` (people `removed_at`), `remove_person`, `set_role`, `invites_open`, `invite_cancel`, `project_update`, `project_remove`, `my_access`, `log_sentence`. |
| `src/kaptyorka/hub.py` | New routes; request decide with role and reason; `/api/me/access`; `/api/log` rows carry a sentence. |
| `src/kaptyorka/cli.py` | `people remove`, `people role`, `invites` (list, `--cancel`), `project edit`, `project remove`; plain error sentences. |
| `src/kaptyorka/web/index.html` | People, Access, invites, login, sign-out, empty states, inline confirms, member "My access" + "Ask for access", phone cards, Team/Machines/Log polish. |
| `README.md`, `CHANGELOG.md` | The people story in page order; 0.6.0 section. |
| tests | `test_admin_ops.py`, `test_hub_admin.py`, edits to `test_cli.py`, `test_page.py`, `test_page_people.py`, `test_readme.py`. |

---

### Task 1: Store operations, hub routes, command line

**Files:**
- Create: `src/kaptyorka/admin_ops.py`
- Modify: `src/kaptyorka/store.py` (one line: `admin_ops.ensure_schema(self.db)` after the `manager.ensure_schema` line), `src/kaptyorka/hub.py`, `src/kaptyorka/cli.py`
- Test: `tests/test_admin_ops.py`, `tests/test_hub_admin.py`, `tests/test_cli.py`

**Interfaces:**
- `admin_ops.ensure_schema(db)`: adds `people.removed_at REAL` when missing.
- `remove_person(store, ws, name: str, by: Principal) -> dict`: refuses `by` not admin ("admin only"), the admin ("the admin cannot be removed"), the `manager` person ("the manager account cannot be removed; use kaptyorka start --no-manager"), yourself ("you cannot remove yourself"), unknown ("no person named X"). Does, in this order and under one lock where possible: revoke all devices (`revoked_at`), delete open invites, close open doors and pending knocks, drop `project_access` and `machine_access` rows and pending requests, mark workers of that boss closed with error "boss removed" and queue close orders through `ws` (outside the lock), set `removed_at`, and rename the row to `f"{name} (removed)"` keeping it unique (append ` #<id>` if taken). Logs `person_removed` under `by`. Returns counts `{"devices","workers","doors","grants","requests"}`. `people()` and every name lookup skip removed rows; `person_create` with the old name makes a fresh person.
- `set_role(store, name, role, by) -> None`: member ↔ manager only ("the admin role cannot be given or taken here"); logs `role`.
- `invites_open(store) -> List[dict]` (`code`, `person`, `created`, `expires`; the table's key is the code itself, which only the admin sees); `invite_cancel(store, code, by)`; the invite answer gains `expires` and the page text says "This link works for 7 days".
- `project_update(store, name, description, by)`, `project_remove(store, name, by) -> dict` (drops grants and the folder learned per machine; keeps notes; refuses when a worker is live in it: "close its workers first"); logs.
- `my_access(store, ws, principal) -> dict`: `{"projects": [{"name","role","description"}], "machines": [{"host","owner","shared"}], "requests": [pending ones of this person with status]}`.
- `log_sentence(row) -> str`: plain sentence per known kind (grant, revoke, person, device, invite, project_register, project_removed, person_removed, role, page_login, work_decide, manager, rules, skills, proposal, conflict, backup); unknown kinds fall back to `f"{kind}: {detail}"`.
- Routes (admin only unless said): `DELETE /api/people/{name}`; `POST /api/people/{name}/role {role}`; `GET /api/invites`; `DELETE /api/invites/{code}`; `POST /api/projects/{name} {description}`; `DELETE /api/projects/{name}`; `POST /api/requests {kind, target, why}` (any member; kind in project|machine); `POST /api/requests/{id}` gains `{role: read|write}` on approve and `{reason}` on deny, the reason reaching the asker's page and sessions through `ws.notify`; `GET /api/me/access` (any principal); `GET /api/log` rows gain `sentence`.
- CLI: `kaptyorka people remove <name>` (asks `Remove bob? Their devices stop working at once. [y/N]` unless `--yes`), `kaptyorka people role <name> member|manager`, `kaptyorka invites` (table) and `kaptyorka invites --cancel <code>`, `kaptyorka project edit <name> --description "..."`, `kaptyorka project remove <name> [--yes]`. Every hub error prints its sentence and exits 1; a connection failure prints `kaptyorka hub unreachable at <url>`.

- [ ] **Step 1: Write the failing tests**: `test_remove_person_closes_workers_and_doors`, `test_remove_refusals` (admin, manager, self, unknown, member caller), `test_removed_name_can_be_reused`, `test_removed_person_hidden_everywhere` (people list, grant dropdown data, feed names still show "bob (removed)" on an old session), `test_set_role_bounds`, `test_invites_list_and_cancel`, `test_project_update_and_remove_keeps_notes`, `test_project_remove_refuses_live_worker`, `test_my_access_member_view`, `test_request_kind_validated`, `test_member_routes_forbidden` (every new admin route → 403), `test_approve_with_role_and_deny_with_reason`, `test_log_sentences`, CLI tests for each command including the plain error path.
- [ ] **Step 2: Run** `python3 -m pytest tests/test_admin_ops.py tests/test_hub_admin.py tests/test_cli.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the same plus `tests/test_hub_people.py tests/test_store_people.py tests/test_hub_workers.py -q`. Expected: PASS.
- [ ] **Step 5: Commit** `feat(admin): remove people and projects safely, roles, invites, ask for access, log sentences`.

---

### Task 2: Admin flows on the page

**Files:**
- Modify: `src/kaptyorka/web/index.html`
- Test: `tests/test_page_people.py`, `tests/test_page.py`, a headless click script kept under `tests/` as a slow test (`test_page_slow.py`, `-m slow`) for the inline confirm and the add-person flow

**Interfaces:** Task 1 routes.

- [ ] **Step 1: Write the failing tests** (string checks for the new ids and texts; the slow click test for `test_confirm_survives_refresh` and `test_add_person_shows_link`).
- [ ] **Step 2: Implement:**
  - People: "Add person" creates the person AND shows the invite panel at once with `Send this link to bob. It works for 7 days.`; the panel stays until closed, and "Open invites (2)" lists them with Cancel; a role select on each row (member/manager, inline confirm when going up: "Make bob a manager? A manager session can decide work requests and message anyone. Yes / Cancel"); "Remove" per row with inline confirm `Remove bob? Their devices stop working at once; their messages and notes stay. Remove / Cancel`; revoked devices fold into "2 revoked" that expands; the "Old machines / shared token" panel hides when the shared token is off and nothing is waiting; the `manager` person shows with a short hint and no Remove.
  - Access: "New project" form (name, description) in the empty state and the header; project rows get an Edit (description, inline) and Remove (inline confirm: `Remove proj-a? Its notes stay; people lose their roles in it. Remove / Cancel`); the grant form reads as a sentence `Give [bob] [write] access to [docs]` with a one-line hint per role (read: see notes and sessions; write: also edit notes and start workers; manage: also give access); the admin's own tag shows "admin" without an ×; × on a tag asks inline (`Take bob's write access to docs? Yes / Cancel`); Approve offers read or write; Deny takes an optional reason inline; "Recent decisions" lists the last 10 decided requests; the machines panel is titled "Who may use each machine", the shared control is a labelled switch "Shared with others who use this machine", owner change shows "Saved".
  - Login: the line `Use the link from kaptyorka start, or ask your admin for an invite.` under the field; a wrong token shows `That token did not work.` without reloading.
  - Header: a "Sign out" item (clears the stored token, returns to the lock screen) and the signed-in name visible at 390 px (initial badge).
  - Empty states (exact copy): Team `No sessions online. Open your coding tool in a project folder on a joined machine and it appears here.`; Messages `No messages yet. Click a session on the Team tab to write to it.`; Notes `No notes yet. Notes are shared documents your team and its agents keep. Start one with the Write button.`; Machines `No machines yet. Join one with the link from kaptyorka start.`; Workers `No workers. Ask your agent to work(project, task) and the session appears here.`; Rules `No team rules yet. Write the rules every agent on the team follows. Edit.`; Skills `No team skills. Upload them with kaptyorka rules push <folder>.`; Proposals `No proposals. Agents file one when their person corrects them the same way twice.`
  - Confirms: replace the three `confirm()` calls (device revoke, shared token off, close worker) with the inline pattern; no `prompt()`/`alert()` anywhere (grep must find none).
- [ ] **Step 3: Screenshots** to `docs/screens/`: `people.png`, `people-invite.png`, `access.png`, `access-empty.png`, `login.png` at 1440 px, plus `*-phone.png` at 390 px for people and access; look at each.
- [ ] **Step 4: Run** `python3 -m pytest tests/test_page.py tests/test_page_people.py -q` and `python3 -m pytest -m slow tests/test_page_slow.py -q`. Expected: PASS.
- [ ] **Step 5: Commit** `feat(page): add, change and remove people and projects from the page; inline confirms; empty states`.

---

### Task 3: Member view, phone layouts, polish

**Files:**
- Modify: `src/kaptyorka/web/index.html`
- Test: `tests/test_page.py`

- [ ] **Step 1: Write the failing tests** (ids and texts: `my-access`, `ask-access`, the Team second line, the legend).
- [ ] **Step 2: Implement:**
  - Member: a "My access" panel at the top of the Team tab (projects with role, machines they may use, pending requests with status) and an "Ask for access" sentence form `Ask for [read/write] access to project [name]` / `use of machine [name]` with a why field (POST `/api/requests`), machine names offered from `/api/machines` the member may see.
  - Phone (≤ 640 px): the tab bar shows a scroll hint (fade on the right) and the admin tabs reachable; People, Access and Log tables become cards with every action button visible; Team cards show `bob · box-1 · proj-a` as a second line; Machines cards show the "Auto-update" label next to the switch and hide empty fields; the Messages filter chips collapse into one "Filter" button.
  - Team: the R/H/S marks hidden for members; for the admin a legend line `R rules text · H hooks · S skills` under the table once.
  - Log: rows show the sentence from Task 1, time ago and person; the exact time on hover.
- [ ] **Step 3: Screenshots** to `docs/screens/`: `member.png` (replace), `member-phone.png`, `team-phone.png`, `log.png`, `machines-phone.png`; look at each.
- [ ] **Step 4: Run** `python3 -m pytest tests/test_page.py tests/test_page_people.py tests/test_page_workers.py -q`. Expected: PASS.
- [ ] **Step 5: Commit** `feat(page): member access view and request form; phone layouts; plain log`.

---

### Task 4: Command-line walk and docs

**Files:**
- Modify: `src/kaptyorka/cli.py` (help texts and error sentences), `README.md`, `CHANGELOG.md`
- Test: `tests/test_cli.py`, `tests/test_readme.py`

- [ ] **Step 1: Walk** `kaptyorka --help` and every subcommand's `--help` as a first-time admin: every command has a one-line help in plain English; hidden flags stay hidden; a missing argument prints the usage and one sentence; every hub error path prints a sentence (test the common ones: not joined, unreachable, 403, 404, 409).
- [ ] **Step 2: README**: the "People and access" section tells the story in page order (add a person → send the link → they join → give access → change a role → remove them), from the page and from the command line; Commands table rows for the new commands; the screenshots referenced only in `docs/screens/` (the README does not embed images). CHANGELOG: fold into `## 0.6.0`.
- [ ] **Step 3: Run** `python3 -m pytest tests -q -W default` (full suite, once). Expected: all pass, no warnings.
- [ ] **Step 4: Commit** `docs: people and access in page order; command help`.
