# Login Copy Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `kaptyorka login-copy user@host` copies this machine's Claude Code login to the same tool on another machine the person owns, over ssh, and checks that it is logged in there.

**Architecture:** One new module `src/kaptyorka/login_copy.py` with one function, wired as a CLI command. It reuses `fleet._ssh` and `fleet.PATH_FIX`. The secret travels only on the stdin of one ssh command, never in argv, never through the hub, never in output. Login state on both sides comes from `claude auth status` JSON (`loggedIn`), as the bridge already reads it.

**Tech Stack:** Python 3.10, stdlib `subprocess`, `json`, `shlex`. No new dependencies.

**Spec:** Narek's request (2026-11-07, confirmed 2026-11-10): a command on his laptop that copies his own Claude login to another machine under his user, over ssh, never via the hub; Claude Code only in this first version; private file mode on the target; one plain line saying what happened. Recorded in project memory `kaptyorka-login-copy.md`. A login wrapper around each tool's interactive login was rejected.

Facts it relies on (checked on Claude Code 2.1.322, Linux): the login lives in `${CLAUDE_CONFIG_DIR:-~/.claude}/.credentials.json`, mode 0600; copying that one file to the DGX made `claude auth status` there report `loggedIn: true` (done by hand on 2026-11-07). `claude auth status` prints JSON on stdout and exits 1 when logged out. On macOS Claude Code keeps the login in the keychain, not in that file.

## Global Constraints

- Python 3.10 floor; no new dependencies; `python3 -m pytest tests -q` green.
- Tests never call real `ssh` or `claude`: every external call goes through the injected `run`.
- The credential text never appears in argv, in printed output, in exceptions or in logs.
- No team names in code, tests or docs (alice, box-1).
- Commit trailers as in `git log -1`.
- Version 0.7.0 in `pyproject.toml`, `src/kaptyorka/__init__.py`, `tests/test_cli.py`; a `## 0.7.0` CHANGELOG section.

## Review Focus

1. The target already logged in: never overwrite without `--force` (it may be logged in on purpose, for example to another account). Test: `test_refuses_a_logged_in_target_without_force`.
2. The secret in a failure path: a failing copy step must not echo stdin or the file. Test: `test_secret_never_in_argv_or_output` checks every recorded argv and every returned line.
3. `claude` missing on the target (exit 127 from the remote shell): stop before copying with a clear line. Test: `test_target_without_claude`.
4. A local login that has already expired: refuse to copy a dead login. Test: `test_refuses_when_not_logged_in_here`.
5. A target string that is an ssh option (`-oProxyCommand=...`) or holds spaces: refused before any ssh. Test: `test_bad_target`.

---

### Task 1: `login_copy.copy`

**Files:**
- Create: `src/kaptyorka/login_copy.py`
- Test: `tests/test_login_copy.py`

**Interfaces:**
- Consumes: `fleet._ssh(target, command) -> List[str]`, `fleet.PATH_FIX`.
- Produces: `copy(target: str, force: bool = False, run=subprocess.run, config_dir: Optional[Path] = None, platform: str = sys.platform) -> Tuple[bool, str]`: (ok, one result line). `config_dir` defaults to `Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")`.

Order of steps, each through `run`, stopping at the first that fails:

1. Target check: refuse a target that starts with `-` or contains whitespace (same rule as `fleet.add`): `"{target}: not a user@host"`.
2. Local file: `config_dir / ".credentials.json"` must be a regular file. Missing on darwin: `"this Mac keeps the Claude login in the keychain; login-copy works from Linux only for now"`. Missing elsewhere: `"no Claude Code login on this machine; run claude auth login first"`.
3. Local state: `run(["claude", "auth", "status"], capture_output=True, text=True, timeout=10)`; JSON `loggedIn` must be `True`, else `"Claude Code is not logged in on this machine; run claude auth login first"`. `FileNotFoundError`, `OSError` and `subprocess.TimeoutExpired` count as not logged in.
4. Remote state: `run(fleet._ssh(target, PATH_FIX + "claude auth status"), capture_output=True, text=True, timeout=30)`. Exit 127: `"{target}: Claude Code is not installed there"`. JSON `loggedIn is True` and not `force`: `"{target}: Claude Code is already logged in there; add --force to replace that login"`. Exit 255 without JSON: `"{target}: could not reach it over ssh: {first stderr line}"`. Any other exit without JSON: `"{target}: could not read Claude Code's login state there: {first stderr line}"`. Both stop before the copy, with or without `force`. On all three ssh calls (steps 4, 5, 6), `subprocess.TimeoutExpired` gives `"{target}: could not reach it over ssh: timed out"` and `OSError` (no ssh installed) gives `"{target}: could not reach it over ssh: {first line of the error}"`.
5. Copy: `run(fleet._ssh(target, REMOTE_WRITE), input=<file text>, capture_output=True, text=True, timeout=30)` with
   `REMOTE_WRITE = 'umask 077; d="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"; t="$d/.credentials.json.kaptyorka"; mkdir -p "$d" && cat > "$t" && chmod 600 "$t" && mv -f "$t" "$d/.credentials.json" || { rm -f "$t"; exit 1; }'` (a failed write never leaves part of the login behind).
   Non-zero exit: `"{target}: copy failed: {first stderr line}"`.
6. Verify: the step-4 command again. `loggedIn is True`: `(True, "{target}: Claude Code is logged in there")`. Otherwise: `"{target}: copied, but Claude Code there still says it is not logged in; run claude auth login there"`.

The returned line never contains the file text; stderr lines are passed through `fleet._first_line`.

- [ ] **Step 1: Write the tests**

`tests/test_login_copy.py` with a fake `run` that records `(argv, kwargs)` and answers by matching the argv (local `claude auth status`; ssh status; ssh write). Credentials file written to `tmp_path / ".credentials.json"` with text `SECRET-TOKEN-TEXT`. Tests:

- `test_copies_and_verifies`: remote first logged out, then logged in after the write. Returns `(True, "alice@box-1: Claude Code is logged in there")`; the write call's `input` equals the file text; its argv is `fleet._ssh("alice@box-1", REMOTE_WRITE)`.
- `test_secret_never_in_argv_or_output`: run `copy` through the success path and through a failing write (exit 1, stderr `"disk full"`); `SECRET-TOKEN-TEXT` is in no recorded argv element and in no returned line.
- `test_refuses_a_logged_in_target_without_force`: remote logged in; returns `(False, "...already logged in there; add --force to replace that login")`, no write call. With `force=True` the write happens.
- `test_refuses_when_not_logged_in_here`: local JSON `loggedIn: false` (exit 1); no ssh call at all.
- `test_no_local_file`: missing file on `platform="linux"` and on `platform="darwin"` gives the two texts above; no call to `run`.
- `test_target_without_claude`: remote status exit 127; no write.
- `test_unreachable_target`: remote status exit 255, stderr `"ssh: connect to host box-1 port 22: No route to host"`; line names that first stderr line; no write.
- `test_ssh_timeout`: the fake `run` raises `subprocess.TimeoutExpired` on the remote status call, and in a second case on the write call; both return `(False, "alice@box-1: could not reach it over ssh: timed out")` and the secret is in no line.
- `test_unreadable_remote_state`: remote status exit 1 with non-JSON stdout and stderr `"bash: line 1: claude: Permission denied"`; the "could not read Claude Code's login state there" line, no write, also with `force=True`.
- `test_bad_target`: `"-oProxyCommand=x"` and `"alice@box 1"`; no call to `run`.
- `test_copied_but_still_logged_out`: verify step says not logged in; returns the "copied, but" line with `False`.

- [ ] **Step 2: Run them to see them fail**

Run: `python3 -m pytest tests/test_login_copy.py -q`
Expected: FAIL (module missing).

- [ ] **Step 3: Implement `copy` in `src/kaptyorka/login_copy.py`** as specified above; a small `_logged_in(result) -> Optional[bool]` helper parses stdout JSON on any exit code.

- [ ] **Step 4: Run them to see them pass**

Run: `python3 -m pytest tests/test_login_copy.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/kaptyorka/login_copy.py tests/test_login_copy.py
git commit -m "feat: login-copy copies this machine's Claude Code login over ssh"
```

### Task 2: Command, docs, version

**Files:**
- Modify: `src/kaptyorka/cli.py` (a `_login_copy(args)` handler next to `_fleet_add` at line 607, the parser right after `fleet`, and `login-copy` after `fleet` in the hand-written `metavar` command list at lines 681-682)
- Modify: `tests/test_cli.py` (`test_subcommands_listed` at lines 42-52: `"login-copy"` right after `"fleet"` in the expected names; the new command tests; `0.7.0` at line 39)
- Modify: `README.md` (section "Doctor, fleet and auto-update" at line 382: one short paragraph and example; the Commands table, which starts at line 431: one row after the `fleet` rows at 451-452; Repository layout: `login-copy` in the `cli.py` command list at line 467, and `login_copy.py` in the module list, next to `fleet.py` near line 479)
- Modify: `CHANGELOG.md`, `pyproject.toml`, `src/kaptyorka/__init__.py`

**Interfaces:**
- Consumes: `login_copy.copy(target, force, run=subprocess.run)`.
- Produces: `kaptyorka login-copy <user@host> [--force]`; prints the result line; exit 0 when ok, 1 otherwise. Parser help: `"copy this machine's Claude Code login to another machine of yours over ssh"`; `--force` help: `"replace a login that is already there"`.

- [ ] **Step 1: Tests**

In `tests/test_cli.py`: `kaptyorka login-copy alice@box-1` with `login_copy.copy` monkeypatched to return `(True, "alice@box-1: Claude Code is logged in there")` prints that line and exits 0; returning `(False, "x")` exits 1; `--force` is passed through. Version assertion becomes `"0.7.0"`.

- [ ] **Step 2: Run them to see them fail**

Run: `python3 -m pytest tests/test_cli.py -q`
Expected: FAIL.

- [ ] **Step 3: Implement**

CLI wiring as in Interfaces. README paragraph (plain words): `kaptyorka login-copy alice@box-1` copies your Claude Code login from this machine to the same user on that machine, over ssh, so Claude Code there is logged in as you without a browser. The login goes straight from this machine to that one, never through the hub, and is saved there readable only by that user. It refuses when Claude Code is not logged in here, is not installed there, or is already logged in there (`--force` replaces that login). Use it only for machines and accounts that are yours. Claude Code only for now, and both machines must be Linux: a Mac keeps the Claude login in its keychain, so it cannot be copied from one, and a file copied to one would not be used. Commands table row: `| kaptyorka login-copy <user@host> | Copy your Claude Code login to another machine of yours over ssh (--force replaces one already there) |` (no "Admin:" prefix: anyone can use it). CHANGELOG `## 0.7.0`: one bullet with the same content in a sentence or two. Versions to 0.7.0.

- [ ] **Step 4: Run the whole suite**

Run: `python3 -m pytest tests -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/kaptyorka/cli.py tests/test_cli.py README.md CHANGELOG.md pyproject.toml src/kaptyorka/__init__.py
git commit -m "docs: login-copy command, 0.7.0"
```

### Release

Only with Narek's yes to 0.7.0, kursi's PASS and green CI: tag `v0.7.0`, push the tag, watch `publish.yml`.

Before tagging, check that a copied login does not log the other machine out when Claude refreshes its token: `claude auth status` on the laptop and on the hub machine (both share the login copied on 2026-11-07, re-done after Narek's password change). If one was logged out by the other's refresh, the README must say so plainly, or the feature needs a rethink before PyPI. This does not block the build or the push to main.
