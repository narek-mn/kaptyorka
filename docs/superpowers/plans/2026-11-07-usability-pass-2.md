# Usability pass 2 (0.6.0) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The things the first audit deferred: from the page a person can attach a file to a message, the admin can open and close doors, a writer can delete a note (with undo) and restore an old version, everyone can propose a rule and the admin can see rule history and restore a version, the log can be filtered by kind and paged, and the admin's settings sit in one Settings tab. Plus the small fix left from part 3: the bridge sends the age of its git read, not its clock time.

**Architecture:** Hub-side operations stay out of `store.py`: note delete/undo goes into `admin_ops.py` (one `notes.deleted_at` column; `store.py` gets `deleted_at IS NULL` one-line filters), the hub gains the missing routes (`POST /api/proposals`, note delete/undo, log `kind` and `before`), the page gets the flows with the inline-confirm pattern that already exists. The git age fix touches `bridge.py` (send `age`), `workers.py` (`clean_facts` accepts `age`) and `memory.py` (`read_at = now - age`).

**Tech Stack:** Python 3.10, `mcp` SDK (Starlette), SQLite, one HTML page, no new dependency.

**Spec:** `docs/superpowers/specs/2026-11-06-team-brain-design.md` ("Constraints", "Anti-goals": the page stays one plain file; no manual step) and `.superpowers/ux-audit.md` (git-ignored; the rows marked "later" in the previous pass: Messages file/doors/hint, Notes delete/restore, Rules propose/history, Log paging/kind, settings area, Machines empty fields, Team legend).

## Global Constraints

- Python 3.10 floor; no new dependency; the page stays one HTML file, dark, no build, no CDN.
- No team names in code, tests, fixtures or screenshots; use alice, bob, carol, proj-a, box-1.
- `Store.lock` is not reentrant; new store-side code goes in `admin_ops.py` (same lock, connection and clock as the store); `store.py` changes only by one-line touches listed in the task report: `deleted_at IS NULL` filters in `notes_list`, `note_history` and the `MAX(id)` subquery, and the optional `file_id` on `send_from_web`.
- No browser dialogs (`confirm`, `prompt`, `alert`); destructive actions confirm inline the way People → Remove does (`confirmInline`, Esc cancels, survives the 2 s refresh); after a delete show "Deleted X. Undo" for 8 s.
- Members never reach an admin route; every new route is checked for the member case in a test.
- Every empty state names the next step in plain English; every control has a visible label or a sentence around it; wrong input gets one plain sentence.
- Phone width 390 px, 16 px gutters, no horizontal page scroll on every changed tab, as admin and as member.
- Screenshots of every changed screen (1440 px and 390 px; admin and member where they differ; empty states) go to `docs/screens/` with demo names, taken with the demo hub in a temp `KAPTYORKA_HOME` on a free port; never against `~/.kaptyorka`.
- No version bump: fold the changes into the existing `## 0.6.0` CHANGELOG section.
- Commit trailers on every commit: the two lines exactly as in `git log -1 --format=%B` of HEAD; copy them, never retype.
- Run the whole test suite (`python3 -m pytest tests -q`, in the venv) before every commit that touches `hub.py`, `store.py` or `admin_ops.py`; the slow page tests (`-m slow tests/test_page_slow.py`) once per page task.

## Review Focus

1. A note deleted on the page must vanish from every list and from the `notes_read` tool and the brief, and Undo must bring every version back (Task 1 `test_note_delete_hides_everywhere`, `test_note_undo_restores_all_versions`).
2. A member must not delete a note in a project where they only read, and no one but the admin or the author may delete a Team-board note (Task 1 `test_note_delete_needs_write`, `test_team_note_delete_admin_or_author`).
3. A rule proposal filed from the page must reach the proposer as a message when the admin decides it, since there is no session to notify (Task 1 `test_page_proposal_decision_reaches_person`).
4. The git age must make a bridge whose clock is ahead of the hub's no longer win over a fresher stop-hook read (Task 2 `test_age_beats_clock_skew`).
5. Attaching a file and sending must show the file in the thread at once and refuse one over the limit with a plain sentence (Task 3, `test_attach_sends_file` in the slow page tests; checked in the screenshot).

---

## File map

| File | Responsibility |
|---|---|
| `src/kaptyorka/admin_ops.py` | `ensure_schema` (+ `notes.deleted_at`), `note_delete`, `note_undo`, `purge_deleted_notes`, `log_kinds`, `log_page`, `may_delete_note`. |
| `src/kaptyorka/store.py` | `deleted_at IS NULL` filters (three one-line touches). |
| `src/kaptyorka/hub.py` | `POST /api/proposals`, `DELETE /api/notes`, `POST /api/notes/undo`, `/api/log?kind=&before=`, `GET /api/log/kinds`; page proposals decided reach the person as a message. |
| `src/kaptyorka/bridge.py`, `workers.py`, `memory.py`, `facts.py` | git read `age`. |
| `src/kaptyorka/web/index.html` | Messages attach + doors + hint; Notes delete/undo/restore; Rules propose/history/restore; Log kind filter + Load more; Settings tab; Machines empty fields; Team legend. |
| `README.md`, `CHANGELOG.md` | The new page actions, one line each; 0.6.0 section. |
| tests | `test_admin_ops.py`, `test_hub_admin.py`, `test_hub_rules.py`, `test_bridge.py`, `test_memory.py`, `test_workers.py`, `test_page.py`, `test_page_slow.py`, `test_readme.py`. No CLI change in this pass. |

---

### Task 1: Hub side: note delete and undo, proposals from the page, log kinds and paging

**Files:**
- Modify: `src/kaptyorka/admin_ops.py`, `src/kaptyorka/store.py` (three one-line filters), `src/kaptyorka/hub.py`
- Test: `tests/test_admin_ops.py`, `tests/test_hub_admin.py`, `tests/test_hub_rules.py`

**Interfaces:**
- `admin_ops.ensure_schema(db)`: also adds `notes.deleted_at REAL` when missing.
- `may_delete_note(store, principal, project, topic) -> None`: raises `KaptyorkaError`. Project note: the principal must have `write` or `manage` there (admin always may). Team-board note (`project` None): the admin or the author of the *oldest* version (the person who started the note; `principal.person_name` compared to that row's `author`); others get "only the admin or the person who started a team note can delete it". A principal with no person (the shared token, `principal.legacy` or `person_id` None) may never delete.
- `note_delete(store, principal, topic, project) -> int`: stamps `deleted_at = now` on every version of (project, topic); 0 versions → "no note named X"; logs `note_delete` with `f"{project or 'team'}:{topic}"`. Returns the count.
- `note_undo(store, principal, topic, project) -> int`: clears `deleted_at` on the versions of that note deleted in the last 10 minutes by anyone (same permission check); 0 → "nothing to undo for X". Logs `note_undo`.
- `purge_deleted_notes(store, now) -> int`: hard-deletes note rows whose `deleted_at` is older than 10 minutes (`UNDO_S = 600`); called from the hub's nightly cleanup right after `store.prune(now)` (find the call site of `prune` in `hub.py`). The log row of the delete stays. Test: `test_purge_removes_only_old_deleted_notes` (a note deleted 11 minutes ago goes, one deleted 2 minutes ago and a live note stay).
- `store.notes_list`, `note_history` and the `MAX(id)` subquery skip `deleted_at IS NOT NULL` rows, so the tools `notes_list`/`notes_read`, the brief and the feed all hide deleted notes with no other change.
- `log_kinds() -> List[Tuple[str, str]]`: the plain groups in order and the raw kinds each covers: `[("people", [...person_*, role, invite_*, device_*]), ("access", [grant/revoke/request kinds]), ("projects", [project_*]), ("machines", [machine_*, legacy_*, auto_update*]), ("messages", [retention, door_*, knock_*]), ("rules", [rules_*, skill*, propose, proposal_*, conflict]), ("workers", [worker_*, work_*]), ("sign-ins", [page_login, page_*])]`; every kind `log_sentence` knows appears in exactly one group (a test asserts it); unknown kinds fall under "other".
- `log_page(store, limit, person, kind, before) -> List[dict]`: rows (with `sentence`) newer-first, `id < before` when given, filtered by the group `kind` (its raw kinds in an `IN (...)`), `limit` ≤ 1000.
- Hub routes:
  - `DELETE /api/notes?topic=&project=` and `POST /api/notes/undo` `{topic, project}` for any signed-in person (the permission check decides); 204 or 400 with the sentence.
  - `POST /api/proposals` `{text, evidence}` for any signed-in person: refuses the shared token (`principal.legacy` or `person_id` None) with the `OWN_TOKEN` sentence like the `propose_rule` tool; runs `check_secrets` (from `secrets_check`) on text and evidence; then `rs.propose(principal, f"web:{name}", text, evidence)`; 200 `{"id"}`. Test `test_page_proposal_refuses_shared_token`. `api_proposal_decide` keeps calling `tell(row["session_key"], text)`: `ws.notify` already writes to a `web:<person>` key, so the test only pins that the decision shows up in the proposer's page feed.
  - `GET /api/log?limit=&person=&kind=&before=` uses `log_page`; `GET /api/log/kinds` returns the groups `[{"kind": "people", "label": "People"}, ...]`. Admin only, as before.

- [ ] **Step 1: Failing tests** in `tests/test_admin_ops.py`:

```python
def test_note_delete_hides_everywhere(store_alice_bob):  # fixture: store, admin alice, member bob with write on proj-a
    ...
    store.notes_write_from_web("alice", "plan", "v1", "proj-a", principal=alice)
    store.notes_write_from_web("alice", "plan", "v2", "proj-a", principal=alice)
    assert admin_ops.note_delete(store, alice, "plan", "proj-a") == 2
    assert store.notes_list(alice, "proj-a") == []
    assert store.notes_read("plan", alice, "proj-a") is None
    assert store.log(5)[0]["kind"] == "note_delete"

def test_note_undo_restores_all_versions(...):
    ... delete, then admin_ops.note_undo(...) == 2; note_history has 2 versions, newest "v2"

def test_note_delete_needs_write(...):
    # carol with read on proj-a
    with pytest.raises(KaptyorkaError, match="no access"): admin_ops.note_delete(store, carol, "plan", "proj-a")

def test_team_note_delete_admin_or_author(...):
    # bob started a team note, carol saved v2: carol is still refused, bob may, alice may; the shared token may not
    with pytest.raises(KaptyorkaError, match="only the admin or the person who started"): ...

def test_log_kinds_cover_every_sentence_kind():
    groups = dict(admin_ops.log_kinds())
    known = set(admin_ops.KNOWN_KINDS)  # expose the kinds log_sentence maps, as a module constant
    seen = [k for ks in groups.values() for k in ks]
    assert sorted(seen) == sorted(set(seen)) and known <= set(seen)

def test_log_page_before_and_kind(...):
    # 5 log rows of mixed kinds; before=<id of row 3> gives the two older; kind="people" gives only those
```

and in `tests/test_hub_admin.py` / `tests/test_hub_rules.py` (hub fixtures as the existing tests use):

```python
def test_note_delete_and_undo_routes(hub): DELETE 204, GET feed hides it, POST undo 204, visible again
def test_member_cannot_delete_read_only_note(hub): 400 with the sentence
def test_page_proposal_decision_reaches_person(hub):
    # bob posts /api/proposals; alice decides accept; bob's page feed shows a message from alice containing "accepted"
def test_log_kinds_and_paging_routes(hub): /api/log/kinds admin only (member 403); /api/log?kind=people&before=
```

- [ ] **Step 2: Run them, see them fail** (`python3 -m pytest tests/test_admin_ops.py tests/test_hub_admin.py tests/test_hub_rules.py -q`).
- [ ] **Step 3: Implement** the interfaces above; the three `store.py` filters; the hub routes. Keep every error one plain sentence.
- [ ] **Step 4: Whole suite passes** (`python3 -m pytest tests -q`).
- [ ] **Step 5: Commit** `feat(hub): delete and undo notes, propose rules from the page, log kinds and paging`.

---

### Task 2: Git read age instead of clock time

**Files:**
- Modify: `src/kaptyorka/bridge.py` (`heartbeat` payload), `src/kaptyorka/workers.py` (`_git`), `src/kaptyorka/memory.py` (`git_seen`)
- Test: `tests/test_bridge.py`, `tests/test_workers.py`, `tests/test_memory.py`

**Interfaces:**
- The bridge keeps `read_at` (its own clock) inside its cached facts, but the heartbeat body's `facts.git[cwd]` carries `age` = seconds since that read (`max(0, time.time() - read_at)`, rounded to 1 decimal) and no `read_at`.
- `workers._git` accepts `age` (finite number ≥ 0, cut at 86400) and still accepts `read_at` from 0.3–0.5 bridges.
- `memory.git_seen`: `read_at = now - age` when `age` is present; else the old rule (`min(read_at, now)`, missing → `now`). Nothing else changes: the hub still drops a fact read before the stored one.

- [ ] **Step 1: Failing tests**

```python
# test_bridge.py
def test_heartbeat_sends_git_age_not_read_at(bridge_with_fake_hub): body["facts"]["git"][cwd] has "age" (0 <= age < 5) and no "read_at"
# test_workers.py
def test_clean_facts_git_age(): clean_facts({"git": {"/p": {"branch": "main", "subject": "x", "age": 12.5}}})["git"]["/p"]["age"] == 12.5; "age": -1 and "age": "soon" dropped
# test_memory.py
def test_age_beats_clock_skew(ms):
    # bridge clock 90 s ahead: a cached read (age 70) arrives after the stop hook stamped a fresher fact at now;
    # with age, read_at = now - 70 < stored read_at → dropped; the stored (newer) fact stays
```

- [ ] **Step 2: Run, fail.**
- [ ] **Step 3: Implement**; `facts.py` unchanged except a comment if useful.
- [ ] **Step 4: Whole suite passes.**
- [ ] **Step 5: Commit** `fix(bridge): send the age of the git read, so a clock ahead of the hub cannot hide a fresh commit`.

---

### Task 3: Page: Messages (attach, doors, hint), Notes (delete, undo, restore), Rules (propose, history, restore)

**Files:**
- Modify: `src/kaptyorka/web/index.html`
- Test: `tests/test_page.py` (static checks as the existing ones), `tests/test_page_slow.py` (headless Chrome)

**Interfaces (page behaviour, exact copy):**
- **Messages, compose bar:** a paper-clip button "Attach" (icon + label on desktop, icon with `aria-label` at 390 px) opens a hidden `<input type=file>`. A chosen file shows as a chip "name · size ×" next to the textarea; Send first `POST /api/files` (header `X-File-Name`), then `/api/send` with `file_id`; the hub's `api_send` accepts an optional integer `file_id` and, before sending, checks in the hub (not in `store.py`): the sender has a person (else refuse), the file exists, its `person_id` equals `principal.person_id`, and its `message_id IS NULL` (not sent yet); any failure is 400 "that file is not yours to send" (or "no such file"). Test `test_attach_refuses_another_persons_file` (bob attaches alice's file → 400) in `tests/test_hub_admin.py`. Then it passes `file_id` through `store.send_from_web(..., file_id=None)`, a new optional parameter handed to the existing `_insert(..., file_id)`: two one-line touches in `store.py`, listed in the report. A message with a file goes straight through `store.send_from_web`, never the manager route (as the `send` tool does). Over the limit: the hub's sentence ("file too large: …") shows under the compose bar. The sent message shows the attachment at once (the thread already renders `m.file`).
- **Messages, Doors group (admin):** each door tag gets an × that confirms inline "Close the door between bob and carol? Their sessions stop talking directly. Close / Cancel" → `POST /api/doors/{id}/close`. An "Open door" button in the group header opens an inline form "Open a door between [person] and [person]  Open  Cancel" (two selects of people, not the same, not the manager) → `POST /api/doors`. A one-line hint under the group the first time a knock or door appears: "A door lets two people's sessions talk directly, without the admin."; the hint hides after it was shown once (`localStorage` key `kaptyorka.door-hint`), wrapped in try/catch like the other storage reads.
- **Notes:** in the open note's tools a "Delete" button (writers only, per `may_delete_note` — the page shows it when the viewer may write there or wrote the latest version or is admin; the hub decides). Inline confirm "Delete plan? All 3 versions go. Delete / Cancel" → `DELETE /api/notes`; then the topic list shows a toast row "Deleted plan. Undo" for 8 s → `POST /api/notes/undo` (the hub keeps the undo valid for 10 minutes as slack; the page never promises it). If the note someone has open is deleted meanwhile, the next refresh closes it and the topic list shows "This note was deleted." for 8 s. When an old version is open, a "Restore this version" button (only for viewers who may write there, or the admin) saves its text as a new version (`POST /api/notes`) and reopens the note at v-latest.
- **Rules, Team rules panel:** a "History N" button like Notes (`GET /api/rules/history`), listing `vN · author · time`; clicking a version shows its text read-only with "old version vN" in the byline and, for the admin, "Restore this version" (`POST /api/rules` with that text; the new version is current). A "Propose a rule" button for everyone in the Proposals panel header opens an inline form: textarea "The rule" (maxlength 2000), input "Why (what happened)" (maxlength 4000), Send / Cancel → `POST /api/proposals`; the new row appears as open. Proposals empty state for members: "No proposals yet. Propose a rule when your agent keeps getting the same correction." (the admin's stays as it is).
- **Team legend:** when the admin sees the R/H/S marks, one legend line at the panel foot: "R rules · H hooks · S skills: what each session's tool has loaded"; hidden for members.
- **Machines cards:** empty fields are not rendered; one line "No tools reported yet" when a machine has none.

- [ ] **Step 1: Static tests** in `tests/test_page.py` (the file already asserts on the HTML text): the attach control, the door close confirm text, the delete confirm prefix, "Restore this version", "Propose a rule", the legend line, no `confirm(`/`prompt(`/`alert(` call.
- [ ] **Step 2: Slow tests** in `tests/test_page_slow.py` (same fixture style as `test_confirm_survives_refresh`):

```python
def test_attach_sends_file(admin_page): set a file on the input via CDP (DOM.setFileInputFiles) or by calling the page's upload helper with a Blob; click Send; wait for ".msg .att" or a file link; store.messages shows file_id
def test_note_delete_undo(admin_page): open note, Delete, confirm; "Undo" toast; click; note back with all versions
def test_propose_from_page(member_page): fill, Send; row with status open
def test_member_sees_no_restore(member_page): bob with read on proj-a opens an old version of a proj-a note: no "Restore this version" button and no Delete; on Rules history, no Restore
```

- [ ] **Step 3: Implement**, following the existing helpers (`confirmInline`, `button`, `act`, `quiet`, `changed`, `busy`); do not redraw a panel while its inline form is open.
- [ ] **Step 4: Run** `python3 -m pytest tests/test_page.py -q` and `python3 -m pytest -m slow tests/test_page_slow.py -q`; then the whole suite.
- [ ] **Step 5: Screenshots** with the demo hub: `messages-attach.png`, `messages-doors.png` (admin; door tag with ×, Open door form), `notes-delete.png` (confirm open), `notes-undo.png`, `notes-old-version.png` (Restore button), `rules-history.png`, `rules-propose.png` (member), plus `-phone` variants of messages-doors, notes-delete, rules-propose.
- [ ] **Step 6: Commit** `feat(page): attach files, doors, note delete and restore, rule history and proposals`.

---

### Task 4: Page: Log filter and paging, Settings tab; CLI and docs

**Files:**
- Modify: `src/kaptyorka/web/index.html`, `README.md`, `CHANGELOG.md`
- Test: `tests/test_page.py`, `tests/test_readme.py`

**Interfaces:**
- **Log tab:** header has the person select (as now) and a kind select "Everything / People / Access / Projects / Machines / Messages / Rules / Workers / Sign-ins" (from `GET /api/log/kinds`); both filters call `/api/log?...` (the feed's 200 rows are used only when no filter is set). A "Load more" button under the table fetches `before=<last id>` and appends; it hides when fewer than `limit` rows came back. Phone cards as now.
- **Settings tab** (admin only; icon `i-gear` added to the sprite; sits last in the nav and under More on a phone): three panels. "Messages": "Keep messages for [N] days. Older messages and their files are deleted every night." with Save (moved from the Log header, which loses the form). "Manager": state line "The manager session is on/off" with, when off, the sentence "Turn it on with `kaptyorka start --manager` on the hub machine." and when on, "Switch it off with `kaptyorka start --no-manager` on the hub machine." (closing the manager worker from Machines also switches it off today, per `hub.py` admin close; check that before adding it to the copy, and name it only if it really stays off) (read from `/api/settings`, which gains `manager_enabled` from `ManagerStore.enabled()` or the equivalent accessor; read-only on the page). "Machines that joined before personal tokens": the Shared token panel moved here from People (People loses it); same controls and copy as today.
- **README:** one short block "From the page you can also: attach a file to a message, open or close a door, delete a note and undo, bring back an old note or rules version, propose a rule, filter and page the log; settings (message keeping, manager state, old shared token) sit in Settings." in the page-order section; CHANGELOG lines under 0.6.0.

- [ ] **Step 1: Tests**: `test_page.py` asserts the Settings tab markup, the kind select, "Load more", no retention form in `#audit`; `test_readme.py` keeps passing (it checks commands named in the README exist).
- [ ] **Step 2: Implement.**
- [ ] **Step 3: Whole suite; slow page test** `test_confirm_survives_refresh` still passes (nav changed).
- [ ] **Step 4: Screenshots**: `log-filter.png`, `settings.png`, `settings-phone.png`, `log-phone.png` retaken, `more-phone.png` retaken (Settings in the menu), `people.png` retaken (no shared-token panel).
- [ ] **Step 5: Audit walk**: the task report lists every row of `.superpowers/ux-audit.md` as done (which pass) or still deferred, checked against the code (including the 390 px filter chips and the Auto-update label on phone cards).
- [ ] **Step 6: Commit** `feat(page): log filter and paging, a Settings tab`.

---

## Rulings made while planning (for the ledger)

- Project deletion was already settled as archive; notes get a soft delete (`deleted_at`) so undo is cheap, and the nightly cleanup hard-deletes them after the undo window, so "gone" on the page is true in the database and the backups too. The log keeps the trail.
- Team-board notes: only the admin or the person who started the note may delete it (the latest author would let anyone save a version and then delete everything); anyone with write there may edit, as before.
- No `kaptyorka log` command: the admin reads the log on the page; nobody asked for it (manager).
- Skills upload from the page stays deferred: the CLI push is the right tool (a folder of files).
- The manager switch stays command-line only on the page (read-only state): turning a session on from a browser is a bigger decision than a toggle suggests.
