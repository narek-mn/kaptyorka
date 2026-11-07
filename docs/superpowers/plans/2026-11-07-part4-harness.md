# Part 4: Harness (0.5) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Team rules and team skills live in the hub, reach every tool on every joined machine without anyone copying files, the hub shows per session which layers (rules text, hooks, skills) are in place, and agents can propose rules and log rule conflicts for the admin.

**Architecture:** A new module `rules.py` (`RulesStore(store)`, same lock and connection as `MemoryStore`) holds versioned team rules, skill files, proposals and conflicts; `store.py` gets one `ensure_schema` line. Rules reach agents as text through the start brief (`brief.compose` already takes `rules_text`) under a fixed precedence heading. Skills reach machines through the bridge: the heartbeat answer carries a skills version, the bridge fetches the files when it changes and writes them to `~/.claude/skills/team-<name>/SKILL.md`, which Claude Code reads directly and OpenCode and Cursor read as a compatibility path (verified against vendor docs 2026-11-07: opencode.ai/docs/skills lists `~/.claude/skills` as a global path; cursor.com/docs/context/skills lists `~/.claude/skills/`; Codex's skills folder could not be verified, so Codex gets rules text only). The bridge reports per tool which layers are in place; the Team tab shows three marks per session. A Rules tab on the page edits rules, lists skills, and decides proposals.

**Tech Stack:** Python 3.10, `mcp` SDK (Starlette), SQLite, one HTML page, no new dependency.

**Spec:** `docs/superpowers/specs/2026-11-06-team-brain-design.md`, sections "Part 4: Harness (0.5)", "Serving team rules", "Rule conflicts", "Constraints", "My calls" (rules kept forever; brief budget 6,000 cut rules first).

## Global Constraints

- Python 3.10 floor; no new dependency.
- No team names in code, tests, fixtures or screenshots; use alice, bob, proj-a, box-1.
- `Store.lock` is not reentrant; `RulesStore` follows the same rule as `MemoryStore` (no public `Store` method inside `with self.lock`; `log_add` after the lock).
- `store.py` changes only where a task names the exact line; new tables and methods live in `rules.py`.
- Only scope `team` is accepted in 0.5: project rules live in the project repo (spec), and the brief says so. The schema keeps a `scope` column as the spec names it.
- Hooks are kaptyorka's own and installed by `join`; nothing in this part lets the hub install a hook.
- The bridge writes files only under `~/.claude/skills/team-*/` and only replaces or removes a folder whose `SKILL.md` carries the kaptyorka marker line; `leave` removes exactly those.
- A skill name matches `^[a-z0-9]+(-[a-z0-9]+)*$` and is 1 to 48 characters (so `team-<name>` stays under OpenCode's 64-character id limit; Cursor needs the frontmatter `name` to equal the folder name).
- Caps: rules text 20,000 characters; one skill text 20,000; at most 50 skills; proposal text 2,000, evidence 4,000; conflict fields 1,000 each; at most 20 open proposals per person.
- The brief's rules section keeps the existing cut rule (rules go first, replaced by `RULES_POINTER`); the full text is one `rules()` call away.
- Every external command goes through an injected `run`; tests never call real agent tools except under `-m slow`.
- Version bump touches `pyproject.toml`, `src/kaptyorka/__init__.py`, `tests/test_cli.py`, `CHANGELOG.md`.
- Commit trailers on every commit: `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>` and `Claude-Session: https://claude.ai/code/session_01WHiZ1E64m7csjvMWwiGkdu`.

## Review Focus

1. A person's own skill folder named `team-foo` without the marker line must never be overwritten or deleted by the bridge (Task 4 `test_sync_leaves_foreign_folder_alone`).
2. Saving rules identical to the current text must not create a new version (Task 1 `test_set_rules_same_text_is_no_new_version`).
3. A rejected proposal's reason must reach the proposer's session even when that session is offline now, and never another person's session (Task 2 `test_reject_notifies_proposer_only`).
4. A skills version from the hub that the bridge already applied must not rewrite the files on every heartbeat (Task 4 `test_sync_is_idempotent_per_version`).
5. A 0.4 bridge (no `harness` in facts, no skills fetch) must still heartbeat fine and show dashes, not "missing", on the Team tab (Task 5 `test_old_bridge_shows_unknown_layers`).

---

## File map

| File | Responsibility |
|---|---|
| `src/kaptyorka/rules.py` (new) | `ensure_schema`, `RulesStore`: rules versions, skills, skills version hash, proposals, conflicts; the fixed heading and trailer text. |
| `src/kaptyorka/skills_sync.py` (new) | Writes and removes `~/.claude/skills/team-<name>/SKILL.md` with the marker; `~/.kaptyorka/skills.json` remembers the applied version. |
| `src/kaptyorka/hub.py` | Tools `rules`, `propose_rule`, `conflict`; routes for rules, skills, proposals, conflicts; heartbeat answer `skills_version`; brief gets the rules text. |
| `src/kaptyorka/brief.py` | Rules section heading; "rules only" line in section 2. |
| `src/kaptyorka/bridge.py` | Skills sync after a heartbeat whose version differs; `harness` in facts. |
| `src/kaptyorka/facts.py` | `harness(adapters, home, skills_current)`: per tool `{rules, hooks, skills}`. |
| `src/kaptyorka/workers.py` | `clean_facts` passes `harness` (bools per known tool). |
| `src/kaptyorka/hub_feed.py` | `session_row` adds `harness` for the session's tool from its device facts. |
| `src/kaptyorka/cli.py` | `kaptyorka rules push <dir>`. |
| `src/kaptyorka/remove.py` | `leave` removes team skill folders. |
| `src/kaptyorka/web/index.html` | Rules tab; three marks per session on the Team tab. |
| `README.md`, `CHANGELOG.md`, version files | 0.5.0. |
| tests | `test_rules.py`, `test_hub_rules.py`, `test_skills_sync.py`, plus edits to `test_brief.py`, `test_bridge.py`, `test_facts.py`, `test_workers.py`, `test_page.py`, `test_cli.py`, `test_remove.py`, `test_readme.py`. |

---

### Task 1: Rules store

**Files:**
- Create: `src/kaptyorka/rules.py`
- Modify: `src/kaptyorka/store.py` (`Store.__init__`: `rules.ensure_schema(self.db)` right after the `memory.ensure_schema` line; one line)
- Test: `tests/test_rules.py`

**Interfaces:**
- Consumes: `Store` (`lock`, `db`, `clock`, `log_add`, `Principal`), the `MemoryStore` pattern in `memory.py`.
- Produces: in `rules.py`:
  - `ensure_schema(db)` creates `rules(id INTEGER PRIMARY KEY, scope TEXT NOT NULL, version INTEGER NOT NULL, text TEXT NOT NULL, author TEXT NOT NULL, time REAL NOT NULL, UNIQUE(scope, version))`, `skills(scope TEXT NOT NULL, name TEXT NOT NULL, text TEXT NOT NULL, author TEXT NOT NULL, time REAL NOT NULL, PRIMARY KEY(scope, name))`, `proposals(id INTEGER PRIMARY KEY, person_id INTEGER NOT NULL, session_key TEXT NOT NULL, text TEXT NOT NULL, evidence TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('open','accepted','rejected')), reason TEXT NOT NULL DEFAULT '', decided_by TEXT NOT NULL DEFAULT '', time REAL NOT NULL, decided_at REAL)`, `conflicts(id INTEGER PRIMARY KEY, person_id INTEGER NOT NULL, session_key TEXT NOT NULL, project TEXT NOT NULL, rule_a TEXT NOT NULL, rule_b TEXT NOT NULL, time REAL NOT NULL)`.
  - Constants: `SCOPE = "team"`; `NAME_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")`; `MAX_NAME = 48`; `MAX_TEXT = 20000`; `MAX_SKILLS = 50`; `MAX_PROPOSAL = 2000`; `MAX_EVIDENCE = 4000`; `MAX_CONFLICT = 1000`; `MAX_OPEN = 20`; `HEADING = "Team rules (project rules in this repo override these; these override your personal rules):"`; `TRAILER = "If your person corrects you the same way twice, call propose_rule(text, evidence). If two rules clash and you must ask your person, call conflict(rule_a, rule_b) so the admin can fix the text."`
  - `def rules_text(text: str) -> str`: `""` for empty text, else `HEADING + "\n\n" + text.strip() + "\n\n" + TRAILER`.
  - `def check_name(name: str) -> str`: returns the name or raises `KaptyorkaError("skill name must be 1-48 characters of lowercase letters, digits and single hyphens")`.
  - `class RulesStore(store)` with `lock`, `db`, `clock` like `MemoryStore`:
    - `current(scope: str = SCOPE) -> dict`: `{"text", "version", "author", "time"}`; version 0 and empty text when none.
    - `set_rules(text: str, by: str, scope: str = SCOPE) -> int`: new version `max+1`; returns the version; when `text.strip()` equals the current text returns the current version and writes nothing; raises on `len(text) > MAX_TEXT` ("rules text too long: limit 20000") and on a scope other than `SCOPE` ("only team rules are kept in the hub; project rules live in the project repo"). Logs `rules` with `f"team rules v{n} by {by}"`.
    - `history(scope: str = SCOPE, limit: int = 20) -> List[dict]` newest first.
    - `skills(scope: str = SCOPE) -> List[dict]`: `[{"name", "text", "author", "time"}]` sorted by name.
    - `set_skills(items: Dict[str, str], by: str, scope: str = SCOPE) -> dict`: replaces the whole set (names not in `items` are removed); `check_name` each; text cap; `len(items) > MAX_SKILLS` raises ("too many skills: limit 50"); unchanged rows keep their time; returns `{"added": [...], "changed": [...], "removed": [...]}`; logs `skills` with that summary when anything changed.
    - `skills_version(scope: str = SCOPE) -> str`: first 12 hex characters of sha256 over `"\n".join(f"{name}\0{text}" for sorted rows)`; `""` when no skills.
    - `propose(principal, session_key: str, text: str, evidence: str) -> int`: caps; `MAX_OPEN` open per person raises ("you have 20 open proposals; wait for the admin"); logs `propose`.
    - `proposals(status: Optional[str] = None, limit: int = 100) -> List[dict]` newest first, with `person` name joined.
    - `decide(proposal_id: int, accept: bool, reason: str, by: str) -> dict`: only an `open` proposal (else "proposal already decided" / "no such proposal"); accept appends `"\n\n" + text` to the current team rules as a new version via `set_rules`; returns the proposal row (with `session_key`, so the hub can notify); logs `proposal` with accepted/rejected.
    - `conflict(principal, session_key: str, project: str, rule_a: str, rule_b: str) -> int`; caps; logs `conflict`.
    - `conflicts(limit: int = 100) -> List[dict]` newest first with `person`.

- [ ] **Step 1: Write the failing tests** in `tests/test_rules.py` (fake clock as `test_memory.py` does): `test_rules_versions_increment_and_history`, `test_set_rules_same_text_is_no_new_version`, `test_set_rules_rejects_project_scope`, `test_skills_replace_set_and_version_hash` (version changes when a text changes, is `""` when empty, `removed` lists the dropped name), `test_skill_name_rules` (`"Team"`, `"a--b"`, `"-a"`, 49 chars rejected; `"a"`, `"my-skill-2"` accepted), `test_propose_caps_and_open_limit`, `test_accept_appends_new_rules_version`, `test_decide_twice_raises`, `test_conflict_logged`.
- [ ] **Step 2: Run** `python3 -m pytest tests/test_rules.py -q`. Expected: import error.
- [ ] **Step 3: Implement `rules.py` and the one `store.py` line.**
- [ ] **Step 4: Run** `python3 -m pytest tests/test_rules.py tests/test_store.py -q`. Expected: PASS.
- [ ] **Step 5: Commit** `feat(rules): versioned team rules, skills, proposals and conflicts in the hub`.

---

### Task 2: Hub tools, routes and the brief

**Files:**
- Modify: `src/kaptyorka/hub.py` (`build_app`: `rs = RulesStore(store)`; tools; routes; heartbeat answer; `INSTRUCTIONS`), `src/kaptyorka/brief.py`
- Test: `tests/test_hub_rules.py`, `tests/test_brief.py`, `tests/test_hub_tools.py` (tool count)

**Interfaces:**
- Consumes: Task 1; `ws.notify(to_key, text)` in `workers.py` for hub-originated messages; `brief.compose(..., rules_text=...)`.
- Produces:
  - Tools (23 in all): `rules() -> str` returns `rs.current()["text"]` with a first line `f"Team rules v{version}:"` or `"No team rules yet."`; `propose_rule(text: str, evidence: str) -> str` returns `f"Proposal {id} filed; the admin decides on the Rules tab."`; `conflict(rule_a: str, rule_b: str) -> str` returns `f"Conflict {id} logged for the admin."` (project = `session_project(agent, principal)`).
  - Routes: `GET /api/rules` → `{"text","version","author","time","skills":[{"name","author","time","size"}],"skills_version"}` for any principal; `POST /api/rules` body `{"text"}` admin only (403 "admin only"), answers `{"version"}`; `GET /api/rules/history`; `PUT /api/skills` body `{"skills": {name: text}}` admin only, answers the `set_skills` summary; `GET /bridge/skills` → `{"version", "skills": [{"name","text"}]}` for any device token; `GET /api/proposals?status=open` (admin sees all; others their own); `POST /api/proposals/{id}` body `{"accept": bool, "reason"}` admin only, then `ws.notify(session_key, text)` where text is `f"Your rule proposal {id} was accepted by {by}: team rules are now v{n}."` or `f"Your rule proposal {id} was rejected by {by}: {reason}"`; `GET /api/conflicts` admin only.
  - Heartbeat answer gains `"skills_version": rs.skills_version()`.
  - Brief: `api_brief` passes `rules_text=rules.rules_text(rs.current()["text"])` into `brief.compose`; `brief._fit` is unchanged (rules are cut first to `RULES_POINTER`).
  - `INSTRUCTIONS` adds one sentence naming `rules()`, `propose_rule(text, evidence)` and `conflict(rule_a, rule_b)`.

- [ ] **Step 1: Write the failing tests** with the in-process app helpers (`tests/helpers.py`): `test_rules_tool_and_brief_heading` (after admin POST, `rules()` returns the text and a start brief contains `HEADING` and `TRAILER`), `test_long_rules_cut_to_pointer_in_brief` (rules of 7,000 characters: brief under 6,000, contains `RULES_POINTER`, `rules()` returns the whole text), `test_post_rules_member_forbidden` (403), `test_put_skills_and_bridge_fetch` (admin PUT two skills; a member device GET `/bridge/skills` sees both; heartbeat answer has the same `skills_version`), `test_propose_and_accept_makes_new_version`, `test_reject_notifies_proposer_only` (two people; bob proposes from session `s1`; the admin rejects; `inbox` of `s1` has the reason; alice's session inbox does not), `test_conflict_tool_logs_with_project`, `test_tool_count_is_23` (adjust the existing count test).
- [ ] **Step 2: Run** `python3 -m pytest tests/test_hub_rules.py -q`. Expected: FAIL (unknown tool / 404).
- [ ] **Step 3: Implement.** Admin checks follow the existing `principal.is_admin()` routes (`hub.py` around line 663).
- [ ] **Step 4: Run** `python3 -m pytest tests/test_hub_rules.py tests/test_brief.py tests/test_hub_tools.py tests/test_hub_routes.py -q`. Expected: PASS.
- [ ] **Step 5: Commit** `feat(hub): rules, propose_rule and conflict tools; rules and skills routes; rules in the brief`.

---

### Task 3: `kaptyorka rules push <dir>`

**Files:**
- Modify: `src/kaptyorka/cli.py` (subparser `rules` with sub-command `push <dir>`)
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: Task 2 routes; `paths.load_config()` for hub and token; `http.request`.
- Produces: `def _rules_push(args) -> int`: reads `<dir>/RULES.md` (missing: print `"no RULES.md in <dir>"`, return 1) and every `<dir>/skills/*.md` (name = file stem, checked with `rules.check_name` before any upload: one bad name fails the whole push with its message), `POST /api/rules` then `PUT /api/skills`, prints `f"team rules v{n}; skills: {added} added, {changed} changed, {removed} removed"`. A 403 prints `"only the admin's device may push rules"` and returns 1.

- [ ] **Step 1: Write the failing tests** (fake `http.request` as other CLI tests do): `test_rules_push_uploads_rules_and_skills`, `test_rules_push_missing_rules_md`, `test_rules_push_bad_skill_name_fails_before_upload`, `test_rules_push_forbidden`.
- [ ] **Step 2: Run** `python3 -m pytest tests/test_cli.py -q -k rules`. Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `python3 -m pytest tests/test_cli.py -q`. Expected: PASS.
- [ ] **Step 5: Commit** `feat(cli): kaptyorka rules push <dir>`.

---

### Task 4: Skills on every machine

**Files:**
- Create: `src/kaptyorka/skills_sync.py`
- Modify: `src/kaptyorka/bridge.py` (after `self.hub` is set in `heartbeat`: `self._sync_skills()`), `src/kaptyorka/remove.py` (`leave` calls `skills_sync.remove_all(Path.home(), log)`)
- Test: `tests/test_skills_sync.py`, `tests/test_bridge.py`, `tests/test_remove.py`

**Interfaces:**
- Consumes: `GET /bridge/skills` (Task 2); `write_atomic` from `adapters/base.py`; `paths.home()`.
- Produces in `skills_sync.py`:
  - `MARKER = "<!-- kaptyorka: team skill, replaced on change; edit it in the team's rules folder -->"`; `PREFIX = "team-"`; `STATE = "skills.json"` (in `paths.home()`): `{"version": str, "names": [str]}`.
  - `def render(name: str, text: str) -> str`: frontmatter `---\nname: team-<name>\ndescription: <d>\n---\n`, then `MARKER`, blank line, body. `<d>` is the `description:` value of the skill text's own frontmatter when it has one (the frontmatter block is then dropped from the body), else the first non-empty body line; cut to 200 characters, one line.
  - `def sync(home: Path, version: str, items: List[dict], log) -> List[str]`: writes `home/.claude/skills/team-<name>/SKILL.md` for each item whose rendered text differs; removes every `team-*` folder whose `SKILL.md` contains `MARKER` and whose name is not in `items`; never touches a folder without the marker (logs `f"left alone, not ours: {path}"`); returns one line per write or removal; saves `STATE`.
  - `def applied_version(home_kaptyorka: Path) -> str` reads `STATE`.
  - `def remove_all(home: Path, log) -> List[str]`: removes every marked `team-*` folder and `STATE`.
  - In `bridge.py`: `_sync_skills()`: `v = self.hub.get("skills_version")`; when `v` is a string and differs from `applied_version(paths.home())`, GET `/bridge/skills` and call `sync` with its items; any error is `_report("skills", msg)` and does not stop the heartbeat loop.

- [ ] **Step 1: Write the failing tests** with `tmp_path` as home: `test_render_frontmatter_from_skill_description`, `test_render_description_from_first_line`, `test_sync_writes_and_removes_marked_folders`, `test_sync_leaves_foreign_folder_alone` (a `team-foo/SKILL.md` without the marker stays byte-identical, and is not deleted when `foo` is absent from items), `test_sync_is_idempotent_per_version` (second `sync` with the same items returns `[]` and changes no mtime), `test_remove_all`, in `test_bridge.py` `test_bridge_fetches_skills_once_per_version` (two heartbeats with the same version: one GET; a new version: another GET), in `test_remove.py` `test_leave_removes_team_skills`.
- [ ] **Step 2: Run** `python3 -m pytest tests/test_skills_sync.py -q`. Expected: import error.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `python3 -m pytest tests/test_skills_sync.py tests/test_bridge.py tests/test_remove.py -q`. Expected: PASS.
- [ ] **Step 5: Commit** `feat(bridge): team skills written to ~/.claude/skills/team-<name> and removed on leave`.

---

### Task 5: Harness layers per session

**Files:**
- Modify: `src/kaptyorka/facts.py` (`harness(adapters, home, skills_current, platform)`; `machine()` adds `"harness"`), `src/kaptyorka/bridge.py` (`_machine_facts` passes `skills_current = applied_version(...) == self.hub.get("skills_version", "")`), `src/kaptyorka/workers.py` (`clean_facts` keeps `harness` as `{tool: {"rules": bool, "hooks": bool, "skills": bool|None}}` for known tools only), `src/kaptyorka/hub_feed.py` (`session_row` adds `"harness"`), `src/kaptyorka/brief.py` (`_you` adds the line), `src/kaptyorka/web/index.html` (Team tab marks)
- Test: `tests/test_facts.py`, `tests/test_workers.py`, `tests/test_hub_routes.py` or `tests/test_page.py`, `tests/test_brief.py`

**Interfaces:**
- Consumes: `doctor._has_hooks`, `doctor.tool_configured`, `skills_sync.applied_version`, `WorkerStore.facts(device_id)`.
- Produces:
  - `facts.harness(adapters, home: Path, skills_current: bool) -> Dict[str, dict]` for installed tools: `rules` = the hook that carries the brief is present (claude-code `SessionStart`; codex `SessionStart`; cursor `stop`; opencode the plugin file); `hooks` = `doctor.tool_configured(adapter, home)`; `skills` = `skills_current` for claude-code, opencode and cursor, `None` for codex.
  - `session_row` adds `"harness": facts.get("harness", {}).get(a.tool)` (a dict, or `None` when the device never reported one).
  - Brief section 2 line, only when the session's own entry exists and `hooks` or `skills` is `False`: `"Harness on this tool: rules only (hooks or skills missing; run kaptyorka doctor --fix)."`
  - Team tab: three marks after the tool name, letters `R H S` in `var(--dim)` when true, a struck/`--fg-weak` letter when false, a `–` for `None`/unknown; title text `"rules text: yes, hooks: no, skills: n/a"`.

- [ ] **Step 1: Write the failing tests**: `test_harness_layers_per_tool` (fake home with Claude hooks installed and `skills_current=True` → `{"rules": True, "hooks": True, "skills": True}`; codex gets `skills: None`), `test_clean_facts_keeps_harness_known_tools_only`, `test_session_row_has_harness_for_its_tool`, `test_old_bridge_shows_unknown_layers` (heartbeat facts without `harness`: `session_row["harness"] is None` and the brief has no harness line), `test_brief_rules_only_line`.
- [ ] **Step 2: Run** the named test files. Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `python3 -m pytest tests/test_facts.py tests/test_workers.py tests/test_hub_routes.py tests/test_brief.py tests/test_page.py -q`. Expected: PASS.
- [ ] **Step 5: Commit** `feat: per-session harness marks (rules, hooks, skills)`.

---

### Task 6: Rules tab

**Files:**
- Modify: `src/kaptyorka/web/index.html`
- Create: `docs/screens/rules.png`
- Test: `tests/test_page.py`

**Interfaces:**
- Consumes: Task 2 routes.
- Produces: a `rules` tab (icon `#i-rules`, a simple document glyph in the existing symbol style) visible to everyone, between Notes and Machines: the current team rules (version, author, time) rendered as plain paragraphs; for the admin an Edit button that turns it into a textarea with Save (`POST /api/rules`) and Cancel; a "Skills" list (name, size, time); "Proposals" (open first; admin gets Accept and Reject, Reject asks for a reason in an inline input, never `prompt()`); "Conflicts" (admin only). The tab count badge shows open proposals for the admin. Same tokens, no new fonts, phone width works like the other tabs.

- [ ] **Step 1: Write the failing test** `test_page_has_rules_tab` (the HTML contains `data-view="rules"`, `/api/rules`, `/api/proposals`; `VIEWS` includes `"rules"`).
- [ ] **Step 2: Implement.** Headless screenshot `docs/screens/rules.png` with demo names at 1440 px the same way `docs/screens/project-state.png` was made; look at it; commit only that file.
- [ ] **Step 3: Run** `python3 -m pytest tests/test_page.py -q`. Expected: PASS.
- [ ] **Step 4: Commit** `feat(page): Rules tab`.

---

### Task 7: Docs and version 0.5.0

**Files:**
- Modify: `README.md` (new section "Team rules and skills" after "What a session knows on start"; the brief section's "Team rules (empty for now)" line; Commands table `rules push`; "What `join` changes" and "Leave" mention the skills folder; tool count 23; Files section `skills.json`), `CHANGELOG.md`, `pyproject.toml`, `src/kaptyorka/__init__.py`, `tests/test_cli.py`
- Test: `tests/test_readme.py`

- [ ] **Step 1: Write** the README section: where rules live (the team's private folder: `RULES.md` plus `skills/*.md`), how to push (`kaptyorka rules push <dir>`), the precedence sentence verbatim from `HEADING`, how skills land (`~/.claude/skills/team-<name>/`, read by Claude Code, OpenCode and Cursor; Codex gets the rules text only), the three marks, proposals and conflicts, and that project rules stay in the project repo.
- [ ] **Step 2: Bump** to 0.5.0 in the four files; CHANGELOG entry listing the above and the new file.
- [ ] **Step 3: Run** `python3 -m pytest tests -q -W default` (full suite, once). Expected: all pass, no warnings.
- [ ] **Step 4: Commit** `docs: team rules and skills; 0.5.0`.
