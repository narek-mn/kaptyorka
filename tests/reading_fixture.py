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


MEASURE = """(() => {
  const lum = c => { const v = c.match(/[\\d.]+/g).slice(0, 3).map(Number).map(x => { x /= 255; return x <= 0.03928 ? x / 12.92 : Math.pow((x + 0.055) / 1.055, 2.4); });
    return 0.2126 * v[0] + 0.7152 * v[1] + 0.0722 * v[2]; };
  const ratio = (a, b) => { const x = lum(a), y = lum(b); return (Math.max(x, y) + 0.05) / (Math.min(x, y) + 0.05); };
  const texts = [...document.querySelectorAll("#log .msg:not(.sys) .text")];
  const t = texts.find(n => n.textContent.length > 1200) || texts[0];
  const cs = getComputedStyle(t);
  const p = t.querySelector("p, li") || t;
  const range = document.createRange(); range.selectNodeContents(p);
  const probe = document.createElement("span"); probe.textContent = "abcdefghijklmnopqrstuvwxyz ABCDEFGHIJKLMNOPQRSTUVWXYZ 0123456789 .,";
  probe.style.cssText = "position:absolute;visibility:hidden;white-space:nowrap;font:" + getComputedStyle(p).font; document.body.appendChild(probe);
  const avg = probe.getBoundingClientRect().width / probe.textContent.length; probe.remove();
  let bg = getComputedStyle(document.getElementById("log")).backgroundColor;
  if (bg === "rgba(0, 0, 0, 0)") bg = getComputedStyle(document.body).backgroundColor;
  const pcs = getComputedStyle(p);
  return { font_px: parseFloat(pcs.fontSize), line_height: Math.round(parseFloat(pcs.lineHeight) / parseFloat(pcs.fontSize) * 100) / 100,
           text_width_px: Math.round(t.getBoundingClientRect().width), chars_per_line: Math.round(p.getBoundingClientRect().width / avg),
           contrast: Math.round(ratio(pcs.color, bg) * 10) / 10, raw_markdown: /(^|\\n)(## |\\d\\. |- |\\|)/.test(t.textContent) || t.textContent.includes("```"),
           messages: texts.length, log_width: document.getElementById("log").clientWidth };
})()"""
