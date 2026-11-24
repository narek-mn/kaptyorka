# Stuck sessions: research

A team session got an overnight task, ended its turn at about 19:00 and then did nothing for
about 16.8 hours. The cause was one Bash call run in the background
(`run_in_background: true`, `timeout: 3000000`):
`while pgrep -f run_suite.sh >/dev/null; do sleep 10; done; ...`. `pgrep -f` matched the
waiting shell's own command line, so the loop never ended. It printed nothing and never
finished, so no completion notice ever woke the session. The hub showed it as working the
whole time, and nothing noticed.

This file records what was checked on a real `claude --bg` session before building the check.

## How it was tested

Claude Code 2.1.331 (`claude --version`), Linux. A probe script started a `claude --bg`
session (model haiku) in a scratch folder next to the projects folder, with
`KAPTYORKA_HOME` set to a temp folder. It read `~/.claude/sessions/<pid>.json`, the
transcript, the `subagents/` folder, the session's `tasks/` folder and the child processes
every 10 s, then ran `claude stop` and `claude rm` and deleted the scratch folder, the
transcript folder and the tasks folder. Messages were delivered with
`ClaudeAdapter.deliver`, the same socket path the bridge uses. One run per case.

## a. `status` and `statusUpdatedAt` in the sessions file

| What the session was doing | `status` |
|---|---|
| A turn is running (model or a foreground tool) | `busy` |
| Turn over, nothing pending | `idle` |
| Turn over, a background Bash task is still running | **`shell`** |
| Turn over, a Monitor watch is still running | `shell` |
| Turn over, a background subagent is still running | `busy` |

- `statusUpdatedAt` (ms) is when `status` last changed: the start of the current busy or
  shell period. During a 120 s foreground command it kept growing from the turn's start.
- `shell` is new to kaptyorka. The bridge passes `status` through unchanged, and the page
  shows anything other than `idle` as "Working" (`index.html`, `e.act !== "idle"`). That
  explains the incident: the session was really `shell`, with its turn over, but the page
  showed "Working".
- `updatedAt` matched `statusUpdatedAt` in every sample. It is not a heartbeat.
- Other keys seen: `kind: "bg"`, `jobId`, `messagingSocketPath`, `name`, `version`.
  No key lists running tasks.
- In 2.1.331 the Agent tool starts subagents in the background by default. The parent's turn
  ends (the transcript's `turn_duration` line has `pendingBackgroundAgentCount: 1`), yet
  `status` stays `busy` until the subagent's notice arrives.

## b. Subagents

- While a subagent ran, the main transcript did not change. The subagent wrote to
  `~/.claude/projects/<slug>/<sessionId>/subagents/agent-<id>.jsonl` (plus a `.meta.json`).
- That subagent file also stops changing while one of the subagent's own tools runs. During
  its 90 s command it went 85 s without a write. So subagent files only show recent model
  activity; they cannot show that a running tool is still alive.
- The session's tasks folder held `<agentId>.output` for the subagent and a `.output` file
  for the subagent's foreground Bash call.

## c. Does a hub message land mid-turn?

- **While a foreground tool runs** (status `busy`): the message is queued. The transcript
  gets a `queue-operation` `enqueue` line at once, which changes the transcript's mtime. The
  model reads the message only **after the running tool call returns**, still inside the same
  turn (`remove`, `reason: "absorbed_mid_turn"`). A message cannot interrupt a running tool.
  A foreground Bash call is capped at 10 min, so the longest delay is one tool call.
- **While the turn is over and a background task runs** (status `shell`): the message starts
  a new turn straight away. The session answered within 2 s, while the background task kept
  running.
- So for the incident's case (status `shell`, a silent background task), a hub message
  **does** wake the session, which can then check the task and stop it.
- Side effect: delivering a message writes the transcript, so `active` (the transcript's
  mtime) moves even when the model has done nothing. The stuck check must not treat its own
  delivery as "it wrote again".

## d. What can keep a session busy or waiting for hours with no transcript writes

| Way | Bounded? | Seen as |
|---|---|---|
| Foreground Bash | yes, at most 10 min per call | `busy`, plus a `tasks/<id>.output` file |
| Foreground `sleep N` | Claude Code blocks it ("use Monitor ... or run_in_background") | - |
| **Background Bash** | **no: its `timeout` is ignored.** With `timeout: 60000`, the command was still running at 157 s. | `shell` (turn over) |
| Monitor | yes, at most 30 min, then one notice | `shell`; no `.output` file |
| Background subagent | as long as its own steps | `busy`, writes under `subagents/` |

- A background Bash task is a direct child of the session's `claude` process:
  `/bin/bash -c source <snapshot> ... && eval '<command>' ...`. The wrapper and the command's
  process both hold `tasks/<taskId>.output` open as stdout. This was checked in
  `/proc/<pid>/fd`.
- Where the bridge can see background tasks, on Linux, with no extra tools:
  1. The session's pid comes from the sessions file name.
  2. Its child processes come from `/proc/<pid>/task/*/children`, or by scanning `/proc/*/stat`
     for the parent pid.
  3. A child whose fd 1 points to
     `/tmp/claude-<uid>/<cwd-slug>/<sessionId>/tasks/<id>.output` is a live task. The
     link's target names the task.
  4. The output file's mtime is the time of the task's last output; its size can be 0.
  5. The command is the `eval '...'` part of the wrapper's command line (parsed with
     `shlex`). The transcript also has it: a `tool_use` with `run_in_background`, followed by
     the result "Command running in background with ID: <id>".

  macOS has no `/proc`, so this check would need `lsof` there. It is Linux only at first.
- `claude stop <id>` killed the session's background tasks too: no sleeper was left after
  cleanup.

## Why overnight mode never woke it

`overnight.on_stop` lets the session stop with no "continue" when the Stop hook's
`background_tasks` is not empty ("it waits for its own background work, which wakes it").
`overnight.watch` acts only on a crash or a usage-limit wait. A background task that never
ends therefore leaves the session asleep until the 16-hour cap.

## Proposal for step 3

Two signals, both judged by the hub from bridge reports. No judgement when a field is
missing or the bridge is old.

1. **Silent background task**: status `shell` (or `busy`), a live task whose output file has
   not changed for `SILENT_S`, and no transcript write (other than our own delivery) for the
   same time. The bridge reports per session `tasks: [{id, silent_s, command}]`, with the
   command cut to the first 100 characters and passed through `safe()`.
2. **Busy, no writes**: status `busy`, with no write to the main transcript and no write under
   `subagents/` for `STUCK_S` = 30 min. A foreground tool is capped at 10 min, so a busy
   session that is really working writes at least every 10 min.

Idle sessions (including the Idle fold) can never be stuck: `idle` never counts.

**Threshold for a silent task: 30 min** (`SILENT_S = STUCK_S`).
- Builds and test suites print as they go.
- A wait loop like the incident's prints nothing. But a session that needs to wait more than
  30 min for a known job is better served by being asked than by being left alone.
- Waking it costs one short turn: it checks, says "still waiting on X" and goes back to waiting.
- Missing it cost 16 hours.

The session's own idea was 1 h. If 30 min proves noisy (for example, a silent
`docker build`), the cheaper fix is two stages: wake the session at 30 min, and tell people
and the manager only if the task is still silent at 60 min.

**What happens in an episode.** An episode ends when the session writes again (not counting
our own message) or goes `idle`.
- The hub sends one message to the person and, for a worker, to its boss. The message names
  the command, how long it has been silent, and how to stop it: `claude stop <id>` for a bg
  session; for an interactive one, open it and stop the task.
- In the `shell` case the hub also messages the session itself, because that wakes it (see c).
- Overnight mode wakes a session whose only pending work is a silent task. `watch` sends
  "your background task <cmd> has printed nothing for 30 min; check whether it is stuck and
  stop it if so".
