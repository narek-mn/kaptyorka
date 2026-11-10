# Manager Wake (0.6.2) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The hub's manager session can be woken by a pushed message again: it is started without `--restricted`, which in Claude Code 2.1.3xx removes the session's inbox socket.

**Architecture:** Only the launch command and the manager's settings file change. `--restricted` is replaced by `--setting-sources project,local`, which skips the admin's user settings (permission mode, allow rules, hooks, plugins) the same way, while the session keeps its inbox socket, its `--settings` hooks and its `--mcp-config` server. The folder's `CLAUDE.md` now loads as project memory, so the `--append-system-prompt-file` flag goes. The settings file pins `permissions.defaultMode` to `default` so no other source can raise it.

**Tech Stack:** Python 3.10, pytest. No new dependencies.

**Spec:** none; the findings below are the spec. Measured on Claude Code 2.1.322 with `--bg` sessions:

| start flags | inbox socket | `--settings` hooks ran | `--mcp-config` server started |
|---|---|---|---|
| `--restricted --tools=ToolSearch` (0.6.1) | no | yes | yes |
| `--tools=ToolSearch` (no restriction) | yes | yes | yes |
| `--safe-mode --tools=ToolSearch` | yes | **no** | **no** |
| `--setting-sources project,local --tools=ToolSearch` | yes | yes | yes |

With `--setting-sources project,local`: user settings and plugins do not load; the admin's user-level `CLAUDE.md` memory still loads, and the folder's `CLAUDE.md` loads as project memory. Two identical hook commands from different entries run once. `--mcp-config` is variadic: the token after its file must be a flag, never the first message.

## Global Constraints

- Python 3.10 floor; no new dependencies; `python3 -m pytest tests -q` green.
- Never run `kaptyorka` or `claude` against the real machine in tests.
- No team names in code, tests or docs.
- Commit trailers as in `git log -1`.
- Version 0.6.2 in `pyproject.toml`, `src/kaptyorka/__init__.py`, `tests/test_cli.py`; a `## 0.6.2` CHANGELOG section.

## Review Focus

1. The first message must still be the last argv element and must follow a flag that takes no list (`--tools=ToolSearch` is such a flag; `--mcp-config` and `--setting-sources` are not). Test: `test_manager_command_runs_on_its_own_settings_and_mcp_config` asserts the full argv order.
2. `--append-system-prompt-file` removed: the manager instructions reach the session only through the folder's `CLAUDE.md`, which `hub.py` writes again when rules change (unchanged). README must say so.
3. `DeliveryError` for a session without a socket is unchanged: the only no-socket session is a restricted one, among them an old 0.6.1 manager still running during an upgrade.

---

### Task 1: Launch without `--restricted`

**Files:**
- Modify: `src/kaptyorka/launch.py:90-105` (`Launcher.command`, manager branch and docstring)
- Modify: `src/kaptyorka/manager.py:67-77` (comment at 67-69, `DENY` at 70, `settings()` at 75-77)
- Test: `tests/test_launch.py::test_manager_command_runs_on_its_own_settings_and_mcp_config`
- Test: `tests/test_manager.py:292-296`, the module constant `SETTINGS_WANTED` that the settings test compares against

**Interfaces:**
- Produces: `Launcher.command(order, folder)` for a manager order returns exactly
  `["claude", "--bg", "-n", name, "--settings", f"{folder}/settings.json", "--strict-mcp-config", "--mcp-config", f"{folder}/mcp.json", "--setting-sources", "project,local", "--tools=ToolSearch", first]`
- Produces: `manager.settings(kaptyorka)["permissions"] == {"allow": ["mcp__kaptyorka__*"], "deny": DENY, "defaultMode": "default"}`

- [ ] **Step 1: Change the two tests to the new argv and the new permissions dict**

In `test_manager_command_runs_on_its_own_settings_and_mcp_config`, the expected list becomes the one in Interfaces (no `--restricted`, no `--append-system-prompt-file`); keep the `--dangerously-skip-permissions` / `--resume` assertions and the codex refusal. Add `assert argv[-1] == FIRST + "fix the tests" and argv[-2] == "--tools=ToolSearch"`. In `SETTINGS_WANTED`, add `"defaultMode": "default"` to `permissions`.

- [ ] **Step 2: Run them to see them fail**

Run: `python3 -m pytest tests/test_launch.py tests/test_manager.py -q -k "manager_command or settings"`
Expected: FAIL on the argv list and the missing `defaultMode`.

- [ ] **Step 3: Implement**

`launch.py`: replace `"--restricted"` with `"--setting-sources", "project,local"`, drop the `--append-system-prompt-file` pair. Docstring: "`--setting-sources project,local` skips the admin's user settings; the folder's CLAUDE.md loads as project memory; the first message stays last, after `--tools=`, because `--mcp-config` and `--setting-sources` take lists." `manager.py`: `settings()` adds `"defaultMode": "default"`; rewrite the comment above `DENY` (no `--restricted`).

- [ ] **Step 4: Run the two test files**

Run: `python3 -m pytest tests/test_launch.py tests/test_manager.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/kaptyorka/launch.py src/kaptyorka/manager.py tests/test_launch.py tests/test_manager.py
git commit -m "fix(manager): start without --restricted so the hub can push to it"
```

### Task 2: Docs and version

**Files:**
- Modify: `README.md:269` (the flags paragraph of "The manager"), `README.md:271` (unchanged unless it names a flag)
- Modify: `tests/test_readme.py:53-54`
- Modify: `CHANGELOG.md`, `pyproject.toml`, `src/kaptyorka/__init__.py`, `tests/test_cli.py:39`

**Interfaces:**
- Consumes: the argv from Task 1.
- Produces: nothing new; `DeliveryError` text stays as it is.

- [ ] **Step 1: Update the tests**

`tests/test_readme.py:53-54`: replace "`--restricted`" with "`--setting-sources project,local`" and drop the `--append-system-prompt-file` item. `tests/test_cli.py:39`: `"0.6.2"`.

- [ ] **Step 2: Run them to see them fail**

Run: `python3 -m pytest tests/test_readme.py tests/test_cli.py -q`
Expected: FAIL on the two changed assertions.

- [ ] **Step 3: Implement**

`README.md:269`: the flags are `--settings ~/.kaptyorka/manager/settings.json`, `--strict-mcp-config --mcp-config ~/.kaptyorka/manager/mcp.json`, `--setting-sources project,local` and `--tools=ToolSearch`; say that `--setting-sources project,local` makes Claude Code skip the admin's user settings (permission mode, allow rules, hooks, plugins), that the admin's user-level `CLAUDE.md` still loads as memory, that the manager's own instructions come from the folder's `CLAUDE.md`, and that `settings.json` pins the permission mode to `default`. Remove the sentence about bypass mode and file tools. Add one sentence: without `--restricted` the session has an inbox socket, so the hub can wake it with a message (Claude Code 2.1.3xx removes that socket in restricted mode). `README.md:426` stays. `CHANGELOG.md`: a `## 0.6.2` section with one bullet: "The manager wakes up when a message arrives. It is started with `--setting-sources project,local` instead of `--restricted`, which in current Claude Code also removed the session's inbox socket, so a pushed message never reached it. Its instructions now load from its folder's `CLAUDE.md`, and so does the admin's user-level `CLAUDE.md` (user settings, hooks and plugins still do not). Upgrade: after `kaptyorka update` on the hub machine, close the running manager once (Close on its Machines row, or `close_worker(\"manager\")`) so the hub starts it again with the new flags." Version to 0.6.2 in `pyproject.toml` and `__init__.py`.

- [ ] **Step 4: Run the whole suite**

Run: `python3 -m pytest tests -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add README.md tests/test_readme.py CHANGELOG.md pyproject.toml src/kaptyorka/__init__.py tests/test_cli.py
git commit -m "docs(manager): 0.6.2, launch flags and wake"
```

### Release (after kursi's PASS and green CI)

Tag `v0.6.2` on main, push the tag, watch `publish.yml`. On the hub machine: `kaptyorka update`, then close the running 0.6.1 manager (it was started outside the bridge's process group, survives the update, and `keep_manager` never relaunches while a manager is live): Close on its Machines row or `close_worker("manager")`; then `kaptyorka start --manager` if it was switched off. Check: the manager shows up with the new flags (`systemctl --user status kaptyorka-worker-manager.scope` shows `--setting-sources`), and a page message to `manager` gets an answer within a minute.
