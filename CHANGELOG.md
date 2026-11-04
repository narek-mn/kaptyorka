# Changelog

## 0.1.0

First release.

- Five commands: `start`, `join`, `stop`, `update`, `remove`.
- `kaptyorka start` runs the hub, with a live web page: Team, Messages, Notes.
- `kaptyorka join` sets up Claude Code, OpenCode, Codex and Cursor, and starts the bridge.
- `kaptyorka stop` keeps both off after a reboot; `update` upgrades with pipx and restarts; `remove` undoes everything.
- Seven agent tools: who, send, inbox, status, notes_list, notes_read, notes_write.
- Push messages for Claude Code, OpenCode and Codex. Cursor is pull only.
