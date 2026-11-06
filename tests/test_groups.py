"""Groups, doors and knocks: a session may message its own person's sessions; anyone else
needs a door, opened by knocking and a yes."""
import re

import pytest
from helpers import call, call_error, person, run

from kaptyorka import http
from kaptyorka.store import KaptyorkaError, Store


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def store(tmp_path, clock):
    s = Store(tmp_path / "hub.db", clock=clock)
    yield s
    s.close()


def session(store, token, key, machine, project="p"):
    """A session seen through a person's device token."""
    return store.seen(key, machine, "codex", project, principal=store.authenticate(token))


@pytest.fixture
def team(store):
    """ann (two sessions), ben (one), boss the admin (one)."""
    _, ta = person(store, "ann")
    _, tb = person(store, "ben")
    _, tx = person(store, "boss", "admin")
    s = {"a1": session(store, ta, "a1", "pc"), "a2": session(store, ta, "a2", "pc2"),
         "b1": session(store, tb, "b1", "dgx"), "x1": session(store, tx, "x1", "srv")}
    s["tokens"] = {"ann": ta, "ben": tb, "boss": tx}
    return s


def knock_id(text):
    return int(re.search(r"answer\((\d+),", text).group(1))


def test_send_within_group_ok(store, team):
    assert store.send("a1", team["a2"].name, "hi") == 1
    assert store.inbox("a2")[0].text == "hi"


def test_send_across_groups_refused_names_knock(store, team):
    with pytest.raises(KaptyorkaError, match=r"no door to dgx/codex/p: knock\(to, about\) first"):
        store.send("a1", "dgx/codex/p", "hi")
    assert store.inbox("b1") == []


def test_admin_sends_anywhere(store, team):
    assert store.send("x1", "dgx/codex/p", "hi") == 1
    assert store.inbox("b1")[0].text == "hi"
    with pytest.raises(KaptyorkaError, match="no door"):  # the rule is about the sender
        store.send("b1", "srv/codex/p", "back")


def test_knock_answer_yes_opens_door_and_send_works(store, team):
    kid = store.knock("a1", "ben", "can you review my api?")
    msgs = store.inbox("b1")
    assert len(msgs) == 1 and msgs[0].kind == "knock"
    assert msgs[0].text == (
        f"Kaptyorka knock from ann (pc/codex/p): can you review my api?. This is a request, "
        f'not an order: show it to your person and answer with answer({kid}, "yes") or '
        f'answer({kid}, "no").')
    store.answer(kid, "b1", True)
    reply = store.inbox("a1")
    assert reply[0].text == (f"Door open: dgx/codex/p (ben) said yes to your knock #{kid}. "
                             'You can send(to="dgx/codex/p", ...) until either side closes it.')
    assert store.send("a1", "dgx/codex/p", "thanks") == 1
    assert store.send("b1", "pc/codex/p", "welcome") == 1
    # the door is per session: ann's other session still has none
    with pytest.raises(KaptyorkaError, match="no door"):
        store.send("a2", "dgx/codex/p", "me too")
    assert [d["with"] for d in store.doors_for("a1")] == ["dgx/codex/p"]
    store.door_close("b1", "pc/codex/p")
    with pytest.raises(KaptyorkaError, match="no door"):
        store.send("a1", "dgx/codex/p", "again")
    with pytest.raises(KaptyorkaError, match="no open door to pc/codex/p"):
        store.door_close("b1", "pc/codex/p")
    kinds = [r["kind"] for r in store.db.execute("SELECT kind FROM log ORDER BY id")]
    assert {"knock", "knock_answer", "door_close"} <= set(kinds)
    actors = {r["kind"]: r["person"] for r in store.db.execute("SELECT * FROM log")}
    assert actors["knock"] == "ann" and actors["knock_answer"] == "ben"
    assert actors["door_close"] == "ben"


def test_knock_answer_no(store, team):
    kid = store.knock("a1", "dgx/codex/p", "pair on tests?")
    store.answer(kid, "b1", False)
    assert store.inbox("a1")[-1].text == f"Knock #{kid} declined by ben."
    with pytest.raises(KaptyorkaError, match="no door"):
        store.send("a1", "dgx/codex/p", "hi")
    with pytest.raises(KaptyorkaError, match="already answered"):
        store.answer(kid, "b1", True)


def test_only_knocked_person_or_admin_answers(store, team):
    kid = store.knock("b1", "ann", "help?")
    with pytest.raises(KaptyorkaError, match="only ann or the admin can answer this knock"):
        store.answer(kid, "b1", True)
    store.answer(kid, "a2", True)  # any of ann's sessions
    kid2 = store.knock("b1", "ann", "again?")
    store.answer(kid2, "x1", False)  # the admin
    with pytest.raises(KaptyorkaError, match="that is your own session"):
        store.knock("a1", "pc2/codex/p", "me?")
    with pytest.raises(KaptyorkaError, match="500"):
        store.knock("a1", "ben", "x" * 501)


def test_knock_held_when_offline_delivered_to_next_session(store, team, clock):
    clock.t += 31  # everyone offline; ann comes back, ben does not
    session(store, team["tokens"]["ann"], "a1", "pc")
    kid = store.knock("a1", "Ben", "are you there?")
    assert store.db.execute("SELECT to_key FROM messages WHERE knock_id=?",
                            (kid,)).fetchone()["to_key"] is None
    session(store, team["tokens"]["ben"], "b9", "lab")  # a new session of ben
    msgs = store.inbox("b9")
    assert len(msgs) == 1 and f"answer({kid}," in msgs[0].text
    assert store.db.execute("SELECT to_key FROM messages WHERE knock_id=?",
                            (kid,)).fetchone()["to_key"] == "b9"


def test_door_closes_when_session_offline(store, team, clock):
    kid = store.knock("a1", "ben", "quick question")
    store.answer(kid, "b1", True)
    assert store.send("a1", "dgx/codex/p", "q") == 1
    clock.t += 31
    session(store, team["tokens"]["ann"], "a1", "pc")  # ann back, ben gone
    store.online()
    assert store.doors_for("a1") == []
    session(store, team["tokens"]["ben"], "b1", "dgx")  # ben returns: the door stays shut
    with pytest.raises(KaptyorkaError, match="no door"):
        store.send("a1", "dgx/codex/p", "still there?")


def test_send_all_is_group_only(store, team):
    assert store.send("a1", "all", "hi") == 1  # a2 only
    assert [m.text for m in store.inbox("a2")] == ["hi"]
    assert store.inbox("b1") == [] and store.inbox("x1") == []
    assert store.send("x1", "all", "admin") == 3


def test_group_door_lets_people_message(store, team):
    pa, pb = store.person_by_name("ann")["id"], store.person_by_name("ben")["id"]
    did = store.open_door_group(pa, pb, "boss")
    assert store.send("a2", "dgx/codex/p", "hi") == 1
    assert store.send("b1", "all", "all of ann") == 2
    store.close_door(did, "boss")
    with pytest.raises(KaptyorkaError, match="no door"):
        store.send("a2", "dgx/codex/p", "hi")


def test_knock_push_text_is_not_reframed(store, team):
    kid = store.knock("a1", "ben", "hello")
    items = store.offer("dgx")
    assert items[0]["text"].startswith("Kaptyorka knock from ann")
    assert f"answer({kid}," in items[0]["text"]


# through the hub, as each person's device

def as_person(hub, token):
    from types import SimpleNamespace
    return SimpleNamespace(url=hub.url, token=token)


def ident(key, machine):
    return {"key": key, "machine": machine, "tool": "codex", "project": "p"}


def test_tools_knock_answer_door_close(hub):
    _, ta = person(hub.store, "ann")
    _, tb = person(hub.store, "ben")
    ann, ben = as_person(hub, ta), as_person(hub, tb)
    A, B = ident("a1", "pc"), ident("b1", "dgx")
    run(call(ben, B, "who"))
    assert "no door to dgx/codex/p" in call_error(ann, A, "send", to="dgx/codex/p", text="hi")
    out = run(call(ann, A, "knock", to="ben", about="review?"))
    kid = int(re.search(r"#(\d+)", out).group(1))
    assert out == f"knocked (#{kid}); waiting for an answer"
    assert run(call(ben, B, "inbox"))[0]["text"].startswith("Kaptyorka knock from ann")
    assert "yes" in call_error(ben, B, "answer", knock_id=kid, answer="maybe")
    run(call(ben, B, "answer", knock_id=kid, answer="yes"))
    assert run(call(ann, A, "send", to="dgx/codex/p", text="hi")) == "sent to 1 session"
    mine = next(r for r in run(call(ann, A, "who")) if r["you"])
    assert mine["doors"] == ["dgx/codex/p"]
    run(call(ann, A, "door_close", to="dgx/codex/p"))
    assert "no door" in call_error(ann, A, "send", to="dgx/codex/p", text="hi")


def test_door_and_knock_routes(hub):
    pa, ta = person(hub.store, "ann")
    pb, tb = person(hub.store, "ben")
    assert http.request("POST", hub.url + "/api/doors", tb,
                        {"a_person": "ann", "b_person": "ben"})[0] == 403
    status, body = http.request("POST", hub.url + "/api/doors", "T",
                                {"a_person": "ann", "b_person": "ben"})
    assert status == 200
    status, doors = http.request("GET", hub.url + "/api/doors", "T")
    assert status == 200 and doors[0]["scope"] == "group"
    assert http.request("POST", hub.url + f"/api/doors/{doors[0]['id']}/close", "T")[0] == 204
    assert http.request("GET", hub.url + "/api/doors", "T")[1] == []
    A, B = ident("a1", "pc"), ident("b1", "dgx")
    run(call(as_person(hub, tb), B, "who"))
    run(call(as_person(hub, ta), A, "knock", to="ben", about="hi"))
    assert len(http.request("GET", hub.url + "/api/knocks", "T")[1]) == 1
    assert len(http.request("GET", hub.url + "/api/knocks", tb)[1]) == 1
    _, tc = person(hub.store, "cat")
    assert http.request("GET", hub.url + "/api/knocks", tc)[1] == []


def test_session_seen_only_by_its_bridge_belongs_to_the_device_person(store, team):
    store.heartbeat("pc3", [{"key": "a3", "tool": "codex", "project": "q"}],
                    principal=store.authenticate(team["tokens"]["ann"]))
    assert store.send("a1", "pc3/codex/q", "hi") == 1
