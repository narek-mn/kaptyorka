# kubrik

- Tests: in a venv (`python3 -m venv .venv && . .venv/bin/activate && pip install -e . pytest`), run `python3 -m pytest tests -q`.
- Python 3.10 is the floor: no newer syntax or stdlib.
- Never call real agent tools in tests, except tests marked `-m slow`.
- No push and no PyPI release without approval.
