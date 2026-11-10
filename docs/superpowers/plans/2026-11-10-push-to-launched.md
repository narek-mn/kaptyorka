# Push to Launched Sessions, Start Brings the Bridge Back (0.7.1) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A message to the manager reaches it by push, and `kaptyorka start` turns the bridge back on after `kaptyorka stop`.

**Architecture:** Two small fixes. (1) `Store.offer` and `Store.ack` also accept a message whose target is the session of a live worker that the polling device launched (`workers.device_id == principal.device_id`, `workers.key == agent key`, `closed_at IS NULL`). Today both refuse it, because the manager's agent row carries its own machine (`manager@<host>`) and its own person and device, while the bridge that launched it polls as the admin's machine and device. (2) `_start` in `cli.py` restarts `kaptyorka-bridge` when it is installed and not active.

**Tech Stack:** Python 3.10, pytest, SQLite. No new dependencies.

**Spec:** observed on the hub machine on 2026-11-10, Claude Code 2.1.322, kaptyorka 0.7.0: the manager session has an inbox socket (0.6.2 fix works), a page message to it stayed `push_state='new'`, `offered_at NULL` for minutes; its agent row has `machine='manager@zeus'`, `device_id=2` (the manager's device), and its worker row has `device_id=3` (the admin's bridge device, machine `dgx`) and the same `key`. `Store.offer` skips it on `t["machine"] != machine` and on `_owns`; `Store.ack` would refuse the receipt on `_owns` and the lease would re-offer it every `LEASE_S`. Separately: `kaptyorka stop` runs `systemctl --user disable --now` on hub and bridge; `kaptyorka start` reinstalls only the hub, so the bridge stays off and the manager (launched through that bridge) never starts.

## Global Constraints

- Python 3.10 floor; no new dependencies; `python3 -m pytest tests -q` green.
- `store.py` changes are ruled touches only: one helper and two call-site lines, nothing else.
- `Store.lock` is not reentrant: the helper runs inside the existing `with self.lock` blocks and must not take the lock.
- No team names in code or tests (alice, box-1).
- Commit trailers as in `git log -1`.
- Version 0.7.1 in `pyproject.toml`, `src/kaptyorka/__init__.py`, `tests/test_cli.py:39`; a `## 0.7.1` CHANGELOG section.

## Review Focus

1. Another person's bridge must still get nothing: a worker launched by device A is not offered to device B. Test: `test_offer_launched_only_to_the_launching_device`.
2. A closed worker's session is not pushed through the old bridge. Test: `test_offer_skips_closed_worker`.
3. The receipt is accepted, so the lease does not re-offer. Test: `test_ack_launched_marks_read`.
4. `kaptyorka start` on a machine without a bridge (a hub-only machine) prints "not installed" and still succeeds. Test: `test_start_without_bridge`.
5. `kaptyorka start` while the bridge already runs does not restart it. Test: `test_start_leaves_a_running_bridge`.

---

### Task 1: Offer and ack to sessions the device launched

**Files:**
- Modify: `src/kaptyorka/store.py` (`offer` at 1392-1421, `ack` at 1423-1437, one new helper next to `_owns` at 170)
- Test: `tests/test_scoping.py` (next to `test_offer_respects_the_device`)

**Interfaces:**
- Produces: `Store._launched_keys(self, principal: Optional[Principal]) -> set` — keys of workers with `device_id = principal.device_id`, `closed_at IS NULL`, `key IS NOT NULL`; empty set, without a query, when `principal is None` or `principal.device_id is None` (the shared token). Called only with the lock held.
- `offer`: the skip becomes `if t["key"] not in launched and (t["machine"] != machine or not _owns(t, principal)): continue`, with `launched = self._launched_keys(principal)` computed once before the loop.
- `ack`: the refusal becomes `if row is None or (not _owns(row, principal) and row["key"] not in self._launched_keys(principal)): return False`; the SELECT adds `a.key`.

- [ ] **Step 1: Write the tests** in `tests/test_scoping.py`, using the `two` fixture and a worker row inserted under the lock as `tests/test_manager.py:130-135` does (`manager_row` pattern), with `device_id = pr.device_id`, `key = "m1"`, `state = 'up'`. The manager-like session: `s.seen("m1", "manager@srv", "claude-code", "manager", principal=<a fourth person "manager" with its own device>)` so its machine, person and device differ from root's.
  - `test_offer_launched_to_the_launching_device`: `s.send("r1", <m1's name>, "wake")`; `s.offer("root@srv", "", pr)` returns one item with `key == "m1"`.
  - `test_offer_launched_only_to_the_launching_device`: the same message; `s.offer("alice@pc", "", pa) == []` and `s.offer("root@srv", "", pa) == []`; if the fixtures can build a principal with no device cheaply, assert it gets `[]` too (skip otherwise).
  - `test_offer_skips_closed_worker`: set `closed_at` on the worker row; `s.offer("root@srv", "", pr) == []`.
  - `test_ack_launched_marks_read`: offer then `s.ack(mid, True, pr) is True` and the message is read; `s.ack(mid, True, pa) is False` on a second message.
- [ ] **Step 2: Run them to see them fail**: `python3 -m pytest tests/test_scoping.py -q` — FAIL on the first and last.
- [ ] **Step 3: Implement** as in Interfaces.
- [ ] **Step 4: Run** `python3 -m pytest tests/test_scoping.py tests/test_store.py tests/test_hub_manager.py -q` — PASS.
- [ ] **Step 5: Commit** `fix(hub): the bridge that launched a session may push to it`.

### Task 2: `start` turns the bridge back on; docs; version

**Files:**
- Modify: `src/kaptyorka/cli.py` (`_start` at 148-163)
- Test: `tests/test_cli.py` (near the existing start tests; find them with `grep -n "_start\|\"start\"" tests/test_cli.py`)
- Modify: `README.md:86` (the `kaptyorka start` sentence; `README.md:459` already says start brings them back and stays as is), `CHANGELOG.md`, `pyproject.toml`, `src/kaptyorka/__init__.py`, `tests/test_cli.py:39`

**Interfaces:**
- Consumes: `service.is_active(name, run=...) -> Optional[bool]` (None: not installed), `service.restart(name, run=...) -> List[str]` (enables and starts a disabled unit; "not installed" when the unit file is missing).
- Produces: after `_serve` returns 0, `_start` first writes `hub.json` as today, then calls `service.is_active("kaptyorka-bridge")`; when it is `False`, prints each line of `service.restart("kaptyorka-bridge")`; `ServiceError` prints to stderr and makes `_start` return 1, after `hub.json` was saved (the hub is up). `True` or `None`: nothing.

- [ ] **Step 1: Tests**, with `service.is_active`, `service.restart` and `_serve` monkeypatched:
  - `test_start_turns_the_bridge_back_on`: `is_active` False; restart called once; its line printed; exit 0.
  - `test_start_leaves_a_running_bridge`: `is_active` True; restart not called.
  - `test_start_without_bridge`: `is_active` None; restart not called; exit 0.
  - `test_start_reports_a_bridge_that_will_not_start`: `is_active` False, `restart` raises `ServiceError`; the error is on stderr, exit 1, and `hub.json` is written.
  - version `"0.7.1"`.
- [ ] **Step 2: Run them to see them fail.**
- [ ] **Step 3: Implement.** README:86 adds: "It also turns this machine's bridge back on when `kaptyorka stop` switched it off." CHANGELOG `## 0.7.1`: two bullets — "Messages to the manager now reach it at once: the bridge that started a session may deliver to it, even when the session is registered under its own name (the manager is). Before, the manager only saw messages on its next turn, which an idle manager never had." and "`kaptyorka start` turns the bridge back on after `kaptyorka stop`; before, only the hub came back, so the manager could not start."
- [ ] **Step 4: Run the whole suite**: `python3 -m pytest tests -q` — PASS.
- [ ] **Step 5: Commit** `fix(cli): start turns the bridge back on; 0.7.1`.

### Release

Narek asked to keep the team on the current kaptyorka; after kursi's PASS and green CI: tag `v0.7.1`, push, watch `publish.yml`. Then on the hub machine `kaptyorka update` (no manager restart needed: the fix is hub side), and send a page message to `manager`: it must be `read` within a minute and the manager must answer.
