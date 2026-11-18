# Machine coordination (0.9.14)

**Approved by the owner, 2026-11-18.** The hub knows who is using a shared machine and how
heavily; the manager decides when a second person wants to work there.

## What changes where
- **`usage.py` (new).** Reads use per OS user on the bridge: CPU (cores used, averaged over the
  minute between two reads of `/proc/<pid>/stat`), memory (RSS), and, where `nvidia-smi` is on
  PATH, each GPU's busy % and memory plus which OS user's processes hold GPU memory
  (`--query-compute-apps=gpu_uuid,pid,used_memory`, pid mapped to its owner through `/proc`).
  Only numbers per OS user leave the machine: no pids, process names or command lines. It also
  holds the "heavy" rule and the one-line descriptions, so the thresholds live in one place.
- **`bridge.py`.** The facts build (once a minute, as today) adds `use`; the heartbeat sends its
  `age`, like git's.
- **`workers.py`.** `clean_facts` keeps a checked `use`. `host_use(host)` gives the freshest use
  reported for a host, with each OS user mapped to the people whose devices run as it there.
  `machine_rows` drops `use` for everyone but the admin.
- **`hub.py` `work`.** Held for the manager only when another person is in the same folder (as
  before) or uses the machine heavily; the notice carries the numbers. Light use starts at once,
  so people share a machine without waiting. Someone present with no fresh report: it starts and
  the manager is told for information. Without a manager, it starts as before.
- **`workers.py` `choose`.** When there is a choice, a machine nobody else uses heavily wins
  over one that someone does (after own account and free folder, before load).
- **`hub.py` tick.** A person's first session on a host (none there for 10 minutes before) while
  someone else's use there is heavy: the manager is told once, to warn that person in one line
  and tell the admin. Nothing is refused.
- **`manager.py`.** `text_busy` carries the load; a new `text_in_use` and `text_arrival`; one
  bullet in MANAGER_SECTION.

## Heavy (usage.py)
A person holds a GPU when their processes use at least 1 GB of its memory. Their use of a machine
is heavy when any of these is true:
- they hold at least half of its GPUs;
- a GPU they hold is at least 80% busy or 80% full;
- they use at least half of its CPU cores;
- they use at least half of its memory.

The manager gets the numbers and this rule of thumb, and makes the call. No settings.

## Edge cases
- **One person, two OS users:** both accounts' numbers add up under that person; their own use
  never holds their own work.
- **The manager and workers:** the manager account is nobody: its sessions never make a machine
  in use, and it never arrives. A hub-started worker counts as its boss's presence; it never
  triggers an arrival warning (the work path decided it). Numbers belong to the OS account, so a
  worker on someone's shared account counts in that account's numbers.
- **An account no one joined from** (root, a service): shown as "account X", so heavy use by it
  still counts.
- **No GPU / no nvidia-smi:** CPU and memory only; the GPU rule does not apply.
- **nvidia-smi hangs or fails:** 5 s timeout; GPU numbers are left out and not asked again for
  10 minutes.
- **Stale report:** use older than 3 minutes (the bridge's age plus the time since the hub heard
  it) is ignored, as if unknown: never a hold, but the manager hears when work starts next to
  someone it could not check.
- **First read:** CPU needs two reads; the first report has memory and GPU only.
- **`/proc` hidden (hidepid):** only what the bridge's account can see is reported.
- **Privacy:** personal sessions still never reach the hub; arrivals are seen only for
  team-project sessions. Members never get `use` on `/api/machines`.
