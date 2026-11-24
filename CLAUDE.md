# kaptyorka

- Tests: in a venv (`python3 -m venv .venv && . .venv/bin/activate && pip install -e . pytest pytest-xdist`), run `python3 -m pytest tests -q -n auto --dist worksteal` (about 40 s on 8 cores; one file: `python3 -m pytest tests/test_x.py -q`). Before a commit, `python3 tests/changed.py` runs only the tests for what changed against origin/main.
- Python 3.10 is the floor: no newer syntax or stdlib.
- Never call real agent tools in tests, except tests marked `-m slow`.
- `KAPTYORKA_TEST_IDENTITY` (JSON of an `Identity`: key, machine, tool, project) overrides session detection in `identity.identify()`. It exists only so tests can run the `kaptyorka mcp` shim outside an agent tool; never set it elsewhere.
- No push and no PyPI release without approval.
- `KAPTYORKA_SKIP_SERVICE=1` makes `kaptyorka join` skip installing the bridge service. Tests only; never set it elsewhere.
- No team names (people, projects, machines) in code, tests, fixtures or screenshots; use alice, bob, proj-a, box-1.
- `Store.lock` is not reentrant: never call a locking store method from inside a `with self.lock` block.
- Never run `kaptyorka` commands against the real machine (`~/.kaptyorka`, your Claude config, services). Try them only in tests or with `KAPTYORKA_HOME=<temp folder>` on a free port; never `start`, `join`, `leave`, `stop`, `update`, `remove` for real.
- Before a release that touches the manager (launch, bridge reports, keep-alive), run `python3 tests/soak_manager.py` (about 18 minutes; sandboxed, real `claude`) and keep its `result.txt`.
- Before a release that changes the manager's rules (`manager.py` texts), its tools or its launch, run `python3 tests/eval_manager.py --out DIR` in the venv (sandboxed like the soak, real `claude`; about 15 minutes) and keep its `result.txt`: scripted situations scored by checks on what the manager did. `--only CASE`, `--repeat N` for a pass rate. Its checkers have fast tests in `tests/test_eval_manager.py`; a new manager rule gets a case there. Never next to the soak or the slow suite.
- Slow page tests (`tests/test_page*_slow.py`, headless Chrome) may run on 2 workers (`-n 2`); the other slow tests always one at a time (no `-n`).
- Never run `tests/soak_manager.py` and the slow suite (`-m slow`) at the same time: both start real `claude` sessions and their timers fail each other. A failed slow test keeps its session transcripts in `~/.cache/kaptyorka-test-failures/` (or `$KAPTYORKA_TEST_KEEP`); the failure output names the folder.
- Claude Code internals: `src/kaptyorka/compat.py` lists every undocumented thing kaptyorka reads, each with a probe. When Claude Code updates (the hub tells the admins once per version), run `python3 -m pytest -m slow tests/test_claude_compat_slow.py` (about 2 minutes, real `claude`; not beside the slow suite or the soak). Move `CLAUDE_CODE_TESTED` in `adapters/claude.py` to the version it prints only when it passes; when it fails, its message names each broken internal: fix that first. A new internal read anywhere gets its `Dep` and probe in compat.py in the same change.
