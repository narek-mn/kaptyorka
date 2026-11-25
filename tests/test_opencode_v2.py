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


# --- sessions open in an app with no TUI window here (desktop app, web) --------------------------

@pytest.fixture
def app_only(monkeypatch, tmp_path):
    """A V2 service with no TUI on the machine: what the desktop app over SSH leaves. Routes as
    OpenCode 2.3.2 / 2.4.1 answer them; `rows` and `active` are the service's state."""
    now = time.time() * 1000
    a, b = str(tmp_path / "proj-a"), str(tmp_path / "proj-b")
    state = {"active": {"ses_long": {"type": "running"}}, "rows": [
        {"id": "ses_child", "parentID": "ses_app", "location": {"directory": a},
         "time": {"created": now, "updated": now}},
        {"id": "ses_app", "location": {"directory": a}, "time": {"created": now - 9e5, "updated": now - 6e4}},
        {"id": "ses_tui", "location": {"directory": b}, "time": {"created": now, "updated": now - 1e3}},
        {"id": "ses_gone", "location": {"directory": a},
         "time": {"created": now, "updated": now - 2e3, "archived": now - 1e3}},
        {"id": "ses_long", "location": {"directory": b}, "time": {"created": 1, "updated": now - 4e6}},
        {"id": "ses_old", "location": {"directory": a}, "time": {"created": 1, "updated": now - 3e6}},
    ]}
    calls = []

    def call(svc, method, path, body=None, timeout=None):
        calls.append((method, path))
        if path == "/api/session/active":
            return (200, {"data": state["active"]}) if state["active"] is not None else (404, None)
        if path.startswith("/api/session?"):
            return 200, {"data": state["rows"], "cursor": {}}
        row = next((r for r in state["rows"] if path == "/api/session/" + r["id"]), None)
        return (200, {"data": row}) if row else (404, None)

    monkeypatch.setattr(opencode, "service", lambda: {"url": "http://127.0.0.1:1", "Authorization": "x"})
    monkeypatch.setattr(opencode, "_v2_call", call)
    monkeypatch.setattr(opencode, "_tuis", lambda: [])
    return state, calls, a, b


def test_v2_sessions_open_in_an_app_with_no_tui_are_listed(app_only):
    """No TUI, no note: top-level sessions that ran a step in the last 30 minutes, or run a
    turn now, with their own folder; never a subagent's, an archived or an old idle one."""
    state, calls, a, b = app_only
    got = OpenCodeAdapter(find_ports=lambda: pytest.fail("no V1 port scan on V2")).sessions()
    assert [(s.key, s.project, s.cwd) for s in got] == [
        ("opencode:ses_app", "proj-a", a), ("opencode:ses_tui", "proj-b", b),
        ("opencode:ses_long", "proj-b", b)]
    assert [s.activity for s in got] == ["idle", "idle", "busy"]  # from the list row and active
    assert [s.title for s in got] == ["", "", ""]
    state["rows"][1]["title"], state["rows"][2]["title"] = "App work", "New session - 2026-11-25T10:00:00.000Z"
    assert [s.title for s in OpenCodeAdapter().sessions()] == ["App work", "", ""]  # from the list row too
    calls.clear()
    got = OpenCodeAdapter(find_ports=lambda: pytest.fail("no V1 port scan on V2")).sessions()
    assert 55 < got[0].idle_s < 65 and got[2].write_s > 3900
    assert [p for m, p in calls] == ["/api/session?limit=100&order=desc", "/api/session/active"]
    calls.clear()  # every folder, newest first, and no call per session
    state["active"] = None  # a service without /api/session/active: the time rule alone
    assert [s.key for s in OpenCodeAdapter().sessions()] == ["opencode:ses_app", "opencode:ses_tui"]


def test_v2_a_tui_window_still_claims_its_session_once(app_only, monkeypatch):
    import subprocess as sp
    import sys

    state, calls, a, b = app_only
    window = sp.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        monkeypatch.setattr(opencode, "_tuis", lambda: [window.pid])
        opencode.open_dir().mkdir(parents=True, exist_ok=True)
        (opencode.open_dir() / f"{window.pid}.json").write_text(
            json.dumps({"session": "ses_tui", "directory": b}))
        keys = [s.key for s in OpenCodeAdapter().sessions()]
        assert keys == ["opencode:ses_tui", "opencode:ses_app", "opencode:ses_long"]
    finally:
        window.kill()
        window.wait()


def test_compat_probe_of_the_active_sessions_route(app_only, monkeypatch, home):
    from kaptyorka import compat

    state, calls, a, b = app_only
    monkeypatch.setattr(opencode, "major", lambda: 2)
    dep = compat.BY_ID["opencode.app_sessions"]
    assert compat.PROBES[dep.id](compat.Ctx(home, None)) == (compat.OK, "")
    state["active"] = None
    assert compat.PROBES[dep.id](compat.Ctx(home, None))[0] == compat.BROKEN


def test_v2_no_list_means_no_active_call(app_only, monkeypatch):
    state, calls, a, b = app_only
    real = opencode._v2_call
    monkeypatch.setattr(opencode, "_v2_call", lambda svc, m, p, *x, **k: (
        (503, None) if p.startswith("/api/session?") else real(svc, m, p, *x, **k)))
    assert OpenCodeAdapter().sessions() == []
    assert "/api/session/active" not in [p for m, p in calls]


def test_v2_service_list_failing_never_fails_the_report(app_only, monkeypatch):
    state, calls, a, b = app_only
    real = opencode._v2_call

    def down(svc, method, path, body=None, timeout=None):
        if path.startswith("/api/session?"):
            raise OSError("restarting")
        return real(svc, method, path, body, timeout)
    monkeypatch.setattr(opencode, "_v2_call", down)
    assert OpenCodeAdapter().sessions() == []


def test_doctor_notes_a_service_of_another_version(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    (tmp_path / "opencode").mkdir()
    reg = tmp_path / "opencode" / "service.json"
    monkeypatch.setattr(opencode, "_tuis", lambda: [])
    monkeypatch.setattr(opencode, "major", lambda: 2)
    monkeypatch.setattr(opencode, "version", lambda: "2.3.2")
    monkeypatch.setattr(opencode, "service", lambda: {"url": "http://127.0.0.1:1", "Authorization": "x"})
    reg.write_text(json.dumps({"url": "http://127.0.0.1:1", "pid": 1, "version": "2.4.1"}))
    c = doctor._opencode_server()
    assert c.ok and "service 2.4.1" in c.info and "on PATH is 2.3.2" in c.info
    reg.write_text(json.dumps({"url": "http://127.0.0.1:1", "pid": 1, "version": "2.3.2"}))
    assert doctor._opencode_server().info == ""
    reg.write_text(json.dumps({"url": "http://127.0.0.1:1", "pid": 1}))  # no version: ask the service
    monkeypatch.setattr(opencode, "_v2_call", lambda svc, m, p, *a, **k: (
        (200, {"version": "2.4.1", "pid": 1}) if p == "/api/info" else (404, None)))
    assert "service 2.4.1" in doctor._opencode_server().info
    monkeypatch.setattr(opencode, "version", lambda: None)  # the desktop app alone, no CLI
    assert "on PATH is missing" in doctor._opencode_server().info
    monkeypatch.setattr(opencode, "service", lambda: None)
    assert doctor._opencode_server().info == ""


# --- the TUI plugin itself, under node ---------------------------------------------------------

HARNESS = r"""
import { pathToFileURL } from "node:url";
import { existsSync, readFileSync } from "node:fs";
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
const note = `${process.argv[5]}/${process.pid}.json`;
out.note = JSON.parse(readFileSync(note, "utf8"));
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
out.noteAfter = existsSync(note);
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
    (plug / "tui.js").write_text(opencode.TUI_V2 % {"mark": TUI_MARK, "exe": json.dumps(str(exe)),
                                                    "open": json.dumps(str(tmp_path / "open"))})
    if mode == "slot":  # the module OpenCode's Bun build hands to plugins
        solid = plug / "node_modules" / "@opentui" / "solid"
        solid.mkdir(parents=True)
        (solid / "package.json").write_text('{"name": "@opentui/solid", "type": "module", "main": "index.js"}')
        (solid / "index.js").write_text(FAKE_SOLID)
    harness = tmp_path / "harness.mjs"
    harness.write_text(HARNESS.replace('"SID"', json.dumps(SID)))
    work = tmp_path / "proj-a"
    work.mkdir()
    r = subprocess.run([NODE, str(harness), str(plug / "tui.js"), mode, str(work), str(tmp_path / "open")],
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


@pytest.mark.parametrize("mode", ["toast", "slot"])
def test_tui_plugin_notes_the_session_its_window_shows(tmp_path, mode):
    """The bridge's exact answer to "which session does this window have open", while it runs."""
    out, _, work = run_plugin(tmp_path, mode)
    assert out["note"] == {"session": SID, "directory": work}
    assert out["noteAfter"] is False  # the plugin unloaded (or OpenCode quit): the note goes


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


def test_v2_login_check_never_starts_the_service(home, monkeypatch):
    monkeypatch.setattr(opencode, "major", lambda: 2)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    seen, out = [], {"text": ""}

    def login_output(command, args, run, **kw):
        seen.append(args)
        assert kw.get("timeout") == 15  # it boots a private server first
        return out["text"]
    monkeypatch.setattr(opencode, "login_output", login_output)
    a = OpenCodeAdapter(home)
    # the shape of `auth list --format json` (cli/src/commands/handlers/auth/list.ts:22-34)
    out["text"] = json.dumps([{"id": "anthropic", "name": "Anthropic", "connections": [
        {"type": "env", "name": "ANTHROPIC_API_KEY"},
        {"type": "credential", "id": "cred_1", "label": "alice", "method": "oauth"}]}])
    assert a.logged_in(None) is True
    assert seen == [["auth", "list", "--standalone", "--format", "json"]]
    out["text"] = json.dumps([{"id": "openai", "name": "OpenAI", "connections": [
        {"type": "env", "name": "OPENAI_API_KEY"}]}])
    assert a.logged_in(None) is None  # a provider variable only: unknown, never "not logged in"
    out["text"] = "[]\n"
    assert a.logged_in(None) is None
    for broken in (None, "", "Error: something\n", '{"x": 1}'):
        out["text"] = broken
        assert a.logged_in(None) is None
