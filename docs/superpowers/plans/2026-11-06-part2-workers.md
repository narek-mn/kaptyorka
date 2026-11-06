# Part 2: Workers (0.3) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A session asks `work(project, task)` and the hub starts a worker session on the right machine, under the right account, links it to its boss, watches it, and lets the human attach to it; plus the fleet, doctor, auto-update and warnings that keep the machines usable.

**Architecture:** Workers live in a new module `workers.py` (tables, launch stream, states, warnings) that shares the store's SQLite connection, lock and clock but is not part of `Store` (`store.py` gets two small touches only). Launch orders are signed with a key both sides can derive (HMAC-SHA256 under `sha256(signing secret)`; the hub stores that hash already), travel in the bridge's existing poll stream as a new item kind, and the bridge runs the worker detached from its own service (`systemd-run --user --scope` on Linux, `setsid` on macOS) in Claude Code's background mode or tmux. Machine facts ride on the heartbeat, whose answer becomes JSON so the hub can talk back (version, projects to look for, auto-update).

**Tech Stack:** Python 3.10, `mcp` SDK (Starlette), SQLite, `hmac`/`hashlib`, `subprocess` (always argv lists, never a shell string), tmux, systemd-run, one HTML page.

**Spec:** `docs/superpowers/specs/2026-11-06-team-brain-design.md`, section "Part 2: Workers (0.3)", plus "Constraints", "What stays the same", "My calls", "Left to the builder".

## Global Constraints

- Python 3.10 floor; no new dependency (a new one needs a reason written next to it).
- No team names in code, tests, fixtures or screenshots; use alice, bob, proj-a, box-1.
- `Store.lock` is not reentrant: never call a locking store method from inside a `with lock` block. `WorkerStore` takes the same lock and follows the same rule.
- Tests never call real agent tools, ssh, tmux or systemd-run except under `-m slow`; everything that runs a command takes an injected `run`.
- `store.py` grows only by: one `ADDED_COLUMNS` line for `agents.cwd`, the `cwd` parameter threaded through `heartbeat`/`_touch`, and one call `workers.ensure_schema(self.db)` in `Store.__init__`. Everything else about workers lives in `workers.py`.
- Nothing is bypassed in a worker: no `--dangerously-skip-permissions`, no permission flags at all; the account's saved settings apply.
- Nobody, admin included, starts a worker under another person's own (not shared) account.
- Worker launch commands are argv lists; the task text is passed as one argument, never interpolated into a shell string.
- A 0.2 bridge against a 0.3 hub and a 0.3 bridge against a 0.2 hub keep messaging working: the bridge accepts both 204 and 200 from the heartbeat; the hub tolerates heartbeats without facts.
- Version bump touches `pyproject.toml`, `src/kaptyorka/__init__.py`, `tests/test_cli.py`, `CHANGELOG.md`.
- Commit trailers on every commit: `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>` and `Claude-Session: https://claude.ai/code/session_01WHiZ1E64m7csjvMWwiGkdu`.

## Review Focus

1. A task text with quotes, newlines, `$(...)` or a leading `-` must reach the worker unchanged and run nothing: the launcher builds argv lists and puts the text after the tool's own flags (Task 7 test `test_task_text_is_one_argument`).
2. A replayed or stale launch order (same id again, or `issued` older than 10 minutes) must not start a second worker (Task 1 and Task 7 tests).
3. A worker whose boss goes offline for a minute and comes back must not be marked unattended; only two minutes of boss absence does (Task 3 test `test_boss_blip_is_not_unattended`).
4. Two `work()` calls for the same project at the same moment must not both take the plain folder: the second gets a worktree (Task 3 test `test_second_launch_in_same_folder_gets_worktree`).
5. A shared-token (legacy) device has no signing secret and cannot run workers; `work()` must say so in one sentence instead of hanging in `starting` (Task 5 test `test_legacy_device_cannot_host_workers`).

---

## File map

| File | Responsibility |
|---|---|
| `src/kaptyorka/orders.py` (new) | Sign and verify launch orders; canonical JSON; replay window. Used by hub and bridge. |
| `src/kaptyorka/workers.py` (new) | `ensure_schema`, `WorkerStore`: device facts, folders, candidates, workers and launch rows, states, adopt, close, warnings. |
| `src/kaptyorka/facts.py` (new, bridge side) | Machine facts for the heartbeat: os user, tmux, load, tool versions, hooks present, git facts per session folder, known project folders. |
| `src/kaptyorka/launch.py` (new, bridge side) | `Launcher`: folder or worktree, per-tool argv, detached start, close, worker detection. Takes `run`. |
| `src/kaptyorka/doctor.py` (new) | Checks and fixes for one machine; the tool-config checks are shared with `facts.py`. |
| `src/kaptyorka/fleet.py` (new) | `fleet add` / `fleet list` over ssh through an injected `run`. |
| `src/kaptyorka/bridge.py` | Heartbeat with facts and JSON answer; dispatch `launch` and `close` items; auto-update at a quiet moment. |
| `src/kaptyorka/adapters/base.py`, `claude.py`, `opencode.py`, `codex.py`, `cursor.py` | `Session.cwd`. |
| `src/kaptyorka/hub.py` | Heartbeat JSON answer; launches in poll/ack; `/bridge/worker`; tools `work`, `workers`, `adopt`, `close_worker`; routes `/api/workers*`, `/api/warnings`, `/api/machines` facts, device `auto_update`; brief lines; boss mark in `who`. |
| `src/kaptyorka/store.py` | Only what Global Constraints allow. |
| `src/kaptyorka/cli.py` | `doctor`, `fleet`. |
| `src/kaptyorka/join.py` | Save `person` from the join answer into `config.json`. |
| `src/kaptyorka/web/index.html` | Machines tab (facts, workers, attach lines, folder occupancy), warnings banner, auto-update toggle. |
| `README.md`, `CHANGELOG.md`, version files | 0.3.0. |
| tests | `test_orders.py`, `test_workers.py`, `test_facts.py`, `test_launch.py`, `test_hub_workers.py`, `test_doctor.py`, `test_fleet.py`, `test_page_workers.py`, `test_worker_slow.py`, plus edits to `test_bridge.py`, `test_claude.py`, `test_readme.py`, `test_cli.py`. |

---

### Task 1: Signed launch orders

**Files:**
- Create: `src/kaptyorka/orders.py`
- Test: `tests/test_orders.py`

**Interfaces:**
- Produces:
  - `signing_key(secret: str) -> str`: `hashlib.sha256(secret.encode()).hexdigest()`. The hub stores exactly this in `devices.secret_hash`; the bridge computes it from `config.json["secret"]`.
  - `sign(order: dict, key: str) -> dict`: returns a copy with `"sig"` = HMAC-SHA256 hex of `json.dumps(order_without_sig, sort_keys=True, separators=(",", ":"))` under `key.encode()`.
  - `verify(order: dict, key: str, now: float, seen_ids: Collection[int]) -> Optional[str]`: `None` when good; else one of the exact strings `"bad signature"`, `"order too old"` (when `now - order["issued"] > MAX_AGE_S`), `"order already run"` (when `order["id"] in seen_ids`), `"malformed order"` (missing `sig`, `id` or `issued`).
  - `MAX_AGE_S = 600`.

- [ ] **Step 1: Write the failing tests**

```python
def test_sign_and_verify_roundtrip():
    key = signing_key("s3cret")
    o = sign({"id": 1, "kind": "launch", "issued": 1000.0, "task": "x"}, key)
    assert verify(o, key, now=1001.0, seen_ids=()) is None

def test_changed_field_fails():
    key = signing_key("s3cret")
    o = sign({"id": 1, "kind": "launch", "issued": 1000.0, "task": "x"}, key)
    assert verify({**o, "task": "rm -rf"}, key, 1001.0, ()) == "bad signature"

def test_other_key_fails():
    o = sign({"id": 1, "issued": 1000.0}, signing_key("a"))
    assert verify(o, signing_key("b"), 1001.0, ()) == "bad signature"

def test_old_and_replayed_orders_are_refused():
    key = signing_key("s")
    o = sign({"id": 7, "issued": 1000.0}, key)
    assert verify(o, key, 1000.0 + 601, ()) == "order too old"
    assert verify(o, key, 1001.0, seen_ids={7}) == "order already run"

def test_malformed_order():
    assert verify({"id": 1}, signing_key("s"), 0.0, ()) == "malformed order"

def test_key_order_does_not_matter():
    key = signing_key("s")
    o = sign({"b": 1, "a": 2, "id": 1, "issued": 1.0}, key)
    assert verify(dict(reversed(list(o.items()))), key, 1.0, ()) is None
```

- [ ] **Step 2: Run them, expect ImportError**

Run: `python3 -m pytest tests/test_orders.py -q`

- [ ] **Step 3: Implement `orders.py`** (use `hmac.compare_digest`; verify checks `malformed` first, then signature, then age, then replay)

- [ ] **Step 4: Run, expect 6 passed; commit** `feat(orders): signed launch orders`

---

### Task 2: Worker tables, device facts, folders, candidates

**Files:**
- Create: `src/kaptyorka/workers.py`
- Modify: `src/kaptyorka/store.py` (`Store.__init__`: call `workers.ensure_schema(self.db)` after `executescript(SCHEMA)`; `ADDED_COLUMNS` gets `("agents", "cwd", "TEXT NOT NULL DEFAULT ''")`; `heartbeat` session dicts accept `cwd` and `_touch` stores it)
- Test: `tests/test_workers.py`

**Interfaces:**
- Consumes: `Store` (`db`, `lock`, `clock`, `people()`, `may_use(host, person_id)`, `project_role`, `_registered` is private: use `projects_for`/`may_write_project(project, principal)`), `Principal`, `ONLINE_S`.
- Produces:
  - `ensure_schema(db)` creates:
    - `workers(name TEXT PRIMARY KEY, project TEXT NOT NULL, task TEXT NOT NULL, boss_key TEXT NOT NULL, boss_person INTEGER NOT NULL, device_id INTEGER NOT NULL, host TEXT NOT NULL, tool TEXT NOT NULL, folder TEXT NOT NULL DEFAULT '', worktree INTEGER NOT NULL DEFAULT 0, key TEXT, state TEXT NOT NULL CHECK(state IN ('starting','up','unattended','abandoned','closed','failed')), error TEXT NOT NULL DEFAULT '', created REAL NOT NULL, up_at REAL, unattended_since REAL, closed_at REAL, waiting_told REAL)`
    - `launches(id INTEGER PRIMARY KEY AUTOINCREMENT, device_id INTEGER NOT NULL, worker TEXT NOT NULL, kind TEXT NOT NULL CHECK(kind IN ('launch','close')), body TEXT NOT NULL, created REAL NOT NULL, offered_at REAL, done_at REAL, ok INTEGER, error TEXT NOT NULL DEFAULT '')`
    - `device_facts(device_id INTEGER PRIMARY KEY, facts TEXT NOT NULL, folders TEXT NOT NULL DEFAULT '{}', updated REAL NOT NULL)`
    - `device_settings(device_id INTEGER PRIMARY KEY, auto_update INTEGER)` (NULL = default by role)
  - `class WorkerStore(store: Store)` with, in this task:
    - `facts_seen(device_id: int, facts: dict, folders: dict) -> None`: stores JSON; `facts` keys kept: `os_user, tmux (bool), load (float), cpus (int), tools ({tool: version}), hooks_ok ({tool: bool}), version, git ({cwd: {branch, dirty, subject}})`; anything else dropped; strings cut to 200.
    - `facts(device_id) -> dict` and `folders(device_id) -> dict` (empty when none).
    - `machine_rows(principal) -> List[dict]`: `store.machines(principal)` with each device extended by its `facts`, `folders`, `online` (bridge heartbeat within `ONLINE_S`, from `devices.last_used`) and `auto_update` (effective bool: the setting, else `role != 'admin'`).
    - `candidates(principal, project: str, machine: Optional[str]) -> Tuple[List[dict], List[str]]`: returns `(usable, reasons)`. A device is usable when its bridge is online, it has a signing secret (`secret_hash != ''`), and either `device.person_id == principal.person_id` or (`device.shared` and `store.may_use(device.host, principal.person_id)`); with `machine`, only devices on that host. Each usable dict: `{device_id, host, os_user, person_id, tmux, load, tools, folder}` where `folder` = `folders(device)[project]` or `None`. `reasons` has one sentence per device looked at and skipped: `"box-1 (bob): bridge offline"`, `"box-1 (bob): still on the shared token; invite it or assign it so it gets its own token"`, `"box-1 (bob): no folder for proj-a"`, `"box-1 (bob): no tmux (only Claude Code workers can run there)"` (the last is a note, not a skip, unless `tool` is not claude-code: that filtering is done by `choose`).
    - `choose(usable: List[dict], project: str, tool: str, now: float) -> Optional[dict]`: drop devices without a folder; drop devices without tmux when `tool != "claude-code"`; prefer a device whose folder has no online session (`agents` rows with `host`, `os_user` and `cwd == folder` and `last_seen > now - ONLINE_S`); then the lowest `load / cpus`. Returns the chosen dict with `occupied: bool` added.
    - `set_auto_update(device_id, on: Optional[bool])`.

- [ ] **Step 1: Write the failing tests** (use `tests/helpers.py::person` and a `Store(tmp_path/"hub.db", clock=fake)`; invite-redeem two devices so they have secrets)

```python
def test_facts_roundtrip_drops_unknown_keys(ws, dev):
    ws.facts_seen(dev, {"tmux": True, "load": 0.5, "cpus": 4, "junk": 1}, {"proj-a": "/home/alice/proj-a"})
    assert ws.facts(dev) == {"tmux": True, "load": 0.5, "cpus": 4}
    assert ws.folders(dev) == {"proj-a": "/home/alice/proj-a"}

def test_candidates_own_device_shared_device_and_reasons(store, ws, alice, bob):
    # alice's own online device: usable. bob's shared device on a host alice may use: usable.
    # bob's private device: not even listed. a legacy device: skipped with the shared-token sentence.
    ...
    assert [c["host"] for c in usable] == ["box-1", "box-2"]
    assert "box-3 (bob): still on the shared token; invite it or assign it so it gets its own token" in reasons

def test_choose_prefers_free_folder_then_low_load(store, ws, ...):
    ...
    assert ws.choose(usable, "proj-a", "claude-code", now)["host"] == "box-2"

def test_choose_needs_tmux_for_non_claude(ws, ...):
    assert ws.choose([{"tmux": False, "folder": "/x", "load": 0, "cpus": 1, ...}], "p", "codex", 0) is None

def test_effective_auto_update_default_by_role(ws, admin_dev, member_dev):
    rows = ws.machine_rows(admin_principal)
    ...  # admin's device False, member's True; after set_auto_update(member_dev, False) -> False

def test_agents_cwd_is_stored(store):
    store.heartbeat("box-1", [{"key": "claude:1", "tool": "claude-code", "project": "proj-a", "cwd": "/home/alice/proj-a"}])
    assert store.db.execute("SELECT cwd FROM agents WHERE key='claude:1'").fetchone()[0] == "/home/alice/proj-a"
```

- [ ] **Step 2: Run, expect failures**

- [ ] **Step 3: Implement** `ensure_schema`, the store touches, and `WorkerStore` as specified. `WorkerStore` methods take `store.lock` themselves and call only non-locking store internals inside it (read `store.db` directly). Offline bridge = `devices.last_used` older than `ONLINE_S`.

- [ ] **Step 4: Run `tests/test_workers.py` and `tests/test_store.py tests/test_legacy.py`, expect pass; commit** `feat(workers): worker tables, device facts, candidates`

---

### Task 3: Launch stream, linking, states, adopt, close

**Files:**
- Modify: `src/kaptyorka/workers.py`
- Test: `tests/test_workers.py`

**Interfaces:**
- Consumes: `orders.sign`, `orders.signing_key` (the hub signs with `devices.secret_hash` directly: it already is `signing_key(secret)`).
- Produces (all on `WorkerStore`):
  - `create(principal, project, task, device: dict, tool: str, boss_key: str, resume: Optional[str], occupied: bool) -> dict`: name = `f"{project}-w{n}"` with `n` the smallest positive integer free among `workers` rows of that project; inserts the worker (`state='starting'`, `worktree=occupied`) and a `launches` row of kind `launch` whose `body` is the signed order: `{"kind":"launch","id":<launch id>,"issued":now,"worker":name,"person":<boss person name>,"tool":tool,"project":project,"folder":device["folder"],"worktree":occupied,"task":task,"boss":<boss session name>,"resume":resume,"first_message":<text below>}`. Returns the worker dict. Raises `KaptyorkaError` when `task` is empty or the project name fails `PROJECT_NAME`.
    - First message text (exact): `f"You are kaptyorka worker `{name}` for `{boss}` ({person}). Your task:\n\n{task}\n\nTake orders only from `{boss}` and the hub admin. Report to `{boss}` with send(to=\"{boss}\", ...) when done or stuck."`
  - `offer(device_id: int) -> List[dict]`: launch rows for this device with `done_at IS NULL` and (`offered_at IS NULL` or older than 60 s); marks them offered; returns `[json.loads(body) | {"launch": True}]`.
  - `ack(launch_id: int, ok: bool, error: str, device_id: int) -> bool`: False when the row is not this device's; on failure of a `launch` sets the worker `failed` with `error` and returns the boss key via `pending_notices` below; on success of a `close` sets the worker `closed`.
  - `link(worker: str, key: str, device_id: int) -> bool`: worker must be `starting` on this device; sets `key`, `state='up'`, `up_at`. The hub then updates `agents.person_id` for that key to the boss person (session belongs to the boss's group) by calling `store.seen`-free SQL inside the same method.
  - `by_key(key) -> Optional[dict]`, `by_name(name) -> Optional[dict]`, `list(principal) -> List[dict]`: own person's workers, or all for an admin; each dict has `name, project, task, host, os_user, tool, folder, worktree, state, error, boss, key, attach` where `attach` is `f"claude attach {name}"` for claude-code and `f"ssh {os_user}@{host} -t tmux attach -t {name}"` otherwise.
  - `tick(now) -> List[Tuple[str, str]]`: state changes; returns notices `(to_key, text)` for the hub to deliver. Rules: a worker `up` whose boss session has been offline longer than `UNATTENDED_S = 120` becomes `unattended` (`unattended_since=now`); `unattended` longer than `ABANDONED_S = 86400` becomes `abandoned`; a worker `up`/`unattended` whose own session has been offline longer than 600 s becomes `closed` with error `"session gone"`. A worker whose session activity is `permission_prompt` or `needs_input` and `waiting_told` is NULL or older than 1800 s yields the notice `(boss_key, f"worker `{name}` is waiting for a permission; attach with `{attach}`")` and sets `waiting_told=now`. Notices are produced only for online boss keys (an offline boss gets them through adopt).
  - `adopt(principal, name, new_boss_key) -> dict`: worker must belong to the principal's person and be `unattended` or `abandoned`; sets `boss_key`, `state='up'`, clears `unattended_since`; re-targets unread messages addressed to the old boss key from the worker's key (`UPDATE messages SET to_key=?, push_state='new' WHERE to_key=? AND sender_key=? AND read_at IS NULL`). Returns the worker dict.
  - `close(principal, name) -> dict`: worker must belong to the principal's person (or admin) and not be `closed`/`failed`; inserts a `launches` row of kind `close` with body `{"kind":"close","id":..,"issued":now,"worker":name,"tool":tool,"key":key}` signed; state stays until the bridge acks. Returns the worker dict with `note` = `"its worktree <folder> stays in place"` when `worktree`.
  - `unattended_for(person_id) -> List[dict]`: workers in `unattended`/`abandoned` of that person (for the start brief).

- [ ] **Step 1: Write the failing tests** (fake clock; two people; a device with a secret; sessions through `store.heartbeat`)

```python
def test_create_names_and_signs(ws, ...):
    w = ws.create(alice_p, "proj-a", "fix the tests", dev, "claude-code", "claude:boss", None, False)
    assert w["name"] == "proj-a-w1" and w["state"] == "starting"
    [item] = ws.offer(dev["device_id"])
    assert item["launch"] is True and item["worker"] == "proj-a-w1"
    assert verify(item_without_launch_flag, secret_hash_of(dev), now, ()) is None
    assert ws.offer(dev["device_id"]) == []  # offered once per 60 s

def test_second_launch_in_same_folder_gets_worktree(ws, ...):
    # a session online in /home/alice/proj-a; choose() -> occupied True; create() -> worktree 1

def test_link_marks_up_and_moves_session_to_boss_group(ws, store, ...):
    # heartbeat shows claude:w1 with no person; link -> agents.person_id == boss person; state up

def test_failed_launch_marks_failed_with_error(ws, ...):
    assert ws.ack(launch_id, False, "no folder", dev_id) is True
    assert ws.by_name("proj-a-w1")["state"] == "failed" and "no folder" in ws.by_name("proj-a-w1")["error"]

def test_ack_for_another_device_is_refused(ws, ...):
    assert ws.ack(launch_id, True, "", other_dev_id) is False

def test_boss_blip_is_not_unattended(ws, clock, ...):
    # boss offline 60 s -> still up; offline 130 s -> unattended; 24 h later -> abandoned

def test_waiting_notice_once_per_half_hour(ws, ...):
    # worker activity permission_prompt -> one notice to the online boss; tick again -> none; +1801 s -> again

def test_adopt_retargets_held_messages(ws, store, ...):
    # worker sent 2 messages to old boss (offline) -> adopt from a new session -> store.inbox(new) has both

def test_close_queues_signed_close_and_notes_worktree(ws, ...):
    w = ws.close(alice_p, "proj-a-w1")
    assert w["note"].startswith("its worktree ")
    assert ws.offer(dev_id)[0]["kind"] == "close"
    ws.ack(close_id, True, "", dev_id)
    assert ws.by_name("proj-a-w1")["state"] == "closed"

def test_only_own_or_admin_may_adopt_or_close(ws, ...):
    with pytest.raises(KaptyorkaError): ws.close(bob_p, "proj-a-w1")
```

- [ ] **Step 2: Run, expect failures**
- [ ] **Step 3: Implement** as specified; name generation and the launch insert happen in one `with lock` block so two concurrent creates cannot take the same name or both see the folder free (re-check occupancy inside the lock).
- [ ] **Step 4: Run `tests/test_workers.py`, expect pass; commit** `feat(workers): launch stream, linking, states, adopt and close`

---

### Task 4: Warnings

**Files:**
- Modify: `src/kaptyorka/workers.py`
- Test: `tests/test_workers.py`

**Interfaces:**
- Produces: `WorkerStore.warnings(now, hub_version: str) -> List[dict]`, each `{"host", "os_user", "person", "text"}`, texts exactly:
  - `"bridge on {host} ({os_user}) has been offline for {n} minutes"` when `devices.last_used` is older than 600 s and the device is not revoked and not legacy; `n` = whole minutes.
  - `"{host} ({os_user}) runs kaptyorka {v}; the hub runs {hub_version}"` when `_version_at_least(hub_version, parse(v))` is false… simpler: when `v != hub_version` and `v` sorts lower as a tuple of ints.
  - `"{tool} on {host} ({os_user}) is set up but its kaptyorka hooks are missing"` for each `hooks_ok[tool] == False`.
  - `"{host} ({os_user}) has no tmux: only Claude Code workers can run there"` when the device is shared and `tmux` is False.
  - `"worker {name} on {host} is unattended since {hh:mm}"` / `"worker {name} on {host} was abandoned"` / `"worker {name} on {host} failed to start: {error}"` (failed ones from the last 24 h).
- Sorted: failed launches first, then offline bridges, then the rest by host.

- [ ] **Step 1: Write the failing test** `test_warnings_texts_and_order` covering all six texts with box-1/alice/bob.
- [ ] **Step 2: Run, expect failure**
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run, expect pass; commit** `feat(workers): warnings from heartbeats and worker states`

---

### Task 5: Hub side: heartbeat JSON, launches in poll and ack, worker routes and tools

**Files:**
- Modify: `src/kaptyorka/hub.py`, `src/kaptyorka/hub_feed.py` (boss mark), `src/kaptyorka/join.py` (save `person` from the join answer; `/i/{code}` already returns the person name? If not, add `"person"` to its JSON answer in `hub.py`)
- Test: `tests/test_hub_workers.py`, `tests/test_join.py`

**Interfaces:**
- Consumes: `WorkerStore` (Tasks 2 to 4), `orders`.
- Produces:
  - `build_app(store)` creates `ws = WorkerStore(store)`; a background task calls `ws.tick(now)` every 15 s and delivers each notice with `store.send_system(to_key, text)`… `Store` has no such method and must not grow: use the existing `store.send(sender_key, to, text)` with `sender_key = "hub:workers"` after a `store.seen("hub:workers", "hub", "hub", "workers")`; the row is online while the hub lives. Ruling: a `hub` pseudo-session named `hub/hub/workers` is acceptable and visible in `who()`; mask nothing.
  - `POST /bridge/heartbeat` answers **200 JSON** `{"version": __version__, "auto_update": bool, "projects": [registered project names the device's person may read], "you": person_name, "shared": bool}`; body may carry `facts` (dict) and `folders` (dict); stored with `ws.facts_seen(device_id, ...)`. The session dicts may carry `cwd`. A legacy principal gets no `auto_update` (False).
  - `GET /bridge/poll` returns `store.offer(...) + ws.offer(device_id)`; launch items carry `"launch": true`.
  - `POST /bridge/ack` body `{"id", "ok", "launch": true, "error": ""}` routes to `ws.ack`; 404 when it returns False. Messages unchanged.
  - `POST /bridge/worker` body `{"worker", "key"}` → `ws.link`; 404 when False; on success sends the boss `"worker `{name}` is up on {host}; attach with `{attach}`"` through the hub pseudo-session.
  - Tools:
    - `work(project: str, task: str, ctx, machine: str = "", tool: str = "claude-code", resume: str = "") -> str`: checks `store.may_write_project(project, principal)` or the project is unregistered and `agent.project == project` (own folder); `ws.candidates` → `ws.choose` → `ws.create`; `wake_bridges()`; returns `"starting `{name}` on {host} ({os_user})"` plus `" in a worktree, the folder is busy"` when occupied; when nothing usable: `"no machine can take proj-a: "` + reasons joined by `"; "`. Occupied by another person: for now returns `"busy: {host} has {other}'s session in that folder; ask again later or name another machine"` (the manager handles it in part 5).
    - `workers(ctx) -> List[dict]` = `ws.list(principal)`.
    - `adopt(name: str, ctx) -> str` → `"you are now the boss of `{name}`; {k} held messages delivered"`.
    - `close_worker(name: str, ctx) -> str` → `"closing `{name}`"` + `"; " + note` when a worktree.
  - `who()` rows gain `"boss": True` on the caller's boss session and `"worker": name` on sessions that are workers.
  - Start brief: a worker session's brief starts with its first message text when `ws.by_key(key)` exists and the session is younger than 5 minutes (covers tools whose command line cannot carry the message); every session's brief lists `unattended_for(person)` as `"Unattended workers: proj-a-w1 (box-1); adopt(name) takes one over."`; the admin's brief appends the first three warnings.
  - Routes: `GET /api/workers`, `POST /api/workers/{name}/close`, `POST /api/workers/{name}/adopt` (page acting as the person: boss_key = `web:<person>`; a web boss gets notices on the page feed), `GET /api/warnings` (admin), `GET /api/machines` now `ws.machine_rows`, `POST /api/devices/{id}/auto_update` body `{"on": true|false|null}` (admin).
  - `INSTRUCTIONS` gains one sentence: `"work(project, task) starts a worker session for you on another machine; workers() lists yours; adopt(name) takes over an unattended one; close_worker(name) ends one."`

- [ ] **Step 1: Write the failing tests** (in-process hub as in `tests/test_hub_people.py`; a bridge is simulated with raw HTTP calls)

```python
def test_heartbeat_answers_json_with_projects_and_version(...)
def test_poll_carries_signed_launch_and_ack_links_worker(...)
    # work() -> poll shows launch (verify signature with the device's secret) -> POST /bridge/worker -> boss inbox has "is up"
def test_work_refuses_without_write_role(...)
def test_work_allows_own_unregistered_folder(...)
def test_legacy_device_cannot_host_workers(...)
    # answer contains "still on the shared token"
def test_admin_cannot_use_members_private_device(...)
def test_busy_folder_of_another_person_answers_busy(...)
def test_who_marks_boss_and_worker(...)
def test_worker_brief_starts_with_task(...)
def test_unattended_worker_in_brief_and_adopt(...)
def test_close_worker_queues_close_item(...)
def test_auto_update_route_and_machine_rows(...)
def test_join_saves_person(...)  # tests/test_join.py
```

- [ ] **Step 2: Run, expect failures**
- [ ] **Step 3: Implement**; keep the 0.2 bridge working: it ignores the heartbeat body and sends no `facts`.
- [ ] **Step 4: Run `tests/test_hub_workers.py tests/test_hub_people.py tests/test_bridge.py tests/test_join.py`, expect pass; commit** `feat(hub): work, workers, adopt, close_worker; launches in the bridge stream`

---

### Task 6: Session folders and machine facts on the bridge

**Files:**
- Modify: `src/kaptyorka/adapters/base.py` (`Session.cwd: str = ""`), `claude.py` (cwd from the session file), `opencode.py` and `codex.py` (cwd when the pid is known: `identity.proc_cwd(pid)`), `cursor.py` (the workspace path)
- Create: `src/kaptyorka/facts.py`
- Modify: `src/kaptyorka/bridge.py` (heartbeat sends `facts` and `folders`, session dicts include `cwd`; parses a 200 JSON answer into `self.hub = {...}`; 204 still fine)
- Test: `tests/test_facts.py`, `tests/test_bridge.py`, `tests/test_claude.py`

**Interfaces:**
- Produces:
  - `facts.machine(adapters, run=subprocess.run, platform=sys.platform) -> dict` with the keys Task 2 keeps: `os_user` (getpass), `tmux` (`shutil.which("tmux") is not None`), `load` (`os.getloadavg()[0]`, 0.0 on failure), `cpus` (`os.cpu_count() or 1`), `tools` (`{a.tool: version}` for installed adapters; version from `run([exe, "--version"])` first line, cached for 10 minutes), `hooks_ok` (`{a.tool: doctor.tool_configured(a)}`, see Task 8: until then a stub returning True), `version` (`__version__`).
  - `facts.git(cwd: str, run=subprocess.run) -> Optional[dict]`: `{"branch", "dirty": bool, "subject"}` from `git -C cwd rev-parse --abbrev-ref HEAD`, `git -C cwd status --porcelain`, `git -C cwd log -1 --format=%s`; `None` when cwd is not a repo; each call 2 s timeout; cached per cwd for 30 s.
  - `facts.folders(projects: List[str], sessions: List[Session], home: Path) -> dict`: `{project: path}` for each requested project where `home/project` is a directory, overridden by a learned path: the cwd of any session whose basename equals the project; learned paths are remembered in `~/.kaptyorka/folders.json` (written when changed) so a folder is known after the session ends.
  - `Bridge.heartbeat` posts `facts`, `folders` (for `self.hub.get("projects", [])`) and per-session `cwd` and `git` (under `facts["git"][cwd]`). Facts are gathered at most once per 60 s; sessions every beat as today.
  - `Bridge.hub: dict` (last JSON answer; `{}` before the first or on 204).

- [ ] **Step 1: Write the failing tests** (`test_facts.py` with a fake `run`; `test_bridge.py::test_heartbeat_sends_facts_and_reads_answer`; `test_claude.py::test_sessions_carry_cwd`)
- [ ] **Step 2: Run, expect failures**
- [ ] **Step 3: Implement**; the `tests/conftest.py` fence must still pass: facts take `run` and tests inject it.
- [ ] **Step 4: Run those files plus `tests/test_opencode.py tests/test_codex.py tests/test_cursor.py`, expect pass; commit** `feat(bridge): machine facts, session folders, JSON heartbeat answer`

---

### Task 7: Launcher and bridge dispatch of launch and close orders

**Files:**
- Create: `src/kaptyorka/launch.py`
- Modify: `src/kaptyorka/bridge.py`
- Test: `tests/test_launch.py`, `tests/test_bridge.py`

**Interfaces:**
- Consumes: `orders.verify`, `orders.signing_key(cfg["secret"])`, `Bridge.hub` (`you`, `shared`), `facts.folders` learned paths.
- Produces:
  - `class Launcher(cfg: dict, home: Path, run=subprocess.run, popen=subprocess.Popen, platform=sys.platform, log=print)`
    - `folder_for(order) -> Tuple[str, bool]`: `order["folder"]` when set, else learned path, else `home/project`; raises `LaunchError("no folder for {project} on this machine (looked in {paths})")` when none exists. When `order["worktree"]` or the folder has a live session of this account (`Launcher.occupied(folder, sessions)`), returns a worktree: the first free `f"{folder}--{n}"`, created with `run(["git","-C",folder,"worktree","add","-b",f"work/{name}",path])`; `(path, True)`. Raises `LaunchError` with git's stderr when that fails or the folder is not a repo.
    - `command(order, folder) -> List[str]` (argv, the task text is one element):
      - claude-code: `["claude", "--bg", "-n", name, *(["--resume", resume] if resume else []), first_message]`; the bridge reads the id that `claude --bg` prints and the key is `f"claude:{id}"` (Claude Code 2.1.319 ignores `--session-id` with `--bg`).
      - codex: `["tmux", "new-session", "-d", "-s", name, "-c", folder, "codex", *(["resume", resume] if resume else []), first_message]`.
      - opencode: `["tmux", "new-session", "-d", "-s", name, "-c", folder, "opencode", *(["--session", resume] if resume else []), "--prompt", first_message]` (the implementer verifies `opencode --help` shows `--prompt`; if it does not, drop it and set `needs_brief`, so the hub's start brief carries the task).
      - anything else: `LaunchError("no worker support for {tool}")`.
    - `detach(argv, folder, name) -> None`: Linux: `popen(["systemd-run", "--user", "--scope", "--quiet", "--unit", f"kaptyorka-worker-{name}", *argv], cwd=folder, env=os.environ, stdin=DEVNULL, stdout=log file `~/.kaptyorka/workers/{name}.log`, stderr=STDOUT)`; when `systemd-run` is missing, fall back to `start_new_session=True`. macOS: `start_new_session=True`. For claude-code `--bg` returns at once; `wait(timeout=60)` and raise `LaunchError(stdout+stderr)` when the return code is not 0 or the output contains `Workspace not trusted` (message: `"Claude Code does not trust {folder}; open claude there once and accept"`). For tmux: `wait(timeout=10)`, same rule.
    - `launch(order) -> dict`: `{"key": expected key or None, "folder", "worktree": bool, "needs_brief": bool}`.
    - `close(order) -> None`: claude-code: `run(["claude", "stop", name])`; others: `run(["tmux", "kill-session", "-t", name])`; failures raise `LaunchError`.
    - `find_key(order, sessions: List[Session]) -> Optional[str]`: for tmux tools, the key of a session with `tool == order["tool"]` and `cwd == folder`; for claude-code the expected key when it is in `sessions`.
  - `Bridge.poll_once` handles items with `"launch": True`: `verify(order, key, time.time(), self.seen_ids)` (ids persisted in `~/.kaptyorka/launched.json`, last 200); refuses an order whose `person != self.hub.get("you")` unless `self.hub.get("shared")` with the error `"this account belongs to {you}"`; `launch` → remembers `pending[worker] = (order, result)`; `close` → `close`; every outcome acks with `{"id", "ok", "launch": True, "error"}`. Each heartbeat checks `pending` with `find_key` and posts `/bridge/worker` `{"worker", "key"}`; a pending launch not seen within 120 s is reported through `/bridge/worker` with `{"worker", "error": "the session did not show up within 2 minutes; see ~/.kaptyorka/workers/{name}.log"}` and the hub marks it failed (extend `ws.link` / the route to accept `error`).

- [ ] **Step 1: Write the failing tests** (fake `run`/`popen` recording argv; a fake session list)

```python
def test_task_text_is_one_argument(launcher):
    argv = launcher.command(order(task='say "hi"; $(rm -rf /) -x\nline2'), "/home/alice/proj-a")
    assert argv[-1] == 'You are kaptyorka worker ...' and 'say "hi"' in argv[-1]
    assert argv[0] == "claude" and "--bg" in argv and "--dangerously-skip-permissions" not in argv

def test_folder_default_learned_and_missing(...)
def test_occupied_folder_gets_worktree_with_branch(...)  # git worktree add -b work/proj-a-w1 /home/alice/proj-a--1
def test_detach_uses_systemd_scope_on_linux_and_setsid_on_mac(...)
def test_untrusted_workspace_is_a_clear_error(...)
def test_close_commands(...)
def test_bridge_refuses_replay_and_bad_signature(hub, home, ...)
def test_bridge_refuses_other_persons_order_unless_shared(...)
def test_bridge_reports_worker_key_when_session_appears(...)
def test_bridge_reports_timeout_after_two_minutes(...)
```

- [ ] **Step 2: Run, expect failures**
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run `tests/test_launch.py tests/test_bridge.py`, expect pass; commit** `feat(bridge): start and close workers from signed orders`

---

### Task 8: Doctor

**Files:**
- Create: `src/kaptyorka/doctor.py`
- Modify: `src/kaptyorka/cli.py` (`doctor [--fix]`), `src/kaptyorka/facts.py` (`hooks_ok` uses `doctor.tool_configured`)
- Test: `tests/test_doctor.py`, `tests/test_cli.py`

**Interfaces:**
- Produces:
  - `doctor.tool_configured(adapter, home: Path) -> bool`: True when the tool's config holds our MCP entry and, for tools with hooks, our hook commands (`is_our_hook`); implemented per tool by reading the same files `configure` writes; no writes.
  - `doctor.checks(cfg, adapters, home, run, platform) -> List[Check]` with `Check(name: str, ok: bool, detail: str, fix: Optional[str])`, names exactly: `config`, `hub reachable`, `token valid`, `bridge service`, `hub service` (only on the hub machine: `hub.json` present), `tmux`, and one `tool: <name>` per installed adapter. `fix` is the command to run by hand (`kaptyorka join`, `systemctl --user restart kaptyorka-bridge`, `sudo apt install -y tmux` / `brew install tmux`).
  - `doctor.run(cfg, fix: bool, ...) -> int`: prints `ok   <name>` / `FAIL <name>: <detail> -> <fix>` one per line; with `--fix` re-runs `configure` for failing tools and `service.restart` for a stopped bridge, then re-checks; returns 1 when anything still fails.

- [ ] **Step 1: Write the failing tests** (fake home with a Claude settings file with and without our hooks; fake run; `test_cli.py::test_doctor_prints_one_line_per_check`)
- [ ] **Step 2: Run, expect failures**
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run, expect pass; commit** `feat(cli): kaptyorka doctor`

---

### Task 9: Fleet

**Files:**
- Create: `src/kaptyorka/fleet.py`
- Modify: `src/kaptyorka/cli.py` (`fleet add <user@host>... --person <name> [--key-from <file>]`, `fleet list`)
- Test: `tests/test_fleet.py`, `tests/test_cli.py`

**Interfaces:**
- Consumes: the admin routes through `cli._admin_call` (`POST /api/invites` gives `{"url"}`), `GET /api/machines`.
- Produces:
  - `fleet.add(targets: List[str], person: str, key_file: Optional[Path], hub_url: str, invite: Callable[[str], str], run=subprocess.run) -> List[str]`: one line per target. Per target, through `run(["ssh", "-o", "BatchMode=yes", target, <command>])`: `pipx --version || (python3 -m pip install --user pipx && python3 -m pipx ensurepath)`; `pipx install kaptyorka || pipx upgrade kaptyorka`; `kaptyorka join <invite-url> --name <user>@<host>`; when `key_file`: the key's `NAME=value` line is passed on stdin to `kaptyorka join --env-stdin` (new flag: `join.py` reads one `NAME=value` per line from stdin and writes them into the bridge service environment through `service.install(..., env=...)`); then `which claude opencode codex` to report tools. Lines: `"{target}: joined as {name}; tools: claude-code, codex"` or `"{target}: failed at {step}: {stderr first line}; run by hand: {command}"`. Never echoes the key.
  - `fleet.list(machines: List[dict]) -> List[str]`: `"{host} {os_user:<12} {person:<10} {online|offline} {version}"` per device.

- [ ] **Step 1: Write the failing tests** (fake `run` asserting the ssh argv and that the key never appears in any argv; the stdin carries it)
- [ ] **Step 2: Run, expect failures**
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run, expect pass; commit** `feat(cli): kaptyorka fleet add and list`

---

### Task 10: Auto-update

**Files:**
- Modify: `src/kaptyorka/bridge.py`
- Test: `tests/test_bridge.py`

**Interfaces:**
- Produces: after a heartbeat answer with `version` newer than `__version__` (compare int tuples) and `auto_update` true, `Bridge._maybe_update(run)`: at the next `_step` whose poll delivered nothing and no launch is pending, logs `"updating kaptyorka {old} -> {new}"` and runs `run([sys.executable's venv bin/kaptyorka, "update"])` once per version (the update restarts the service; if it fails, log once and retry after 1 hour). `Bridge(..., run=subprocess.run)` gains the parameter.

- [ ] **Step 1: Write the failing tests** `test_auto_update_runs_once_at_quiet_moment`, `test_no_auto_update_when_off_or_same_version`
- [ ] **Step 2: Run, expect failures**
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run `tests/test_bridge.py`, expect pass; commit** `feat(bridge): auto-update when the hub is newer`

---

### Task 11: Slow tests: a real worker

**Files:**
- Create: `tests/test_worker_slow.py`

- [ ] **Step 1: Write** three tests marked `slow`:
  - `test_claude_worker_round_trip`: a hub in a thread, this machine joined with a real device (invite redeemed into a temp `KAPTYORKA_HOME`), a `Bridge` with the real `ClaudeAdapter` in a thread, a throwaway folder beside the repo as in `tests/test_claude_slow.py` (git-initialised), `work()` called through the MCP client as a boss session; assert the worker shows up `up` within 120 s, the boss inbox gets "is up", then `close_worker` and the session is gone. Skip when `claude` is missing or the workspace is untrusted.
  - `test_worker_outlives_bridge_restart`: Linux only; the launcher starts `["sleep", "300"]` through `detach` under a scope unit; kill the launching process; `systemctl --user is-active kaptyorka-worker-<name>.scope` says active; stop the scope at the end.
  - `test_tmux_worker_starts_and_closes`: tmux present; a fake tool argv (`["bash", "-c", "sleep 300"]`) in a tmux session named by the launcher; `close` kills it.
- [ ] **Step 2: Run `python3 -m pytest tests/test_worker_slow.py -m slow -q`**; expected: 3 passed (or skips with the stated reasons). Fix what fails in `launch.py`.
- [ ] **Step 3: Commit** `test: real worker launch, bridge restart survival, tmux worker`

---

### Task 12: Page: Machines tab, workers, warnings, auto-update

**Files:**
- Modify: `src/kaptyorka/web/index.html`
- Test: `tests/test_page_workers.py` (page-source tests as in `tests/test_page_people.py`) plus one headless screenshot `docs/screens/machines.png` with demo names

**Interfaces:**
- Consumes: `GET /api/machines` (facts, folders, online, auto_update), `GET /api/workers`, `POST /api/workers/{name}/close|adopt`, `GET /api/warnings`, `POST /api/devices/{id}/auto_update`.
- Produces:
  - A `Machines` tab for everyone, between Notes and People: one card per host (icon, name, owner); under it one row per account: person, online dot, version, load as `x.x / n cpus`, tmux yes/no, tools with versions, folders known (project → path) with `busy` when a session is in that folder; the admin sees an `auto-update` switch per account.
  - A `Workers` section at the top of that tab: name, project, machine, state badge (`starting`, `up`, `unattended`, `abandoned`, `failed`, `closed`), boss, the attach line in a copyable `<code>`, buttons `Adopt` (unattended/abandoned, own) and `Close`.
  - A warnings banner above the tabs for the admin: one line per warning, the first three, `+n more` opens the tab.
  - Style as the 0.1.8 page: same tokens, no new fonts.

- [ ] **Step 1: Write the page-source tests** (tab present, routes referenced, states listed, banner element)
- [ ] **Step 2: Implement**
- [ ] **Step 3: Demo hub screenshot** (scratchpad script as in part 1, demo names only) at desktop and 390 px; look at both; commit only `docs/screens/machines.png`.
- [ ] **Step 4: Run `tests/test_page_workers.py tests/test_page_people.py tests/test_page.py`, expect pass; commit** `feat(page): machines tab with workers and warnings`

---

### Task 13: README, CHANGELOG, 0.3.0

**Files:**
- Modify: `README.md` (sections: What agents can do gains `work`, `workers`, `adopt`, `close_worker`; new section `## Workers` with the headline story in five lines and the attach lines; `## Commands` gains `doctor`, `fleet add`, `fleet list`; `## Files` gains `workers/`, `folders.json`, `launched.json`; a line on permissions: nothing bypassed, the boss is told when a worker waits; a line that only Claude Code reports waiting), `CHANGELOG.md` (`## 0.3.0`), `pyproject.toml`, `src/kaptyorka/__init__.py`, `tests/test_cli.py`
- Test: `tests/test_readme.py`

- [ ] **Step 1: Bump the version in the four places; run `python3 -m pytest tests/test_cli.py -q`**, expect pass.
- [ ] **Step 2: Write the README and CHANGELOG text**; every command example must be one `test_readme.py` accepts.
- [ ] **Step 3: Run `python3 -m pytest tests -q -W default`**, expect all pass, no warnings.
- [ ] **Step 4: Commit** `docs: workers, doctor, fleet; v0.3.0`

---

## Self-review notes

- Spec coverage: machine facts (6), asking for a worker steps 1 to 6 (2, 3, 5), bridge launch and detach (7), permissions notice (3, 5), boss and worker, adopt, abandoned, close (3, 5), one session per folder (2, 3, 7), fleet (9), doctor (8), auto-update (10), warnings (4, 5, 12), slow tests (11), README (13). Backup is in Part 3's plan with memory, not here.
- Signing: the spec says "a per-device signing secret the hub returned once and never sends again"; the hub stores only its hash, so the HMAC key is that hash, which both sides can compute. Equivalent strength; no schema change. Recorded as a ruling.
- Folder discovery: the spec's `~/<project>` default is kept and extended with learned folders from sessions, because real projects rarely sit in the home root. Recorded as a ruling.
- Codex and OpenCode prompt flags are to be verified by Task 7's implementer against the installed versions (codex-cli 0.166.0, opencode 1.19.5 on this PC).
