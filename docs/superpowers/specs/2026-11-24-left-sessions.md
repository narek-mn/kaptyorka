# Left-behind sessions: research and smallest design

Research only; nothing built. Checked on this laptop with Claude Code 2.1.329 (this session) and
2.1.331 (`claude --version`), real `claude` in a scratch folder beside the repo, cleaned up after.
Each claim says how it was checked: **[ran]** an experiment here, **[read]** files on disk or our
code, **[inferred]** not verified.

## 1. Why `active` stays fresh on a session nobody touched

`idle_s` comes from `ClaudeAdapter._title`: the transcript file's **mtime** [read,
adapters/claude.py]. Claude Code itself keeps writing to an idle session's transcript, so mtime is
not "the person or the model did something":

- An idle interactive session (last turn 13:08Z) got an `away_summary` system entry 3 min later,
  then `compact_boundary` + an `isCompactSummary` user entry + "Compacted while idle, before the
  prompt cache expired" 56 min later (14:04Z), then an untimestamped metadata block
  (`last-prompt`, `custom-title`, `ai-title`, `agent-name`, `agent-color`, `mode`,
  `permission-mode`, `session-owner`, `atis-latch`) about an hour after that (file mtime 14:59Z)
  [read, one real transcript]. That block appears 154 times in one long transcript, so Claude Code
  appends it again and again [read]. What triggers the later block is **not verified**.
- A quiet `--bg` session watched for 4 minutes got no writes at all; `claude agents` and
  `claude logs` do not touch the transcript either [ran]. So the writes are Claude Code's own idle
  jobs (summary, idle compaction, metadata), not our status line or hooks.
- A hub push writes a `queue-operation` and then a real turn (user + assistant) [read: `last_write`
  already skips queue-operation]. Such a turn counts as real activity.
- `statusUpdatedAt`/`updatedAt` in `~/.claude/sessions/<pid>.json` also moved with the idle
  compaction (18:04 local = 14:04Z, not the last turn at 13:08Z) [read]: not truthful either.
- `write_s` (`last_write`: the newest timestamped line except queue-operation) also counts the
  idle `system` and compaction entries [read]: same drift, smaller.

**Truthful measure (proposal):** the timestamp of the newest transcript line with `type` user
(`isMeta` not true, `isCompactSummary` not true, `isSidechain` not true) or assistant
(`isSidechain` not true), read from the tail as `last_write` already does. Its subagents'
transcripts count too (subagents/*.jsonl, as `_wrote` does). It needs one more filter in the
existing tail read, so it costs nothing extra. Use it for `idle_s`; `write_s` keeps its meaning.

- OpenCode 2: `time.updated` changes on activity [inferred]. Since 0.9.32 the window note
  supplies the session; idle time can stay `None` (no fold) until checked.
- Codex: `updatedAt` of the thread [read, adapter]. Whether Codex updates it while idle is **not
  verified**.

## 2. Parentage

- **`derived-from`** [ran: three launches from a session's Bash, plus 26 transcripts on disk]:
  - `claude --bg` from inside a session: `{"via":"launch","id":<parent>,"started":<child>}`.
  - `claude -p`: `"via":"subprocess"`, transcript `entrypoint: sdk-cli`; the session file is gone
    when it ends.
  - Any other child process that inherits the env (the manager in tmux, started by a bridge that
    a session started): `"via":"subprocess"` [read].
  - Other `via` values seen: `resume` (id = itself), `compact`. Not "parent".
  - The **same `claude --bg` with the parent's `CLAUDE_*` env removed has no `derived-from`**
    [ran]. So parentage comes from the environment (`CLAUDE_CODE_SESSION_ID`), not from the process
    tree. A bridge running as a systemd service has no such env, so its workers have no parent
    entry [inferred]; the hub knows their boss anyway.
- **The parent's own list**: every session has `CLAUDE_CODE_CHILD_NOTES_DIR`
  (`/tmp/claude-<uid>/<project>/<session id>/started/`). Claude Code adds one empty file,
  `<child session id>.<random>`, for each child it records (`--bg` and `-p` both) [ran: two new
  files after E1/E2, none for the env-stripped E3]. This is the cheapest list of what a session
  started.
- **Subagents** of the Agent tool are not sessions: their transcripts are under
  `<transcript>/subagents/`, with no `~/.claude/sessions` file (528 subagent files, 8 session
  files) [read]. They never reach the hub as sessions.
- **Reliability across 2.1.33x**: seen on 2.1.329 and 2.1.331 only [ran/read]. It is
  undocumented, so treat it as a hint.
- **OpenCode**: child sessions have `parentID`, and `_v2_newest` skips them [read]. The plugin
  note lists only the open route's root [read]. Claude Code sessions started by OpenCode (the
  wt-678 case) carry no `derived-from`, since OpenCode is not a Claude session [inferred]. Mark
  them by process ancestry: an `opencode` process among the session pid's ancestors (the bridge
  already builds `children()` from /proc) [inferred, not run].

## 3. Trash that reaches the hub, and how to tell it cheaply

| Kind | Mark (in the bridge) | Checked |
|---|---|---|
| `claude -p` one-shot | transcript `entrypoint: sdk-cli` (also `derived-from via subprocess`) | ran |
| Helper session started by another session | `derived-from` with a parent id | ran |
| Started by another tool (OpenCode, scripts) | ancestor process is a tool, or folder under `/tmp` | inferred |
| Lived under N min with no hub message | the hub knows `started`, `last_seen` and messages | read (store) |
| Left idle for days | truthful idle time (part 1) over a day | read |

A finished `--bg` job whose process ended is already dropped: the adapter checks that the pid is
alive and the socket exists [read]. A job that is idle but still alive stays listed: that is the
"left" case.

## 4. Closing

- `claude stop <short id>` then `claude rm <short id>` end a `--bg` session [ran]. An
  interactive terminal session can only be ended by its owner.
- The parent's Stop hook runs with the parent's env [inferred; our Bash tools have it]. It can
  list `CHILD_NOTES_DIR`, keep the ids that are still live in `~/.claude/sessions` (pid alive,
  `kind: bg`), and block the stop once: "you started kleft-a (7c7105cb), still running: stop it
  (`claude stop 7c7105cb`) or say in one line why it must keep running". That is a directory
  listing and the session-file scan `offhub.session` already does, well inside the 3 s hook budget.
  It can be done like the done check: once per stop sequence, honouring `stop_hook_active`.

## Smallest design

- **(a) Truthful idle time.** `idle_s` from the last real user or assistant line (part 1). The
  existing Idle fold (over a day) then catches left sessions. Once per session per day, the hub
  sends the owner one line: "3 sessions of yours on box-1 have done nothing for 5 days: a, b, c;
  close them or ignore this".
- **(b) Children under their parent.**
  - The bridge reports `parent` (the `derived-from` id with `via` launch or subprocess) and
    `oneshot` (entrypoint sdk-cli).
  - The page shows a child under its parent, as workers are shown.
  - The hub never lists a child or one-shot session that lived under 2 minutes and sent or got no
    hub message.
- **(c) Parent's Stop check.** In the Claude Code Stop hook: block once with the list of live `bg`
  children from `CHILD_NOTES_DIR`, as in part 4. Never in overnight mode's own continue logic;
  never for workers the hub started (the hub closes those).
- **(d) Parent gone, child alive.** The row says "left by <parent name>". The owner hears it once,
  with `claude stop <id>`. Auto-close is safe only for a `--bg` child with `derived-from via
  launch`, idle (truthful) over a day, whose parent session has ended, and with no hub message in
  that time: then the bridge runs `claude stop` + `claude rm` and tells the owner. Never for
  interactive sessions, workers or the manager.
