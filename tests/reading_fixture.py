"""A realistic chat for the page's reading tests and screenshots: bob's page talks with his
session api-dev over two days (agent-shaped markdown: a long status report, code, paths, a
table, a short ok, a file), and api-dev works together with the team machine's trainer and its
worker parser-tests (a group of three). The hub clock stands still."""
import time

from conftest import start_hub
from helpers import person
from kaptyorka.manager import ManagerStore
from kaptyorka.store import Store

REPORT = """## Status: tokenizer migration

Done so far, in order:

1. Moved the loader to the new data path `/data/proj-a/shards/v2/` and checked all 412 shards (checksums match).
2. Replaced the old tokenizer with the new one in `src/proj_a/text/tokenize.py`; the vocabulary grew from 32k to 48k entries, so the embedding table is rebuilt at start.
3. Ran the unit tests: **214 passed**, 3 skipped (the GPU-only ones), none failed.
4. Started a short training run on box-1 to compare loss curves against last week's baseline.

What I still need from you:

- Decide whether we keep the old checkpoints. They take 380 GB and nothing reads them since the switch.
- Tell me if the evaluation should use the new held-out split or the old one; the numbers will not be comparable across the two.

Risks: the first epoch is about 9% slower because of the bigger vocabulary. I think that is acceptable, but if the nightly job has a hard time limit it may need another 40 minutes. *Nothing is pushed yet*; the branch is `tokenizer-v2`."""

CODE = """Found the cause of the import error. In `src/proj_a/cli/main.py` the plugin loader runs before the config is read:

```python
def main(argv=None):
    plugins = load_plugins(Path("~/.config/proj-a/plugins").expanduser())  # too early
    config = read_config(argv)
    for p in plugins:
        p.setup(config)
```

The fix moves `load_plugins` after `read_config` and passes `config.plugin_dir`. Full path of the failing file on box-1: `/home/u/work/proj-a/.venv/lib/python3.12/site-packages/proj_a_plugins/very_long_module_name/registry.py`."""

TABLE = """Benchmark after the change (3 runs each, median):

| case | before | after | change |
|------|-------:|------:|-------:|
| load 1k docs | 4.1 s | 2.3 s | -44% |
| tokenize 1M lines | 61 s | 66 s | +8% |
| full test suite | 212 s | 198 s | -7% |

The tokenizer is slower as expected; everything else got faster."""

SHORT_LINES = [
    "ok", "Thanks, looking now.", "Yes, keep the old checkpoints for one more week, then delete them.",
    "Use the new held-out split and note it in the report.",
    "Pushed `tokenizer-v2`. CI is green on all four Python versions.",
    "Can you also check why the nightly job printed a warning about the cache folder?",
    "The cache folder warning comes from an old setting in `~/.config/proj-a/settings.toml`; I removed it and the warning is gone.",
    "Great work. Please write the summary for the team note.",
]


def build(tmp_path):
    """The store, bob's page url and the hub's stop()."""
    store = Store(tmp_path / "hub.db")
    t0 = time.time()
    store.clock = lambda: t0
    ManagerStore(store).set_enabled(False, "test")
    person(store, "alice", "admin")
    bob, btoken = person(store, "bob")
    store.project_register("proj-a", by="alice")
    store.grant_project("proj-a", bob, "write", "alice")
    store.heartbeat("box-2", [
        {"key": "b1", "tool": "claude-code", "project": "proj-a", "label": "api-dev"},
        {"key": "bw", "tool": "claude-code", "project": "proj-a", "label": "parser-tests"}],
        principal=store.authenticate(btoken), facts={"host": "box-2", "os_user": "bob"})
    _, code = store.team_machine_add("box-1", "alice")
    ttoken, _ = store.invite_redeem(code, "box-1", host="box-1", os_user="u")
    store.heartbeat("box-1", [{"key": "t1", "tool": "codex", "project": "proj-a", "label": "trainer"}],
                    principal=store.authenticate(ttoken), facts={"host": "box-1", "os_user": "u"})
    from test_working_together import said, worker
    worker(store, "bw", "b1", bob, name="parser-tests", ago=3 * 3600)
    # bob and api-dev over two days: 32 messages
    b1 = store.agent("b1").name
    day = 86400
    texts = [REPORT, CODE, TABLE] + SHORT_LINES
    msgs = []
    for i in range(16):
        ago = day + 7200 - i * 5400 if i < 8 else 5000 - (i - 8) * 500
        msgs.append(("web", SHORT_LINES[i % len(SHORT_LINES)] if i % 3 else
                     "Please continue with the next step and tell me what you find; " * (1 + i % 2), ago))
        msgs.append(("b1", texts[i % len(texts)], ago - 120))
    for who, text, ago in msgs:
        if who == "web":
            store.send_from_web("bob", b1, text, bob)
        else:
            store.send("b1", "bob", text)
        with store.lock:
            store.db.execute("UPDATE messages SET created=?, read_at=? WHERE id=(SELECT MAX(id) FROM messages)",
                             (t0 - ago, t0 - ago + 5))
    fid = store.file_add("loss-curve.csv", b"step,loss\n1,2.31\n2,1.92\n", bob)
    store.send("b1", "bob", "Here is the loss curve as a table.", file_id=fid)
    with store.lock:
        store.db.execute("UPDATE messages SET created=? WHERE id=(SELECT MAX(id) FROM messages)", (t0 - 600,))
    # the group: api-dev, trainer and the worker parser-tests
    for a, b, text, ago in (
            ("b1", "t1", "Is box-1 free this afternoon for a training run of about two hours?", 6000),
            ("t1", "b1", "Yes, box-1 is free until 18:00; the GPU is reserved for you.", 5700),
            ("b1", "t1", "Can you rerun the training with the new tokenizer on box-1?", 5400),
            ("t1", "b1", REPORT.replace("## Status: tokenizer migration", "## Training status"), 5000),
            ("b1", "bw", "Write tests for the parser edge cases: empty input, BOM, CRLF line endings.", 4600),
            ("bw", "b1", CODE, 4000),
            ("b1", "t1", "Thanks. Keep the GPU reserved until the comparison run is done.", 3600),
            ("t1", "b1", TABLE, 3000),
            ("b1", "bw", "Good catch. Apply the fix and add a test for the plugin order.", 2400),
            ("bw", "b1", "Done: fix applied, 2 tests added (`test_plugins_load_after_config`, `test_plugin_dir_from_config`).", 1800),
            ("t1", "b1", "Comparison run finished: loss 1.87 vs baseline 1.91 at step 20k. Details in the note.", 1200),
            ("b1", "t1", "ok", 1100),
            ("b1", "t1", "Great, write it up in the team note please, with the table from before.", 900)):
        said(store, a, b, text, ago)
    stop = start_hub(store)
    return store, f"http://127.0.0.1:{stop.port}/#t={btoken}", stop


def manager_chat(store):
    """The manager's chat as the admin alice reads it: the three sessions and bob's page write to
    the manager, it answers each, and alice writes to it herself (a run of two, then one more).
    Returns alice's page url fragment ("#t=...")."""
    import hashlib
    import secrets
    from test_working_together import said
    mtoken = ManagerStore(store).ensure_account("box-1", "alice")
    store.heartbeat("box-1", [{"key": "k:mgr", "tool": "claude-code", "project": "manager", "label": "manager"}],
                    principal=store.authenticate(mtoken), facts={"host": "box-1", "os_user": "u"})
    mgr = store.agent("k:mgr").name
    token = secrets.token_urlsafe(32)
    with store.lock:
        alice = store.db.execute("SELECT id FROM people WHERE name='alice'").fetchone()[0]
        store.db.execute("INSERT INTO devices (person_id, name, token_hash, created) VALUES (?,?,?,?)",
                         (alice, "alice-phone", hashlib.sha256(token.encode()).hexdigest(), store.clock()))
    for who, to, text, ago in (
            ("b1", mgr, "tokenizer-v2 is ready for review: 214 tests pass, the branch is pushed.", 3600),
            ("k:mgr", "b1", "Got it. parser-tests reviews it; I told it to start now.", 3540),
            ("t1", mgr, "The training run on box-1 finished: loss 1.87 against 1.91 for the baseline at step 20k. "
                        "The GPU is free again from now.", 2400),
            ("@bob", mgr, "Please give box-1 to parser-tests for an hour, it needs the GPU for the slow tests.", 1500),
            ("k:mgr", "bob", "Done: parser-tests has box-1 until 15:00. trainer knows.", 1440),
            ("bw", mgr, "Parser edge cases are covered: empty input, BOM and CRLF. 2 tests were failing on main "
                        "before my fix; both pass now. Waiting for api-dev to merge.", 1200),
            ("k:mgr", "bw", "Thanks. api-dev, please merge `parser-edge` after `tokenizer-v2`; both are approved.", 1140),
            ("@alice", mgr, "What is still open on proj-a today?", 600),
            ("@alice", mgr, "Short list please, most urgent first.", 570),
            ("k:mgr", "alice", "Three things, most urgent first:\n\n1. **Merge order**: `tokenizer-v2` first, then "
                               "`parser-edge`. api-dev does both once CI is green.\n2. The nightly job may need 40 more "
                               "minutes with the bigger vocabulary; nobody has changed its limit yet.\n3. Old "
                               "checkpoints (380 GB) are kept one more week, then deleted.", 500),
            ("@alice", mgr, "Raise the nightly limit to 3 hours and tell bob.", 200)):
        if who.startswith("@"):  # a person's page to the manager, as the hub keeps it
            with store.lock:
                store.db.execute("INSERT INTO messages (sender_key, sender_name, to_key, to_name, machine, tool, project, text, created)"
                                 " VALUES (?,?,'k:mgr',?,'','','',?,?)", ("web:" + who[1:], who[1:], mgr, text, store.clock()))
        elif to in ("alice", "bob"):  # the manager to a person's page
            with store.lock:
                store.db.execute("INSERT INTO messages (sender_key, sender_name, to_key, to_name, machine, tool, project, text, created)"
                                 " VALUES ('k:mgr',?,?,?,'','','',?,?)", (mgr, "web:" + to, to, text, store.clock()))
        else:
            said(store, who, "k:mgr" if to == mgr else to, text)
        with store.lock:
            store.db.execute("UPDATE messages SET created=?, read_at=? WHERE id=(SELECT MAX(id) FROM messages)",
                             (store.clock() - ago, store.clock() - ago + 5))
    return f"#t={token}"


# each bubble in the open chat: its side ("mine", "peer" or "" for the left), where it sits in
# the column, its colours and the contrast of its text on its own surface
SIDES = """(() => {
  const lum = c => { const v = c.match(/[\\d.]+/g).slice(0, 3).map(Number).map(x => { x /= 255; return x <= 0.03928 ? x / 12.92 : Math.pow((x + 0.055) / 1.055, 2.4); });
    return 0.2126 * v[0] + 0.7152 * v[1] + 0.0722 * v[2]; };
  const ratio = (a, b) => { const x = lum(a), y = lum(b); return Math.round((Math.max(x, y) + 0.05) / (Math.min(x, y) + 0.05) * 10) / 10; };
  const log = document.getElementById("log"), ls = getComputedStyle(log);
  const col = [...log.children].find(c => c.classList.contains("group")).getBoundingClientRect();
  return [...log.querySelectorAll(".group .msg > .bub")].map(b => {
    const g = b.closest(".group"), r = b.getBoundingClientRect(), cs = getComputedStyle(b), p = b.querySelector("p, li") || b;
    return { side: g.classList.contains("mine") ? "mine" : g.classList.contains("peer") ? "peer" : "", bg: cs.backgroundColor,
             left: Math.round(r.left - col.left), right: Math.round(col.right - r.right), width: Math.round(r.width), col: Math.round(col.width),
             contrast: ratio(getComputedStyle(p).color, cs.backgroundColor), head: !!g.querySelector(".head"),
             pane: log.clientWidth - parseFloat(ls.paddingLeft) - parseFloat(ls.paddingRight) };
  });
})()"""


MEASURE = """(() => {
  const lum = c => { const v = c.match(/[\\d.]+/g).slice(0, 3).map(Number).map(x => { x /= 255; return x <= 0.03928 ? x / 12.92 : Math.pow((x + 0.055) / 1.055, 2.4); });
    return 0.2126 * v[0] + 0.7152 * v[1] + 0.0722 * v[2]; };
  const ratio = (a, b) => { const x = lum(a), y = lum(b); return (Math.max(x, y) + 0.05) / (Math.min(x, y) + 0.05); };
  const texts = [...document.querySelectorAll("#log .msg:not(.sys) .text")];
  const t = texts.reduce((a, b) => b.textContent.length > a.textContent.length ? b : a, texts[0]);  /* the longest */
  const cs = getComputedStyle(t);
  const p = t.querySelector("p, li") || t;
  /* characters on each full line of the chat's paragraphs (a paragraph's last line left out), the median */
  const lines = [];
  [...document.querySelectorAll("#log .msg:not(.sys) .text p")].forEach(b => {
    /* a new line where a character sits half a line lower (inline code's box is a little higher) */
    const w = document.createTreeWalker(b, NodeFilter.SHOW_TEXT), half = parseFloat(getComputedStyle(b).lineHeight) / 2, got = [];
    let n, top = null;
    while ((n = w.nextNode())) for (let i = 0; i < n.length; i++) {
      const r = document.createRange(); r.setStart(n, i); r.setEnd(n, i + 1);
      const rc = r.getClientRects()[0]; if (!rc) continue;
      if (top === null || rc.top > top + half) { top = rc.top; got.push(0); }
      got[got.length - 1]++; }
    got.slice(0, -1).forEach(c => lines.push(c));
  });
  lines.sort((a, b) => a - b);
  let bg = "rgba(0, 0, 0, 0)";  /* the surface the text sits on: its bubble */
  for (let n = p; n && bg === "rgba(0, 0, 0, 0)"; n = n.parentElement) bg = getComputedStyle(n).backgroundColor;
  const pcs = getComputedStyle(p);
  return { font_px: parseFloat(pcs.fontSize), line_height: Math.round(parseFloat(pcs.lineHeight) / parseFloat(pcs.fontSize) * 100) / 100,
           text_width_px: Math.round(t.getBoundingClientRect().width), chars_per_line: lines.length ? lines[Math.floor(lines.length / 2)] : null,
           contrast: Math.round(ratio(pcs.color, bg) * 10) / 10, raw_markdown: /(^|\\n)(## |\\d\\. |- |\\|)/.test(t.textContent) || t.textContent.includes("```"),
           messages: texts.length, log_width: document.getElementById("log").clientWidth };
})()"""
