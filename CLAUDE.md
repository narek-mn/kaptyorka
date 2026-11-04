# kaptyorka

- Tests: in a venv (`python3 -m venv .venv && . .venv/bin/activate && pip install -e . pytest`), run `python3 -m pytest tests -q`.
- Python 3.10 is the floor: no newer syntax or stdlib.
- Never call real agent tools in tests, except tests marked `-m slow`.
- `KAPTYORKA_TEST_IDENTITY` (JSON of an `Identity`: key, machine, tool, project) overrides session detection in `identity.identify()`. It exists only so tests can run the `kaptyorka mcp` shim outside an agent tool; never set it elsewhere.
- No push and no PyPI release without approval.
