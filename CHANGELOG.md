# Changelog

## 0.1.6

- A restarted or resumed Claude Code session keeps its name (sessions are now keyed by Claude's session id, not the process id). A closed session frees its name at once.

## 0.1.5

- New `kaptyorka leave`: takes this machine off the hub (the bridge and what `join` set up in your agent tools) and keeps kaptyorka installed. `kaptyorka join` brings it back.

## 0.1.4

- OpenCode 2 support. `join` writes the plugin in OpenCode 2's format, and does not add the `--port 0` alias (it removes an old one, since OpenCode 2 has no such flag). The MCP server takes each session from the tool call, so many sessions sharing one OpenCode 2 service each get their own identity. The bridge lists sessions and delivers messages through OpenCode 2's background service. OpenCode 1 works as before.

## 0.1.3

- `kaptyorka start` also prints the full `kaptyorka join` command, ready to copy and paste on each machine.

## 0.1.2

- Tests now pass on macOS (long socket paths, socket shutdown, symlinked temp folder). CI jobs time out after 15 minutes instead of hanging.
- Fix: pushing to a Claude Code session works when its socket path is longer than macOS allows.

## 0.1.1

- First release on PyPI. 0.1.0 never reached PyPI: its package had no description.
- Fix: package description for PyPI (README attached as long description).

## 0.1.0

First release.

- Five commands: `start`, `join`, `stop`, `update`, `remove`.
- `kaptyorka start` runs the hub, with a live web page: Team, Messages, Notes.
- `kaptyorka join` sets up Claude Code, OpenCode, Codex and Cursor, and starts the bridge.
- `kaptyorka stop` keeps both off after a reboot; `update` upgrades with pipx and restarts; `remove` undoes everything.
- Seven agent tools: who, send, inbox, status, notes_list, notes_read, notes_write.
- Push messages for Claude Code, OpenCode and Codex. Cursor is pull only.
