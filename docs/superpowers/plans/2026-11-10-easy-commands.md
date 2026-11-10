# Easy Commands and a Sleeping Manager (0.8.0) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every command does the obvious thing once and says so in one short line when there is nothing to do; the manager sleeps when idle and wakes when something is sent to it; `join` never writes Claude Code hooks twice.

**Architecture:** Small guards at the top of existing handlers in `cli.py`, `service.py`, `remove.py`, `doctor.py`, `adapters/claude.py`, `join.py`; two ruled touches in `store.py` (offline label match and held-message routing, both for the manager person only); the manager's relaunch rule in `manager.py` and one line in `workers.py` tick. No new commands, no new flags.

**Tech Stack:** Python 3.10, pytest, SQLite. No new dependencies.

**Spec:** the owner's words (2026-11-10): "do not allow the hub run kaptyorka twice, tell that this is already running; this is just one example; make usage easy and intuitive with short and understandable responses; do not overengineer". The findings are in `.superpowers/cli-audit.md` (git-ignored; numbers below refer to its rows). Manager restarts: Claude Code 2.1.3xx retires a `--bg` session after about 60 idle minutes (official agent-view docs: "unattached for about an hour: the supervisor stops the process"; no setting changes it; only a manual pin in agent view). The hub then closes the worker as "session gone" after `GONE_S` and launches a fresh manager: one every 71.5 minutes, forever, even with nothing to do. A resumed `--bg --resume <id>` gets a new session id (probed), so wake routes by label.

## Global Constraints

- Python 3.10 floor; no new dependencies; `python3 -m pytest tests -q` green.
- `store.py`: only the two ruled touches in Task 1. `Store.lock` is not reentrant.
- Never run `kaptyorka` or `claude` for real in tests; service calls are monkeypatched.
- No team names in code, tests, docs or this plan's code blocks (alice, box-1, hub-1).
- Output copy is exactly as written below: short, plain, no unit file paths, no tokens except where named.
- Commit trailers as in `git log -1`.
- Version 0.8.0 in `pyproject.toml`, `src/kaptyorka/__init__.py`, `tests/test_cli.py:39`; a `## 0.8.0` CHANGELOG section.

## Review Focus

1. `start` on a running hub with unchanged settings must not restart it (it drops every agent's stream). Test: `test_start_twice_does_not_restart`.
2. `join <invite>` on a machine already joined to the same hub must not spend the invite or replace the token (on the hub machine it would replace the admin token). Test: `test_join_invite_same_hub_refused`.
3. A message to `manager` while it sleeps is accepted, wakes it, and is delivered to the new session. Test: `test_message_to_sleeping_manager_wakes_and_reaches_it`.
4. An idle manager is not relaunched with nothing to do. Test: `test_sleeping_manager_not_relaunched_without_work`.
5. Join sees a hook it did not write (a shell line that contains `kaptyorka hook claude-code start`) and leaves it alone. Test: `test_hooks_portable_line_counts_as_present`.

---

### Task 1: The manager sleeps when idle and wakes on a message

**Files:**
- Modify: `src/kaptyorka/store.py` (`_targets` at 1300-1323; `_deliverable` fallback at 1362-1365)
- Modify: `src/kaptyorka/workers.py` (`tick`, the `GONE_S` check at 641)
- Modify: `src/kaptyorka/manager.py` (`may_launch` at 295-327; the Settings state)
- Modify: `src/kaptyorka/hub.py` (`/api/settings` manager_state), `src/kaptyorka/web/index.html` (Settings copy for the new state)
- Test: `tests/test_manager.py`, `tests/test_store.py`

**Rules:**
- `_targets`: after the online name and label match and the offline full-name match, one more: the most recently seen agent whose `label` equals `to` (case-insensitive). So `to="manager"` resolves while the manager sleeps.
- `_deliverable`: when a held message's target is offline and no online row has its `to_name`, route it to the online session of the same person with the same label (the old target row's `person_id` and `label`), if exactly one exists.
- `workers.tick`: the manager's worker counts as gone after `ONLINE_S` + 60 s offline instead of `GONE_S` (`gone = 60 if w["manager"] else GONE_S`).
- `may_launch`: when the latest manager worker row is `closed` with error `session gone` (Claude retired it), return `(False, "the manager is asleep; a message or request wakes it")` unless there is work: an unread message to an agent of the manager person, or an open work request. With work, the usual checks apply. A failed start, a first start, or a manager closed for any other reason keeps today's behaviour.
- `/api/settings` `manager_state`: `asleep` for that case. Settings copy: "Asleep: it wakes when someone sends it a message."

- [ ] **Step 1: Tests**
  - `test_message_to_sleeping_manager_wakes_and_reaches_it`: a manager agent (label `manager`) offline, its worker closed with `session gone`; `send_from_web("alice", "manager", "hi", ...)` succeeds; `may_launch` is `(True, "")`; after a new online manager agent with label `manager` appears, `offer` for the launching device returns the message with the new key.
  - `test_sleeping_manager_not_relaunched_without_work`: same setup, no message; `may_launch` returns the asleep reason; `manager_state` is `asleep`.
  - `test_manager_gone_after_a_minute`: tick with the manager's agent offline for `ONLINE_S + 61` s closes it as `session gone`; a normal worker is still up.
  - `test_offline_label_match` in `tests/test_store.py`: an offline agent with label `box` is the target of `to="box"`; two online ones with that label still raise "several sessions".
- [ ] **Step 2: Run them to see them fail.**
- [ ] **Step 3: Implement** as in Rules.
- [ ] **Step 4: Run** `python3 -m pytest tests/test_manager.py tests/test_store.py tests/test_scoping.py tests/test_hub_manager.py tests/test_hub_workers.py -q`: PASS.
- [ ] **Step 5: Commit** `fix(manager): sleep when Claude retires it, wake on a message`.

### Task 2: Join never writes Claude Code hooks twice

**Files:**
- Modify: `src/kaptyorka/adapters/claude.py` (`configure` around 157-166, `is_our_hook`)
- Modify: `src/kaptyorka/join.py:100` (`exe`)
- Test: `tests/test_claude.py`, `tests/test_join.py`

**Rules:** an event's hook counts as present when any command in it contains `kaptyorka hook claude-code <name>` on word boundaries (regex `\bkaptyorka hook claude-code <name>\b`); then join adds nothing and never rewrites it. Only an entry whose whole command is exactly an older kaptyorka path plus ` hook claude-code <name>` is replaced, as today. `is_our_hook` uses the same search (so `doctor` and `leave` see the portable line; `leave` still removes only commands it can tell are kaptyorka's own: exact path form or a line that contains the hook text). `exe`: when `sys.argv[0]` is `~/.local/bin/kaptyorka` (pipx's stable link), use it as is, without `resolve()`.

- [ ] **Step 1: Tests** `test_hooks_portable_line_counts_as_present` (settings with `[ -x ~/.local/bin/kaptyorka ] && ~/.local/bin/kaptyorka hook claude-code start || true` for start and prompt; configure adds only stop, leaves the two lines byte-identical), `test_doctor_sees_portable_hooks`, `test_join_keeps_the_pipx_link_path`.
- [ ] **Step 2-4:** fail, implement, `python3 -m pytest tests/test_claude.py tests/test_join.py tests/test_doctor.py -q` PASS.
- [ ] **Step 5: Commit** `fix(join): see hooks written by hand; keep the pipx link path`.

### Task 3: `start`, `stop`, `update`, `leave`: once, and say so

**Files:** `src/kaptyorka/cli.py` (`_serve` 56-102, `_start` 148-170, `_stop` 486, `_update` 527, `_restart` 497), `src/kaptyorka/service.py` (`stop` 167), `src/kaptyorka/remove.py` (`leave`), tests in `tests/test_cli.py`, `tests/test_remove.py`.

**Behaviour and exact copy** (`<url>` is the hub address):
- `start`, hub unit active and port, host and manager setting equal to `hub.json`: print `the hub is already running: <url>` and return 0. No restart, no other output. Still turns the bridge back on (0.7.1 rule). (#1)
- `start` when `config.json` points to a hub that is not this machine's (no `hub.json` and no hub unit here): print `this machine is joined to <hub>; it does not run a hub. To turn its bridge back on: kaptyorka join` and return 1. (#3)
- `start` with a new port or host while `config.json`'s hub is this machine's old hub address: rewrite `cfg["hub"]` to the new address (token kept) and restart the bridge. (#2)
- `start` output when it does start: the first start (no `hub.json` before) prints
  `hub running: <url>`, `open the page: <url>/#t=<admin token>  (keep this link private: it signs you in)`, `invite a teammate: kaptyorka invite <name>`, and MANAGER_NOTE; a later start that restarts prints only `hub restarted: <url>`. No `join --hub --token` line (this machine is already joined). No `installed <path>` line. (#4, #5)
- `stop`: per unit, `is_active` None: say nothing; False: `<bridge|hub> already stopped`; True: `bridge stopped` / `hub stopped`; when the hub was stopped add `teammates cannot reach the hub until: kaptyorka start`. Plain names, not unit names. `stop` help text: `stop the hub and the bridge on this machine (start brings back the hub; join brings back the bridge)`. (#6, #7, #8)
- `update` when the version after pipx equals the running one: print `kaptyorka <v> is already the latest` and return 0, no restart. `_restart` skips units that are not installed, silently. (#9, #10)
- `leave` when nothing was configured: print `this machine is not joined; nothing to undo`. When it did undo: `left: this machine's bridge and tool settings are removed; it stays on the team until the admin removes it (People)`. (#14, #15)

- [ ] **Step 1: Tests** one per bullet, named `test_start_twice_does_not_restart`, `test_start_on_a_member_machine_refused`, `test_start_new_port_moves_this_machine`, `test_first_start_output`, `test_restart_output_is_one_line`, `test_stop_already_stopped`, `test_stop_member_machine`, `test_update_already_latest`, `test_leave_not_joined`.
- [ ] **Step 2-4:** fail, implement, run those files: PASS.
- [ ] **Step 5: Commit** `fix(cli): start, stop, update and leave do it once and say so`.

### Task 4: `join` and `doctor`: no surprises

**Files:** `src/kaptyorka/cli.py` (`_join` 582), `src/kaptyorka/join.py`, `src/kaptyorka/doctor.py`, tests in `tests/test_join.py`, `tests/test_doctor.py`, `tests/test_cli.py`.

**Behaviour and exact copy:**
- `join <invite link>` when `config.json` already has hub and token for the same hub address: print `this machine is already joined to <hub> as <machine>. To repair it: kaptyorka join` and return 1 before redeeming. For a different hub: go on, and print `moved from <old hub> to <new hub>` first. (#11)
- plain `join` that changed nothing: print `already joined to <hub>; all set` and nothing else (still runs every repair step; restarts the bridge only when its unit changed or it was not running). Print each tool's line and its restart note only when that tool's files changed. Drop the doubled `claude-code: Claude Code:` prefix (one tool name). The page link line only on a first join. (#12, #13)
- `doctor`: not joined: one line `not joined: kaptyorka join <invite link>`; hub not reachable: fix `is the hub running? On the hub machine: kaptyorka start`; token refused (401): fix `ask your admin for a new invite link, then: kaptyorka join <link>`; bridge stopped: fix `kaptyorka doctor --fix`; tools: only those in `cfg["tools"]` when set. (#16-#20)

- [ ] **Step 1: Tests** `test_join_invite_same_hub_refused` (the invite is not redeemed: the fake hub sees no redeem call; config unchanged), `test_join_invite_other_hub_moves`, `test_join_nothing_changed_says_all_set`, `test_join_prints_tool_lines_only_when_changed`, `test_doctor_not_joined_one_line`, `test_doctor_fix_texts`.
- [ ] **Step 2-4:** fail, implement, run: PASS.
- [ ] **Step 5: Commit** `fix(cli): join and doctor say what is true and what to do`.

### Task 5: Small output fixes, docs, version

**Files:** `src/kaptyorka/cli.py` (`_invite` 228, `_who`, `_fleet_list` 646, `_people` 245), `README.md` (Quick start, Commands, "Leave, stop, update, remove", "The manager" paragraph on keeping it alive), `CHANGELOG.md`, versions, tests.

- `invite <name>` for a new person: `added <name> to the team` then `send this link to <name> (works once, for 7 days): <url>`; for an existing person only the second line. (#22)
- Empty lists: `who`: `nobody is online`; `fleet list`: `no machines yet`; `people` never empty (admin). Ages in `who` as `12s`, `5m`, `3h`, `2d` ago. (#26)
- README: describe the new `start`/`stop`/`join`/`update`/`leave` answers in a sentence each where those commands are described; "The manager": "When it has been idle for about an hour, Claude Code stops it to save resources. The manager then sleeps: a message or a request to it starts it again within a minute, with a fresh conversation. Settings shows Asleep." CHANGELOG `## 0.8.0`: four bullets (commands say when there is nothing to do and no longer restart a running hub; join no longer adds Claude Code hooks twice and refuses an invite for a hub it is already on; the manager sleeps when idle and wakes on a message instead of restarting every 71 minutes; doctor's fixes point to the step that works).
- [ ] **Steps:** tests for invite and empty `who`/`fleet list`; implement; whole suite `python3 -m pytest tests -q` PASS; commit `docs: easy commands, sleeping manager; 0.8.0`.

### Release

After kursi's PASS and green CI: tag `v0.8.0`, push, watch `publish.yml`; `kaptyorka update` on the hub machine and on the laptop; on the hub machine run `kaptyorka start` twice and see the one-line answer the second time; check the manager wakes on a page message after it sleeps (needs an hour idle; check the Settings state now and report).

---

## Review changes (binding; they override the task text above where they differ)

Task 1, sleep and wake:
1. Held messages for the manager: in `_deliverable`, a held message whose old target agent belongs to the manager person goes to that person's online session with **no door check** (`_may_message` is skipped for this case only). Tests, one each: admin page sender, member page sender (with `person_id`), member session sender, hub notice (`HUB_KEY` sender). Each must reach the new manager session.
2. The offline label match in `_targets` applies only to agents of the manager person, and comes **after** the page-person match. `test_offline_label_match` becomes: the manager label resolves while it is offline; an ordinary offline label still raises "no session named".
3. Wake only on deliverable work: unread messages to the manager person whose `push_state` is not `failed`. Test: a failed one does not wake it.
4. No "open work request" wake clause (a request's notice is already a message).
5. `work` busy-folder path (hub.py around 530-538): when the manager is enabled and asleep, add the request and notify the manager's last session key, so the notice is an unread message that wakes it and (by 1) reaches the new session. One test.
6. Weekly and other `manager_pushes` wait while it sleeps and go out when it wakes: one sentence in README and CHANGELOG, no wake for them.
7. One helper `ManagerState.asleep(now) -> bool` used by `may_launch` and `/api/settings`; never compare reason text. Test: a manager wrongly closed as gone and then seen again is revived to `up` and is not shown asleep.
8. Machines tab: the manager row shows `asleep`, not closed / "session gone". The review gets screenshots of Settings and Machines (slow page test or chrome helper in tests).
9. The unread-message query lives in `manager.py`; `store.py` has only the two touches.

Task 2, hooks:
10. When a hand-written line containing the hook text is present for an event, remove kaptyorka's own exact-form entry for that event (so an already-doubled machine ends with one). Test `test_hooks_own_duplicate_of_portable_removed`.
11. `is_our_hook` lives in `adapters/base.py` and is shared with codex and cursor: also run `tests/test_codex.py` and `tests/test_cursor.py`. Compare `exe` against `Path.home() / ".local/bin/kaptyorka"`, not a literal `~`.

Tasks 3-5, commands:
12. `start --manager` / `--no-manager` on a running hub: apply the switch (`_apply_manager`, clears the trust block) before the "already running" answer; no restart if nothing else changed. Test `test_start_manager_flag_on_running_hub_unblocks`.
13. The already-running answer is two lines: `the hub is already running: <url>` and `open the page: <url>/#t=<token>  (keep this link private: it signs you in)`. The same second line after `hub restarted: <url>`.
14. `leave` after undoing: two lines, `left: bridge and tool settings removed from this machine` and `it stays on the team until the admin removes it (People); kaptyorka join brings it back`.
15. Plain `join` with nothing changed must not run `claude mcp remove` / `add`: skip both when `claude mcp get kaptyorka` (or the user config it reads) already shows the same command and args; say in the code how it is detected.
16. `fleet add` on a machine already joined to that hub: its result line reads `<target>: already joined to this hub` (not "failed at join"); test.
17. Also tests for: the stop line about teammates (#8); `update` silent about the hub on a member machine (#10); the `leave` joined copy (#15); doctor's tools filter (#19); `who` ages; an already-running `start` still turning a stopped bridge back on.
18. Line references fixed above (`_restart` 497, `_stop` 486).

Release adds: after `join` on the laptop, each Claude Code event has one kaptyorka hook, and the settings change is committed in the config repo.
