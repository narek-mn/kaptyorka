# Overnight tasks

A teammate gives a session a task and goes home. Nobody is there until morning.
kaptyorka keeps that session from getting stuck, keeps it inside safe limits,
and writes a morning report.

Status: built for Claude Code (see the README, "Overnight tasks").

## On and off

- **On by itself** when a prompt says "overnight task", "run this overnight",
  "while I sleep" or similar. The session shows one line:
  `kaptyorka: overnight mode is on for this task. To stop it: /hub overnight off`
- **On by hand:** `/hub overnight`. **Off by hand:** `/hub overnight off`
  (`kaptyorka: overnight mode is off`).
- **Off by itself** the moment a real person types in that session again:
  `kaptyorka: overnight mode is off: you are back`.
  Messages from the hub, the manager or kaptyorka do not count as a person.
- **Safety cap:** off after 16 hours, whatever happens. There is no other timer.

## What the developer sees at turn-on

The developer is still there, so the session shows the night rules once,
in a short list, and waits for a "yes" or for changes in plain words:

```
Overnight rules for this task:
  Allowed: everything you could do yourself, except:
  1. sudo, su, pkexec, doas                     blocked
  2. force push or history rewrite on main, develop   blocked
  3. deleting system folders, your home folder,
     other users' folders, or protected folders (~/recordings)  blocked
  4. hardware commands (motorctl, /dev/ttyUSB0) blocked
  Plus the team and project rules, and your own Claude Code permission rules.
Reply "ok", or change it, e.g. "you may move the motors".
```

If the developer says "you may move the motors", the session repeats it back
in one line: `Overnight: hardware commands are allowed for this task.`
That change is for this task only.

## The four blocked classes

Everything else is allowed, including reading, creating and editing files
anywhere the user can (logs, configs, assets outside the project).

| Class | What IS caught | What is NOT caught |
|---|---|---|
| 1. Admin rights | A command that runs `sudo`, `su`, `pkexec` or `doas`, also inside `&&`, `;`, pipes and `$( )`. | A program that uses admin rights inside it. |
| 2. Shared git history | `git push --force` / `-f` / `--force-with-lease` / `+branch`, and deleting a shared branch on the server; `rebase`, `reset` to an older commit, `commit --amend`, `filter-repo` while on a shared branch. Shared = `main`, `develop`, or the project's own list. | A git alias, or a tool that pushes on its own. |
| 3. System-wide deletes | Deleting `/`, `/etc`, `/usr`, `/boot`, `/var`, `/opt` and other system folders, the home folder itself, other users' home folders; deleting or overwriting anything in the machine's protected-folders list (`rm`, `rmdir`, `find -delete`, `shred`, `mv`, `rsync --delete`, `>` into a file, the Write/Edit tools). | Ordinary deletes are allowed: a temp file, an old log, a build folder. |
| 4. Real hardware | A command that names something on the hardware list: a command (`motorctl`), a script path (`./scripts/home_axes.sh`), or a device path (`/dev/ttyUSB0`). The developer writes this list per project or per machine. kaptyorka never guesses it and has nothing built in. | A command that is not on the list. |

**A script or program that does these things inside it is not caught.**
The hook sees the command line, not what a program does after it starts.

When the hook cannot read a command well enough, and it might touch a blocked
class (`eval`, `bash -c "$CMD"`, `xargs rm`, a command name built from
variables), it blocks it and notes it. It never guesses "allow".

The protected-folders list is always blocked, unless the developer allows a
folder for this task.

## Rules that already exist still win

Overnight mode only adds limits. It never loosens anything.

- **Team and project rules** (the hub's rules that every session reads) still
  apply. If a team or project rule forbids something, it stays forbidden,
  even if the night rules allow it. Claude follows these rules by reading
  them; the hook does not check them.
- **The developer's Claude Code permission settings** keep priority: deny
  rules, ask rules and other hooks. Overnight mode only answers a question
  that would otherwise wait for a person. A deny rule stays a deny. A call
  that an ask rule sends to a person is skipped and noted, not allowed.
- **Hub permissions:** every hub action (send, work on another machine,
  notes, rules) is still checked against the person's project role.
  Overnight mode gives nothing on the hub.
- kaptyorka never starts a session with `--dangerously-skip-permissions`
  and never edits the developer's permission settings.

**When rules clash** (the overnight list allows X, but a team or project rule
forbids it, or a deny rule in Claude Code settings blocks something the task
needs), the safer rule wins and the session skips X. Then:

- Claude tells kaptyorka about the skip with a short kaptyorka call, because
  the hook cannot read rules written as text. kaptyorka writes it to the local log.
- **Team sessions:** kaptyorka also sends the hub a short event with fixed
  text and only the project name (no secrets, no paths). The manager gets
  it and tells that developer once, in plain words:
  `Your overnight task allowed X, but the project rule "No pushes on Friday"
  forbids it, so the session skipped it.`
- **Personal sessions:** the local log only. Nothing goes to the hub or the manager.
- In both cases the clash is in the morning report.

## What happens when the session would get stuck

| Situation | What kaptyorka does |
|---|---|
| Claude asks a question with options | Says no to the question: "No human until morning. Take the option you recommend and note it." The choice goes into the report. |
| A permission prompt inside the night rules | Allows it. |
| A permission prompt outside the night rules (or an ask rule) | Says no: "Skip this and note it." It goes into the report. |
| An MCP server asks for input | Declines it and notes it. |
| Claude stops but the task is not done | Sends "continue" at most 3 times in a row. |
| The session crashed | Starts it again with `claude --resume` in tmux, at most 2 times a night. |
| Usage or rate limit | Waits (Claude Code itself waits for a subscription limit). If it does not continue, kaptyorka sends "continue" after the reset, waiting at most 6 hours. |

## Morning report

```
Overnight task: "move the parser to the new API"  (box-1, proj-a)
22:40 to 03:15, finished.

Done
  - parser moved, 214 tests pass, committed on overnight-parser (not pushed)
Decisions made for you
  - used option "keep the old flag" (recommended) for "Remove --legacy?"
Skipped, needs your yes
  - git push --force to main (class 2)
  - rm -rf ~/recordings/old (protected folder)
Rule clashes
  - you allowed moving the motors, but the project rule
    "No hardware without a person" forbids it: skipped
Events
  - 01:10 usage limit, continued 02:05
```

- **Team sessions:** a page on the hub, plus a hub message to that person only.
- **Personal sessions:** a local file only. Nothing goes to the hub.

## Which tools

| Tool | Overnight mode |
|---|---|
| Claude Code | Everything on this page. |
| Codex, Cursor, OpenCode | Not yet. `/hub overnight` says "not yet for this tool"; `kaptyorka doctor` and the README say the same. |

## Known limits

- Claude Code's hook does not say who sent a prompt. kaptyorka remembers each
  message it delivers, and also reads the session's own record of the prompt
  when it is already written. Any other prompt counts as a person, which only
  turns overnight mode off (the safe side). The report says when and why.
- A hook that crashes or runs too long lets the command through. kaptyorka's
  hook catches its own errors and says no while overnight mode is on.
- A crashed session restarted from tmux may miss settings that were only in
  the developer's terminal (for example a key in the shell). The report
  says when this restart happened.

## Decided

1. A call that the developer's own ask rule sends to a person is skipped and noted.
2. Similar phrases ("while I sleep", "run this overnight", "by the morning") also turn it on.

## Sources

Checked against Claude Code 2.1.329 (`claude --version`) and these docs, read 2026-11-20:

- Hooks reference: https://code.claude.com/docs/en/hooks
  (PreToolUse `permissionDecision` allow/deny/ask, deny reason shown to Claude;
  AskUserQuestion answered only by allow + `updatedInput.answers`, denied by
  deny; PermissionRequest `decision.behavior` allow/deny + `message`, fires only
  when a person would be asked; UserPromptSubmit `prompt`, `systemMessage`,
  `additionalContext`, also fires for cross-session messages, scheduled tasks,
  subagent reports and Claude Code's own turns, with no field for who sent it;
  Stop `decision: "block"` + `reason`, `stop_hook_active`, 8-continuation cap;
  StopFailure `error: "rate_limit"`; Notification `quota_auto_resume_*`;
  Elicitation decline; SessionEnd)
- Permissions, "Precedence among rules, hooks, and mods": https://code.claude.com/docs/en/permissions
  (a hook's allow never overrides a deny rule; an ask rule still prompts
  after a hook's allow; a hook's deny wins over allow rules)
- Permission modes, "Actions no mode auto-approves": https://code.claude.com/docs/en/permission-modes
- Wait for a usage limit to reset: https://code.claude.com/docs/en/interactive-mode
- Settings, `autoContinueAtUsageLimit`: https://code.claude.com/docs/en/settings-reference
- Sessions, `--resume`: https://code.claude.com/docs/en/sessions
- Cross-session messaging (how hub pushes arrive): https://code.claude.com/docs/en/cross-session-messaging
- kaptyorka code: `src/kaptyorka/adapters/claude.py` (`deliver` sends hub messages to
  the session's inbox socket as user messages), `src/kaptyorka/store.py`
  (pushed text starts `Kaptyorka message from`; knocks and doors as written),
  `src/kaptyorka/launch.py` (workers start with `claude --bg`)
