"""Workers on team machines: a person machine turned into a team machine hosts workers for
anyone with the write role in the project; a session on a team machine may ask for one; the
worker closes itself when its task is done or its boss session really ended. A bridge is
simulated with raw HTTP calls, as in test_hub_workers."""
from types import SimpleNamespace

import pytest

import kaptyorka
from conftest import start_hub
from helpers import call, call_error, person, run
from kaptyorka import http
from kaptyorka.hub import ON_SHARED
from kaptyorka.store import Store
from kaptyorka.workers import BOSS_ENDED_S, WorkerStore

ADMIN = "T"  # the fixture's legacy token belongs to the admin


@pytest.fixture
def clock():
    return [1_000_000.0]


@pytest.fixture
def hub(tmp_path, clock):
    store = Store(tmp_path / "hub.db", clock=lambda: clock[0], legacy_token=ADMIN)
    stop = start_hub(store)
    yield SimpleNamespace(url=f"http://127.0.0.1:{stop.port}", token=ADMIN, store=store,
                          app=stop.app, ws=WorkerStore(store))
    stop()
    store.close()


def as_token(hub, token):
    return SimpleNamespace(url=hub.url, token=token)


def ident(key, project="proj-a", machine="laptop", tool="claude-code"):
    return {"key": key, "machine": machine, "tool": tool, "project": project}


def heartbeat(hub, token, host, sessions=(), folders=None, tools=(), **extra):
    body = {"machine": host, "sessions": list(sessions), "tools": list(tools),
            "facts": {"host": host, "os_user": "u", "version": kaptyorka.__version__,
                      "tmux": True, "load": 0.1, "cpus": 4},
            "folders": {"proj-a": "/w/proj-a"} if folders is None else folders, **extra}
    return http.request("POST", hub.url + "/bridge/heartbeat", token, body)


def bridge(hub, person_id, host, **kw):
    code = hub.store.invite_create(person_id)
    token, secret = hub.store.invite_redeem(code, f"u@{host}", host=host, os_user="u")
    status, answer = heartbeat(hub, token, host, **kw)
    assert status == 200
    return SimpleNamespace(token=token, secret=secret, host=host, answer=answer,
                           device_id=hub.store.authenticate(token).device_id)


def poll(hub, b):
    status, items = http.request("GET", f"{hub.url}/bridge/poll?machine={b.host}", b.token)
    assert status == 200
    return items


def take(hub, b, key):
    """The bridge on b takes the one launch waiting there and links its session `key` (a
    session already in the folder gets a notice beside it)."""
    [item] = [i for i in poll(hub, b) if i.get("kind") == "launch"]
    assert http.request("POST", hub.url + "/bridge/ack", b.token,
                        {"id": item["id"], "ok": True, "launch": True})[0] == 204
    assert http.request("POST", hub.url + "/bridge/worker", b.token,
                        {"worker": item["worker"], "key": key})[0] == 204
    return item


@pytest.fixture
def admin(hub):
    """The admin's own token (admin work refuses the shared token once they have one)."""
    pid, token = person(hub.store, "root", "admin")
    return as_token(hub, token)


@pytest.fixture
def lab(hub, admin):
    """proj-a; alice (write) with her laptop's bridge; jetson-1 joined as if it were a person,
    with its own bridge: what the hub had before anyone turned it into a team machine."""
    hub.store.project_register("proj-a", by="test")
    alice_id, alice_token = person(hub.store, "alice")
    hub.store.grant_project("proj-a", alice_id, "write", "test")
    laptop = bridge(hub, alice_id, "laptop")
    jet_id = hub.store.person_create("jetson-1")
    jet = bridge(hub, jet_id, "jetson-1")
    return SimpleNamespace(alice_id=alice_id, alice=as_token(hub, alice_token), laptop=laptop,
                           jet_id=jet_id, jet=jet)


ASKER = ident("k:alice", machine="laptop")


def test_a_person_machine_hosts_no_worker_until_it_is_a_team_machine(hub, admin, lab):
    """The admin steps, proven: before, jetson-1 is someone's private machine; one command
    (`kaptyorka machine team --from jetson-1`, the route behind it) is all it takes."""
    before = run(call(lab.alice, ASKER, "work", project="proj-a", task="x", machine="jetson-1"))
    assert "jetson-1" in before and not before.startswith("starting"), before
    status, done = http.request("POST", hub.url + "/api/people/jetson-1/team", admin.token)
    assert status == 200 and done["name"] == "jetson-1"
    assert heartbeat(hub, lab.jet.token, "jetson-1")[1]["shared"] is True
    assert run(call(lab.alice, ASKER, "work", project="proj-a", task="x",
                    machine="jetson-1")) == "starting `proj-a-w1` on jetson-1 (u)"


def team(hub, admin):
    status, _ = http.request("POST", hub.url + "/api/people/jetson-1/team", admin.token)
    assert status == 200


def test_the_worker_runs_as_alices_on_the_team_machine(hub, admin, lab):
    team(hub, admin)
    run(call(lab.alice, ASKER, "work", project="proj-a", task="x", machine="jetson-1"))
    item = take(hub, lab.jet, "claude:w1")
    assert item["person"] == "alice"  # the bridge takes it: the hub says the account is shared
    w = hub.ws.by_name("proj-a-w1")
    assert w["state"] == "up" and w["host"] == "jetson-1" and w["person"] == "alice"
    assert hub.ws.boss_key("proj-a-w1") == "k:alice"
    # its bridge keeps reporting it: alice's session on a shared account is not dropped
    sessions = [{"key": "claude:w1", "tool": "claude-code", "project": "proj-a",
                 "cwd": "/w/proj-a"}]
    assert heartbeat(hub, lab.jet.token, "jetson-1", sessions)[0] == 200
    assert hub.store.agent("claude:w1").person_id == lab.alice_id


def test_undo_turns_it_back_and_is_refused_while_others_workers_run(hub, admin, lab):
    team(hub, admin)
    run(call(lab.alice, ASKER, "work", project="proj-a", task="x", machine="jetson-1"))
    take(hub, lab.jet, "claude:w1")
    url = hub.url + "/api/people/jetson-1/team"
    assert http.request("DELETE", url, admin.token) == \
        (400, "other people's workers run on jetson-1 (proj-a-w1); close them first")
    run(call(lab.alice, ASKER, "close_worker", name="proj-a-w1"))
    item = poll(hub, lab.jet)[0]
    http.request("POST", hub.url + "/bridge/ack", lab.jet.token,
                 {"id": item["id"], "ok": True, "launch": True})
    assert http.request("DELETE", url, admin.token) == (200, {"name": "jetson-1"})
    assert heartbeat(hub, lab.jet.token, "jetson-1")[1]["shared"] is False
    row = next(p for p in hub.store.people() if p["name"] == "jetson-1")
    assert not row["team"] and row["role"] == "member"
    answer = run(call(lab.alice, ASKER, "work", project="proj-a", task="x", machine="jetson-1"))
    assert "jetson-1 is someone's own machine, not a team machine" in answer
    assert http.request("DELETE", url, admin.token) == (400, "jetson-1 is not a team machine")


def test_who_may_start_on_a_team_machine(hub, admin, lab):
    """The write role in the project, on a real team machine only; a hidden one stays off;
    admins anywhere. A device merely marked shared still needs a grant."""
    team(hub, admin)
    bob_id, bob_token = person(hub.store, "bob")
    hub.store.grant_project("proj-a", bob_id, "read", "test")
    bob = as_token(hub, bob_token)
    assert "that takes the write role" in call_error(
        bob, ident("k:bob"), "work", project="proj-a", task="x", machine="jetson-1")
    carol_id, carol_token = person(hub.store, "carol")
    hub.store.grant_project("proj-a", carol_id, "write", "test")
    hub.store.team_machine_set_visible("jetson-1", "carol", False, "test")
    carol = as_token(hub, carol_token)
    assert run(call(carol, ident("k:carol"), "work", project="proj-a", task="x",
                    machine="jetson-1")) == (
        "no machine can take proj-a: no machine named jetson-1 that you may use; check the name"
        " on the page's Machines list, or ask an admin")
    # dave's account on box-1 is marked shared but is no team machine: a grant is needed
    dave_id = hub.store.person_create("dave")
    box = bridge(hub, dave_id, "box-1")
    hub.store.device_set_shared(box.device_id, True)
    answer = run(call(lab.alice, ASKER, "work", project="proj-a", task="x", machine="box-1"))
    assert answer == ("no machine can take proj-a: box-1 (u): a shared account you have no access"
                      " to; an admin can give it: `kaptyorka grant <you> machine:box-1`")
    hub.store.grant_machine("box-1", lab.alice_id, by="test")
    assert run(call(lab.alice, ASKER, "work", project="proj-a", task="x",
                    machine="box-1")).startswith("starting `proj-a-w1` on box-1")
    # an admin with no role in the project
    assert run(call(admin, ident("k:root"), "work", project="proj-a", task="x",
                    machine="jetson-1")).startswith("starting `proj-a-w2` on jetson-1")


def test_a_session_on_the_team_machine_asks_and_is_the_boss(hub, admin, lab):
    team(hub, admin)
    jet = as_token(hub, lab.jet.token)
    here = ident("claude:j1", machine="u@jetson-1")
    assert call_error(jet, here, "work", project="proj-a", task="x") == (
        "Error executing tool work: jetson-1 is a team machine without the write role in proj-a;"
        " an admin runs `kaptyorka grant jetson-1 proj-a:write`")
    hub.store.grant_project("proj-a", lab.jet_id, "write", "test")
    assert run(call(jet, here, "work", project="proj-a", task="x")) == \
        "starting `proj-a-w1` on jetson-1 (u)"
    take(hub, lab.jet, "claude:w1")
    with hub.store.lock:
        row = hub.store.db.execute("SELECT boss_key, boss_person FROM workers WHERE"
                                   " name='proj-a-w1'").fetchone()
    assert (row["boss_key"], row["boss_person"]) == ("claude:j1", lab.jet_id)
    # that worker is a session of the team machine too, but still may not start more
    worker = ident("claude:w1", machine="u@jetson-1")
    for tool, args in (("work", {"project": "proj-a", "task": "y"}),
                       ("adopt", {"name": "proj-a-w1"}),
                       ("close_worker", {"name": "proj-a-w9"})):  # its own name: done
        assert ON_SHARED in call_error(jet, worker, tool, **args)
    assert http.request("POST", hub.url + "/api/workers/proj-a-w1/close", lab.jet.token,
                        headers={"X-Kaptyorka-Key": "claude:w1"}) == (403, ON_SHARED)
    # nor may alice's worker on it
    run(call(lab.alice, ASKER, "work", project="proj-a", task="x", machine="jetson-1"))
    take(hub, lab.jet, "claude:w2")
    assert ON_SHARED in call_error(jet, ident("claude:w2", machine="u@jetson-1"), "work",
                                   project="proj-a", task="y")
    # the session that asked closes its worker
    assert run(call(jet, here, "close_worker", name="proj-a-w1")).startswith("closing")


def test_first_use_lines(hub, admin, lab, clock):
    """Each thing that stops "open a session on jetson-1 in proj-a" gets one line with the fix."""
    def ask():
        return run(call(lab.alice, ASKER, "work", project="proj-a", task="x", machine="jetson-1"))

    assert ask() == ("no machine can take proj-a: jetson-1 is someone's own machine, not a team"
                     " machine; if the whole team uses it, an admin runs `kaptyorka machine team"
                     " --from <its person>`")
    team(hub, admin)
    heartbeat(hub, lab.jet.token, "jetson-1", folders={})
    assert ask() == ("no machine can take proj-a: jetson-1 (u): no folder for proj-a; its bridge"
                     " looks for ~/proj-a once the team machine has a role there: `kaptyorka grant"
                     " jetson-1 proj-a:write`, or open a session in the proj-a folder there once")
    hub.store.grant_project("proj-a", lab.jet_id, "write", "test")
    assert ask().endswith("jetson-1 (u): no folder for proj-a; clone it to ~/proj-a there, or"
                          " open a session in its folder there once")
    heartbeat(hub, lab.jet.token, "jetson-1")
    clock[0] += 3600  # its bridge has been silent for an hour
    assert ask() == ("no machine can take proj-a: jetson-1 (jetson-1): bridge offline; on that"
                     " machine run `kaptyorka doctor --fix`")
    assert run(call(lab.alice, ASKER, "work", project="proj-a", task="x",
                    machine="jetson-9")).endswith("no machine named jetson-9 that you may use;"
                                                  " check the name on the page's Machines list,"
                                                  " or ask an admin")


# --- closing by itself ----------------------------------------------------------------------

def session(key, tool="claude-code"):
    return {"key": key, "tool": tool, "project": "proj-a", "cwd": "/w/proj-a"}


WORKER = ident("claude:w1", machine="u@jetson-1")


@pytest.fixture
def up(hub, admin, lab):
    """alice's session on her laptop asked; worker proj-a-w1 runs on team machine jetson-1;
    both bridges report (the laptop's knows its Claude Code list is complete)."""
    team(hub, admin)
    run(call(lab.alice, ASKER, "work", project="proj-a", task="x", machine="jetson-1"))
    take(hub, lab.jet, "claude:w1")
    lab.beat = lambda boss=True, overnight=(): (
        heartbeat(hub, lab.jet.token, "jetson-1", [session("claude:w1")], tools=["claude-code"],
                  **({} if overnight is None else {"overnight": list(overnight)})),
        heartbeat(hub, lab.laptop.token, "laptop", [session("k:alice")] if boss else [],
                  tools=["claude-code"]))
    lab.beat()
    return lab


def later(hub, clock, seconds, **beat):
    clock[0] += seconds
    hub.up.beat(**beat)
    hub.app.state.tick()


def closing(hub, b):
    return [i for i in poll(hub, b) if i["kind"] == "close"]


def finish_close(hub, b, item, left):
    assert http.request("POST", hub.url + "/bridge/ack", b.token,
                        {"id": item["id"], "ok": True, "launch": True, "error": "",
                         "left": left})[0] == 204


def texts_to(hub, key):
    with hub.store.lock:
        return [r["text"] for r in hub.store.db.execute(
            "SELECT text FROM messages WHERE to_key=? ORDER BY id", (key,))]


def test_the_worker_closes_itself_when_its_task_is_done(hub, up):
    jet = as_token(hub, up.jet.token)
    assert run(call(jet, WORKER, "close_worker", name="proj-a-w1")) == \
        "closing `proj-a-w1`: your task is done; your commits, branch and files stay"
    [item] = closing(hub, up.jet)
    assert (item["auto"], item["folder"], item["key"]) == ("task done", "/w/proj-a", "claude:w1")
    finish_close(hub, up.jet, item, {"folder": "/w/proj-a", "branch": "work/proj-a-w1",
                                     "uncommitted": 1})
    w = hub.ws.by_name("proj-a-w1")
    assert (w["state"], w["error"]) == ("closed", "task done")
    line = ("worker `proj-a-w1` closed: task done; it left branch work/proj-a-w1 with 1"
            " uncommitted file in /w/proj-a; nothing was deleted")
    assert texts_to(hub, "k:alice")[-1] == line
    assert line not in texts_to(hub, "web:alice")  # her session is there: one line, there


def test_done_is_refused_in_overnight_mode_and_for_a_page_boss(hub, up, clock):
    jet = as_token(hub, up.jet.token)
    up.beat(overnight=["claude:w1"])
    assert call_error(jet, WORKER, "close_worker", name="proj-a-w1").endswith(
        "overnight mode is on in this session; it stays open until overnight mode ends")
    up.beat()
    with hub.store.lock:
        hub.store.db.execute("UPDATE workers SET boss_key='web:alice'")
    assert call_error(jet, WORKER, "close_worker", name="proj-a-w1").endswith(
        "your boss is a person on the page; stay open, they close you there")
    assert closing(hub, up.jet) == [] and hub.ws.by_name("proj-a-w1")["state"] == "up"


def test_closed_ten_minutes_after_its_boss_session_ended(hub, up, clock):
    hub.up = up
    later(hub, clock, 60, boss=False)  # the laptop's bridge reports, without alice's session
    later(hub, clock, BOSS_ENDED_S - 1, boss=False)
    assert closing(hub, up.jet) == []
    later(hub, clock, 2, boss=False)
    [item] = closing(hub, up.jet)
    assert item["auto"] == "its boss session ended"
    later(hub, clock, 30, boss=False)
    assert closing(hub, up.jet) == []  # one order, not one a tick
    finish_close(hub, up.jet, item, {"folder": "/w/proj-a", "branch": "main", "uncommitted": 0})
    line = ("worker `proj-a-w1` closed: its boss session ended; it left branch main with 0"
            " uncommitted files in /w/proj-a; nothing was deleted")
    assert texts_to(hub, "k:alice")[-1] == line and texts_to(hub, "web:alice")[-1] == line


def test_a_boss_seen_again_starts_the_count_over(hub, up, clock):
    hub.up = up
    later(hub, clock, 60, boss=False)
    later(hub, clock, BOSS_ENDED_S - 120, boss=True)  # back (a bridge restart, say)
    later(hub, clock, 300, boss=False)
    later(hub, clock, 120, boss=False)
    assert closing(hub, up.jet) == []


def test_never_closed_while_the_boss_is_only_offline(hub, up, clock):
    """A closed lid: the laptop's bridge says nothing, so its session has not ended."""
    hub.up = up
    for _ in range(8):
        clock[0] += 600
        heartbeat(hub, up.jet.token, "jetson-1", [session("claude:w1")], tools=["claude-code"],
                  overnight=[])
        hub.app.state.tick()
    assert closing(hub, up.jet) == []
    assert hub.ws.by_name("proj-a-w1")["state"] == "unattended"  # today's rule


@pytest.mark.parametrize("case", ["overnight", "older bridge", "page boss", "codex boss"])
def test_never_closed_by_itself_when(hub, up, clock, case):
    hub.up = up
    beat = {"overnight": ["claude:w1"]} if case == "overnight" else \
        {"overnight": None} if case == "older bridge" else {}
    if case == "older bridge":  # it never said whether overnight mode is on
        with hub.store.lock:
            hub.store.db.execute("UPDATE workers SET overnight=NULL")
    if case == "page boss":
        with hub.store.lock:
            hub.store.db.execute("UPDATE workers SET boss_key='web:alice', boss_gone_since=?",
                                 (clock[0],))
    if case == "codex boss":  # its list may miss live sessions: never "ended"
        with hub.store.lock:
            hub.store.db.execute("UPDATE agents SET tool='codex' WHERE key='k:alice'")
    later(hub, clock, 60, boss=False, **beat)
    later(hub, clock, BOSS_ENDED_S + 60, boss=False, **beat)
    assert closing(hub, up.jet) == []
    assert hub.ws.by_name("proj-a-w1")["state"] in ("up", "unattended")


def test_end_to_end_ask_start_report_close(hub, admin, lab, home, monkeypatch, clock):
    """Session A (alice's, on her laptop) asks; the real bridge of team machine jetson-1 starts
    worker B (a fake Claude Code); B does its task, reports to A and closes itself; A hears what
    B left. Only the agent tool is fake; git and the hub are real."""
    import subprocess
    import time

    from kaptyorka import facts
    from kaptyorka import launch as launch_mod
    from kaptyorka.adapters.base import Session
    from kaptyorka.bridge import Bridge
    from kaptyorka.launch import Launcher
    from test_bridge import FakeAdapter
    from test_launch import KEY, FakePopen, FakeRun

    from kaptyorka import identity

    monkeypatch.setattr(identity, "facts",
                        lambda: {"host": "jetson-1", "os_user": "u", "version": kaptyorka.__version__})
    monkeypatch.setattr(facts.shutil, "which", lambda n: None)  # no tool --version is run
    monkeypatch.setattr(launch_mod.shutil, "which", lambda n: None)  # no systemd-run
    folder = home / "proj-a"
    folder.mkdir()
    for argv in (["init", "-q", "-b", "main"], ["commit", "-q", "--allow-empty", "-m", "start"]):
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-C", str(folder),
                        *argv], check=True)
    clock[0] = time.time()  # the bridge checks the order's age on its own clock
    team(hub, admin)
    hub.store.grant_project("proj-a", lab.jet_id, "read", "test")  # its bridge finds ~/proj-a
    cfg = {"hub": hub.url, "token": lab.jet.token, "secret": lab.jet.secret,
           "machine": "u@jetson-1"}
    claude, lines, stop = FakeAdapter("claude-code", "claude"), [], FakeRun()
    launcher = Launcher(cfg, home, run=stop, popen=FakePopen(), log=lines.append)
    jet = Bridge(cfg, [claude], lines.append, launcher=launcher)
    jet.heartbeat()  # learns its projects
    jet.heartbeat()  # the hub learns the folder

    a = ident("claude:a", machine="laptop")
    heartbeat(hub, lab.laptop.token, "laptop", [session("claude:a")], tools=["claude-code"])
    assert run(call(lab.alice, a, "work", project="proj-a", task="write notes.txt",
                    machine="jetson-1")).startswith("starting `proj-a-w1` on jetson-1")
    jet.poll_once(0)  # the launch
    claude._sessions.append(Session(KEY, "claude-code", "proj-a", cwd=str(folder)))
    jet.heartbeat()
    assert hub.ws.by_name("proj-a-w1")["state"] == "up", (hub.ws.by_name("proj-a-w1"), lines)

    # B works: one new file, a report to its boss, then the done signal
    (folder / "notes.txt").write_text("done\n")
    b = ident(KEY, machine="u@jetson-1")
    boss = hub.store.agent("claude:a").name
    jetc = as_token(hub, lab.jet.token)
    run(call(jetc, b, "send", to=boss, text="notes.txt written"))
    assert run(call(jetc, b, "close_worker", name="proj-a-w1")).startswith("closing `proj-a-w1`")
    jet.poll_once(0)  # the close: `claude stop`, then git says what it left
    assert stop.calls == [["claude", "stop", "ab12cd34"]]
    assert hub.ws.by_name("proj-a-w1")["state"] == "closed"
    got = [m["text"] for m in run(call(lab.alice, a, "inbox"))]
    assert "notes.txt written" in got
    assert got[-1] == (f"worker `proj-a-w1` closed: task done; it left branch main with 1"
                       f" uncommitted file in {folder}; nothing was deleted")
    assert (folder / "notes.txt").exists()
