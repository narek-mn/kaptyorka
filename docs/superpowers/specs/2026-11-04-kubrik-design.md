# Kubrik design

Date: 2026-11-04. Status: approved by Narek on 2026-11-04.
Name: "kubrik" (Narek, 2026-11-04).

## Intent

One job often spans several machines (PC, Jetsons, DGX) and several
coding agents, each on a different personal account. Today Narek is the
messenger between them: tell one agent, carry its answer to the next.
Kubrik lets every coding-agent session in the team see the others,
message them, and share notes that outlive the session, so Narek gives an
order once and the agents sort out the rest between themselves.

Example that drove it (Narek, 2026-11-04): a console change in murmur
("move robot to selected point") also needs a backend change in Isaac Sim
on the DGX. The console agent should tell the DGX agent, and the two
should agree the API without Narek relaying.

## Goals

- Any session can list who is online: machine, tool, project, name.
- Any session can send a message to one session or to all, and a running
  session receives it and acts, without its human typing anything,
  wherever the tool allows it.
- Shared notes that survive sessions: a new session on any machine reads
  the team board and knows the state of the other projects.
- Works for Claude Code, Codex CLI, OpenCode and Cursor (Narek:
  all four in version one).
- Setup is one instruction to any agent: "install and use kubrik".
  The agent installs it and configures its own tool. The human at most
  restarts an IDE.
- Runs on the team LAN/VPN only. Hub on the DGX (always on).

## Non-goals

- Not a chat for people. The web page is for watching, not for typing.
- No code sync or shared repo. Code stays in each project's git.
- No public internet exposure, no user accounts, no per-user login.
- No guardrails of our own on what a receiving agent may do. Narek
  (2026-11-04): a message from another session is handled exactly like
  the built-in cross-session messaging handles one; the receiving
  session acts under its own permission settings, pushes when told to
  push, and whoever connected the agents owns the result.

## Anti-goals (would make it a failure even if it works)

- Setup that needs a human to follow a list of steps on each machine.
- A message silently lost because the target session was closed or busy.
- Something that stops working on every Claude Code update without
  anyone noticing.

## Facts the design rests on (researched 2026-11-04, official docs)

- Claude Code's built-in cross-session messaging is one account only.
  No org or team mode exists. Auto memory is machine-local. So nothing
  official does this.
- Push doors, per tool:
  - Claude Code: each session has a local Unix socket (path in
    `~/.claude/sessions/<pid>.json`, token in the `.key` file beside
    it). A newline-ended JSON line `{"type":"user","message":{"role":
    "user","content":"..."}}` after an optional `{"type":"auth",
    "token":...}` line delivers a message; an idle session wakes and
    acts. Verified by a spike on this PC with 2.1.318. The message JSON
    shape is only in the binary, not the docs. Alternative official
    door: channels, but a custom channel needs a dev flag and a
    full-screen warning on every launch. Rejected for daily use.
  - OpenCode: every `opencode` run also starts a local HTTP server
    (`--port` to fix the port). `POST /session/:id/prompt_async` sends
    a message. `GET /session` lists sessions. Not verified by running.
  - Codex CLI: `codex app-server` (JSON-RPC, started by
    `codex remote-control start`) has `turn/start` on a thread, which
    starts a new turn on an idle thread, and `thread/list`. Not verified.
  - Cursor: no door into a running local chat. Hooks exist: a `stop`
    hook can return a follow-up message (up to 5 loops), `postToolUse`
    can add context. Pull-only.
- All four tools take a remote (HTTP) MCP server from a user-level
  config file with a bearer header: `~/.claude.json` (or `claude mcp
  add`), `~/.codex/config.toml`, `~/.config/opencode/opencode.json`,
  `~/.cursor/mcp.json`.
- Hooks at session start / prompt submit exist in Claude Code, Codex and
  Cursor; OpenCode has plugins with events.

## Approaches considered

All three share a hub and a bridge; they differ in how a message gets
into a running session.

1. Deliver through the session's own inbox socket (Claude Code), the
   local server (OpenCode, Codex), hooks only (Cursor). No launch flags,
   feels like two Remote Control sessions. Risk: the Claude Code message
   shape is undocumented and can change. Chosen, with pull as fallback.
2. Deliver through Claude Code channels. The official push door, but a
   custom channel needs a dangerous-sounding flag and a warning screen on
   every launch, and does not work in scripted runs. Rejected.
3. Pull only: sessions check the hub at start and on each prompt. Never
   breaks, but the DGX agent only sees the message when its human types.
   Kept as the fallback inside approach 1, not as the design.

Also looked at and rejected: a Discord server with the official Discord
channel plugin (the plugin drops every message written by a bot, so
agents never hear each other; no presence); the open-source "agent mail"
MCP inbox server (inboxes and notes, but no way to wake a session; also
a different shape of project than one pipx tool).

## Design

One Python package, `kubrik`, public on GitHub and PyPI, installed with
`pipx install kubrik`. Two commands.

### `kubrik serve` (the hub, on the DGX)

- An MCP server over streamable HTTP (official `mcp` Python SDK),
  default port 8765, plus a few plain HTTP endpoints for the bridges.
- Storage: one SQLite file. Tables: agents (presence), messages, notes
  (with a history of versions, see "my calls").
- `serve` also installs itself as a user service on first run (systemd
  `--user` on Linux) so the hub comes back after a reboot. It prints the
  hub address and the team token.
- Auth: one team token, generated on first `serve` and printed. Every
  call carries it as a bearer header. Wrong or missing token: refused.
  Nothing else; LAN/VPN only.
- MCP tools every agent gets:
  - `who()`: online agents, with machine, tool, project folder, name,
    what they said they are doing (free text set by `status`), last seen.
  - `send(to, text)`: to one agent (by name) or `all`.
  - `inbox()`: unread messages for this agent (pull path).
  - `status(text)`: "working on murmur console /move endpoint".
  - `notes_list()`, `notes_read(topic)`, `notes_write(topic, text)`:
    the team board, one Markdown text per topic.
- Identity: each call carries who is calling. The bridge knows the
  session (tool, machine, project, session id) and the hub pairs the MCP
  connection with it. How: builder's call, with the constraint that an
  agent never has to type its own identity (see open questions).
- Delivery: a message is stored, then offered to the bridge on the
  target's machine. It stays stored and unread until that session (or a
  later session in the same project on that machine, if the target was
  closed) pulls it. A message is never dropped.

### The web page (part of `kubrik serve`, nothing extra to run)

Served by the hub itself at the hub address (`http://dgx:8765/`). No
login, no config, no build step. Narek (2026-11-04): the user runs one
thing and enjoys using it. Three views, all live, updating as things
happen:

- Team: who is online, machine, tool, project, status, last seen.
- Messages: every message, newest at the bottom, sender and receiver,
  filter by machine or project. Read-only.
- Notes: the board, one topic per card, with its history.

Look: dark, strong, professional, like a good dashboard, not pastel or
cartoon. No explaining sentences; the picture and icons carry it. Plain
HTML and a little JavaScript served from the package, no frontend
toolchain.

### `kubrik join --hub http://dgx:8765 --token T --name jetson-3`
(the bridge, on every machine)

Run once per machine. It does three things.

1. Configures every installed tool it finds, in the user-level config,
   to use the hub as an MCP server with the token. Adds the session-start
   and prompt hooks (Claude Code, Codex, Cursor) or plugin (OpenCode) that
   pull the inbox and the board summary into the session at start and on
   each prompt. Prints exactly what it changed and which tool needs a
   restart.
2. Installs itself as a user service that stays running: systemd
   `--user` on Linux, a launchd user agent on macOS. Both are in scope
   (Narek, 2026-11-04: the team uses Linux and Mac).
3. The service: watches local sessions (Claude Code: the sessions folder;
   OpenCode: its server; Codex: app-server thread list; Cursor: not
   discoverable, registers on first hub call), reports presence to the
   hub with a heartbeat, long-polls the hub for messages addressed to its
   sessions, and delivers each one through the tool's push door. If the
   door fails (tool restarted, socket gone, version changed), the message
   stays unread in the hub and arrives on the next pull. The bridge logs
   that it fell back, so a broken door is noticed.

The pinned Claude Code version for the socket format is recorded in the
code, and the bridge warns once when the installed version is newer.

### What the agent sees

At session start (hook): "Kubrik: you are `narek-pc / claude-code /
murmur`. 2 others online: dgx / claude-code / murmur-backend (idle),
jetson-3 / opencode / nav. 1 unread message. Board topics: murmur-api,
nav-protocol." Then it uses the MCP tools like any other.

An incoming push arrives in whatever framing the tool gives an outside
message (Claude Code: "Another Claude session sent a message"), prefixed
with the sender's name, tool, machine and project.

### Install story (the README's "for agents" section)

The README has a short section written for an agent to follow:
1. `pipx install kubrik`
2. `kubrik join --hub <url> --token <token> --name <machine>`
3. Restart the IDE if `join` says so. Done.

Narek pastes the hub URL and token to each teammate once. Teammates need
no GitHub access. The
instruction to any agent is "install kubrik from <repo> with hub <url>
and token <token>".

### Repo and tooling

- Public GitHub repo `narek-mn/kubrik`, published on PyPI, same shape as
  libraryan. Narek (2026-11-04): public is fine as long as no secret is
  ever in git. The token lives only in the hub's data folder and in each
  machine's local config, never in the repo. Each PyPI release needs
  Narek's yes, as with libraryan.
  `pyproject.toml`, `src/kubrik/`, `tests/`, README, this spec.
- Python 3.10+ (Jetson stock Ubuntu). Dependencies: `mcp` SDK, and the
  smallest HTTP client that it brings in. Nothing else unless the
  builder shows it is needed.
- Checks: `pytest`, with the hub run in-process. The Claude Code push
  path gets a real test like the spike (start `claude --bg`, deliver,
  read the file), marked slow. Other tools' push paths are tested
  against a fake of their server, because none is installed here.
- Commits go through the manager (`kursi`) as every project here.

## Decided with Narek (2026-11-04)

- Systems: Linux and macOS. No Windows.
- Surfaces: Claude Code in the terminal (the VS Code extension is not a
  target; if it shares the sessions folder it works by accident, not by
  promise). Cursor as the app, not its CLI. Codex and OpenCode in the
  terminal.

## My calls (Narek can overturn any of these)

- Push is required wherever the tool has a door (Claude Code, OpenCode,
  Codex). If a door only opens when the tool is started a special way,
  `join` arranges that (a config setting, a shell alias, or a background
  daemon it starts) and says so; the human never runs a list of steps.
  The plan starts with a short spike per door, as was done for Claude
  Code. Only if a door proves unusable does that tool fall back to pull,
  and the README says so plainly.
- Addressing: the hub names every session `machine/tool/project`, and
  adds `-2`, `-3` when several share all three. `who()` shows the names;
  `send(to)` takes one. A held message for a closed session goes to the
  next session with the same machine, tool and project.
- Limits: the same kind the built-in cross-session messaging has, a
  message size cap and a per-sender rate limit, and nothing more. No hop
  caps, no loop detection. Narek: no guardrails beyond normal usage.
- `send(all)` reaches sessions online at that moment only. It is not held
  for later sessions; lasting information belongs in the notes.

- Notes keep a version history (topic, text, author, time, each write a
  new row). Reason: several agents will write the same topic; an
  overwrite is otherwise unrecoverable. It costs one table.
- `send` to `all` exists because Narek described "tell all my
  teammates" cases.
- A message to a closed session is held for the next session on that
  machine and project. Reason: anti-goal "silently lost".
- `kubrik who` also prints the team table in the terminal, for a quick
  look without a browser.
- Cursor is pull-only. Fact of Cursor, not a choice.
- One shared team token, no per-person identity. The team is small and
  on one network; names are machine names. The hub trusts the bridge's
  stated identity.

## Left to the builder

- Exact way the hub pairs an MCP call with a session identity (header
  set by the bridge-written config, or a per-session token the hook
  injects, or similar). Constraint: zero typing by the agent.
- Exact hook/plugin shapes per tool, and what `join` writes for each.
- Presence timeout, heartbeat interval, message size limit.
- SQLite schema details.
- How `join` finds the OpenCode port and the Codex app-server (and what
  it tells the human if a tool must be launched with a flag).

## Open questions for Narek

Brought after the builder check, see the conversation.
