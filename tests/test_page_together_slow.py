"""The page in a real headless Chrome: a member's list by machine, Working together (a group with
a worker, a hidden partner, the group's conversation, the chat header's partners), and a list
that holds still when the feed flickers. Run with: pytest -m slow tests/test_page_together_slow.py
Screenshots go to KAPTYORKA_SHOTS."""
import json
import os
import time
from pathlib import Path

import pytest

from chrome import chrome, chrome_path
from conftest import start_hub
from helpers import person
from kaptyorka.manager import ManagerStore
from kaptyorka.store import Store, session_id
from test_working_together import both, said, worker

pytestmark = [pytest.mark.slow,
              pytest.mark.skipif(chrome_path() is None, reason="no Chrome or Chromium installed")]

ROW = '[...document.querySelectorAll("#agents .row")].find(r => r.querySelector(".name")?.textContent === %s)'


def row(name):
    return ROW % json.dumps(name)


@pytest.fixture
def team(tmp_path):
    """bob (write on proj-a) with sessions on box-2 and box-3, the team machine box-1 in proj-a,
    and carol (her session hidden from bob). bob's api-dev works with the team machine's
    trainer, and started a worker; his docs session talks to carol's. The hub's clock stands
    still, so every session stays online for the whole test."""
    store = Store(tmp_path / "hub.db")
    t0 = time.time()
    store.clock = lambda: t0
    ManagerStore(store).set_enabled(False, "test")
    person(store, "alice", "admin")
    bob, btoken = person(store, "bob")
    carol, ctoken = person(store, "carol")
    store.project_register("proj-a", by="alice")
    store.project_register("proj-c", by="alice")
    store.grant_project("proj-a", bob, "write", "alice")
    store.grant_project("proj-c", carol, "write", "alice")

    def beat(token, host, user, sessions):
        store.heartbeat(host, sessions, principal=store.authenticate(token),
                        facts={"host": host, "os_user": user})

    beat(btoken, "box-2", "bob", [
        {"key": "b1", "tool": "claude-code", "project": "proj-a", "label": "api-dev", "activity": "busy"},
        {"key": "bw", "tool": "claude-code", "project": "proj-a", "label": "parser-tests", "activity": "busy"},
        {"key": "b2", "tool": "codex", "project": "proj-a", "label": "docs"},
        {"key": "b4", "tool": "codex", "project": "proj-a", "label": "migrations"}])
    beat(store.device_add(bob, "box-3"), "box-3", "bob", [
        {"key": "b3", "tool": "opencode", "project": "proj-a", "label": "ui-review"}])
    _, code = store.team_machine_add("box-1", "alice")
    ttoken, _ = store.invite_redeem(code, "box-1", host="box-1", os_user="u")
    beat(ttoken, "box-1", "u", [
        {"key": "t1", "tool": "claude-code", "project": "proj-a", "label": "trainer", "activity": "busy"},
        {"key": "t2", "tool": "codex", "project": "proj-a", "label": "eval-runs"}])
    beat(ctoken, "box-4", "carol", [{"key": "c1", "tool": "codex", "project": "proj-c", "label": "billing"}])
    worker(store, "bw", "b1", bob, name="parser-tests", ago=450)
    for a, b, text, ago in (  # earlier work first: a link needs 4 real messages each way over 15 min
            ("b1", "t1", "Is the training data for proj-a on the new disk yet?", 2400),
            ("t1", "b1", "Yes, copied this morning, 2.1 TB, checksums match.", 2350),
            ("b1", "t1", "Good. Please switch the loader to the new path then.", 2000),
            ("t1", "b1", "Switched; a dry run reads all shards without errors.", 1900),
            ("b1", "t1", "Next I will change the tokenizer; wait for my message.", 1500),
            ("t1", "b1", "Waiting. The GPU stays reserved for us until tonight.", 1450),
            ("b2", "c1", "Hello, I am writing the export docs for proj-a.", 2400),
            ("c1", "b2", "Hi, the export lives in the billing service.", 2300),
            ("b2", "c1", "Does it support CSV as well as the JSON format?", 1800),
            ("c1", "b2", "Both, CSV since last month; JSON is the default.", 1700),
            ("b2", "c1", "And is there a size limit per export file?", 1200),
            ("c1", "b2", "Yes, 50 MB; bigger exports are split in parts.", 1100),
            ("b1", "t1", "Can you rerun the training with the new tokenizer?", 600),
            ("t1", "b1", "Started. First epoch in about 4 minutes.", 560),
            ("b1", "bw", "Write tests for the parser edge cases: empty input, BOM, CRLF.", 400),
            ("bw", "b1", "Done: 6 tests added, 2 of them fail on CRLF. Fixing now.", 240),
            ("t1", "b1", "Epoch 1 done, loss 1.92. Continuing.", 130),
            ("b2", "c1", "Which page documents the export?", 300),
            ("c1", "b2", "The billing guide, section 4.", 280)):
        said(store, a, b, text, ago)
    store.send_from_web("bob", store.agent("b1").name, "How is the run going?", bob)
    stop = start_hub(store)
    try:
        yield store, f"http://127.0.0.1:{stop.port}/#t={btoken}", bob
    finally:
        stop()
        store.close()


def shots(tmp_path):
    return Path(os.environ.get("KAPTYORKA_SHOTS") or tmp_path)


def test_member_list_by_machine_and_working_together(team, tmp_path):
    store, url, _ = team
    out = shots(tmp_path)
    wide = "document.documentElement.scrollWidth <= document.documentElement.clientWidth"
    for w, h, tag in ((1440, 900, "desktop"), (390, 844, "phone")):
        with chrome(w, h) as page:
            page.go(url)
            page.wait('!!document.querySelector("#agents .wt-head")')
            # under the manager: Working together, one group, its title line
            order = page.js('[...document.querySelectorAll("#agents > *")].map(n => n.dataset.key || n.id || n.className)')
            assert order[:3] == ["mgr", "wt-cap", "grp:g" + session_id("b1")[1:]], order
            assert page.js('document.querySelector("#agents .wt-head").textContent') == \
                "proj-a· 3 sessions· active 2m ago"
            names = page.js('[...document.querySelector("#agents .wt-rows").querySelectorAll(".row .name")].map(n => n.textContent)')
            assert names == ["api-dev", "parser-tests", "trainer"], names
            assert page.js(f'{row("parser-tests")}.classList.contains("wt-sub")')  # under its boss
            # bob's docs talk to carol's session: a group of its own with "another session"
            others = page.js('[...document.querySelectorAll("#agents .wt-other .name")].map(n => n.textContent)')
            assert others == ["another session"]
            text = page.js("document.body.innerText")
            for secret in ("billing", "carol", "box-4", "proj-c", "section 4"):
                assert secret not in text, secret
            # the rest by machine: box-1 (team), box-2, box-3; no session twice
            heads = page.js('[...document.querySelectorAll("#agents .g-head")].map(h => h.textContent)')
            assert heads == ["bob@box-21", "bob@box-31", "u@box-1Team1"], heads
            listed = page.js('[...document.querySelectorAll("#agents .row .name")].map(n => n.textContent)')
            assert len(listed) == len(set(listed)), listed
            assert page.js(wide)
            page.screenshot(out / f"together-list-{tag}.png")
            # a machine folds, and stays folded after a reload
            page.js('document.querySelector("#agents .g-head[data-key=\\"g:m:bob@box-3\\"]").click()')
            page.wait(f'!({row("ui-review")})')
            page.js("window.before = 1; location.reload()")
            page.wait('!window.before && !!document.querySelector("#agents .wt-head")')
            assert page.js(f'!({row("ui-review")})') and page.js(f'!!({row("eval-runs")})')
            page.js('document.querySelector("#agents .g-head[data-key=\\"g:m:bob@box-3\\"]").click()')
            page.wait(f'!!({row("ui-review")})')
            # a session in a group: who it works with, at the top of its chat
            page.js(f'{row("api-dev")}.click()')
            page.wait('!document.getElementById("chat-with").hidden')
            chips = page.js('[...document.querySelectorAll("#chat-with .with")].map(c => c.textContent)')
            assert chips == ["parser-tests", "trainer"], chips
            assert page.js('document.getElementById("chat-with").firstChild.textContent') == "Working with"
            page.wait('!!document.querySelector("#log .working")')
            time.sleep(0.5)
            assert page.js(wide)
            page.screenshot(out / f"together-chips-{tag}.png")
            page.js('[...document.querySelectorAll("#chat-with .with")].find(c => c.textContent === "trainer").click()')
            assert page.wait('document.getElementById("chat-name").textContent') == "trainer"
            if w < 720:
                page.js('document.getElementById("chat-back").click()')
                page.wait('getComputedStyle(document.querySelector("#home .side")).display !== "none"')
            # the title line opens the group's conversation: who wrote to whom, read only
            page.js('document.querySelector("#agents .wt-head").click()')
            page.wait('document.querySelectorAll("#log .group").length >= 3')
            assert page.js('document.getElementById("chat-name").textContent') == "proj-a"
            assert page.js('document.getElementById("chat-sub").textContent') == "3 sessions · active 2m ago"
            assert page.js('document.getElementById("compose").hidden') and page.js('document.getElementById("read-only").hidden')
            heads = page.js('[...document.querySelectorAll("#log .group .head")].map(h => h.querySelector("b").textContent + ">" + (h.querySelector(".to .to-n")?.textContent || ""))')[-5:]
            assert heads == ["api-dev>trainer", "trainer>api-dev", "api-dev>parser-tests", "parser-tests>api-dev",
                             "trainer>api-dev"], heads
            assert "How is the run going?" not in page.js('document.getElementById("log").textContent')
            assert page.js('document.querySelector("#agents .wt-head").classList.contains("on")') or w < 720
            assert page.js(wide)
            time.sleep(0.5)
            page.screenshot(out / f"together-conversation-{tag}.png")
            if w < 720:
                page.js('document.getElementById("chat-back").click()')
                page.wait('getComputedStyle(document.querySelector("#home .side")).display !== "none"')
                assert page.js('!!document.querySelector("#agents .wt-head")')


FAKE = """(() => {
  const real = window.fetch;
  window.__script = null; window.__polls = 0; window.__errs = []; window.addEventListener("error", e => window.__errs.push(String(e.message)));
  window.fetch = function (u, o) {
    if (String(u) === "/api/feed" && window.__script) {
      window.__polls++;
      return Promise.resolve(new Response(JSON.stringify(window.__script(window.__polls)), { status: 200 }));
    }
    return real.apply(this, arguments);
  };
})()"""


def test_list_holds_still_when_the_feed_flickers(team, tmp_path):
    """A session missing for one poll keeps its row (the same node, in its place) and its group;
    a renamed one keeps its open chat; a new message between two sessions pulses their rail
    and redraws neither row; the "is working" line is one line, not risen again."""
    store, url, _ = team
    with chrome() as page:
        page.call("Page.addScriptToEvaluateOnNewDocument", source=FAKE)
        page.go(url)
        page.wait('!!document.querySelector("#agents .wt-head")')
        page.js('window.__base = null; fetch("/api/feed", { headers: { Authorization: "Bearer " + localStorage.getItem("kaptyorka.token") } })'
                '.then(r => r.json()).then(f => { window.__base = f; })')
        page.wait("!!window.__base")
        page.js(f'{row("api-dev")}.click()')
        page.wait('!!document.querySelector("#log .working")')
        # tag the live nodes: a redraw that re-creates them loses the tag
        page.js(f'{row("trainer")}.__tag = 1; {row("ui-review")}.__tag = 2; {row("api-dev")}.__tag = 3;'
                'document.querySelector("#log .working").__tag = 4;')
        page.js("""window.__script = n => {
          const f = JSON.parse(JSON.stringify(window.__base));
          const t1 = f.agents.find(a => a.label === "trainer"), b3 = f.agents.find(a => a.label === "ui-review");
          if (n === 1 || n === 3) {  // the trainer and ui-review miss a poll; the group with them
            f.agents = f.agents.filter(a => a !== t1 && a !== b3);
            f.groups = f.groups.filter(g => !g.members.some(m => m.id === t1.id));
          }
          if (n >= 4) {  // a message passes inside the group
            const g = f.groups.find(g => g.members.some(m => m.id === t1.id));
            g.last_id = g.last_id + n;
          }
          return f;
        }""")
        page.wait("window.__polls >= 1")
        time.sleep(0.3)
        assert page.js(f'{row("trainer")}?.__tag') == 1 and page.js(f'{row("ui-review")}?.__tag') == 2
        assert page.js(f'!!{row("trainer")}.closest(".wt-rows")')  # still in its group, not moved below
        page.wait("window.__polls >= 3")
        time.sleep(0.3)
        assert page.js(f'{row("trainer")}?.__tag') == 1 and page.js(f'{row("api-dev")}?.__tag') == 3
        page.wait("window.__polls >= 5")
        time.sleep(0.3)
        assert page.js('document.querySelectorAll("#log .working").length') == 1
        assert page.js(f'{row("trainer")}?.__tag') == 1  # a pulse is a class on the rail, not a redraw
        page.wait('!!document.querySelector("#agents .wt-rows.pulse")')
        # gone for longer than the grace: the row goes, and the group without it
        page.js("""window.__script = n => {
          const f = JSON.parse(JSON.stringify(window.__base));
          const b3 = f.agents.find(a => a.label === "ui-review");
          f.agents = f.agents.filter(a => a !== b3);
          const a = f.agents.find(a => a.label === "docs"); a.name = a.name + "-3";  // a new name
          return f;
        }""")
        page.js(f'{row("docs")}.click()')
        page.js('document.getElementById("text").value = "half typed"')
        start = page.js("window.__polls")
        page.wait(f"window.__polls >= {start + 4}", timeout=15)
        time.sleep(0.3)
        assert page.js(f'!({row("ui-review")})')
        assert page.js('document.getElementById("chat-name").textContent') == "docs"
        assert page.js('document.querySelector("#agents .row.on .name").textContent') == "docs"
        assert page.js('document.getElementById("text").value') == "half typed"
        # the working line: one, and a chat redraw does not make it rise again
        page.js(f'{row("api-dev")}.click()')
        page.wait('!!document.querySelector("#log .working")')
        start = page.js("window.__polls")
        page.js("""window.__script = (() => { const f0 = JSON.parse(JSON.stringify(window.__base)); return n => {
          const f = JSON.parse(JSON.stringify(f0)), b1 = f.agents.find(a => a.label === "api-dev");
          const m = f.messages.filter(m => m.from_web).pop();
          f.messages.push(Object.assign({}, m, { id: 100000 + n, text: "still there? " + n }));
          return f; }; })()""")
        page.wait(f"window.__polls >= {start + 3}")
        time.sleep(0.3)
        assert page.js('document.querySelectorAll("#log .working").length') == 1
        assert page.js('document.querySelector("#log .working").classList.contains("kept")')
        assert page.js('document.querySelector("#log .working .what").textContent') == "is working"


def test_admin_mine_by_machine_and_everyone(tmp_path):
    """The admin's Mine shows a group one of their sessions is in, with the other person's
    session; Everyone keeps its person headers under Working together, no session twice."""
    store = Store(tmp_path / "hub.db")
    t0 = time.time()
    store.clock = lambda: t0
    ManagerStore(store).set_enabled(False, "test")
    alice, atoken = person(store, "alice", "admin")
    _, ctoken = person(store, "carol")
    store.heartbeat("box-2", [{"key": "a1", "tool": "codex", "project": "proj-a", "label": "api-dev"},
                              {"key": "a3", "tool": "codex", "project": "proj-a", "label": "migrations"}],
                    principal=store.authenticate(atoken), facts={"host": "box-2", "os_user": "alice"})
    store.heartbeat("box-3", [{"key": "a2", "tool": "codex", "project": "proj-a", "label": "ui-review"}],
                    principal=store.authenticate(store.device_add(alice, "box-3")),
                    facts={"host": "box-3", "os_user": "alice"})
    store.heartbeat("box-4", [{"key": "c1", "tool": "codex", "project": "proj-c", "label": "billing"},
                              {"key": "c2", "tool": "codex", "project": "proj-c", "label": "reports"}],
                    principal=store.authenticate(ctoken), facts={"host": "box-4", "os_user": "carol"})
    both(store, "a1", "c1", ago=50)  # real work between them
    stop = start_hub(store)
    try:
        with chrome() as page:
            page.go(f"http://127.0.0.1:{stop.port}/#t={atoken}")
            page.wait('!!document.querySelector("#agents .wt-head")')
            names = page.js('[...document.querySelector("#agents .wt-rows").querySelectorAll(".row .name")].map(n => n.textContent)')
            assert names == ["api-dev", "billing"], names
            heads = page.js('[...document.querySelectorAll("#agents .g-head")].map(h => h.textContent)')
            assert heads == ["alice@box-21", "alice@box-31"], heads
            assert page.js(f'!({row("reports")})')  # carol's other session: not in Mine
            page.js('document.querySelector("#h-scope [data-scope=everyone]").click()')
            page.wait('!!document.querySelector("#agents .g-head[data-key=\\"g:p:carol\\"]")')
            assert page.js('!!document.querySelector("#agents .wt-head")')
            page.js('document.querySelector("#agents .g-head[data-key=\\"g:p:carol\\"]").click()')
            page.wait(f'!!({row("reports")})')
            names = page.js('[...document.querySelectorAll("#agents .row .name")].map(n => n.textContent)')
            assert names.count("billing") == 1 and names.count("api-dev") == 1, names
    finally:
        stop()
        store.close()


def test_stuck_and_background_task_chips_in_groups(team):
    """A stuck session in a group and a session waiting on a background task under a machine
    header keep their chips; the stuck chat's line shows once."""
    store, url, _ = team
    with chrome() as page:
        page.call("Page.addScriptToEvaluateOnNewDocument", source=FAKE)
        page.go(url)
        page.wait('!!document.querySelector("#agents .wt-head")')
        page.js('fetch("/api/feed", { headers: { Authorization: "Bearer " + localStorage.getItem("kaptyorka.token") } })'
                '.then(r => r.json()).then(f => { window.__base = f; })')
        page.wait("!!window.__base")
        page.js("""window.__script = n => {
          const f = JSON.parse(JSON.stringify(window.__base));
          const b1 = f.agents.find(a => a.label === "api-dev"), b4 = f.agents.find(a => a.label === "migrations");
          b1.stuck = { kind: "busy", s: 7300 };
          b4.activity = "shell";
          f.groups[0].quiet = true; f.groups[0].active_s = 3 * 3600 + 120;  // hours since the last real message
          return f; }""")
        page.wait(f'{row("api-dev")}.textContent.includes("Stuck 2 h")')
        assert page.js(f'!!{row("api-dev")}.closest(".wt-rows")')
        assert page.js('document.querySelector("#agents .wt-head").textContent').endswith("quiet 3 h")  # kept, not dropped
        assert page.js(f'{row("migrations")}.textContent').endswith("Background task")
        assert page.js(f'{row("migrations")}.closest(".bg-rows").id') == "g-m_bob_box-2"  # under its machine, not Idle
        page.js(f'{row("api-dev")}.click()')
        page.wait('!!document.querySelector("#log .working")')
        time.sleep(4.5)  # two polls
        assert page.js('document.querySelectorAll("#log .working").length') == 1
        assert page.js('document.querySelector("#log .working .what").textContent').startswith("looks stuck")


def test_two_session_chat_keeps_its_sides(team):
    """A chat of just two sessions: the same session is on the left however much of the
    conversation is loaded (also when its older messages are gone and the other one speaks first)."""
    store, url, _ = team
    both(store, "b3", "b4", ago=60)  # ui-review and migrations: a group of two
    sides = ('[...document.querySelectorAll("#log .group")].map(g => g.querySelector(".head b").textContent + ":" +'
             ' (g.classList.contains("peer") ? "right" : "left"))')
    with chrome() as page:
        page.go(url)
        rows = '[...document.querySelectorAll("#agents .wt-rows")].find(r => r.textContent.includes("ui-review"))'
        page.wait(f'!!({rows})')
        page.js(f'(({rows}).parentElement.querySelector(".wt-head") || ({rows}).previousElementSibling).click()')
        page.wait('document.querySelectorAll("#log .group").length >= 8')
        full = page.js(sides)
        assert len(set(full)) == 2 and {x.split(":")[1] for x in full} == {"left", "right"}, full
        # the hub now gives the chat without its first message: the other session speaks first
        page.js("""(() => { const f = window.fetch; window.fetch = (u, o) => f(u, o).then(r => !/\\/api\\/groups\\/.*\\/messages/.test(String(u)) ? r :
          r.json().then(d => new Response(JSON.stringify(Object.assign(d, { messages: d.messages.slice(1) })), { status: 200 }))); })()""")
        page.wait(f'{sides}.length === {len(full) - 1}', timeout=20)
        cut = page.js(sides)
        assert cut == full[1:] and cut[0] != full[0], (full, cut)
