# Part 3: Memory (0.4) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A fresh session knows its project (state note, who works on it, recent git activity) from the first turn, a session that did work is made to update the project's state before it stops, decisions are kept, and the hub can be backed up and restored with one command each.

**Architecture:** A new module `memory.py` (`MemoryStore(store)`, same lock and connection as `WorkerStore`) holds git-facts history, done-check bookkeeping and the state/decisions helpers; `store.py` is not touched except where this plan names a line. The start brief is rebuilt as ordered sections with a character budget and a 10-second per-session cache. The done check is a new hook event `stop`: Claude Code and Codex stop hooks block once with a reason; Cursor gets a follow-up message; OpenCode gets the reminder in its next prompt brief. Backup uses SQLite's online-backup API, tar, optional `gpg --symmetric`, and scp.

**Tech Stack:** Python 3.10, `mcp` SDK (Starlette), SQLite (`sqlite3.Connection.backup`), `tarfile`, `gpg` and `scp` through an injected `run`, one HTML page.

**Spec:** `docs/superpowers/specs/2026-11-06-team-brain-design.md`, sections "Part 3: Memory (0.4)", "Backup", "Constraints", "My calls" (brief budget 6,000; git facts 30 days; done check blocks at most once per session and only after a work signal; backup encryption with gpg when present).

## Global Constraints

- Python 3.10 floor; no new dependency.
- No team names in code, tests, fixtures or screenshots; use alice, bob, proj-a, box-1.
- `Store.lock` is not reentrant; `MemoryStore` follows the same rule as `WorkerStore`.
- `store.py` changes only where a task names the exact line; new tables and methods live in `memory.py`.
- Hooks must answer within their tool's budget: the hook's HTTP timeout stays 3 s (`hook.TIMEOUT_S`); the hub caches each session's start brief for 10 s; the done check answers from stored rows, never by running git.
- A session that only read or discussed is never blocked; a session is blocked at most once; the reason text says exactly what to write and how.
- Backup never prints the passphrase; the passphrase file is 0600; an unencrypted copy is said plainly.
- Tests never call real agent tools, git over the network, gpg or scp except under `-m slow`; everything that runs a command takes an injected `run`.
- Version bump touches `pyproject.toml`, `src/kaptyorka/__init__.py`, `tests/test_cli.py`, `CHANGELOG.md`.
- Commit trailers on every commit: `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>` and `Claude-Session: https://claude.ai/code/session_01WHiZ1E64m7csjvMWwiGkdu`.

## Review Focus

1. A session whose folder is a worktree (`<folder>--2`) must count as the same project as the folder, for git facts and for the done check (Task 1 test `test_worktree_counts_as_the_project`).
2. A state note written from the page by the same person (not from the session) must satisfy the done check too, since the human may update it (Task 4 test `test_state_written_from_page_by_same_person_satisfies`).
3. Two sessions of the same person in the same project: a commit by one must not make the other's stop block (Task 4 test `test_signal_is_per_session`).
4. The brief for a session with no project access, no notes and no git facts must still be short and well-formed, under budget, with no empty headings (Task 3 test `test_brief_minimal_session`).
5. `kaptyorka restore` on a running hub must refuse (the SQLite file is open) and say to stop the hub first (Task 7 test `test_restore_refuses_while_hub_runs`).

---

## File map

| File | Responsibility |
|---|---|
| `src/kaptyorka/memory.py` (new) | `ensure_schema`, `MemoryStore`: git facts history, work signals and done checks, state note validation, decisions. |
| `src/kaptyorka/brief.py` (new) | Builds the start brief from sections with a budget; the 10 s cache. |
| `src/kaptyorka/hub.py` | `/api/brief` uses `brief.py`; tools `state`, `state_write`, `decide`; route `/api/done-check`; heartbeat feeds `git_seen`. |
| `src/kaptyorka/hook.py` | New event `stop` for claude-code, codex, cursor; the OpenCode reminder in `prompt`. |
| `src/kaptyorka/adapters/claude.py`, `codex.py` | `configure` adds the Stop hook; `doctor.tool_configured` mirrors it. |
| `src/kaptyorka/backup.py` (new) | `backup`, `restore`, `install_nightly` through injected `run`. |
| `src/kaptyorka/cli.py` | `backup`, `restore`. |
| `src/kaptyorka/retention.py` | Prunes git facts (30 days) in the same loop. |
| `src/kaptyorka/web/index.html` | Notes tab: project state first, recent git activity under it. |
| `README.md`, `CHANGELOG.md`, version files | 0.4.0. |
| tests | `test_memory.py`, `test_brief.py`, `test_done_check.py`, `test_backup.py`, `test_memory_slow.py`, plus edits to `test_hook.py`, `test_claude.py`, `test_codex.py`, `test_doctor.py`, `test_page.py`, `test_cli.py`, `test_readme.py`. |

---

### Task 1: Git facts history

**Files:**
- Create: `src/kaptyorka/memory.py`
- Modify: `src/kaptyorka/store.py` (`Store.__init__`: `memory.ensure_schema(self.db)` after the workers call; one line), `src/kaptyorka/hub.py` (heartbeat route: after `ws.facts_seen(...)` call `ms.git_seen(principal, device_id, facts.get("git") or {}, folders)`), `src/kaptyorka/retention.py` (`prune_loop` also calls `memory.prune(store, now)`)
- Test: `tests/test_memory.py`

**Interfaces:**
- Consumes: `WorkerStore.facts_seen` output shape: `facts["git"] = {cwd: {"branch", "dirty", "subject"}}` (Task 6 of part 2); `folders = {project: path}` from the same heartbeat; `Principal`; `store.lock/db/clock`.
- Produces:
  - `ensure_schema(db)`: `git_facts(id INTEGER PRIMARY KEY AUTOINCREMENT, project TEXT NOT NULL, host TEXT NOT NULL, os_user TEXT NOT NULL, person_id INTEGER, cwd TEXT NOT NULL, branch TEXT NOT NULL, dirty INTEGER, subject TEXT NOT NULL, time REAL NOT NULL)` and `done_checks(key TEXT PRIMARY KEY, project TEXT NOT NULL, started REAL NOT NULL, signal_at REAL, signal TEXT NOT NULL DEFAULT '', blocked_at REAL, last_subject TEXT NOT NULL DEFAULT '', last_dirty INTEGER)`.
  - `class MemoryStore(store)`:
    - `project_of(cwd: str, folders: dict) -> str`: the project whose folder equals `cwd` or whose folder plus `--<n>` equals `cwd`; else the basename of `cwd` with a trailing `--<n>` removed.
    - `git_seen(principal, device_id, git: dict, folders: dict) -> List[str]`: for each `cwd`, when `(branch, dirty, subject)` differs from the last stored row for `(host, os_user, cwd)`, insert a row with the device's host/os_user and `person_id = principal.person_id` (None for a shared device whose session person is unknown); returns the projects that changed. Also records per-session work signals (Task 4 uses them): for each online session row of this device whose `cwd` is this cwd, `_signal(key, "commit")` when `subject` changed, `_signal(key, "clean")` when dirty went 1 → 0.
    - `git_recent(project: str, days: int = 7, now=None) -> List[dict]`: rows for the project newer than `now - days*86400`, newest first, each `{person, host, branch, subject, dirty, time}`; `person` is the person name or `os_user`.
    - `branches_with_sessions(project, now) -> Dict[str, List[str]]`: branch → session names online in that project (from `agents` joined with the latest `git_facts` row per cwd).
    - `prune(now) -> int`: deletes `git_facts` older than 30 days; returns the count. `memory.prune(store, now)` is the module-level wrapper the retention loop calls.

- [ ] **Step 1: Write the failing tests**

```python
def test_git_seen_stores_a_row_only_on_change(ms, alice, dev):
    ms.git_seen(alice, dev, {"/home/alice/proj-a": {"branch": "main", "dirty": False, "subject": "init"}}, {"proj-a": "/home/alice/proj-a"})
    ms.git_seen(alice, dev, {"/home/alice/proj-a": {"branch": "main", "dirty": False, "subject": "init"}}, {"proj-a": "/home/alice/proj-a"})
    assert len(ms.git_recent("proj-a")) == 1
    ms.git_seen(alice, dev, {"/home/alice/proj-a": {"branch": "main", "dirty": True, "subject": "init"}}, {...})
    assert [r["dirty"] for r in ms.git_recent("proj-a")] == [True, False]

def test_worktree_counts_as_the_project(ms):
    assert ms.project_of("/home/alice/proj-a--2", {"proj-a": "/home/alice/proj-a"}) == "proj-a"
    assert ms.project_of("/x/y/webshop--3", {}) == "webshop"

def test_git_recent_window_and_person_name(ms, clock, ...):
    # a row 8 days old is excluded; person is the name, or os_user when person_id is None

def test_prune_deletes_older_than_30_days(ms, clock, ...):

def test_heartbeat_feeds_git_facts(hub):  # in-process hub: a heartbeat with facts.git and folders -> git_recent non-empty
```

- [ ] **Step 2: Run, expect failures**
- [ ] **Step 3: Implement** `memory.py` and the three one-line touches.
- [ ] **Step 4: Run `tests/test_memory.py tests/test_hub_workers.py tests/test_store.py`, expect pass; commit** `feat(memory): git facts history from heartbeats`

---

### Task 2: State note and decisions

**Files:**
- Modify: `src/kaptyorka/memory.py`, `src/kaptyorka/hub.py` (tools), `src/kaptyorka/shim.py` (fallback tool list)
- Test: `tests/test_memory.py`, `tests/test_hub_tools.py`

**Interfaces:**
- Consumes: `store.notes_write(key, topic, text, project)`, `store.notes_write_from_web(name, topic, text, project)`, `store.notes_read(topic, principal, project)`, `store._check_project` (private, non-locking: use it only inside the memory lock; otherwise `store.may_write_project`).
- Produces:
  - `memory.STATE_HEADINGS = ("What it is", "Where it stands", "Decisions", "Open problems", "How to run and test")`.
  - `memory.check_state(text: str) -> Optional[str]`: `None` when every heading appears as a Markdown heading (`#`, `##` or `###`, case-insensitive) in that order; else the exact message `"the state note needs these headings in order: What it is, Where it stands, Decisions, Open problems, How to run and test"`.
  - Hub: `store.notes_write` on topic `state` with a project calls `check_state` first (the hub wraps it: in `notes_write` tool and in `/api/notes`; `store.py` unchanged). Tools:
    - `state(project: str, ctx) -> str`: the state note text or `"no state note yet for {project}; write one with state_write(project, text) using the five headings"`.
    - `state_write(project: str, text: str, ctx) -> str`: validates, writes topic `state`, returns `"state of {project} updated"`.
    - `decide(project: str, text: str, ctx) -> str`: appends `f"- {YYYY-MM-DD} {person}: {text}"` to the note `decisions` of the project (`""` = team note), one line, caps text at 500 chars, returns `"decision recorded"`.
  - `INSTRUCTIONS` gains: `"state(project) reads the project's state note; state_write(project, text) updates it (five headings: What it is, Where it stands, Decisions, Open problems, How to run and test); decide(project, text) records one decision."`

- [ ] **Step 1: Write the failing tests** (`check_state` accept/reject/order; `state_write` refused without write role; `decide` appends in date order with author; page `/api/notes` on topic `state` validated)
- [ ] **Step 2: Run, expect failures**
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run `tests/test_memory.py tests/test_hub_tools.py tests/test_readme.py`, expect pass (README tools table: Task 8 adds rows; if `test_readme` requires them now, add the three rows in this task); commit** `feat(hub): state notes with five headings; decide()`

---

### Task 3: The start brief, rebuilt with a budget and a cache

**Files:**
- Create: `src/kaptyorka/brief.py`
- Modify: `src/kaptyorka/hub.py` (`api_brief` delegates the `start` event to `brief.build(...)`; the `prompt` event keeps today's unread-only behaviour)
- Test: `tests/test_brief.py`, `tests/test_hook.py`

**Interfaces:**
- Consumes: `store.online()`, `session_row` masking from `hub_feed`, `store.inbox`, `store.projects_for(principal)`, `ws.unattended_for`, `ws.warnings`, `store.knocks(principal)`, `_request_lines`, `ms.git_recent`, `ms.branches_with_sessions`, `store.notes_read("state", principal, project)`, `ms.project_of(agent.cwd, folders)` (the session's project from its cwd when the device reported folders; else `agent.project`).
- Produces:
  - `brief.BUDGET = 6000`, `brief.CACHE_S = 10`.
  - `brief.build(store, ws, ms, agent, principal, rules_text: str = "") -> str` returns sections in this order, each a short paragraph with a one-line heading, empty sections omitted:
    1. `rules_text` when not empty (Part 4 fills it; today `""`).
    2. `You are `<name>`, <person>, on <host>.` + online others (masked) + `Unattended workers: ...` + pending knocks (`Knocks for you: ...`) + `_request_lines`.
    3. `Projects you may read:` one line each `- <name>: <description> (working now: a, b)` from online sessions in that project.
    4. For the session's project: `State of <project>:` + the note text; then `Recent git activity (7 days):` lines `- <person> on <branch>: "<subject>" (<when>)`, plus `Branches with open sessions: main (alice/pc), work/proj-a-w1 (proj-a-w1)`.
    5. `Unread messages:` as today.
  - Trimming to `BUDGET` characters, cutting in this order until it fits: rules (replaced by `Team rules are longer than fit here; read them with rules().`), then the project list, then git activity, then the state note (cut at a paragraph boundary with `… (read the rest with state(project))`); unread messages and section 2 are never cut.
  - `brief.cached(key, builder) -> str`: per-session cache for `CACHE_S` seconds, keyed by session key, invalidated when the session's inbox changes (unread count differs) so a new message is never hidden by the cache.
  - The hub keeps the admin's warnings line and the legacy line in section 2.

- [ ] **Step 1: Write the failing tests**

```python
def test_brief_sections_in_order(...)         # all five present, in order, for a rich fixture
def test_brief_minimal_session(...)           # no project access, no notes: only section 2 (+5 if unread); len < 400; no heading without content
def test_brief_budget_cuts_in_order(...)      # oversize rules + big state note: rules replaced by the pointer first; then projects; state note last, with the pointer
def test_brief_cache_10s_but_new_message_breaks_it(...)
def test_hook_start_uses_the_new_brief(hub, home, ...)  # tests/test_hook.py: the printed brief starts with "You are"
```

- [ ] **Step 2: Run, expect failures**
- [ ] **Step 3: Implement**; keep `api_brief` under 40 lines by moving the assembly into `brief.py`.
- [ ] **Step 4: Run `tests/test_brief.py tests/test_hook.py tests/test_hub_people.py tests/test_hub_workers.py`, expect pass; commit** `feat(hub): start brief with sections, budget and cache`

---

### Task 4: The done check (hub side)

**Files:**
- Modify: `src/kaptyorka/memory.py`, `src/kaptyorka/hub.py` (route `/api/done-check`; `status` tool and `notes_write`/`state_write` feed the signals)
- Test: `tests/test_done_check.py`

**Interfaces:**
- Consumes: `done_checks` table (Task 1), `_signal(key, kind)` (Task 1), `store.may_write_project`, `store._registered` (private: inside the memory lock only), `agents` row of the session (`project`, `cwd`, `person_id`, `started`, `status`).
- Produces (on `MemoryStore`):
  - `_signal(key: str, kind: str) -> None`: sets `signal_at = now`, `signal = kind` on the session's `done_checks` row (created with `started = agents.started`) only when `signal_at` is NULL or older than the last state write for that project by that session.
  - `status_signal(key, text)`: called by the `status` tool; `_signal(key, "done")` when `"done"` is in the text (case-insensitive).
  - `state_written(key, project, person_id, now)`: records the write; a page write by the same person counts (`key = "web:<person>"`, matched by `person_id`).
  - `done_check(key: str, principal, now) -> dict`: `{"decision": "ok"}` or `{"decision": "block", "reason": <text>}`. Block when all hold: the session's project is registered and the person may write it; `signal_at` is set and newer than `started`; no state write for that project by this session or this person since `signal_at`; `blocked_at` is NULL. On block set `blocked_at = now`. Reason text, exact: `"You did work in {project} ({signal}) but its state note was not updated. Call state_write(\"{project}\", text) with the five headings (What it is, Where it stands, Decisions, Open problems, How to run and test), updating what changed, then stop. If nothing changed in the project, call status(\"done: no state change\") and stop."` A `status` containing `"no state change"` clears the pending signal.
  - Route `GET /api/done-check` (session identity from the headers like `/api/brief`): JSON of `done_check`; 400 without identity. `mode` is included: `"block"` for claude-code and codex, `"remind"` for cursor and opencode (by `agent.tool`).

- [ ] **Step 1: Write the failing tests**

```python
def test_read_only_session_is_never_blocked(...)
def test_commit_signal_blocks_once_then_never_again(...)
def test_state_write_after_signal_clears_the_block(...)
def test_state_written_from_page_by_same_person_satisfies(...)
def test_signal_is_per_session(...)             # two sessions of alice in proj-a: a commit seen in session A's cwd does not block B
def test_no_write_role_never_blocks(...)
def test_status_done_is_a_signal_and_no_state_change_clears_it(...)
def test_route_mode_by_tool(...)
```

- [ ] **Step 2: Run, expect failures**
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run `tests/test_done_check.py tests/test_memory.py`, expect pass; commit** `feat(hub): done check decides from work signals`

---

### Task 5: The stop hook in every tool

**Files:**
- Modify: `src/kaptyorka/hook.py`, `src/kaptyorka/adapters/claude.py`, `src/kaptyorka/adapters/codex.py`, `src/kaptyorka/doctor.py` (`tool_configured` expects the Stop hook for claude-code and codex), `src/kaptyorka/adapters/opencode.py` (plugin: no change if the reminder rides on the `prompt` brief; see below)
- Test: `tests/test_hook.py`, `tests/test_claude.py`, `tests/test_codex.py`, `tests/test_doctor.py`

**Interfaces (verified against the vendors' docs on 2026-11-07):**
- Claude Code `Stop` hook: stdin JSON has `session_id`, `cwd`, `stop_hook_active`, `last_assistant_message`; blocking output is `{"decision": "block", "reason": "..."}` on stdout with exit 0; anything else allows the stop; default command-hook timeout 600 s (we answer in under 3 s anyway).
- Codex `Stop` hook: same stdin fields (`session_id`, `cwd`, `stop_hook_active`); `{"decision": "block", "reason": "..."}` makes Codex continue with the reason as the next prompt; exit 0 must print JSON (print `{}` when nothing to say).
- Cursor `stop` hook: cannot block; `{"followup_message": "..."}` is submitted as the next user message (loop cap 5). Our existing `stop` entry for Cursor already prints the brief as `followup_message`; the done-check reminder is prepended to it when the hub says `block`.
- OpenCode: no verified idle event; the reminder is added to the next `prompt` brief: `/api/brief?event=prompt` prepends the done-check reason when `done_check` would block (without setting `blocked_at`; it is a reminder).
- Produces:
  - `hook.main(tool, "stop", stdin)`: calls `GET /api/done-check`; for claude-code and codex prints the block JSON when `decision == "block"` (codex: `{}` otherwise; claude-code: nothing); for cursor prints the followup JSON with the reason first and the brief after; when `stop_hook_active` is true in stdin, prints nothing (claude-code) / `{}` (codex): never block twice in one stop sequence.
  - `ClaudeAdapter.configure` adds `Stop` → `kaptyorka hook claude-code stop` next to the two existing hooks; `CodexAdapter.configure` adds `Stop` → `kaptyorka hook codex stop`; both idempotent; `unconfigure` removes them; `doctor.tool_configured` requires them. `join` on an already-joined machine adds the missing hook (the existing re-run path).

- [ ] **Step 1: Write the failing tests** (hook stop prints block JSON for claude-code on a hub `block`, nothing on `ok`, nothing when `stop_hook_active`; codex prints `{}` on ok; cursor followup carries reason + brief; configure adds the Stop hook once; doctor flags it missing)
- [ ] **Step 2: Run, expect failures**
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run `tests/test_hook.py tests/test_claude.py tests/test_codex.py tests/test_cursor.py tests/test_doctor.py tests/test_join.py`, expect pass; commit** `feat(hooks): stop hook runs the done check`

---

### Task 6: Page: project state and git activity

**Files:**
- Modify: `src/kaptyorka/web/index.html`, `src/kaptyorka/hub.py` (`GET /api/projects/{name}/activity` → `{"state": text|null, "git": git_recent(7), "branches": {...}}`, read role required)
- Test: `tests/test_page.py`, `tests/test_hub_routes.py`

- [ ] **Step 1: Page-source tests** (route referenced; the Notes tab shows a "State" block first when a project is selected and a "Recent git activity" list)
- [ ] **Step 2: Implement**: in the Notes tab, when a project is selected, show its state note first (rendered as plain paragraphs with the five headings in bold, no Markdown library) and a compact activity list (`person · branch · "subject" · when`); same tokens; no new fonts. Headless screenshot `docs/screens/project-state.png` with demo names at 1440 px; look at it; commit only that file.
- [ ] **Step 3: Run `tests/test_page.py tests/test_page_people.py tests/test_page_workers.py tests/test_hub_routes.py`, expect pass; commit** `feat(page): project state and recent git activity`

---

### Task 7: Backup and restore

**Files:**
- Create: `src/kaptyorka/backup.py`
- Modify: `src/kaptyorka/cli.py` (`backup <user@host:path> [--nightly]`, `restore <file>`), `src/kaptyorka/service.py` only if a timer helper is needed (`install_timer(name, argv, on_calendar)` for systemd user timers; launchd `StartCalendarInterval`)
- Test: `tests/test_backup.py`, `tests/test_cli.py`

**Interfaces:**
- Produces:
  - `backup.make(home: Path, run=subprocess.run, now=time.time) -> Tuple[Path, bool]`: copies `hub.db` with `sqlite3.connect(src).backup(dst)` into a temp folder, tars it with the `files/` folder as `kaptyorka-<YYYYmmdd-HHMM>.tar`; when `shutil.which("gpg")`, runs `gpg --batch --yes --symmetric --cipher-algo AES256 --passphrase-file <home>/backup-pass -o <tar>.gpg <tar>` (creating `backup-pass` with `secrets.token_urlsafe(32)` and mode 0600 on first use) and returns `(path.gpg, True)`; else `(path.tar, False)`.
  - `backup.send(path: Path, dest: str, run) -> None`: `run(["scp", "-o", "BatchMode=yes", "--", str(path), dest])`; dest validated as `user@host:path` (no leading `-`).
  - `backup.run_backup(home, dest, run, out=print) -> int`: prints `"backup: <name> sent to <dest> (encrypted with gpg; passphrase in <home>/backup-pass, keep a copy)"` or `"backup: <name> sent to <dest> UNENCRYPTED (gpg not installed)"`; exit 1 with the failing command's stderr first line otherwise.
  - `backup.restore(home, file: Path, run) -> int`: refuses when the hub service is active (`service.is_active("kaptyorka-hub")`) with `"stop the hub first: kaptyorka stop"`; decrypts with the passphrase file when the name ends in `.gpg`; checks the tar holds `hub.db`; moves the current `hub.db` and `files/` to `hub.db.before-restore` and `files.before-restore` (replacing older ones); extracts; prints `"restored <file>; start the hub again with kaptyorka start"`.
  - `--nightly`: installs a user timer `kaptyorka-backup` running `kaptyorka backup <dest>` at 03:00 local (systemd: `OnCalendar=*-*-* 03:00:00` with a service unit; launchd: `StartCalendarInterval` hour 3); prints the unit lines; `kaptyorka stop` leaves it alone; `kaptyorka remove` removes it (list it in `remove.py`'s units).

- [ ] **Step 1: Write the failing tests** (fake `run` recording argv; a real small SQLite file and files folder in a tmp home; gpg present/absent via monkeypatched `shutil.which`; dest validation; restore refuses while the hub runs; restore round trip without gpg; timer unit text)
- [ ] **Step 2: Run, expect failures**
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run `tests/test_backup.py tests/test_cli.py tests/test_remove.py tests/test_serve.py`, expect pass; commit** `feat(cli): kaptyorka backup and restore`

---

### Task 8: Slow test, README, CHANGELOG, 0.4.0

**Files:**
- Create: `tests/test_memory_slow.py`
- Modify: `README.md` (What agents can do: `state`, `state_write`, `decide`; a section "What a session knows on start" with the five brief sections and the budget; a section "The done check" saying when it blocks, that it blocks once, and the per-tool mode table: Claude Code block, Codex block, Cursor follow-up message, OpenCode reminder on the next prompt; Commands: `backup`, `restore`; Files: `backup-pass`), `CHANGELOG.md`, `pyproject.toml`, `src/kaptyorka/__init__.py`, `tests/test_cli.py`
- Test: `tests/test_readme.py`

- [ ] **Step 1: Slow test** `test_done_check_blocks_a_real_session_once`: a real Claude Code `--bg` session in a throwaway folder beside the repo (as `tests/test_worker_slow.py` does), joined to an in-process hub with a real device, project registered with write role; the task tells it to commit a file; the bridge heartbeat (real `ClaudeAdapter` + real `facts.git`) records the commit; assert the hub's `done_check` answers `block` once, that the session then calls `state_write` (visible as the state note) and stops; teardown as in the worker slow test. Run `python3 -m pytest tests/test_memory_slow.py -m slow -q`, expect 1 passed (fix product code only for real defects; report them).
- [ ] **Step 2: Version bump in the four places; README and CHANGELOG; `python3 -m pytest tests -q -W default`, expect all pass, no warnings.**
- [ ] **Step 3: Commit** `docs: memory, done check, backup; v0.4.0`

---

## Self-review notes

- Spec coverage: project state note and tools (2), brief sections and budget (3), done check incl. per-tool modes and "blocked once" (4, 5), git facts 30 days (1), decisions (2), backup/nightly/restore (7), page (6), slow test and README (8). The "rules" section of the brief is a hook left empty for Part 4.
- Hook budgets: the 3 s HTTP timeout stays; the brief cache and stored-row done check keep the hub's answer fast.
- Per-tool blocking modes come from the vendors' docs read on 2026-11-07 (Claude Code and Codex block via `{"decision":"block","reason"}`; Cursor `followup_message`; OpenCode has no verified idle hook, so the reminder rides on the next prompt brief).
- Open question for the owner, not blocking: should the done check also run for workers (sessions the hub started)? Default here: yes, same rule; workers are told in their first message that state_write is expected.
