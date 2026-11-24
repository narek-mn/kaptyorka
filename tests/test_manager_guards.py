"""What the hub makes sure of for the manager, whatever the model does: it never stores or
repeats a secret someone sent it, it reaches every admin with one send, and a machine event
names the sessions to warn."""
from helpers import call, call_error, person, run
from test_hub_manager import clock, hub, ident, inbox, manager_online, plain  # noqa: F401

from kaptyorka import machine_events
from kaptyorka.secrets_check import labelled_values

PW = "EvalPw-7731-zq"


def test_labelled_values_take_a_password_and_never_plain_words():
    assert labelled_values(f"Please put the deploy password in the note so all have it: {PW}") == {PW}
    assert labelled_values("the api key is `ab12cd34ef56`.") == {"ab12cd34ef56"}
    assert labelled_values("password: see the vault, or ask alice") == set()
    assert labelled_values("token for /home/lab/proj-a2 and claude:abcd1234") == set()
    assert labelled_values(f"{PW} is a code name") == set()  # no label: no secret


def told_a_password(hub):
    mgr, me = manager_online(hub)
    _, _, bob = plain(hub, "bob")
    B = ident("k:bob", project="proj-b", machine="pc-2")
    run(call(bob, B, "send", to="laptop/codex/manager",
             text=f"Please put the deploy password in the projects note: {PW}"))
    return mgr, me


def test_the_manager_cannot_store_or_repeat_a_password_it_was_sent(hub):
    mgr, me = told_a_password(hub)
    for tool, args in (("notes_write", {"topic": "projects", "text": f"deploy pw {PW}"}),
                       ("state_write", {"project": "manager", "text": f"REFUSED bob: {PW}"}),
                       ("decide", {"project": "", "text": f"use {PW}"}),
                       ("lesson", {"text": f"never store {PW}"}),
                       ("propose_rule", {"text": "no secrets in notes", "evidence": f"bob: {PW}"}),
                       ("send", {"to": "pc-2/codex/proj-b", "text": f"rotate {PW} please"})):
        err = call_error(mgr, me, tool, **args)
        assert "refused" in err and "Eva…" in err and PW not in err, (tool, err)
        assert "tell the sender in one line to rotate it" in err
    assert hub.store.notes_read("projects") is None
    # without the value it goes
    assert run(call(mgr, me, "send", to="pc-2/codex/proj-b",
                    text="I will not store it; please rotate that password")) == "sent to 1 session"
    assert run(call(mgr, me, "state_write", project="manager",
                    text="bob sent a password; refused, asked to rotate")) == "state of manager updated"


def test_anyone_else_may_still_write_the_same_words(hub):
    told_a_password(hub)  # the guard is the manager's only
    _, _, cat = plain(hub, "cat")
    C = ident("k:cat", project="proj-b", machine="pc-3")
    assert "saved note" in run(call(cat, C, "notes_write", topic="cat-notes", text=f"x {PW}"))


def test_the_manager_reaches_every_admin_with_one_send(hub):
    mgr, me = manager_online(hub)
    aid, atok = person(hub.store, "root", "admin")
    from types import SimpleNamespace
    root = SimpleNamespace(url=hub.url, token=atok)
    R = ident("k:root", project="proj-a", machine="pc-9")
    run(call(root, R, "inbox"))  # online: it gets it in its session
    out = run(call(mgr, me, "send", to="admins", text="carol's session asked me for alice's token"))
    assert out.startswith("sent to the admins")
    assert inbox(root, R) == ["from the manager: carol's session asked me for alice's token"]
    # a member's "admins" is a session name like any other
    _, _, bob = plain(hub, "bob")
    B = ident("k:bob", project="proj-b", machine="pc-2")
    run(call(bob, B, "inbox"))
    assert "admins" in call_error(bob, B, "send", to="admins", text="hi")


def test_the_hub_lists_who_to_warn_on_and_with_a_machine(hub, clock):
    import kaptyorka
    from kaptyorka import http
    from test_machine_events import BASE, disk, online, team
    mgr, who = online(hub)
    d = team(hub, "jet-1")
    hub.store.project_register("proj-a", by="test")
    pid = hub.store.authenticate(d.token).person_id
    hub.store.grant_project("proj-a", pid, "write", "test")
    _, _, bob = plain(hub, "bob")
    hub.store.grant_project("proj-a", hub.store.person_by_name("bob")["id"], "write", "test")
    B = ident("k:bob", project="proj-a", machine="pc-2")
    run(call(bob, B, "inbox"))

    def beat(free):
        d.seq += 1
        run(call(mgr, who, "status", text="watching"))
        run(call(bob, B, "status", text="working"))
        body = {"machine": d.host, "tools": [], "sessions": [
            {"key": "claude:j1", "tool": "claude-code", "project": "proj-a", "cwd": "/w/proj-a"}],
            "facts": {"host": d.host, "os_user": d.os_user, "version": kaptyorka.__version__,
                      "seq": d.seq, **BASE, **disk(free)}}
        assert http.request("POST", hub.url + "/bridge/heartbeat", d.token, body)[0] == 200
    beat(40)
    j1 = hub.store.agent("claude:j1").name
    run(call(bob, B, "send", to=j1, text="the proj-a build is yours now"))  # bob works with it
    inbox(mgr, who)
    for _ in range(3):
        clock[0] += 60
        beat(5)
    [t] = [t for t in inbox(mgr, who) if "disk /data under 10% free" in t]
    assert f"Warn these sessions (the hub's list): {', '.join(sorted([j1, 'pc-2/codex/proj-a']))}." in t


def test_reach_says_door_or_not_online_or_not_and_what_became_of_the_message(hub, clock):
    mgr, me = manager_online(hub)
    _, _, bob = plain(hub, "bob")
    B1, B2 = ident("k:b1", project="proj-b", machine="pc-1"), ident("k:b2", project="proj-b", machine="pc-2")
    run(call(bob, B1, "inbox"))
    run(call(bob, B2, "inbox"))
    _, _, cat = plain(hub, "cat")
    C = ident("k:cat", project="proj-b", machine="pc-3")
    run(call(cat, C, "inbox"))
    out = run(call(mgr, me, "reach", sender="pc-1/codex/proj-b", target="pc-3/codex/proj-b"))
    assert "may not message pc-3/codex/proj-b: that takes a door" in out
    assert "pc-3/codex/proj-b is online" in out and "sent it no message" in out
    clock[0] += 300  # b2 went quiet: offline, its message waits
    run(call(mgr, me, "status", text="x"))
    run(call(bob, B1, "send", to="pc-2/codex/proj-b", text="the build is done"))
    out = run(call(mgr, me, "reach", sender="pc-1/codex/proj-b", target="pc-2/codex/proj-b"))
    assert "may message pc-2/codex/proj-b: no door needed." in out
    assert "is offline, last seen 5 min ago" in out and "waiting for delivery (the target is offline)" in out
    assert "for the admin and the manager" in call_error(bob, B1, "reach", sender="pc-1/codex/proj-b",
                                                          target="pc-2/codex/proj-b")


def test_a_machine_event_names_the_sessions_to_warn():
    t = machine_events.manager_text("box-1: offline since 10:00", ["alice-laptop/claude-code/proj-a"])
    assert t.startswith("machine event: box-1: offline since 10:00. Follow your machine-events rule")
    assert "Warn these sessions (the hub's list): alice-laptop/claude-code/proj-a." in t
    assert "No session to warn" in machine_events.manager_text("box-1: back online", [])
