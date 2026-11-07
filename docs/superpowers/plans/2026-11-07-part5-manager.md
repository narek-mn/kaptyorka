# Part 5: Manager (0.6) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** One always-on agent session on the hub machine, under its own `manager` account, handles the few cases that need judgment (a busy machine, an abandoned worker, a waiting proposal) through checked tool calls, and `kaptyorka start --no-manager` runs the hub without it with nothing else changed.

**Architecture:** A new module `manager.py` (`ManagerStore(store)`, same lock and connection as the other stores) owns the manager person and device, the files the manager session runs with (`~/.kaptyorka/manager/config.json` and `CLAUDE.md`), the work-request queue and the deduped pushes. The manager session is a worker row named `manager` owned by the admin's page (`boss_key = web:<admin>`), launched through the admin's own bridge on the hub machine like any worker, with one extra order field `env` carrying `KAPTYORKA_CONFIG` so the whole session (shim and hooks) authenticates as the manager device. The hub's tick keeps it alive, writes its files, routes busy-machine work requests to it, and pushes it the events the spec lists. The manager talks to people through the normal tools; two narrow identity rules in `store.py` let the admin's bridge report the manager's session.

**Tech Stack:** Python 3.10, `mcp` SDK (Starlette), SQLite, one HTML page, no new dependency.

**Spec:** `docs/superpowers/specs/2026-11-06-team-brain-design.md`, sections "Part 5: Manager (0.6)", "The manager" (approaches), "Constraints", "Anti-goals" (a rule changed by an agent on its own; a worker killed by software), "My calls" (the manager role cannot answer knocks, open doors or grant).

## Global Constraints

- Python 3.10 floor; no new dependency.
- No team names in code, tests, fixtures or screenshots; use alice, bob, proj-a, box-1.
- `Store.lock` is not reentrant; `ManagerStore` follows the same rule as `MemoryStore`.
- `store.py` changes only where a task names the exact line: the two identity rules in Task 1 and nothing else.
- Everything the manager decides is a tool call the code checks (the person must have `use` of the machine; the folder must be free or a worktree) and logs with the manager's stated reason.
- The manager role may send to anyone and anyone may answer it without a door; it may knock, adopt its own workers and decide work requests; it cannot answer others' knocks, open doors, grant, edit rules, or decide proposals.
- The manager never edits rules: it has no `set_rules` path; it files `propose_rule` like any agent.
- The hub never kills the manager session except through `close_worker` by the admin; a manager that is gone is relaunched at most twice an hour.
- The manager's token is written only to `~/.kaptyorka/manager/config.json` (0600) on the hub machine; it is never printed or sent over the network except as the bearer token of that session.
- `kaptyorka start --no-manager` runs the hub without the manager and nothing else changes; `--manager` turns it back on; the choice is persisted in `hub.json`.
- Hook budgets unchanged; the manager's pushes go through `ws.notify` (hub-originated messages), never through `send`.
- Every external command goes through an injected `run`; tests never call real agent tools except under `-m slow`.
- Version bump touches `pyproject.toml`, `src/kaptyorka/__init__.py`, `tests/test_cli.py`, `CHANGELOG.md`.
- Commit trailers on every commit: the two trailer lines (`Co-Authored-By:` and `Claude-Session:`) exactly as they appear in `git log -1 --format=%B` of the current HEAD; copy them, never retype them.

## Review Focus

1. A hub started with `--no-manager` must create no person, no device, no files and push nothing (Task 5 `test_no_manager_changes_nothing`).
2. A work request whose asker's session goes offline before the manager decides must not start a worker nobody owns: the decision creates the worker only if the boss session is still online, else it tells the asker's person on the page (Task 3 `test_decide_after_asker_left`).
3. The manager must not be able to decide a request onto a machine the asking person may not use, even when it names that machine (Task 3 `test_decide_refuses_machine_without_use`).
4. A manager session that crashes must come back: after its worker row goes `closed` with "session gone", the next tick after 120 s queues a new launch, at most twice an hour, and the admin's page is told on the second failure (Task 2 `test_manager_relaunch_limits`).
5. The admin's bridge heartbeat must keep the manager session online without flipping its person to the admin, and the manager's first tool call must not be refused with "session key in use" when the heartbeat saw the session first (Task 1 `test_heartbeat_keeps_manager_session`, `test_manager_takes_key_first_seen_by_admin_bridge`).

---

## File map

| File | Responsibility |
|---|---|
| `src/kaptyorka/manager.py` (new) | `ensure_schema`, `ManagerStore`: enable flag, person and device, files, keep-alive decisions, work requests, deduped pushes, the fixed CLAUDE.md section. |
| `src/kaptyorka/store.py` | Two identity rules (Task 1), one `ensure_schema` line. |
| `src/kaptyorka/workers.py` | `create_manager`, `manager` flag on worker rows (exempt from unattended/abandoned), `offer` passes `env`. |
| `src/kaptyorka/launch.py` | `env` allow-list; `folder` override in the order. |
| `src/kaptyorka/paths.py` | `KAPTYORKA_CONFIG` override. |
| `src/kaptyorka/hub.py` | `work()` busy path → request; tools `work_decide`, `log`; manager tick (files, keep-alive, pushes, reminders, weekly); `_may_message` use. |
| `src/kaptyorka/cli.py` | `start --no-manager` / `--manager`; `serve --manager-url`. |
| `src/kaptyorka/hub_feed.py`, `web/index.html` | "manager" badge on the Team tab; the manager worker row for the admin. |
| `README.md`, `CHANGELOG.md`, version files | 0.6.0; the carried-over README line about `team-*/` in dotfile repos. |
| tests | `test_manager.py`, `test_hub_manager.py`, `test_manager_slow.py`, plus edits to `test_store_people.py`, `test_workers.py`, `test_launch.py`, `test_cli.py`, `test_serve.py`, `test_page.py`, `test_readme.py`. |

---

### Task 1: Manager store, identity rules, config override

**Files:**
- Create: `src/kaptyorka/manager.py`
- Modify: `src/kaptyorka/store.py` (`Store.__init__`: `manager.ensure_schema(self.db)` after the `rules.ensure_schema` line; `seen()` and `heartbeat()` as below), `src/kaptyorka/paths.py`
- Test: `tests/test_manager.py`, `tests/test_store_people.py`

**Interfaces:**
- Produces in `manager.py`:
  - `PERSON = "manager"`; `DEVICE = "manager"`; `WORKER = "manager"`; `DIR = "manager"` (under `paths.home()`); `RELAUNCH_S = 120`; `MAX_LAUNCHES_PER_HOUR = 2`; `REQUEST_TTL_S = 3600`; `REMIND_AFTER_S = 86400`; `WEEKLY_S = 7 * 86400`.
  - `ensure_schema(db)`: `work_requests(id INTEGER PRIMARY KEY, person_id INTEGER NOT NULL, boss_key TEXT NOT NULL, project TEXT NOT NULL, task TEXT NOT NULL, tool TEXT NOT NULL, resume TEXT, wanted TEXT NOT NULL, occupied_by TEXT NOT NULL, folder TEXT NOT NULL, created REAL NOT NULL, decided_at REAL, decision TEXT NOT NULL DEFAULT '', reason TEXT NOT NULL DEFAULT '', worker TEXT NOT NULL DEFAULT '')`; `manager_pushes(key TEXT PRIMARY KEY, time REAL NOT NULL)`.
  - `MANAGER_SECTION: str`, fixed text for CLAUDE.md: who it is (the team's manager session, account `manager`, started by the hub), what the hub sends it and what to do for each (busy machine → knock or message the occupant, read the answer, call `work_decide(request_id, machine_or_wait, reason)`; abandoned worker, shared-machine bridge offline, launch failed twice → tell the admin's online session in one line with `send`; a proposal or access request waiting a day → remind the admin in one line; weekly → read `log(500)` for repeated conflicts and corrections and file `propose_rule`), its limits (never edit rules; never answer others' knocks; a decision always carries the reason; ask the admin when unsure; no code work), and that the team rules above it bind it too.
  - `claude_md(rules_text: str) -> str`: `rules.rules_text(rules_text)` (or `"No team rules yet."`) + `"\n\n# Manager\n\n"` + `MANAGER_SECTION`.
  - `class ManagerStore(store)`:
    - `enabled() -> bool` (settings key `manager_enabled`, default `"1"`), `set_enabled(on: bool, by: str)` (logs `manager` with on/off).
    - `person_id() -> Optional[int]`; `ensure_account(host: str, by: str) -> Optional[str]`: creates person `manager` with role `manager` once and one device named `manager` for it (`store.device_add`), setting the device's `host` to `host` and `os_user` to the admin device's `os_user` on that host; returns the new token only when a device was created (or re-created after a revoke), else `None`. Idempotent.
    - `admin_device(host: str) -> Optional[dict]`: the admin's own non-legacy device on `host` with a signing secret (the hub machine's bridge), with `online` from `last_used`.
    - `write_files(dir: Path, hub_url: str, token: Optional[str], rules_text: str) -> None`: writes `CLAUDE.md` always (atomic), `config.json` (`{"hub", "token", "machine": "manager@<host>", "person": "manager"}`, 0600) only when `token` is given or the file is missing and a token is given; never overwrites a config with a `None` token.
    - `rules_written_version() / mark_rules_written(version: int)` in settings, so CLAUDE.md is rewritten when the team rules version changes.
    - `session_key(now: float) -> Optional[str]`: the manager person's newest online session key.
    - `may_launch(now: float) -> Tuple[bool, str]`: enabled, no live manager worker row (`starting/up`), admin device online, last launch older than `RELAUNCH_S`, fewer than `MAX_LAUNCHES_PER_HOUR` launches in the last hour; the string is the reason when False. `record_launch(now)`. `launches_last_hour(now) -> int`.
    - Requests: `request_add(person_id, boss_key, project, task, tool, resume, wanted, occupied_by, folder) -> int`; `request(id) -> Optional[dict]`; `request_decide(id, decision: str, reason: str, worker: str = "")`; `expire_requests(now) -> List[dict]` (undecided older than `REQUEST_TTL_S`: marked `decision="expired"` and returned so the hub can tell the asker).
    - `push_once(key: str, now: float, ttl_s: Optional[float] = None) -> bool`: True the first time per key (or again after `ttl_s`); rows pruned after 30 days by `prune(store, now)`.
    - `text_busy(person, project, machine, other, folder) -> str`: exactly `f"{person} wants {project} on {machine}, occupied by {other} in {folder}. Ask them, or pick another machine. Request {id}: work_decide({id}, \"{machine}\" or another machine or \"wait\", reason)."` (the id appended by the caller via `.format`; define the exact string in the module).
  - `paths.config_path()` honours `KAPTYORKA_CONFIG` (an absolute path) when set; `load_config`/`save_config` follow it.
  - `store.py` identity rules (exact places): in `heartbeat()`, the skip condition "another person's session" gets one more exception: the held session's person has role `manager` (the admin's bridge reports the manager's session). In `seen()`, the "session key in use by another person" check gets one exception: the principal's role is `manager` and the held person is the admin (the bridge saw the new session first and gave it to the admin); then the UPDATE assigns the manager person.

- [ ] **Step 1: Write the failing tests**: `test_schema_and_enable_flag`, `test_ensure_account_once_and_rotates_after_revoke`, `test_write_files_never_drops_token`, `test_may_launch_rules` (enabled, no live row, device online, 120 s, 2 per hour with reasons), `test_requests_add_decide_expire`, `test_push_once_and_ttl`, `test_claude_md_has_rules_and_section`, `test_config_path_override` (`KAPTYORKA_CONFIG` via monkeypatch), and in `tests/test_store_people.py`: `test_heartbeat_keeps_manager_session` (admin device heartbeat touches a session whose person is the manager: `last_seen` advances, person unchanged), `test_manager_takes_key_first_seen_by_admin_bridge` (heartbeat first → person admin; `seen` with the manager principal succeeds and sets person manager; a member principal still gets "session key in use").
- [ ] **Step 2: Run** `python3 -m pytest tests/test_manager.py tests/test_store_people.py -q`. Expected: import error / FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `python3 -m pytest tests/test_manager.py tests/test_store_people.py tests/test_store.py tests/test_identity.py -q`. Expected: PASS.
- [ ] **Step 5: Commit** `feat(manager): manager store, account, files and the two identity rules`.

---

### Task 2: The manager as a worker the hub keeps alive

**Files:**
- Modify: `src/kaptyorka/workers.py` (`workers.manager INTEGER NOT NULL DEFAULT 0` via the existing column-add path in `ensure_schema`; `create_manager`; `tick` exemptions; `offer`/order body `env` and `folder`), `src/kaptyorka/launch.py` (`folder_for` honours `order["folder"]` when set; `detach` merges `order["env"]` restricted to `{"KAPTYORKA_CONFIG"}` into the environment; `command` unchanged), `src/kaptyorka/hub.py` (manager tick), `src/kaptyorka/hub_feed.py` (`session_row["role"] = "manager"` for the manager person)
- Test: `tests/test_workers.py`, `tests/test_launch.py`, `tests/test_hub_manager.py`

**Interfaces:**
- Consumes: Task 1.
- Produces:
  - `WorkerStore.create_manager(admin_person_id: int, manager_person_id: int, device: dict, folder: str, config_path: str, first_message: str) -> dict`: a worker row `name="manager"`, `project="manager"`, `tool="claude-code"`, `boss_key=f"web:{admin_name}"`, `boss_person=manager_person_id` (so `link` gives the session to the manager), `manager=1`, `folder=folder`, and a signed launch order with `folder` and `env={"KAPTYORKA_CONFIG": config_path}`; refuses when a live manager row exists.
  - `tick`: rows with `manager=1` never become `unattended` or `abandoned` (the page boss counts as online already, keep it explicit) and are not listed in `warnings` as unattended; "session gone" closing and "is back" apply as to any worker.
  - Hub `manager_tick(now)` inside the existing `tick()`: when `mm.enabled()`: (1) `mm.ensure_account(hub_host, "hub")` where `hub_host = identity.facts()["host"]`; (2) if the rules version changed or files are missing: `mm.write_files(paths.home()/"manager", manager_url, token, rs.current()["text"])`; (3) `ok, why = mm.may_launch(now)`; if ok: `ws.create_manager(...)` with first message `"You are the team's manager session; read CLAUDE.md in this folder and wait for the hub's messages."`, `mm.record_launch(now)`, `wake_bridges()`; (4) on a manager launch failure the usual "failed to start" notice reaches `web:<admin>`; after the second failure in an hour `mm.push_once("manager-failed", now, 3600)` adds the line `"the manager session failed to start twice; see ~/.kaptyorka/workers/manager.log on the hub machine"` to the admin's page. `manager_url` is `f"http://127.0.0.1:{port}"`, passed into `build_app(store, manager_url=...)` by `serve`.
  - `session_row`: `"role": "manager"` when the session's person is the manager person (the page shows a small badge; Task 6).

- [ ] **Step 1: Write the failing tests**: `test_create_manager_order_has_env_and_folder` (order body carries `folder` and `env` with only `KAPTYORKA_CONFIG`; a second create refuses), `test_launcher_env_allowlist` (an order `env` with an extra key: only `KAPTYORKA_CONFIG` reaches `popen`), `test_folder_override`, `test_manager_never_unattended` (boss page, 2 days pass: state stays `up`), `test_manager_relaunch_limits` (tick launches once; "session gone" → relaunch after 120 s; a third launch within the hour is refused; second failure → page notice once), `test_manager_tick_writes_files_and_rewrites_on_rules_change`, `test_disabled_manager_tick_does_nothing`.
- [ ] **Step 2: Run** the three test files. Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `python3 -m pytest tests/test_workers.py tests/test_launch.py tests/test_hub_manager.py tests/test_hub_workers.py tests/test_bridge.py -q`. Expected: PASS.
- [ ] **Step 5: Commit** `feat(manager): the manager is a worker the hub starts and keeps alive`.

---

### Task 3: Busy machines go to the manager

**Files:**
- Modify: `src/kaptyorka/hub.py` (`work()` busy path; tool `work_decide`), `src/kaptyorka/store.py`: none
- Test: `tests/test_hub_manager.py`, `tests/test_hub_workers.py`

**Interfaces:**
- Consumes: Task 1 requests, `ws.candidates/choose/create/folder_people`, `ws.notify`.
- Produces:
  - `work()`: when the chosen device is occupied by another person and `mm.session_key(now)` is not None: `rid = mm.request_add(...)`, `ws.notify(manager_key, manager.text_busy(...))`, return `f"busy: {host} has {other}'s session in that folder; the manager is asking them (request {rid}); you will hear back here"`. Without an online manager the current answer stays.
  - Tool `work_decide(request_id: int, machine: str, reason: str) -> str`, manager role only (`KaptyorkaError("only the manager decides work requests")`), `reason` required (1 to 500 characters): `machine == "wait"` → `mm.request_decide(rid, "wait", reason)`, notify the asker's session (or `web:<person>` when offline) with `f"the manager says wait on {project}: {reason}"`; else recompute `ws.candidates(asker_principal_like, project, machine)` for the asking person (build the check from the request's `person_id`: the person must have `use` of that host or own the device, same rule as `candidates`), choose with `occupied` allowed only as a worktree, `ws.create(...)` with the request's `boss_key` when that session is online (else notify `web:<person>` `"the manager picked {machine} for {project} but your session is gone; ask again"` and mark `decision="asker gone"`); notify the asker `f"the manager started `{name}` on {host} for {project}: {reason}"`; log `work_decide` with `f"request {rid}: {decision} — {reason}"`. Already decided or expired → error with that state.
  - Tick: `for r in mm.expire_requests(now)`: notify the asker `"no decision on your {project} request within an hour; ask again"`.

- [ ] **Step 1: Write the failing tests**: `test_busy_goes_to_manager_when_online` (manager session online: asker gets the request id, the manager's inbox has the exact busy text), `test_busy_without_manager_unchanged`, `test_decide_wait_notifies_asker`, `test_decide_machine_starts_worker_for_asker` (worker's boss is the asker's session; the log line has the reason), `test_decide_refuses_machine_without_use`, `test_decide_after_asker_left` (asker offline: no worker, page notice), `test_decide_twice_and_by_member_refused`, `test_request_expires_after_an_hour`.
- [ ] **Step 2: Run** `python3 -m pytest tests/test_hub_manager.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `python3 -m pytest tests/test_hub_manager.py tests/test_hub_workers.py tests/test_hub_tools.py -q`. Expected: PASS (tool count +1).
- [ ] **Step 5: Commit** `feat(manager): busy machines become work requests the manager decides`.

---

### Task 4: What the manager may do, and what the hub tells it

**Files:**
- Modify: `src/kaptyorka/store.py`: none; `src/kaptyorka/hub.py` (`_may_message` policy applied in `send`/`may_message` via a store-free check in the hub: a manager-role sender or target needs no door; tool `log`; pushes in the tick), `src/kaptyorka/hub_feed.py`
- Test: `tests/test_hub_manager.py`, `tests/test_groups.py`

**Interfaces:**
- Produces:
  - Door rule: the hub's `send` lets a message through when the sender's person or the target's person has role `manager` (checked in the hub with `store.people()` roles, cached per call), otherwise `store.send` as today. `knock`/`answer`: the manager may knock; `answer` by the manager for a knock not aimed at it is refused by the existing rule (test it). `api_door_open`, grants, rules writes, proposal decisions: `admin_only` already refuses the manager (test two of them).
  - Tool `log(limit: int = 200) -> List[dict]`: admin and manager roles only; `store.log(limit)` rows.
  - Pushes (all `ws.notify(manager_key, text)` with `mm.push_once`): a worker turning `abandoned` → `"worker `{name}` of {person} is abandoned (unattended a day) on {host}; tell the admin in one line"` (key `abandoned:{name}`); a bridge offline over 10 minutes on a device marked shared → `"bridge on {host} ({os_user}), a shared machine, has been offline {n} minutes; tell the admin"` (key `offline:{device_id}`, ttl 86400); a worker launch failed twice for the same person within an hour → `"{person}'s workers failed to start twice ({error}); tell the admin"` (key `failed:{person_id}`, ttl 3600); a proposal or access request open for over a day → one reminder per item per day (`remind:proposal:{id}` / `remind:request:{id}`, ttl 86400) with the text `"{what} from {person} has waited a day: {text[:80]}; remind the admin"`; weekly: `"weekly review: read log(500) for repeated conflicts and corrections; file propose_rule for what repeats; never edit rules"` (key `weekly`, ttl `WEEKLY_S`). Nothing is pushed when no manager session is online; keys are not consumed then.

- [ ] **Step 1: Write the failing tests**: `test_manager_messages_anyone_and_is_answered_without_door`, `test_manager_cannot_answer_others_knock`, `test_manager_refused_by_admin_only_routes` (rules POST, proposal decide), `test_log_tool_roles`, `test_pushes_once_each` (abandoned, shared offline, failed twice, proposal reminder, weekly; a second tick pushes nothing new; no manager online → nothing consumed, pushed on the next tick when it is).
- [ ] **Step 2: Run** `python3 -m pytest tests/test_hub_manager.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `python3 -m pytest tests/test_hub_manager.py tests/test_groups.py tests/test_hub_tools.py -q`. Expected: PASS (25 tools).
- [ ] **Step 5: Commit** `feat(manager): manager permissions, log tool and the hub's pushes to it`.

---

### Task 5: `kaptyorka start --no-manager`

**Files:**
- Modify: `src/kaptyorka/cli.py` (`start`: `--no-manager` / `--manager`, persisted in `hub.json` as `"manager": bool`; `serve`: `--manager-url` and `--no-manager` passed through; `_serve` sets `mm.set_enabled` before building the app), `src/kaptyorka/hub.py` (`build_app(store, manager_url: str = "")`)
- Test: `tests/test_serve.py`, `tests/test_cli.py`

- [ ] **Step 1: Write the failing tests**: `test_start_no_manager_persists_and_disables` (`hub.json` has `"manager": false`; the installed unit line carries `--no-manager`; the store setting is `"0"`), `test_start_manager_flag_turns_it_back_on`, `test_no_manager_changes_nothing` (an app built with the setting off: after ticks there is no `manager` person, no device, no files in a temp `KAPTYORKA_HOME`, no notices).
- [ ] **Step 2: Run** `python3 -m pytest tests/test_serve.py tests/test_cli.py -q -k manager`. Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `python3 -m pytest tests/test_serve.py tests/test_cli.py tests/test_hub_manager.py -q`. Expected: PASS.
- [ ] **Step 5: Commit** `feat(cli): kaptyorka start --no-manager`.

---

### Task 6: Page and docs, 0.6.0

**Files:**
- Modify: `src/kaptyorka/web/index.html` (Team tab: a small `manager` badge after the name for rows with `role == "manager"`, same style as the `admin` badge in the header; Machines tab workers list: the `manager` row shows "manager" instead of a boss), `README.md` (new section "The manager" after "Workers": what it is, what it decides and how each decision is checked and logged, what it cannot do, `--no-manager`, the files in `~/.kaptyorka/manager/`, the attach line `claude attach manager` on the hub machine; Commands table; Files section; tool count 25; plus the carried-over line in "Team rules and skills": "If `~/.claude/skills` is a link into a git repo, add `team-*/` to that repo's `.gitignore`."), `CHANGELOG.md`, version files
- Test: `tests/test_page.py`, `tests/test_readme.py`

- [ ] **Step 1: Write the failing tests** `test_page_shows_manager_badge`, README checks as `test_readme.py` does.
- [ ] **Step 2: Implement; bump to 0.6.0.**
- [ ] **Step 3: Run** `python3 -m pytest tests -q -W default` (full suite, once). Expected: all pass, no warnings.
- [ ] **Step 4: Commit** `docs: the manager; 0.6.0`.

---

### Task 7: One real manager session (slow)

**Files:**
- Create: `tests/test_manager_slow.py`
- Test: `pytest -m slow tests/test_manager_slow.py`

- [ ] **Step 1: Write the test** following `tests/test_worker_slow.py`: a temp `KAPTYORKA_HOME`, a real hub on a free port with the manager enabled, a real bridge for the admin device on this machine with a Launcher using real `claude --bg`; assert within 90 s that a session of person `manager` is online, that `~/.kaptyorka/manager/config.json` and `CLAUDE.md` exist with the expected content, that the manager session's brief contains the team rules heading, and that `work_decide` called from the manager session's identity is accepted by the hub (decide `wait` on a request the test creates). Everything stopped in `finally` (`claude stop <id>`, hub, bridge; temp home removed).
- [ ] **Step 2: Run** `python3 -m pytest -m slow tests/test_manager_slow.py -q`. Expected: PASS (one real launch, under 2 minutes).
- [ ] **Step 3: Commit** `test(manager): one real manager session`.
