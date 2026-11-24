"""A send to a session that went quiet a moment ago is held for it, not refused."""
from types import SimpleNamespace

import pytest
from helpers import call, call_error, person, run

from kaptyorka.store import UNREACHABLE, session_id


def ident(key):
    return {"key": key, "machine": "box-1", "tool": "codex", "project": "proj-a"}


@pytest.fixture
def team(hub):
    """alice and bob (members) and the admin, each with a session on box-1, on a fake clock."""
    s, t = hub.store, [10_000.0]
    s.clock = lambda: t[0]
    tokens, keys = {}, {}
    for name, role in (("alice", "member"), ("bob", "member"), ("admin", "admin")):
        _, tokens[name] = person(s, name, role)
        keys[name] = f"k:{name}"

    def beat(*who):
        for name in who:
            s.heartbeat("box-1", [{"key": keys[name], "tool": "codex", "project": "proj-a",
                                   "label": f"{name}-desk"}],
                        principal=s.authenticate(tokens[name]),
                        facts={"host": "box-1", "os_user": name})

    beat("alice", "bob", "admin")
    return SimpleNamespace(hub=hub, s=s, t=t, tokens=tokens, keys=keys, beat=beat,
                           as_=lambda n: SimpleNamespace(url=hub.url, token=tokens[n]))


def test_held_send_reaches_the_session_on_its_next_report(team):
    team.t[0] += 34
    team.beat("admin")  # the admin stays online; bob's session went quiet 34 s ago
    out = run(call(team.as_("admin"), ident(team.keys["admin"]), "send", to="bob-desk", text="hi"))
    assert out == ("held: box-1/codex/proj-a-2 was last seen 34 s ago; it gets the message"
                   " when it next reports")
    by_id = session_id(team.keys["bob"])
    assert run(call(team.as_("admin"), ident(team.keys["admin"]), "send", to=by_id,
                    text="again")).startswith("held: box-1/codex/proj-a-2 was last seen 34 s")
    assert [m.read for m in team.s.messages() if m.to == "box-1/codex/proj-a-2"] == [False, False]
    team.t[0] += 60
    team.beat("bob")  # back: the next report pushes both
    assert [o["text"].split("\n\n")[1].split("\n")[0] for o in team.s.offer("box-1")
            if o["key"] == team.keys["bob"]] == ["hi", "again"]


def test_held_send_expires_after_ten_minutes(team):
    team.t[0] += 601
    team.beat("admin")
    err = call_error(team.as_("admin"), ident(team.keys["admin"]), "send", to="bob-desk", text="x")
    assert "no session named bob-desk" in err
    assert call_error(team.as_("admin"), ident(team.keys["admin"]), "send",
                      to=session_id(team.keys["bob"]), text="x").endswith("no session named " + session_id(team.keys["bob"])
        + "; online: box-1/codex/proj-a (admin-desk)")


def test_online_target_still_says_sent(team):
    assert run(call(team.as_("admin"), ident(team.keys["admin"]), "send", to="bob-desk",
                    text="hi")) == "sent to 1 session"


def test_held_send_checks_doors_like_an_online_one(team):
    """alice may not reach bob online, so not while he is briefly gone either."""
    err_online = call_error(team.as_("alice"), ident(team.keys["alice"]), "send", to="bob-desk",
                            text="x")
    team.t[0] += 40
    team.beat("alice", "admin")
    err_gone = call_error(team.as_("alice"), ident(team.keys["alice"]), "send", to="bob-desk",
                          text="x")
    assert err_online == err_gone and err_gone.endswith(UNREACHABLE.format(to="bob-desk"))
