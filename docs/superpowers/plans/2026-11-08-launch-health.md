# Launch health (0.6.1) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A worker or manager start that cannot work says why (tool not logged in, folder not trusted) instead of failing silently; the hub never double-starts the manager after a failure; the Machines tab shows per machine which agent tools are installed and logged in; Settings says why the manager is not running.

**Architecture:** The bridge learns each tool's login state through the tool's own command (`claude auth status`, `codex login status`, `opencode auth list`) at most every 10 minutes and sends it in the heartbeat facts (`logins`). The hub cleans and stores it like other facts, shows it on Machines and in the admin warnings, and puts it into a "did not show up" error. The manager keep-alive waits two minutes after a failure before a relaunch, and the launcher clears a leftover systemd scope before starting. `/api/settings` gains the manager's launch reason.

**Tech Stack:** Python 3.10, Starlette, SQLite, one HTML page; no new dependency.

**Spec:** `docs/superpowers/specs/2026-11-06-team-brain-design.md` (Part 2 workers, Part 5 manager; "Anti-goals": silent failure, manual steps nobody is told about) and the live incident of 2026-11-07 on the hub machine (recorded in the git-ignored `docs/superpowers/brainstorm/2026-11-06-hub-as-brain.md`): the manager session started with an expired Claude login and an untrusted folder, sat silent, was declared failed after two minutes, the hub relaunched in the same second, the second start was refused ("Unit kaptyorka-worker-manager.scope was already loaded"), both counted against the two-per-hour limit.

## Global Constraints

- Python 3.10 floor; no new dependency; the page stays one HTML file, dark, no build.
- No team names in code, tests, fixtures or screenshots (alice, bob, carol, proj-a, box-1).
- `Store.lock` is not reentrant; `store.py` is not touched in this plan.
- Never run `kaptyorka` commands or tool login commands against the real machine in tests: every call of `claude`, `codex`, `opencode` goes through the injectable `run` and is faked in tests.
- Wrong state gets one plain sentence; product text in everyday words ("Claude Code is not logged in on box-1", never "auth status false").
- Version bump to 0.6.1 in `pyproject.toml`, `src/kaptyorka/__init__.py`, `tests/test_cli.py`; new `## 0.6.1` section in CHANGELOG. Tag v0.6.1 only after kursi's PASS and green CI; Narek said yes in advance to 0.6.1 only; the coordinator who heard him pushes the tag.
- Commit trailers: the two lines exactly as in `git log -1 --format=%B` of HEAD; copy, never retype.
- Whole suite before every commit; slow page tests once per page change.

## Review Focus

1. A relaunch must never be ordered while the previous manager session may still be stopping: after a failure the hub waits `RELAUNCH_S` from the failure time, not from the launch time (Task 1 `test_no_relaunch_within_two_minutes_of_a_failure`).
2. A leftover scope must not make the next start fail: the launcher stops `kaptyorka-worker-<name>.scope` before `systemd-run` (Task 1 `test_detach_clears_a_leftover_scope`).
3. A "did not show up" error names the login problem when the tool reports not logged in (Task 2 `test_no_show_error_names_missing_login`).
4. The login check is bounded: each tool command runs with a 5 s timeout (at most about 15 s once per 10 minutes inside a heartbeat) and failures mean "unknown", not "not logged in" (Task 2 `test_login_check_timeout_is_unknown`).
5. Members must not see other people's machines' login state beyond what `machines()` already lets them see (Task 3: the field rides on the existing device facts, which are already scoped; `test_member_sees_only_own_machine_logins`).

---

## File map

| File | Responsibility |
|---|---|
| `src/kaptyorka/launch.py` | stop a leftover scope before `systemd-run`. |
| `src/kaptyorka/manager.py` | `may_launch`: wait after a failure; `launch_reason()`; `last_failure()`. |
| `src/kaptyorka/hub.py` | `/api/settings` gains `manager_reason` and `manager_error`; warnings include logins. |
| `src/kaptyorka/adapters/*.py`, `facts.py` | `logged_in(run)` per adapter; `facts.machine` adds `logins` (cached 600 s). |
| `src/kaptyorka/workers.py` | `clean_facts` accepts `logins`; `warnings()` adds "not logged in"; no-show error text. |
| `src/kaptyorka/bridge.py` | the no-show error carries the login state. |
| `src/kaptyorka/web/index.html` | Machines cards: tool tag with "not logged in"; Settings: manager reason and last error. |
| `README.md`, `CHANGELOG.md`, version files | 0.6.1. |
| tests | `test_manager.py`, `test_launch.py`, `test_bridge.py`, `test_facts.py`, `test_workers.py`, `test_hub_manager.py`, `test_page.py`, `test_cli.py`. |

---

### Task 1: Never double-start the manager

**Files:**
- Modify: `src/kaptyorka/manager.py`, `src/kaptyorka/launch.py`, `src/kaptyorka/hub.py` (settings route only)
- Test: `tests/test_manager.py`, `tests/test_launch.py`, `tests/test_hub_manager.py`

**Interfaces:**
- `ManagerStore.last_failure() -> Optional[dict]`: the manager worker row when its state is `failed`: `{"created", "error", "failed_at"}` where `failed_at` is the row's `closed_at` (the workers table sets it when a row turns failed, workers.py ~414 and ~476). No new setting. Root cause: today's two-minute wait counts from the launch, and the bridge's two-minute no-show timeout ends at the same moment.
- `may_launch(now)`: two new refusals. (a) Before the launch counting: when the admin device's stored facts say `logins["claude-code"] is False`: `(False, "Claude Code is not logged in on the hub machine; run claude auth login there")`, so a logged-out machine never uses up the two launches per hour (test `test_no_launch_while_claude_is_logged_out`). (b) Right after the "already running" one: `(False, "the manager failed less than 2 minutes ago; it tries again by itself")` while `now - failed_at < RELAUNCH_S`. Every reason ends with the next step: the two-per-hour reason becomes "the manager was launched twice in the last hour; it tries again by itself within the hour"; the trust reason keeps today's `start --manager` hint; the offline-bridge reason adds "check kaptyorka-bridge on the hub machine".
- `launch_reason(now) -> str`: `""` when `may_launch` is ok, else its reason; `"the manager is switched off"` when disabled.
- `Launcher.detach`: on Linux with `systemd-run`, first run `["systemctl", "--user", "stop", f"kaptyorka-worker-{name}.scope"]` through `self.run` with `timeout=5`, ignoring every error including a timeout; then `systemd-run` as today. Code comment: worker names are the primary key, so the only scope that can carry that name belongs to an ended row; stopping it is safe. The existing "Workspace not trusted" detection in `detach` stays exactly as it is.
- `/api/settings` (admin) adds `"manager_state"`: `"starting"` | `"up"` | `"off"` | `"blocked"` (from the newest manager row and `enabled()`), `"manager_reason": mm.launch_reason(now)` (empty when starting or up) and `"manager_error"`: the error only while the newest manager row is `failed`, else `""`.

- [ ] **Step 1: Failing tests**

```python
# tests/test_manager.py
def test_no_relaunch_within_two_minutes_of_a_failure(ms_with_online_admin):
    # a failed manager row 30 s ago → may_launch False with "failed less than 2 minutes ago"; at +121 s → True
def test_launch_reason_texts(...):
    # disabled → "the manager is switched off"; ok → ""
# tests/test_launch.py
def test_detach_clears_a_leftover_scope(launcher_linux_with_systemd):
    # the recorded popen calls start with ["systemctl","--user","stop","kaptyorka-worker-w1.scope"] then ["systemd-run",...]
# tests/test_hub_manager.py
def test_settings_carry_the_manager_reason_and_last_error(hub):
    # manager enabled, no admin device online → manager_reason starts with "the admin's bridge on the hub machine is offline"; a failed row → manager_error == its error and manager_state == "blocked"; a starting row → manager_state == "starting" and manager_reason == ""
def test_no_launch_while_claude_is_logged_out(...):
    # admin device facts logins {"claude-code": False} → may_launch False with "Claude Code is not logged in on the hub machine"; launches_last_hour stays 0
```

- [ ] **Step 2: Run, fail. Step 3: Implement. Step 4: Whole suite. Step 5: Commit** `fix(manager): wait two minutes after a failure before a relaunch; clear a leftover scope; settings say why`.

---

### Task 2: Login state per tool, in the facts and in the no-show error

**Files:**
- Modify: `src/kaptyorka/adapters/base.py`, `claude.py`, `codex.py`, `opencode.py`, `cursor.py`, `src/kaptyorka/facts.py`, `src/kaptyorka/workers.py` (`clean_facts`, `warnings`), `src/kaptyorka/bridge.py`
- Test: `tests/test_facts.py`, `tests/test_workers.py`, `tests/test_bridge.py`

**Interfaces:**
- `Adapter.logged_in(run) -> Optional[bool]` (default `None` = unknown). Each command is the binary `shutil.which(...)` finds (as `_tool_version` does), run with `capture_output=True, text=True, timeout=5`. Claude Code: `claude auth status`, parse JSON from stdout, `loggedIn` boolean (verified on 2.1.319: prints `{"loggedIn": true, ...}`); the output also holds the account email and org: keep only the boolean, never log, save or forward the raw output (test `test_logins_hold_only_booleans`: the facts carry no other key). Not verified: whether an expired login reports `false` or `true`; docs must not promise that expired logins are caught. Codex: `run(["codex", "login", "status"], ...)`: stdout contains "Logged in" → True, "Not logged in" → False (verified locally: prints `Not logged in`). OpenCode: `run(["opencode", "auth", "list"], ...)`: its output ends with a line `N credentials` (verified locally: `0 credentials`; ANSI colour codes may surround it): match `(\d+) credentials` → N > 0 is True, 0 is False; no match or an error → None. Cursor: `None`. Any timeout, OSError, non-zero exit or unparsable output → `None`.
- `facts.machine(...)` adds `"logins": {tool: True|False|None for installed tools}`, cached with `_cached(("logins", ""), 600, ...)` so the tool commands run at most every 10 minutes per bridge.
- `workers.clean_facts`: `logins` → `{tool: bool|None}` for the four known tools only.
- `workers.warnings()`: one row per machine and tool with `logins[tool] is False`: text `"<host>: <Tool name> is not logged in. Run `<login command>` there."` with the tool's own command: Claude Code `claude auth login`, Codex `codex login`, OpenCode `opencode auth login` (all three exist, checked in their --help). Tool names as the page shows them.
- Bridge no-show error: when the worker's tool has `logins[tool] is False` in the bridge's current facts: `"<Tool name> is not logged in on this machine; the session could not start. Run `<login command>` there."`; when `True` or unknown: `"the session did not show up within 2 minutes; check that <Tool name> is logged in and trusts the folder <folder> (open it there once); see ~/.kaptyorka/workers/<name>.log"`.

- [ ] **Step 1: Failing tests**

```python
# tests/test_facts.py
def test_logins_from_tool_commands(fake_run): claude JSON loggedIn false → False; codex "Logged in using ChatGPT" → True; opencode empty list → False
def test_login_check_timeout_is_unknown(fake_run_raising_TimeoutExpired): → None for that tool, the others unaffected, facts.machine still returns
# tests/test_workers.py
def test_clean_facts_logins(): unknown tools dropped, non-bool dropped, None kept
def test_warning_for_a_tool_not_logged_in(): one warning row with the exact text
# tests/test_bridge.py
def test_no_show_error_names_missing_login(hub, home): bridge facts say claude-code False → the worker's error starts with "Claude Code is not logged in on this machine"
```

- [ ] **Step 2: Run, fail. Step 3: Implement. Step 4: Whole suite. Step 5: Commit** `feat(bridge): report whether each agent tool is logged in; say so when a worker never shows up`.

---

### Task 3: Page and docs

**Files:**
- Modify: `src/kaptyorka/web/index.html`, `README.md`, `CHANGELOG.md`, `pyproject.toml`, `src/kaptyorka/__init__.py`, `tests/test_cli.py`
- Test: `tests/test_page.py`, `tests/test_page_slow.py` (only if a click is needed), `tests/test_readme.py`

**Interfaces (page copy, exact):**
- Machines card/table, Tools cell: each installed tool tag gets a state: logged in → tag as today; not logged in → the tag carries a small red dot and the title "not logged in"; unknown → as today. Under the tags, one line per tool that is not logged in: `"Claude Code is not logged in on this machine. Run `claude auth login` there."` (Codex: `codex login`; OpenCode: `opencode auth login`).
- Settings → Manager panel, by `manager_state`: `starting` → `"The manager session is starting."`; `up` → `"The manager session is running."`; `off` → today's off copy; `blocked` → `"The manager session is on but cannot start: <manager_reason>."` and, when `manager_error` is not empty, a second line `"Last start failed: <error>"`.
- Admin warnings banner already lists `warnings()` rows: nothing to add on the page beyond rendering the new kind if kinds are filtered.
- CHANGELOG `## 0.6.1` with the three changes in plain words, without promising that expired logins are caught; README: one sentence in the Machines section ("The Machines tab shows which agent tools are installed and logged in on each machine, as the tools report it; a tool that is not logged in cannot run workers there") and one in the manager section (Settings says why it is not running). Version 0.6.1 in the three places.

- [ ] **Step 1: Static tests** in `tests/test_page.py` for the copy above; `test_cli.py` version string.
- [ ] **Step 2: Implement. Step 3: Screenshots** `machines-login.png` (1440) and `machines-login-phone.png` (390) with a demo hub whose box-1 facts carry `logins: {"claude-code": false, "codex": true}`; `settings-manager-reason.png`. Look at them.
- [ ] **Step 4: Whole suite + slow page tests. Step 5: Commit** `feat(page): show which tools are logged in per machine; Settings says why the manager is not running; 0.6.1`.

---

## Rulings made while planning

- Trust detection stays out: Claude Code 2.1.319 does not keep a reliable flag for an accepted trust dialog (`hasTrustDialogAccepted` stayed false on a trusted folder), and `--bg` prints nothing. The no-show text names trust as the thing to check, which is what a person needs. The existing "Workspace not trusted" detection in `launch.py` and the hub's trust block stay as they are; nothing is added or removed there.
- No wrapper for logins (`kaptyorka login`): rejected by Narek; each tool's own login is the way.
- The login check uses each tool's own status command, never its credential files: files move between versions and are secrets.
