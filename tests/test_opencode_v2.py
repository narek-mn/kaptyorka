"""OpenCode 2: the TUI plugin that gives /hub and the hub indicator, how join, the bridge and leave
write and remove it, and the doctor checks for V2. The plugin runs under node against a fake
context shaped on OpenCode 2's TUI plugin API (sst/opencode v2.3.2,
packages/plugin/src/tui/context.ts) and a fake kaptyorka; no real OpenCode runs."""
import json
import os
import shutil
import subprocess
import time

import pytest

from kaptyorka import doctor, hubstate, offhub, paths
from kaptyorka.adapters import opencode
from kaptyorka.adapters.opencode import COMMAND_MARK, TUI_MARK, OpenCodeAdapter
from kaptyorka.store import session_id as handle_of

NODE = shutil.which("node")
SID = "ses_2fA9xQ"
KEY = f"opencode:{SID}"


@pytest.fixture
def v2(home, monkeypatch):
    monkeypatch.setattr(opencode, "major", lambda: 2)
    return home


def conf(home):
    return home / ".config" / "opencode"


def tui(home):
    return conf(home) / "plugins" / "kaptyorka-hub" / "tui.js"


def test_v2_writes_the_tui_plugin_and_drops_our_hub_md(v2):
    a = OpenCodeAdapter(v2)
    OpenCodeAdapter(v2).install_hub("/k", v2=False)  # a machine that had OpenCode 1
    assert COMMAND_MARK in (conf(v2) / "commands/hub.md").read_text()
    lines = a.configure("/k")
    text = tui(v2).read_text()
    assert TUI_MARK in text[:400] and 'const KAPTYORKA = "/k";' in text
    assert 'slash: { name: "hub", arguments: true }' in text
    assert 'append: "prompt.footer.status"' in text
    assert not (conf(v2) / "commands/hub.md").exists()  # V2 would list it next to the plugin's /hub
    assert any("/hub and the hub indicator written" in line for line in lines)
    assert any("/hub command removed" in line for line in lines)
    assert a.install_hub("/k") == []  # nothing changes on the next bridge start
    assert doctor.hub_installed(a, v2)


def test_v2_leaves_files_it_did_not_write(v2):
    logs = []
    a = OpenCodeAdapter(v2, log=logs.append)
    (conf(v2) / "commands").mkdir(parents=True)
    (conf(v2) / "commands/hub.md").write_text("my own /hub\n")
    tui(v2).parent.mkdir(parents=True)
    tui(v2).write_text("// someone's own plugin\n")
    a.install_hub("/k")
    assert (conf(v2) / "commands/hub.md").read_text() == "my own /hub\n"
    assert tui(v2).read_text() == "// someone's own plugin\n"
    assert any("left alone" in line for line in logs)
    assert not doctor.hub_installed(a, v2)
    tui(v2).unlink()
    (tui(v2).parent / "index.js").write_text("// a folder of theirs with that name\n")
    a.install_hub("/k")
    assert not tui(v2).exists()
    a.unconfigure()
    assert (tui(v2).parent / "index.js").exists()
    assert (conf(v2) / "commands/hub.md").read_text() == "my own /hub\n"


def test_back_on_v1_and_leave_remove_only_our_tui_plugin(v2, monkeypatch):
    a = OpenCodeAdapter(v2)
    a.install_hub("/k")
    assert tui(v2).exists()
    monkeypatch.setattr(opencode, "major", lambda: 1)
    lines = a.install_hub("/k")  # downgraded to OpenCode 1: its /hub is hub.md again
    assert not tui(v2).parent.exists() and COMMAND_MARK in (conf(v2) / "commands/hub.md").read_text()
    assert any("/hub plugin removed" in line for line in lines)
    monkeypatch.setattr(opencode, "major", lambda: 2)
    a.install_hub("/k")
    (tui(v2).parent / "notes.txt").write_text("mine\n")
    a.unconfigure()
    assert not tui(v2).exists() and (tui(v2).parent / "notes.txt").exists()
    assert not (conf(v2) / "plugins/kaptyorka.js").exists()


def test_v1_install_is_unchanged(home):
    a = OpenCodeAdapter(home)
    a.install_hub("/k")
    assert not tui(home).parent.exists()
    assert "kaptyorka hub" in (conf(home) / "plugins/kaptyorka.js").read_text()
    assert doctor.hub_installed(a, home)


def test_v2_doctor_server_check(monkeypatch):
    monkeypatch.setattr(opencode, "major", lambda: 2)
    monkeypatch.setattr(opencode, "_tuis", lambda: [11, 12])
    monkeypatch.setattr(opencode, "_listen_ports", lambda pid: pytest.fail("V2 TUIs never listen"))
    assert opencode.deaf_tuis() == [11, 12]
    c = doctor._opencode_server()
    assert not c.ok and "background service" in c.detail and "(2 windows)" in c.detail
    assert "opencode service start" in c.fix
    monkeypatch.setattr(opencode, "service", lambda: {"url": "http://127.0.0.1:1"})
    assert opencode.deaf_tuis() == [] and doctor._opencode_server().ok


# --- the TUI plugin itself, under node ---------------------------------------------------------

HARNESS = r"""
import { pathToFileURL } from "node:url";
const [plugin, mode, dir] = process.argv.slice(2);
const out = { toasts: [], slots: [], layers: [] };
const handlers = {};
let route = { type: "session", sessionID: "SID" };
const ctx = {
  location: { directory: dir },
  data: {
    session: { get: (id) => ({ id, location: { directory: dir } }) },
    on: (type, h) => { handlers[type] = h; return () => { delete handlers[type]; }; },
  },
  storage: { memory: (key, o) => { const s = structuredClone(o.initial); return [s, (m) => m(s)]; } },
  keymap: { layer: (f) => out.layers.push(f()) },
  ui: {
    toast: { show: (t) => out.toasts.push(t) },
    router: { current: () => route },
    slot: (claim) => { out.slots.push(claim); return () => {}; },
  },
  theme: { text: { muted: "muted", feedback: { success: { base: "green" }, warning: { base: "orange" } } } },
};
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const mod = await import(pathToFileURL(plugin).href);
out.id = mod.default.id;
const cleanup = await mod.default.setup(ctx);
const cmd = out.layers.flatMap((l) => l.commands).find((c) => c.slash && c.slash.name === "hub");
out.slash = cmd.slash;
await cmd.run("off");
route = { type: "home" };
await cmd.run("");
route = { type: "session", sessionID: "SID" };
if (mode === "slot") {
  const el = out.slots[0].render({ sessionID: "SID" });
  await wait(800);
  (globalThis.effects || []).forEach((f) => f());
  out.text = el.read();
  out.fg = el.props.fg;
} else if (handlers["session.created"]) {
  handlers["session.created"]({ data: { sessionID: "ses_other" } });  // another window's chat
  handlers["session.created"]({ data: { sessionID: "ses_child", parentID: "SID" } });
  handlers["session.created"]({ data: { sessionID: "SID" } });
  await wait(800);
}
out.slots = out.slots.map((s) => s.append);
if (cleanup) await cleanup();
console.log(JSON.stringify(out));
"""

FAKE_SOLID = """
export const createElement = (tag) => ({ tag, props: {}, read: () => "" });
export const insert = (el, fn) => { el.read = fn; fn(); };
export const effect = (fn) => { (globalThis.effects ||= []).push(fn); fn(); };  // re-run below, as Solid would
export const setProp = (el, k, v) => { el.props[k] = v; return v; };
"""

FAKE_KAPTYORKA = """#!/bin/sh
echo "$*|$KAPTYORKA_OPENCODE_SESSION|$(pwd)" >> "%s"
case "$1 $2" in
  "hub off") echo "this session is now off the hub: nobody on the hub sees it"; exit 0 ;;
  "hub ") echo "kaptyorka: start a chat first"; exit 0 ;;
  hub-state*) echo on ;;
  *) echo "this session is on the hub" ;;
esac
"""


def run_plugin(tmp_path, mode):
    if not NODE:
        pytest.skip("node not installed")
    log = tmp_path / "calls.log"
    exe = tmp_path / "kaptyorka"
    exe.write_text(FAKE_KAPTYORKA % log)
    exe.chmod(0o755)
    plug = tmp_path / "plug"
    plug.mkdir()
    (plug / "tui.js").write_text(opencode.TUI_V2 % {"mark": TUI_MARK, "exe": json.dumps(str(exe))})
    if mode == "slot":  # the module OpenCode's Bun build hands to plugins
        solid = plug / "node_modules" / "@opentui" / "solid"
        solid.mkdir(parents=True)
        (solid / "package.json").write_text('{"name": "@opentui/solid", "type": "module", "main": "index.js"}')
        (solid / "index.js").write_text(FAKE_SOLID)
    harness = tmp_path / "harness.mjs"
    harness.write_text(HARNESS.replace('"SID"', json.dumps(SID)))
    work = tmp_path / "proj-a"
    work.mkdir()
    r = subprocess.run([NODE, str(harness), str(plug / "tui.js"), mode, str(work)],
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    calls = log.read_text().splitlines() if log.exists() else []
    return json.loads(r.stdout), calls, str(work)


def test_tui_plugin_hub_command_and_toasts(tmp_path):
    out, calls, work = run_plugin(tmp_path, "toast")
    assert out["id"] == "kaptyorka.hub" and out["slash"] == {"name": "hub", "arguments": True}
    assert out["slots"] == []  # no OpenTUI module: no indicator, toasts only
    assert calls[0] == f"hub off|{SID}|{os.path.realpath(work)}"
    off = out["toasts"][0]
    assert off["variant"] == "warning" and off["message"].startswith("this session is now off the hub")
    assert "start a chat first" in out["toasts"][1]["message"]  # /hub on the home screen runs nothing
    # only this window's new top-level chat asks, once
    assert calls[1:] == [f"hub|{SID}|{os.path.realpath(work)}"]
    assert len(out["toasts"]) == 3 and out["toasts"][2]["variant"] == "success"


def test_tui_plugin_indicator_in_the_prompt_footer(tmp_path):
    out, calls, work = run_plugin(tmp_path, "slot")
    assert out["slots"] == ["prompt.footer.status"]
    assert (out["text"], out["fg"]) == ("hub", "green")
    states = [c for c in calls if c.startswith("hub-state ")]
    assert states and states[0].startswith(f"hub-state {KEY} ")
    assert not any(c.startswith("hub|") for c in calls)  # the footer replaces the start toast


# --- hub-state for an OpenCode session --------------------------------------------------------

@pytest.fixture
def joined(home):
    paths.save_config({"hub": "http://127.0.0.1:1", "token": "T", "machine": "box-1"})
    offhub.save_team({"proj-a"}, set())
    return home


def put_cache(ids=(), ok=True):
    (paths.home() / hubstate.CACHE).write_text(json.dumps({"at": time.time(), "ok": ok, "ids": list(ids)}))


def never():
    raise AssertionError("no refresh expected")


@pytest.mark.team_only
def test_hub_state_for_an_opencode_session(joined):
    put_cache([handle_of(KEY)])
    assert hubstate.state(KEY, cwd="/w/proj-a", spawn=never) == "on"
    assert hubstate.state(KEY, cwd="/w/my-notes", spawn=never) == "off"  # a personal folder
    assert hubstate.state("opencode:probe", cwd="/w/proj-a", spawn=never) is None  # not OpenCode's id
    offhub.switch_key(KEY, True)  # /hub off
    assert hubstate.state(KEY, cwd="/w/proj-a", spawn=never) == "off"
    offhub.switch_key(KEY, False)
    put_cache([])
    now_ms = time.time() * 1000
    assert hubstate.state(KEY, cwd="/w/proj-a", spawn=never, seen_ms=now_ms) is None  # just opened
    assert hubstate.state(KEY, cwd="/w/proj-a", spawn=never,
                          seen_ms=now_ms - hubstate.GRACE_S * 1000) == "lost"


@pytest.mark.team_only
def test_cli_hub_state_takes_the_opencode_key(joined, tmp_path, monkeypatch, capsys):
    from test_cli import entry_with_exit

    put_cache([handle_of(KEY)])
    work = tmp_path / "proj-a"
    work.mkdir()
    monkeypatch.chdir(work)
    assert entry_with_exit(["hub-state", KEY, str(int(time.time() * 1000))]) == 0
    assert capsys.readouterr().out == "on\n"
    monkeypatch.chdir(tmp_path)  # not a team project
    assert entry_with_exit(["hub-state", KEY]) == 0
    assert capsys.readouterr().out == "off\n"


@pytest.mark.team_only
@pytest.mark.parametrize("project", ["", "proj-a"])  # /hub team, /hub team proj-a
def test_hub_state_joined_opencode_session_in_a_personal_folder(joined, project):
    from kaptyorka import optin

    put_cache([handle_of(KEY)])
    assert hubstate.state(KEY, cwd="/w/my-notes", spawn=never) == "off"
    optin.save(KEY, project, os.getpid())
    assert hubstate.state(KEY, cwd="/w/my-notes", spawn=never) == "on"
    put_cache([])
    assert hubstate.state(KEY, cwd="/w/my-notes", spawn=never) == "lost"
    offhub.switch_key(KEY, True)  # /hub off still wins
    assert hubstate.state(KEY, cwd="/w/my-notes", spawn=never) == "off"
