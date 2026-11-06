# Kaptyorka 0.2 to 0.6: the team brain

Date: 2026-11-06. Status: approved by the owner on 2026-11-06, after a builder check; manager (kursi) review fixes folded in the same day.
Builds on `2026-11-04-kaptyorka-design.md` (0.1.x, "classic"). Everything in
that document stays true unless this one changes it.

## Intent

Kaptyorka 0.1 lets coding-agent sessions on one LAN see each other, message
each other and share notes, behind one shared token. 0.2 to 0.6 turn the hub
into the team's brain:

- A person opens an agent anywhere, joins, and is a working member of the
  team from the first second: their session knows the projects it may know
  and where each one stands.
- A person reaches any machine's hands through their own agent: "work on
  project P on a build box" starts a session there, under their own account,
  and connects it to the session they are typing in.
- Everyone's agent follows the team's rules without anyone copying files.
- All of this with per-person accounts, so a team can share one hub without
  everyone seeing everything, and so a stolen laptop reaches little.

The owner's one-line version: "join from anywhere, know what you may know,
reach any machine through your own agent."

## Goals

1. Per-person accounts with GitHub-style roles, checked by plain code.
2. A person's sessions form a group; nobody outside it can message in
   unless a door is opened by the person on the other side.
3. A session can ask the hub for a worker: a session started on another
   machine, in a project folder, under the asking person's own account,
   linked to the asking session as its boss.
4. A fresh session gets, on join: the team rules, the projects it may see
   with their state, and recent git facts for its own project.
5. An agent cannot say "done" in a project without updating that project's
   state note (where the tool has a blocking hook).
6. Team rules reach every tool as text; hooks and skills where the tool has
   them. The hub shows per session which it got.
7. Joining is one link. Shared machines are enrolled from the hub machine
   with one command. The hub watches every device and says in plain words
   what is wrong. Bridges keep themselves up to date.
8. Everything that must be exactly right is code with tests. An agent
   judges only where judgment is needed, and every judgment is logged.

## Non-goals

- Linking hubs to each other. One hub per team; split by making a second.
- Starting sessions on Windows. Windows laptops may join and talk.
- Code sync. Code stays in git; the hub points at repos, never copies them.
- A decision model (such as a hosted typed-answer service) anywhere in
  access control.
- "Hands-only" workers where the agent runs on the hub and only commands go
  to the machine. Possible later; not this design.

## Anti-goals

- A session that breaks when the hub is away. Every session keeps working
  alone; it only loses team features. No feature may violate this.
- A step in joining that needs a manual or a second try.
- A worker killed by software. Only people close workers.
- A rule changed by an agent on its own. Agents propose; people accept.
- Team-specific names (people, projects, machines) in this public repo. They
  live in the hub's data and in the team's own private place.

## Words

| Word | Meaning |
|---|---|
| person | a human with an account on the hub |
| admin | the one person who owns the hub; the only one who grants access |
| device | one machine account a person joined from; each has its own token |
| session | one running agent, anywhere |
| worker | a session the hub started on another machine for someone |
| boss | the session a worker was started for |
| group | all sessions of one person |
| door | a temporary permission for two sessions of different people to message |
| knock | a request to open a door |
| manager | the always-on agent session on the hub machine, under the admin account |
| bridge | the per-account background service on each joined machine (exists in 0.1) |
| shim | `kaptyorka mcp`, the stdio MCP server each tool talks to (exists in 0.1) |

## Constraints

- Python 3.10 floor. Dependencies stay `mcp` and what it brings; a new
  dependency needs a reason written next to it.
- Linux and macOS. A worker runs in the tool's own background mode where
  it has one (Claude Code: `claude --bg`, with `claude attach <id>` as the
  human's window); otherwise inside tmux. A machine without tmux can host
  only Claude Code workers, and the page says so.
- The hub is one SQLite file plus a files folder in `~/.kaptyorka`.
- LAN or VPN only, plain HTTP as in 0.1; no TLS. The invite route `/i/<code>`
  and token checks get a small per-address rate limit (10 failures a minute)
  because they are the only routes an unauthenticated client can hit.
- A 0.1.9 bridge or shim must keep working against a 0.2 hub for one minor
  version, so machines can update one at a time. The old shared token acts
  as the admin's device token during that time.
- Tests never call real agent tools except under `-m slow` (as in 0.1).
- The web page stays plain HTML and JavaScript in one file, dark, no build.

## Approaches considered

For each large piece, the alternatives and why one was chosen.

### Identity

1. **Per-person device tokens, issued by the hub.** The hub creates people,
   the admin hands out invite links, each join exchanges an invite for a
   device token stored in the machine's config. Every request carries it as
   today's bearer header; the hub maps it to a person. Chosen: no new
   dependency, works for agents (no browser), revocable per device, and the
   bridge and shim already send a bearer token.
2. External login (OIDC against the team's GitLab or GitHub). Real identity,
   but every agent and bridge would need a browser flow or a client secret,
   and the hub would depend on an internet service, which breaks the
   "works on the LAN alone" rule. Rejected.
3. Keep one shared token and put names in config. Zero work, zero security:
   anyone with the token is anyone. Rejected; the whole point is that a
   stolen token reaches little.

### Starting a session on another machine

1. **Through that account's bridge, which already runs there as that user.**
   The hub puts a launch order in the bridge's poll stream; the bridge runs
   the tool inside tmux in the project folder and reports the new session.
   Chosen: no new credentials, no root, the hub never logs in anywhere, and
   joining is how an account offers itself.
2. ssh from the hub machine into the target account. Needs the admin's key
   on every account, which gives the hub machine power over every account,
   and fails for laptops without sshd. Rejected.
3. The agent on the hub machine reaches out itself (an MCP ssh tool). Same
   objection, plus the manager would become a privileged shell. Rejected.

### Serving team rules

1. **Inject the rules text through the existing start hook, and write
   skill files through the bridge where the tool has a skills folder.** The
   start hook already injects a brief into every tool's context; rules are
   more of the same text. Chosen: zero config writes for the text layer,
   works for all four tools, cannot conflict with the person's own files.
2. Write the rules into each tool's user-level rules file. Overwrites or
   mixes with the person's own rules, and each tool has a different file.
   Rejected for text; used only for skills, which need files.
3. A Git repo every machine pulls. Needs credentials per machine and a
   place in each tool's config. Rejected; the hub is already the
   distribution channel.

### The manager

1. **Bookkeeping in hub code, judgment in one agent session.** Free/busy,
   access, routing, launching, holding reports are code with tests. One
   long-running agent session on the hub machine handles the few cases that
   need judgment (a busy machine, an abandoned worker, a proposed rule) and
   does so through the same tools everyone has, as the admin. Chosen.
2. Everything in the agent. Simple to write, impossible to trust: access
   decisions by a model, no tests. Rejected.
3. Everything in code, no agent. Reliable, but "can these two share a
   build box" and "write a brief for this worker" are judgment, and hard
   rules there would be wrong often. Rejected; the agent part is small and
   can be switched off.

### Rule conflicts

Fixed order: project rules beat team rules, team rules beat personal rules;
within a layer, the more specific rule wins; when an agent still cannot
tell, it names both rules and asks its person, and the hub logs the
conflict. No alternative was seriously considered; this is the standard
layering and matches how the tools already treat project and user scope.

## Design

The order below is the build order. Each part ships as a minor version
(0.2 people, 0.3 workers, 0.4 memory, 0.5 harness, 0.6 manager) and is
usable on its own.

### Part 1: People (0.2)

**Data.** New tables, all in the existing SQLite file:

- `people(id, name, role, created)`; role is `admin`, `member` or
  `manager`. Exactly one admin, created by `kaptyorka start` on first run
  (name from the OS user, changeable on the page). The `manager` person
  (part 5) sees the roster, machines, workers, requests and the log; it
  does not read project notes it was not granted, nor message contents
  other than its own. It may send to anyone, knock, adopt its own workers
  and call `work_decide`; it may not answer other people's knocks, open or
  close doors, grant, revoke, or use admin page routes. These
  limits are role checks in code, so a steered manager cannot widen access.
- `devices(id, person_id, name, token_hash, created, last_used, revoked_at)`.
  One row per `join`. The token is 32 random bytes, URL-safe; the hub stores
  its SHA-256 only. `name` defaults to `user@host`, as the machine name does
  today.
- `invites(code, person_id, created, expires, used_at)`. Created by the
  admin on the page or with `kaptyorka invite <person>`; valid 7 days; one
  use.
- `projects(name, description, repo, created)` and
  `project_access(project, person_id, role)` with role `read`, `write`,
  `manage`.
- `machines(host, owner_person_id)`: a machine is a host. The bridge
  reports the hostname in every heartbeat (the 0.1 "machine name"
  `user@host` becomes the device name). A machine row appears at the first
  heartbeat from that host, with no owner: the host name is self-reported,
  so only the admin names the owner (`kaptyorka machine owner`, the page,
  logged as `machine_owner`). Several
  devices (accounts) can sit on one host; each shows under its own person.
- Devices gain `shared` (bool, default false), set by the admin on the page
  or by `fleet add --shared`. A shared device is an OS account the admin
  offers to the team: workers started through it belong to the person who
  asked (the launch order carries the person, and the hub stores it on the
  session row when the bridge reports it). A session on a shared device
  that was not started by a launch order belongs to no person and may only
  talk to the admin. The hub always prefers the asking person's own account
  on a host; a shared account is used only when they have none there.
- `machine_access(machine, person_id)`: "use".
- `doors(id, a_key, a_person, b_key, b_person, scope, opened_at,
  closed_at, opened_by)`; scope is `session` or `group`.
- `knocks(id, from_key, from_person, to_key, to_person, about, created,
  answered_at, answer)`.
- `requests(id, person_id, kind, target, created, decided_at, decision,
  decided_by)`; kind is `project` or `machine`.
- `log(id, time, person, kind, detail)`: append-only.
- `files(id, message_id, name, size, sha256, created)`; bytes in
  `~/.kaptyorka/files/<id>`.
- `agents` gains `person_id` and `os_user`. `messages` gains `file_id`.
- `schema(version)`; `Store.__init__` migrates forward.

**Migration from 0.1.9.** On first start of 0.2, the hub creates the admin
person and turns the existing `hub-token` into the admin's first device
token (so the admin's already-joined machines keep working and show as the
admin's). Every existing agent row is attributed to the admin. Existing notes whose
topic equals a project folder seen on any agent row become that project's
notes (and the newest one seeds its `state` note when the project is
registered); the rest stay team notes. Unregistered projects are visible
only to the admin's group until registered, so nothing private becomes
visible to a new member on upgrade.
Teammates' machines send the same old token today. The hub keeps a
`legacy` device for it: requests with the old token are attributed by their
0.1 machine name, and the People tab lists every machine name seen with the
old token under "unassigned". The admin assigns each one to a person with
one click; the hub then issues that device its own token and puts a `rekey`
order in that bridge's poll stream. The bridge rewrites `config.json`;
shims and hooks read the config at start, so new sessions use the new token
and running ones keep working under the legacy mapping. The legacy token is
accepted until the admin switches it off on the page; then it is refused with a message naming
`kaptyorka join`. Not tied to a version number, so parts released together
cannot cut anyone off. Nothing to do by hand on the teammates' side.

**Joining.** Three forms, all ending in the same config file:

- `kaptyorka join <invite-url>`: the URL is `http://<hub>:<port>/i/<code>`.
  The command posts the code and the device name, receives a device token,
  saves `hub`, `token`, `machine`, and continues as today's join (configure
  tools, install bridge).
- `kaptyorka join --hub <url> --token <device-token>`: unchanged, for a
  token the person already has.
- `kaptyorka join` with no flags: re-run with saved settings, unchanged.

The README's "for agents" section becomes: paste the invite link to your
agent and say "join this". The invite page itself (GET `/i/<code>` in a
browser) shows the one command for people without an agent.

**Who you are on every request.** The bearer token names the device and the
person. The shim and hook keep sending the identity headers for the session
(key, machine, tool, project); the hub now also knows the person from the
token and stores it on the agent row. The bridge's heartbeat carries the
account's OS user name; the hub stores it so a machine with several joined
accounts shows each account's sessions under the right person.

**Visibility.** What a call returns depends on the caller's person:

- `who()`: every online session's name, machine, tool, person, activity and
  whether it is in your group. Projects of sessions outside your group are
  shown only if you have `read` on that project; otherwise the project
  column reads `(private)`. The roster itself is visible to all members
  (the GitHub default: members see the org's members).
- `notes_*`: a note has a `project` (nullable). Team notes (`project` null)
  are visible to all members and writable by all. Project notes need `read`
  to read and `write` to write. `notes_list` lists only what you may read.
- Messages: see Groups.
- The admin sees everything, always.

A session's project is still the folder name. A folder name that matches a
registered project is that project. A folder name that matches none is an
unregistered project: visible to its person's group and the admin only,
until the admin registers it (the page offers "register" next to it).

**Groups and doors.** `send(to, text)` is allowed when the target session's
person is the sender's person, or the sender is the admin, or an open door
covers the pair (session-to-session, or group-to-group). Otherwise the tool
returns an error naming the rule and the `knock` tool. `send("all")` reaches
the sessions you may message, which for a member is their own group and for
the admin everyone.

New tools:

- `knock(to, about)`: creates a knock and pushes it to the target session as
  a message of kind `knock`, framed so the agent shows it to its person and
  does not act on it: "`<person>`'s session `<name>` asks to talk about
  `<about>`. Answer with `answer(<id>, "yes"|"no")`." If the target person
  has no online session, the knock waits and is delivered to their next
  session, and shows on the page. Knocking needs no grant.
- `answer(knock_id, "yes"|"no")`: a yes opens a session-scope door between
  the two sessions; the knocker's session is told. Only the knocked
  session's person (any of their sessions) or the admin may answer.
- `door_close(to)`: closes a door you are on. A door also closes when either
  session goes offline for longer than the presence timeout. Doors show in
  `who()` and on the page, and are logged.

The admin may open or close any door from the page, including group-scope
doors for two people who work together daily.

**Access requests.** New tool `request_access(kind, target, why)`; the
request shows on the admin's page with approve and deny. A denied or pending
request is visible to the asker in `who()`'s header line and in the start
brief. The admin also grants without a request, on the page or with
`kaptyorka grant <person> <project>:<role>` and
`kaptyorka grant <person> machine:<name>` on the hub machine.

**Attachments.** `send(to, text, file=None)`: `file` is a local path. The
shim reads it (limit 5 MB; images, text, PDF; anything else refused with
the reason), uploads it to `POST /api/files` and the message carries the
file id. `inbox()` returns `file: {id, name, size}`; new tool
`fetch_file(id)` saves it to `~/.kaptyorka/inbox/<id>-<name>` and returns
the path, so the agent can open it with its own file tools. The page shows
images inline and other files as a download. Files go when their message is
pruned.

**Log.** Every grant, revoke, join, device revoke, door open/close, knock
answer, worker start/adopt/close, rule accept, and admin page login is one
row. The page shows the log, newest first, filterable by person. Never
deleted.

**The page.** Login is a device token pasted once (stored in the browser as
today's page token is) or the invite link, which also logs the browser in as
that person's "web" device. The page filters everything by the viewer's
person. The admin sees extra tabs: People (accounts, devices, invites,
revoke), Access (grants, pending requests), Machines (see part 2), Workers
(see part 2), Rules (see part 4), Log. Same dark look as 0.1.8.

**Secrets check.** `send` and `notes_write` refuse text that matches common
secret shapes (long high-entropy tokens with known prefixes, private key
headers), with a message saying what matched. Cheap and local.

**Retention.** Messages and their files older than 180 days are deleted by
the hub once a day. Notes and the log are kept forever. The number is a
setting on the page.

### Part 2: Workers (0.3)

**Machine facts.** The bridge's heartbeat gains: `os_user`, `tmux`
(available or not), `load` (1-minute load average and CPU count), `tools`
installed with their versions, `version` of kaptyorka, and for each running
session its `branch`, `dirty` flag and the subject of its last commit, read
with git in the session's folder (cheap; skipped if the folder is not a
repo). The hub shows all of it on the Machines tab, per machine and per
account.

**Asking for a worker.** New tool
`work(project, task, machine=None, tool=None, resume=None)`:

1. The hub checks the caller has `write` on the project (or it is their own
   unregistered project).
2. Candidate machines: those where the caller has a joined account with an
   online bridge, plus shared accounts on hosts the caller has `use` on.
   This holds for the admin too: nobody, admin included, starts a worker
   under another person's own account. If
   `machine` is given, only that one. A machine without tmux is skipped with
   a reason.
3. If no candidate has the project folder, the answer says so and names the
   machines it looked at. (The folder is `~/<project>` unless the project
   record names another path for that machine.)
4. Among candidates with the folder: prefer one where nobody has a session
   in that folder; then the lowest load. If every candidate already has a
   session in that folder, the worker is started in a worktree (below) and,
   if the occupant is another person, the manager is asked first (part 5;
   until then, the hub asks the occupant directly with a knock-like
   message and waits up to 2 minutes, then answers "busy, ask again").
5. The hub creates a `workers` row (worker name, boss key, boss person,
   project, machine, task, state `starting`), and puts a launch order in
   that account's bridge poll stream, signed with HMAC-SHA256 under a
   per-device signing secret the hub returned once at join and never sends
   again (the bearer token travels on every request and so cannot serve as
   a signing key). The bridge also refuses an order for a person other than
   its own unless the device is marked shared.
6. The tool returns at once: "starting `<name>` on `<machine>`". When the
   bridge reports the new session, the hub links it to the worker row,
   sends the boss "worker `<name>` is up" and sends the worker its first
   message: the task, who its boss is, and the rule that it takes orders
   only from its boss and the admin.

**What the bridge does with a launch order.** Validates the signature.
Picks the folder: the project folder, or a worktree
`~/<project>--<n>` on branch `work/<worker-name>` when the folder is taken
(`git worktree add`). Runs the tool detached, in the folder, with the
environment the bridge itself runs under (so a team API key set at enrol
time reaches the agent): Claude Code with `claude --bg --session-id <uuid>
[--resume <id>] "<first message>"`, so the hub knows the session key before
the session exists and the human attaches with `claude attach`; OpenCode and
Codex with `tmux new-session -d -s <worker-name> -c <folder> <command>` and
their resume flags. The first message is the task, the boss's name and the
order rule (below).

A worker must outlive the bridge: the bridge service is restarted on every
update, and systemd kills its whole control group by default. So the bridge
starts the worker outside its own service: on Linux through
`systemd-run --user --scope --unit kaptyorka-worker-<name>`, on macOS by
detaching into its own process group (and the launchd plist sets
`AbandonProcessGroup`). A slow test restarts the bridge and checks the
worker is still alive. Then the bridge heartbeats so the hub sees the
session within seconds. On any failure it acks the order with the error
text, which the hub passes to the boss.

**Permissions inside a worker** (owner's decision, 2026-11-06). A worker
runs with the permission settings saved on that account, exactly as if its
person had typed `claude` there. When a worker is waiting on a permission prompt (Claude Code reports
this in the session file's status; other tools as the builder finds), the
hub tells the boss once: "worker `<name>` is waiting for a permission;
attach with `<command>`". Nothing is bypassed by kaptyorka.

**Boss and worker.** The worker's messages to its boss go through `send`,
which is allowed because both are in one group. The worker's `who()` shows
its boss marked. If the boss session goes offline, the worker keeps running;
its messages are held (0.1 semantics) and the hub marks the worker
`unattended`. The next online session of the boss's person is offered the
worker: its start brief lists unattended workers, and `adopt(worker)` makes
it the new boss and delivers the held messages. The person may also start a
fresh session with `work(..., resume=<worker>)` semantics in reverse: the
page shows the attach line for every worker (`claude attach <id>` or
`tmux attach -t <name>`), which is the human's window onto it.

A worker unattended for 24 hours is marked `abandoned` on the page. Nobody
and nothing kills it; a person closes it (`close_worker(name)`, or on the
page), which ends the tmux session. Closing a worker whose folder was a
worktree leaves the worktree in place and says so.

**One session per folder.** The hub refuses to start a worker in a folder
that has an online session, from anyone, including the same person. The
worktree path is the only way to have two. The page shows folder occupancy.

**Enrolling shared machines.** `kaptyorka fleet add <user>@<host> [more...]
--person <name> [--key-from <file>]` on the hub machine:

1. ssh to each target (the admin's existing ssh access; nothing is
   installed for ssh).
2. Installs pipx if missing (apt or brew, best effort, reported), then
   `pipx install kaptyorka` or upgrade.
3. Writes an invite for `--person`, runs `kaptyorka join <invite-url>
   --name <user>@<host>` there.
4. If `--key-from` names a local file holding an API key for the agent
   tool, writes it into the bridge service environment on the target (so
   workers inherit it) and nowhere else. The hub never stores it.
5. Reports which agent tools the target has and which are missing; it
   installs none.
6. Prints one line per machine: joined, or what failed and the exact
   command to run by hand.

`kaptyorka fleet list` shows every enrolled account with its bridge state.

**Doctor and self-update.** `kaptyorka doctor` on any machine checks:
services installed and running, config present, hub reachable, token
valid, each tool's config holds our entries, hooks present, tmux present,
and prints one line per check with the fix. `--fix` applies the fixes that
are mechanical (re-run configure, restart service). The bridge asks the hub
for the current version in each heartbeat response; if the hub runs a newer
version and the device's `auto_update` is on (default on for members, off
for the admin's own machines), the bridge runs `kaptyorka update` at the
next quiet moment (no session delivering) and logs it.

**Warnings.** The hub derives warnings from heartbeats and shows them at
the top of the admin's page and in the admin's start brief: bridge offline
longer than 10 minutes, bridge version behind, tool configured but its
hooks missing (the bridge checks its own tool configs in each heartbeat),
no tmux on a shared machine, worker unattended or abandoned, launch failed.
Each warning is one plain sentence with the machine and account.

### Part 3: Memory (0.4)

**Project state.** Each registered project has one special note, topic
`state`, with five sections in this order: What it is, Where it stands,
Decisions, Open problems, How to run and test. `notes_write` on that topic
accepts Markdown and checks the five headings exist. `state(project)`
reads it; `state_write(project, text)` writes it (sugar over notes).

**The brief on start** (the existing `/api/brief?event=start`) becomes:

1. The team rules (part 4), when any.
2. "You are `<name>`, `<person>`, on `<machine>`." Online sessions as
   today, plus your unattended workers, plus pending knocks or requests
   for you.
3. The projects you may read, one line each: name, description, who is
   working on it now.
4. For the project this session sits in: the state note, and the git facts
   the hub has for it from other sessions in the last 7 days: who pushed
   to which branch and when, which branches have open sessions.
5. Unread messages, as today.

The brief is trimmed to a budget (default 6,000 characters): the state note
first, then git facts, then the project list, then the rules, which are cut
with a line saying how to read the whole with `rules()`.

**The done check.** A new hook event `stop` for Claude Code (its `Stop`
hook, which may return `{"decision":"block","reason":...}`). The hook calls
`GET /api/done-check`; the hub answers `ok` or `block` with a reason. It
blocks when all hold: the session sits in a registered project its person
may `write`; since the session started the hub has seen a work signal from
it (a new commit in its folder, from the git facts; or the folder going
from dirty to clean; or the agent setting a status containing "done"); no
`state` write for that project came from this session after that signal;
and this session has not been blocked before (so an agent that cannot
comply is blocked once, not forever). A turn that only read or discussed is
never blocked. The reason text tells the agent exactly what to write and how.
For OpenCode the plugin's idle event shows the same text as a reminder (it
cannot block). Codex and Cursor: the builder verifies whether their stop
hooks can block; if not, reminder. The brief says which mode this session
has.

**Git facts.** Stored from heartbeats in `git_facts(project, machine,
person, branch, commit, subject, dirty, time)`, kept 30 days, one row per
change. Served in the brief and on the project's page.

**Decisions.** A project note with topic `decisions`, append-only by
convention: `decide(project, text)` appends a dated line with the author.
Read through `notes_read`. Cross-project decisions go to the team note
`decisions` the same way.

### Part 4: Harness (0.5)

**What the hub holds.** `rules(scope, text, version, author, time)` with
scope `team` or a project name; one current version per scope, history
kept. `skills(scope, name, text)` files. `hooks` are kaptyorka's own and
installed by `join`; the team cannot add arbitrary hooks through the hub
(a hook is code running on every machine; that stays with the package).

**How rules get in.** The admin edits team rules on the Rules tab or pushes
a folder: `kaptyorka rules push <dir>` reads `RULES.md` and `skills/*.md`
from the team's private place and uploads them. Project rules live in the
project repo as today and are not stored in the hub; the brief points at
them by saying "project rules: see this repo's own files".

**How rules get out.**

- Text: the start brief carries the team rules under a heading that states
  the order ("project rules in this repo override these; these override
  your personal rules"). Every tool gets this, since every tool has the
  start hook (Cursor at the first stop).
- Skills: the bridge writes `~/.claude/skills/team-<name>/SKILL.md` for
  Claude Code and the equivalent folder for OpenCode when it has one (the
  builder verifies the current OpenCode skills location; if none, skills
  are folded into the rules text for that tool). Files are owned by
  kaptyorka (a marker line) and replaced on change; `leave` removes them.
- Hooks: already installed by `join`; the done check is one of them.

**Reporting.** Each heartbeat says, per tool on that account, which layers
are in place: `rules` (hook present), `hooks` (all our hooks present),
`skills` (folder written and current). The Team tab shows three small
marks per session; the brief says "rules only" when that is the case.

**Proposals.** `propose_rule(text, evidence)` creates a proposal visible on
the Rules tab with the evidence; the admin accepts (appends to team rules
as a new version) or rejects, with a reason that goes back to the proposer's
session. Agents are told in the rules text that repeated corrections of the
same kind are what to propose.

**Conflicts.** The rules header states the precedence. When an agent asks
its person because two rules clash, it calls `conflict(rule_a, rule_b)` so
the hub logs it; the Rules tab lists conflicts so the admin can fix the
text.

### Part 5: Manager (0.6)

**What is code (already built by 0.5).** Presence, access, routing,
launching, holding, warnings, retention, log.

**What the manager session does.** One Claude Code (or other tool) session
on the hub machine under the admin account, in `~/.kaptyorka/manager/`,
started by `kaptyorka start` through the admin's own bridge like any worker
(so it is in the tool's background mode and restarts with the machine), with
a `CLAUDE.md` the hub writes from the team rules plus a fixed manager
section. It runs under its own device token with the `manager` role (part
1): it may send to anyone, knock, adopt its own workers and decide launch
requests; it cannot answer others' knocks, open doors or grant anything. The hub pushes it a message when:

- `work()` found only busy machines: "`<person>` wants `<project>` on
  `<machine>`, occupied by `<other person>` in `<folder>`. Ask them, or
  pick another machine." The manager knocks or messages the occupant,
  reads the answer, and calls `work_decide(request_id, machine|wait)`.
- A worker becomes abandoned, a bridge goes offline on a shared machine,
  or a launch fails twice: it tells the admin's online session in one line.
- A proposal or access request has waited a day: a reminder to the admin.

Everything it decides is a tool call the code checks (the person must have
`use`; the folder must be free or a worktree) and logs with the manager's
stated reason. `kaptyorka start --no-manager` runs the hub without it, and
nothing else changes.

**Learning.** The manager reads the log weekly for repeated conflicts and
repeated corrections and files `propose_rule` like any agent. It never
edits rules.

### What stays the same

- `who`, `send`, `inbox`, `status`, `notes_*` keep their names and shapes;
  new fields are added, none removed.
- The bridge's poll/ack loop; launch orders are a new item kind in the same
  stream.
- The page's three original tabs and their look.
- `start`, `join`, `leave`, `stop`, `update`, `remove` keep working as
  documented, with `join <invite-url>` added. New commands: `invite`,
  `grant`, `fleet`, `doctor`, `rules`, `backup`.

### Backup

`kaptyorka backup <user>@<host>:<path>` on the hub machine copies the
SQLite file with the online-backup API, tars it with the files folder,
encrypts with `gpg --symmetric` when gpg is present (passphrase from
`~/.kaptyorka/backup-pass`, created on first run, 0600) and otherwise says
plainly that the copy is unencrypted, then sends it with scp. `kaptyorka backup --nightly <dest>` installs a
user timer that runs it every night; `start` asks nothing. `kaptyorka restore <file>` is the one-command reverse; the
builder tests both once.

## Testing

- Store: one test per new table operation and per visibility rule, with a
  fake clock as today.
- Hub routes and tools: in-process app, several people with different
  tokens, asserting what each sees and may do. Doors, knocks, requests,
  attachments, done check, brief content and budget.
- Bridge: launch order verification, tmux and git commands through an
  injected `run`, worktree choice, heartbeat facts.
- Fleet and doctor: through an injected `run`; no real ssh.
- Migration: a 0.1.9 database file in `tests/fakes` opens and migrates.
- Slow: one real worker launch on this machine (Claude Code in tmux),
  boss-worker exchange, and the done check blocking once.
- The README's command examples are checked by `test_readme.py` as today.

## My calls (the owner can overturn any)

- Device tokens are random, hashed in the store; invites expire in 7 days.
- A knock needs no grant; a `yes` from any of the knocked person's sessions
  opens a session-scope door; group doors are admin-only.
- The worker's branch is `work/<worker-name>`; the worktree is
  `~/<project>--<n>`.
- The done check blocks at most once per session and only after a work
  signal (commit, dirty-to-clean, or a "done" status).
- A machine is a host; a device is one account on it; shared accounts are
  marked by the admin and lend their sessions to the asking person.
- Teammates on the old shared token are reassigned by the admin on the page;
  the bridge is re-keyed through its poll stream; the old token keeps
  working until the admin switches it off.
- The manager has its own role that cannot answer knocks, open doors or grant.
- Retention: messages 180 days; notes, log, rules forever; git facts 30 days.
- Brief budget 6,000 characters, cut in the order given.
- Auto-update defaults on for members' devices, off for the admin's.
- Backup encryption uses gpg when present rather than a new Python
  dependency.
- The manager is a plain agent session in the tool's background mode with a
  written `CLAUDE.md`, not a special process; `--no-manager` turns it off.
- Project folder defaults to `~/<project>` on every machine, overridable
  per machine on the project record. Cloning missing folders is not in
  this design.

## Left to the builder

- Exact HMAC framing of launch orders (the signing secret is per device,
  returned once at join and stored in `config.json`).
- The per-tool command lines for a worker, including resume flags, verified
  against installed versions.
- Whether Codex and Cursor stop hooks can block; the OpenCode skills
  location.
- The page's exact layout for the new tabs, following the 0.1.8 style.
- Secret patterns for the secrets check.
- Load measurement on macOS.
- Hook timeout budget: the start brief grows; the hooks' 5-second timeout
  must still hold, so the brief is cached by the hub per session for 10
  seconds.

## Open questions for the owner

One question came out of the builder check (2026-11-06): permissions
inside an unattended worker. Decided by the owner: the account's saved
settings apply, nothing is bypassed, and the boss is told when the worker
waits. Status: approved by the owner on 2026-11-06.
