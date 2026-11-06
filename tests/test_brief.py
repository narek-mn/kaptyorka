"""The start brief: sections in order, a character budget, a short per-session cache."""
import pytest

from helpers import person
from kaptyorka import brief
from kaptyorka.memory import MemoryStore
from kaptyorka.store import Store
from kaptyorka.workers import WorkerStore

A = "/home/alice/proj-a"
STATE = ("# What it is\nA shop.\n\n# Where it stands\nBeta.\n\n# Decisions\nSQLite.\n\n"
         "# Open problems\nNone.\n\n# How to run and test\npytest")


@pytest.fixture
def clock():
    return [1_000_000.0]


@pytest.fixture
def store(tmp_path, clock):
    s = Store(tmp_path / "hub.db", clock=lambda: clock[0])
    yield s
    s.close()


@pytest.fixture
def ws(store):
    return WorkerStore(store)


@pytest.fixture
def ms(store):
    return MemoryStore(store)


def device(store, name, host):
    pid = person(store, name)[0]
    code = store.invite_create(pid)
    token, _ = store.invite_redeem(code, f"{name}-dev", host=host, os_user="u")
    return store.authenticate(token)


@pytest.fixture
def team(store, ws, ms, clock):
    """alice on box-1 in proj-a (read access, folder reported); bob on box-2 in proj-a."""
    alice, bob = device(store, "alice", "box-1"), device(store, "bob", "box-2")
    store.project_register("proj-a", description="the shop")
    store.grant_project("proj-a", alice.person_id, "read", by="t")
    store.grant_project("proj-a", bob.person_id, "write", by="t")
    ws.facts_seen(alice.device_id, {"host": "box-1", "os_user": "u"}, {"proj-a": A})
    store.heartbeat("box-1", [{"key": "a:1", "tool": "claude-code", "project": "proj-a", "cwd": A}],
                    principal=alice, facts={"host": "box-1", "os_user": "u"})
    store.heartbeat("box-2", [{"key": "b:1", "tool": "codex", "project": "proj-a",
                               "cwd": "/home/bob/proj-a"}],
                    principal=bob, facts={"host": "box-2", "os_user": "u"})
    store.answer(store.knock("b:1", "alice", "talk"), "a:1", True)  # a door, so bob may send
    store.inbox("a:1")
    return alice, bob


def rich(store, ms, clock, alice, bob):
    store.notes_write_from_web("bob", "state", STATE, "proj-a")
    ms.git_seen(alice, alice.device_id, {A: {"branch": "main", "dirty": False,
                                             "subject": "add cart"}}, {"proj-a": A})
    store.send("b:1", store.agent("a:1").name, "hi alice")
    clock[0] += 7200


def build(store, ws, ms, principal, key="a:1", rules=""):
    return brief.build(store, ws, ms, store.agent(key), principal, rules)


def test_brief_sections_in_order(store, ws, ms, clock, team):
    alice, bob = team
    rich(store, ms, clock, alice, bob)
    store.heartbeat("box-1", [{"key": "a:1", "tool": "claude-code", "project": "proj-a", "cwd": A}],
                    principal=alice, facts={"host": "box-1", "os_user": "u"})
    store.heartbeat("box-2", [{"key": "b:1", "tool": "codex", "project": "proj-a",
                               "cwd": "/home/bob/proj-a"}],
                    principal=bob, facts={"host": "box-2", "os_user": "u"})
    store.knock("b:1", "alice", "pair on the cart")
    text = build(store, ws, ms, alice, rules="Team rules:\nbe kind")
    me = store.agent("a:1").name
    heads = ["Team rules:", f"You are `{me}`, alice, on box-1.", "Projects you may read:",
             "State of proj-a:", "Recent git activity (7 days):", "Unread messages:"]
    at = [text.index(h) for h in heads]
    assert at == sorted(at), text
    assert "1 other online: box-2/codex/proj-a." in text
    assert 'Knocks for you: bob (box-2/codex/proj-a) about "pair on the cart".' in text
    assert f"- proj-a: the shop (working now: {me}, box-2/codex/proj-a)" in text
    assert "# Decisions\nSQLite." in text
    assert '- alice on main: "add cart" (2 hours ago)' in text
    assert f"Branches with open sessions: main ({me})" in text
    assert "hi alice" in text.split("Unread messages:")[1]
    assert len(text) <= brief.BUDGET


def test_brief_minimal_session(store, ws, ms):
    carol = device(store, "carol", "box-3")
    store.heartbeat("box-3", [{"key": "c:1", "tool": "codex", "project": "proj-z", "cwd": "/x"}],
                    principal=carol, facts={"host": "box-3", "os_user": "u"})
    text = build(store, ws, ms, carol, key="c:1")
    assert text == "You are `box-3/codex/proj-z`, carol, on box-3. No one else online." \
                   " 0 unread messages."
    assert len(text) < 400
    for head in ("Projects", "State of", "Recent git", "Branches", "Unread messages:"):
        assert head not in text


def test_brief_budget_cuts_in_order(store, ws, ms, clock, team):
    alice, bob = team
    for i in range(80):  # a long project list
        store.project_register(f"proj-{i:02d}", description="d" * 60)
        store.grant_project(f"proj-{i:02d}", alice.person_id, "read", by="t")
    big = STATE + "".join(f"\n\nparagraph {i} " + "x" * 300 for i in range(25))
    store.notes_write_from_web("bob", "state", big, "proj-a")
    ms.git_seen(alice, alice.device_id, {A: {"branch": "main", "subject": "s" * 200}},
                {"proj-a": A})
    pointer = "Team rules are longer than fit here; read them with rules()."

    text = build(store, ws, ms, alice, rules="Team rules:\n" + "r" * 5000)
    assert pointer in text and "rrrr" not in text  # rules first
    assert "Projects you may read:" not in text  # then the project list
    assert "Recent git activity" not in text  # then git
    assert "… (read the rest with state(project))" in text  # the state note last
    assert "# What it is\nA shop." in text and "paragraph 24" not in text
    assert text.startswith(pointer) and "You are `" in text
    assert len(text) <= brief.BUDGET

    text = build(store, ws, ms, alice, rules="Team rules:\n" + "r" * 6000)
    assert pointer in text and len(text) <= brief.BUDGET

    store.notes_write_from_web("bob", "state", STATE, "proj-a")  # a small note: projects go only
    text = build(store, ws, ms, alice, rules="Team rules:\n" + "r" * 5000)
    assert pointer in text and "Projects you may read:" not in text
    assert "Recent git activity" in text and "read the rest" not in text


def test_brief_never_cuts_section_two_or_unread(store, ws, ms, team):
    alice, _ = team
    store.send("b:1", store.agent("a:1").name, "m" * 7000)
    text = build(store, ws, ms, alice, rules="r" * 100)
    assert "m" * 7000 in text and "You are `" in text


def test_brief_cache_10s_but_new_message_breaks_it(store, clock, team):
    me = store.agent("a:1").name
    cache, calls = {}, []

    def get():
        def builder():
            calls.append(1)
            return "brief " + ",".join(m.text for m in store.inbox("a:1"))
        return brief.cached("a:1", builder, unread=lambda: brief.unread_count(store, "a:1"),
                            now=clock[0], cache=cache)

    assert get() == "brief "
    clock[0] += brief.CACHE_S - 1
    assert get() == "brief " and len(calls) == 1  # cached
    store.send("b:1", me, "new")
    assert get() == "brief new" and len(calls) == 2  # a new message is never hidden
    assert brief.unread_count(store, "a:1") == 0
    clock[0] += brief.CACHE_S
    assert get() == "brief " and len(calls) == 3  # expired
    assert brief.cached("b:1", lambda: "other", unread=lambda: 0, now=clock[0],
                        cache=cache) == "other"  # per session
