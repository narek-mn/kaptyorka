# Changelog

## 0.3.0

- Workers: `work` starts a session for you on another machine (Claude Code, Codex or OpenCode); `workers` lists yours; `adopt` takes over an unattended worker; `close_worker` ends one (17 agent tools in all). The boss gets an attach line. A busy folder gets a worktree on branch `work/<worker-name>`. Nothing is bypassed: workers use the account's own permission settings, and the boss is told when a Claude Code worker waits for one.
- A worker whose boss is offline 2 minutes is `unattended`, after 24 hours `abandoned`; nothing kills it.
- Machines tab on the page: machines, workers, warnings, and an auto-update switch for each account.
- New commands: `kaptyorka doctor [--fix]`, `kaptyorka fleet add <user@host>... --person <name> [--key-from <file>]`, `kaptyorka fleet list`.
- Auto-update: the bridge updates when the hub is newer; on by default for members, off for the admin.
- The hub's heartbeat answer is now JSON (version, auto_update, projects, you, shared) instead of an empty answer. We suggest updating the hub first, then the machines. An older bridge keeps working with the new hub; it just cannot use the new parts until it updates.
- The old shared token cannot start, adopt or close workers. The hub also refuses these for a worker on an account the admin marked shared. This stops mistakes, not a hostile worker: that worker runs as the account and can read its token and keys. Share only an account set up for this. Proper worker identity comes in a later version.
- New files in `~/.kaptyorka`: `workers/<name>.log`, `folders.json`, `launched.json`.

## 0.2.0

- People and devices: the admin is created on first start; `kaptyorka invite <person>` makes a one-use, 7-day invite link; `kaptyorka join <invite-link>` joins a machine as that person with its own token and signing secret. `kaptyorka people` lists people. The invite link in a browser shows the join command and opens the page as that person.
- Roles: hub admin and member; project read, write, manage; machine use. New commands `project add`, `grant`, `revoke`. Folders not registered as projects show as `(private)`.
- Groups and doors: `send` stays within your own sessions (admin: anyone); `knock`, `answer` and `door_close` open and close doors between people; `send("all")` reaches the sessions you may message.
- Access requests: `request_access` and an Access tab for the admin; pending and denied requests show in the start brief.
- Attachments: `send(..., file=<path>)` up to 5 MB, `fetch_file` saves to `~/.kaptyorka/inbox/`, images show on the page.
- Notes: team notes are open to all; project notes need read or write. The tools take `project=`.
- Log of who did what (admin tab), retention of messages and files (180 days by default, set on the page), secrets check on messages and notes.
- Page: login with a device token or an invite link; People, Access and Log tabs for the admin.
- Six new agent tools: `projects`, `knock`, `answer`, `door_close`, `request_access`, `fetch_file` (13 in all).
- The admin's own token: the first `kaptyorka start` makes a device `hub` for the admin, joins the hub machine with it and prints only this machine's join line, the page link with that token and how to invite. `join` prints the page link; the page's lock screen takes a pasted token. `kaptyorka machine owner <host> <person>` (or the Access tab) names a machine's owner; a machine has none until then.
- Safety: secrets check covers more key shapes and text attachments; the `kaptyorka` tool will not attach files from `~/.kaptyorka` or `~/.ssh`; names and texts from machines are kept on one line.
- Upgrading from 0.1: nothing to do on teammates' machines. The old shared token keeps working as a "legacy" device until the admin switches it off on the People tab (with their own admin token: once the admin has one, the shared token is refused for admin work and cannot switch itself off); the admin assigns each old machine to a person, and machines on 0.2 then get their own token. Notes from 0.1 whose topic equals a project folder become that project's notes.

## 0.1.9

- `join` names the machine `user@host` by default, so one person's machines stay apart; `start` notes that `--name` picks another name.

## 0.1.8

- Page redesign: Team has worded columns and a colour legend, Messages reads like a chat, Notes lists topics on the left and opens one on the right.
- Send messages and write notes from the page.
- `kaptyorka update` ignores pip's cache, so it always gets the newest release.

## 0.1.7

- Fix: a message held for an offline session goes only to that session, or to a new session that takes its name, never to another session in the same folder.

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
