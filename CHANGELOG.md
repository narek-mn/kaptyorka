# Changelog

## 0.8.1

- `kaptyorka start` on a machine joined to another team's hub now refuses even when that machine once ran a hub of its own, and `doctor` no longer tells it to start one.

## 0.8.0

- The manager sleeps when idle and wakes on a message instead of restarting every 71 minutes. Claude Code stops a manager that has been idle for about an hour; the hub now leaves it stopped until a message or a work request comes for it, then starts it and hands it what was waiting. Settings and Machines show it as asleep. The hub's own notices to it (the weekly review, abandoned workers, reminders) wait while it sleeps and go out when it wakes.

- Commands say when there is nothing to do, and no longer restart a running hub. `kaptyorka start` on a hub that already runs with the same settings prints `the hub is already running` and the page link and restarts nothing; `stop` says `already stopped`; `update` says `already the latest`; `leave` on a machine that is not joined says so; `join <invite link>` on a machine already joined to that hub does nothing. `start` on a machine that only joined another hub says so instead of starting a second hub, and `start --port` moves this machine along. Shorter, plainer output for `start`, `stop`, `leave`, `invite`, `who`, `fleet list`.
- `join` no longer adds Claude Code hooks twice: a hook line written by hand (for example `[ -x ~/.local/bin/kaptyorka ] && ... hook claude-code start || true`) counts as present and is left alone, and our own copy next to it is removed. A plain `join` that changes nothing prints `already joined to <hub>; all set` and does not restart the bridge or re-register the MCP server.
- `doctor` says what to do that works: not joined, a hub that does not answer, a refused token, a stopped bridge. It checks only the tools this machine joined with.

## 0.7.1

- Messages to the manager now reach it at once: the bridge that started a session may deliver to it, even when the session is registered under its own name (the manager is). Before, the manager only saw messages on its next turn, which an idle manager never had.
- `kaptyorka start` turns the bridge back on after `kaptyorka stop`; before, only the hub came back, so the manager could not start.

## 0.7.0

- New `kaptyorka login-copy user@host` copies this machine's Claude Code login to the same user on another machine of yours over ssh, and checks that it is logged in there. The login never goes through the hub. It refuses when Claude Code is not logged in here, is not installed there, or is already logged in there (`--force` replaces that login). Claude Code only, and Linux to Linux only.

## 0.6.2

- The manager wakes up when a message arrives. It is started with `--setting-sources project,local` instead of `--restricted`, which in current Claude Code also removed the session's inbox socket, so a pushed message never reached it. Its instructions now load from its folder's `CLAUDE.md`, and so does the admin's user-level `CLAUDE.md` (user settings, hooks and plugins still do not). Upgrade: after `kaptyorka update` on the hub machine, close the running manager once (Close on its Machines row, or `close_worker("manager")`) so the hub starts it again with the new flags.

## 0.6.1

- The manager no longer starts twice in a row. After a failed start the hub waits two minutes before it tries again, and it does not start the manager while Claude Code is not logged in on the hub machine. The Settings tab says whether the manager session is starting, running, off, or on but unable to start, with the reason and what its last start failed on.
- The Machines tab shows which agent tools are logged in on each machine, as each tool's own status command reports it. A tool that is not logged in gets a red dot and a line with its login command (`claude auth login`, `codex login`, `opencode auth login`), and the admin gets a warning. When a started worker never shows up, its error names the likely cause: the tool is not logged in, or it does not trust the folder yet.
- Clean-up: on Linux, a worker's leftover system scope (`kaptyorka-worker-<name>.scope`) is stopped before a new start, so a session that ended badly no longer blocks the next one.
- Sessions without an inbox socket (Claude Code's restricted sessions, among them the manager) now count as present; messages reach them when they next read the hub.

## 0.6.0

- The manager: one Claude Code session the hub starts on the hub machine through the admin's own bridge, and keeps alive (at most twice an hour). It is a worker named `manager`, under a `manager` person and device. Attach with `claude attach manager` on the hub machine.
- Busy machines: when `work` finds another person's session in the folder, the hub saves a work request and asks the manager. It asks the holder, then calls the new tool `work_decide(request_id, machine|wait, reason)`. The hub checks the asker's use of the machine and write role, and a free folder or a worktree. Every decision is logged with its reason; a request expires after an hour.
- New tool `log(limit)` for the admin and the manager. 25 agent tools in all.
- Manager permissions: messages to and from the manager need no door (page replies too). It may knock. It cannot answer others' knocks, open doors, grant, edit rules or decide proposals.
- The hub tells the manager about an abandoned worker, a shared machine whose bridge is offline, workers that failed to start twice, a proposal or access request waiting a day, and a weekly review.
- Team tab: a `manager` badge on its session. Machines tab: `manager` where the boss would be.
- New flags: `kaptyorka start --no-manager` and `--manager`, kept in `hub.json`. `close_worker("manager")` by the admin switches it off until `start --manager`.
- New files in `~/.kaptyorka`: `manager/` (its folder and `CLAUDE.md`) and `manager-config.json`, on the hub machine.
- Upgrade: nothing to join again. The manager starts by itself after the hub updates. The first launch on a hub machine can fail until Claude Code trusts the folder: run `claude` once in `~/.kaptyorka/manager` and accept the prompt.
- Team rules and skills: if `~/.claude/skills` is a link into a git repo, add `team-*/` to that repo's `.gitignore`.
- Remove people and projects safely. `kaptyorka people remove <person>` (page: **Remove** on the People row, with a question first) stops their devices, closes the workers they started, deletes their open invites, closes their doors, drops their access, requests and open work requests, and clears the machines they owned. A worker that may still run on a machine that took no close order is named. Messages, notes and the log stay, and the name shows as `<name> (removed)`. Removing a project archives it: roles and open requests go, notes are kept and the admin reads them under **Archived**; **Register again** or `kaptyorka project add` brings it back.
- Change a role: `kaptyorka people role <person> member|manager`, or the role list on the People tab. Edit a project's description: `kaptyorka project edit <name> --description <text>`, or **Edit** on the Access tab.
- Invites: they show an expiry ("works for 7 days"). `kaptyorka invites` lists the open ones and `kaptyorka invites --cancel <code>` stops one; the People tab has **Open invites** with **Cancel**. **Add person** shows the new link at once.
- Members see **My access** on the Team tab, and can **Ask for** read or write access to a project or use of a machine. New route `POST /api/requests` and `GET /api/me/access`. The admin approves with a chosen role (read or write) or denies with a reason, which is sent to the person's page and sessions. **Recent decisions** lists the last ten.
- The page: inline questions instead of pop-ups, empty states with a next step, the grant form as a sentence with a hint per role, phone layouts for every tab (on a phone the tabs sit in a bar at the bottom with their names; past five, **More** lists the rest), and plain sentences in the Log ("alice made an invite link for bob"). The hint for `manage` now says it is the same as `write` for now; only the admin gives access.
- Command line: every command has a one-line help in plain words, and `invites`, `rules` and `fleet` now show in `kaptyorka --help`. A missing argument prints the usage and one sentence. When something goes wrong, each command prints one plain sentence and exits with 1: not joined, "cannot reach the hub at <url>", the hub's own reason for a refusal, or a short line for a 401, 403, 404, 409 or 5xx with no reason. `who` prints its errors to stderr like the others. `join` words a spent, cancelled or expired invite plainly.
- New columns, added by themselves when the hub starts: `people.removed_at` and `projects.archived_at`. Nothing to run on upgrade.
- The page, second pass: attach a file to a message (up to 5 MB); the admin opens and closes doors, with one hint line the first time a door or knock shows; delete a note and **Undo** for 8 seconds (the admin, a project writer, or for a team note the person who started it); **Restore this version** for an old note or rules version; **History** on the team rules; **Propose a rule** for everyone. New routes `DELETE /api/notes`, `POST /api/notes/undo`, `POST /api/proposals`, `GET /api/log/kinds`, and `kind` and `before` on `GET /api/log`.
- Log tab: choose a kind (People, Access, Projects, Notes, Machines, Messages, Rules, Workers, Sign-ins, Other) as well as a person, and **Load more** for older rows. Changing the message keeping time and a machine's owner now read as sentences, and so do doors opened and closed, knocks and their answers, rule proposals and their decisions, and the shared token switched on, off or used. Switching a machine's auto-update on, off or back to the default is now logged, with the machine's owner ("alice turned auto-update off for box-1 (bob)").
- A member's message with a file to the manager session needs no door, like the same message without a file.
- New **Settings** tab for the admin (last in the bar; under **More** on a phone): how long messages are kept, whether the manager session is on (read only; `GET /api/settings` now also returns `manager_enabled`), and the old shared token panel, now called **Machines that joined before personal tokens** (moved from People). The Log tab no longer holds the keeping form.
- An upload over the limit (`POST /api/files`, used by the page) is refused with sizes in MB ("file too large: 6.0 MB, limit 5.0 MB"); the page writes sizes in the same units (1 MB = 1,000,000 bytes).

## 0.5.0

- Team rules and skills: the admin uploads a folder with `kaptyorka rules push <dir>` (`RULES.md` and `skills/*.md`; admin device only). The rules text is in every start brief, first, and is cut first when the brief is over 6,000 characters. Skills are written to `~/.claude/skills/team-<name>/` by the bridge, and removed by `leave`. Claude Code reads that folder, OpenCode and Cursor read it as a compatibility path; Codex gets the rules text only.
- New agent tools `rules()`, `propose_rule(text, evidence)` and `conflict(rule_a, rule_b)`. Proposals: the admin accepts (a new version of the team rules) or rejects with a reason sent to the proposer's session, on the new Rules tab. 23 agent tools in all.
- Team tab: three marks per session's tool. R (the start-brief hook is installed), H (all kaptyorka hooks) and S (the team skills folder is current); a dash means unknown or not applicable.
- Limits: rules and skills 20,000 characters each, 50 skills, skill names 1 to 48 lowercase letters, digits and single hyphens; proposals 2,000 characters, evidence 4,000, 20 open per person; conflicts 1,000 characters per rule.
- The proposals list (`GET /api/proposals`) no longer sends the session key or the person id to the browser.
- New file in `~/.kaptyorka`: `skills.json`, the team skill names and version the bridge last wrote.
- Upgrade: no need to run `kaptyorka join` again for this part. Skills appear by themselves once the bridge updates.

## 0.4.0

- Project state notes: each registered project has one, with five headings in this order: What it is, Where it stands, Decisions, Open problems, How to run and test. New agent tools `state(project)`, `state_write(project, text)` and `decide(project, text)`, which adds a dated line to the project's `decisions` note (`project=""` for team decisions). 20 agent tools in all.
- Start brief: five parts in order (team rules, for later; who you are and who is online; the projects you may read; your project's state note, its git activity of the last 7 days and the branches with open sessions; unread messages), at most 6,000 characters, kept 10 seconds per session.
- Git activity: the bridge reports each session folder's branch, last commit and changes; the hub keeps it 30 days.
- The done check: a session that did work in a project it may write (a new commit, its folder going from changed to clean, or a status with the word "done") and did not write the state note since is asked once, at stop, to call `state_write`. A read-only session is never asked; `status("done: no state change")` clears it. Claude Code and Codex block the stop once; Cursor gets the reason as the next follow-up message; OpenCode gets it as a reminder on its next prompt.
- New stop hook for Claude Code and Codex: on a machine joined before, run `kaptyorka join` again (or `kaptyorka doctor --fix`). Codex users approve the new hook once with `/hooks`.
- Page: the Notes tab shows the project's state and recent git activity when a project's note is open.
- New commands: `kaptyorka backup <user@host:path>` (encrypted with gpg when installed, passphrase in `~/.kaptyorka/backup-pass`, keep a copy; a plain tar otherwise, and it says so), `backup --nightly` (every day at 03:00) and `kaptyorka restore <file>` (stop the hub first; the old data stays as `*.before-restore`).
- New file in `~/.kaptyorka`: `backup-pass`.

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
