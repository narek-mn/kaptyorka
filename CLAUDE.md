# kaptyorka

- Tests: in a venv (`python3 -m venv .venv && . .venv/bin/activate && pip install -e . pytest`), run `python3 -m pytest tests -q`.
- Python 3.10 is the floor: no newer syntax or stdlib.
- Never call real agent tools in tests, except tests marked `-m slow`.
- `KAPTYORKA_TEST_IDENTITY` (JSON of an `Identity`: key, machine, tool, project) overrides session detection in `identity.identify()`. It exists only so tests can run the `kaptyorka mcp` shim outside an agent tool; never set it elsewhere.
- No push and no PyPI release without approval.
- `KAPTYORKA_SKIP_SERVICE=1` makes `kaptyorka join` skip installing the bridge service. Tests only; never set it elsewhere.
- No team names (people, projects, machines) in code, tests, fixtures or screenshots; use alice, bob, proj-a, box-1.
- `Store.lock` is not reentrant: never call a locking store method from inside a `with self.lock` block.
