import functools

import pytest
from helpers import person

from kaptyorka.store import KaptyorkaError, Store


def one_person(s):
    """Sessions seen without a principal belong to one person, so they may message each other."""
    _, token = person(s, "me")
    me = s.authenticate(token)
    s.seen = functools.partial(s.seen, principal=me)
    s.heartbeat = functools.partial(s.heartbeat, principal=me)
    return s


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
    s = one_person(Store(tmp_path / "hub.db", clock=clock))
    yield s
    s.close()


def test_names_get_suffix_when_shared(store):
    a = store.seen("claude:1", "pc", "claude-code", "proj-a")
    b = store.seen("claude:2", "pc", "claude-code", "proj-a")
    assert (a.name, b.name) == ("pc/claude-code/proj-a", "pc/claude-code/proj-a-2")


def test_offline_after_30s(store, clock):
    store.seen("k", "pc", "codex", "nav"); clock.t += 31
    assert store.online() == []


def test_send_unknown_name_lists_online(store):
    store.seen("k", "pc", "codex", "nav")
    with pytest.raises(KaptyorkaError, match="pc/codex/nav"): store.send("k", "box-2/codex/typo", "hi")


def test_send_all_online_only(store, clock):
    for i, m in enumerate(["pc", "box-2", "jet"]): store.seen(f"k{i}", m, "codex", "p")
    clock.t += 31; store.seen("k0", "pc", "codex", "p"); store.seen("k1", "box-2", "codex", "p")
    assert store.send("k0", "all", "hi") == 1


def test_held_message_goes_to_next_session_same_project(store, clock):
    store.seen("s", "pc", "codex", "a"); store.seen("old", "box-2", "claude-code", "proj-a")
    store.send("s", "box-2/claude-code/proj-a", "hi"); clock.t += 31
    store.seen("new", "box-2", "claude-code", "proj-a")
    assert [m.text for m in store.inbox("new")] == ["hi"]
    assert store.inbox("new") == []


def test_message_to_online_dash2_not_taken_by_first(store):
    store.seen("s", "pc", "codex", "a")
    store.seen("one", "box-2", "codex", "m"); store.seen("two", "box-2", "codex", "m")
    store.send("s", "box-2/codex/m-2", "hi")
    assert store.inbox("one") == [] and len(store.inbox("two")) == 1


def test_held_message_not_taken_by_other_session_in_same_folder(store, clock):
    store.seen("s", "pc", "codex", "a")
    store.seen("one", "boo", "opencode", "workspace"); store.seen("two", "boo", "opencode", "workspace")
    store.send("s", "boo/opencode/workspace-2", "hi")
    clock.t += 31; store.seen("s", "pc", "codex", "a"); store.seen("one", "boo", "opencode", "workspace")
    assert store.inbox("one") == []
    store.seen("two", "boo", "opencode", "workspace")
    assert [m.text for m in store.inbox("two")] == ["hi"]


def test_held_message_goes_to_new_session_after_restart(store, clock):
    sess = {"key": "claude:S1", "tool": "claude-code", "project": "proj-a"}
    store.seen("s", "pc", "codex", "a")
    store.heartbeat("box-2", [sess], ["claude-code"])
    store.send("s", "box-2/claude-code/proj-a", "hi"); clock.t += 5
    store.heartbeat("box-2", [], ["claude-code"])  # session gone, name free
    store.seen("s", "pc", "codex", "a")
    new = store.seen("claude:S2", "box-2", "claude-code", "proj-a")
    assert new.name == "box-2/claude-code/proj-a"
    assert [m.text for m in store.inbox("claude:S2")] == ["hi"]


def test_offer_frames_and_ack_marks_read(store):
    store.seen("s", "pc", "codex", "a"); store.seen("t", "box-2", "codex", "b")
    store.send("s", "box-2/codex/b", "hi")
    [o] = store.offer("box-2")
    assert o["key"] == "t" and o["text"] == "Kaptyorka message from pc/codex/a:\n\nhi"
    store.ack(o["id"], True); assert store.inbox("t") == [] and store.offer("box-2") == []


def test_failed_push_stays_for_pull(store):
    store.seen("s", "pc", "codex", "a"); store.seen("t", "box-2", "codex", "b")
    store.send("s", "box-2/codex/b", "hi")
    [o] = store.offer("box-2")
    store.ack(o["id"], False)
    assert store.offer("box-2") == []
    assert [m.text for m in store.inbox("t")] == ["hi"]


def test_offer_lease_reoffers_unacked(store, clock):
    store.seen("s", "pc", "codex", "a"); store.seen("t", "box-2", "codex", "b")
    store.send("s", "box-2/codex/b", "hi")
    [o] = store.offer("box-2")
    assert store.offer("box-2") == []
    clock.t += 61
    store.seen("s", "pc", "codex", "a"); store.seen("t", "box-2", "codex", "b")
    assert [x["id"] for x in store.offer("box-2")] == [o["id"]]


def test_text_and_rate_limits(store):
    store.seen("s", "pc", "codex", "a"); store.seen("t", "box-2", "codex", "b")
    with pytest.raises(KaptyorkaError, match="too long"):
        store.send("s", "box-2/codex/b", "x" * 32_001)
    for _ in range(30):
        store.send("s", "box-2/codex/b", "hi")
    with pytest.raises(KaptyorkaError, match="rate"):
        store.send("s", "box-2/codex/b", "hi")


def test_notes_keep_history(store):
    store.seen("a", "pc", "codex", "x"); store.notes_write("a", "api", "v1"); store.notes_write("a", "api", "v2")
    assert store.notes_read("api").text == "v2"
    assert [n.text for n in store.note_history("api")] == ["v2", "v1"]
    assert store.notes_list()[0]["topic"] == "api"


def test_survives_reopen(tmp_path):
    path = tmp_path / "hub.db"
    s = one_person(Store(path))
    s.seen("a", "pc", "codex", "x"); s.seen("b", "box-2", "codex", "y")
    s.notes_write("a", "api", "v1")
    s.send("a", "box-2/codex/y", "hi")
    s.close()
    s2 = Store(path)
    assert s2.notes_read("api").text == "v1"
    assert [m.text for m in s2.messages()] == ["hi"]
    s2.close()


def test_heartbeat_sets_activity_and_seen_keeps_it(store):
    store.heartbeat("pc", [{"key": "a", "tool": "codex", "project": "p", "activity": "x"}])
    store.seen("a", "pc", "codex", "p")
    assert store.agent("a").activity == "x"
    store.heartbeat("pc", [{"key": "a", "tool": "codex", "project": "p"}])
    assert store.agent("a").activity == "x"
    store.heartbeat("pc", [{"key": "a", "tool": "codex", "project": "p", "activity": "y"}])
    assert store.agent("a").activity == "y"


def test_send_to_offline_held_name_reaches_next_session(store, clock):
    store.seen("s", "pc", "codex", "a"); store.seen("old", "box-2", "claude-code", "proj-a")
    clock.t += 31; store.seen("s", "pc", "codex", "a")
    assert store.send("s", "box-2/claude-code/proj-a", "hi") == 1
    store.seen("new", "box-2", "claude-code", "proj-a")
    assert [m.text for m in store.inbox("new")] == ["hi"]


def test_returning_agent_gets_suffix_if_name_taken(store, clock):
    store.seen("s", "pc", "codex", "a"); store.seen("A", "box-2", "codex", "m")
    clock.t += 31; store.seen("s", "pc", "codex", "a")
    assert store.seen("B", "box-2", "codex", "m").name == "box-2/codex/m"
    assert store.seen("A", "box-2", "codex", "m").name == "box-2/codex/m-2"
    store.send("s", "box-2/codex/m", "hi")
    assert store.inbox("A") == [] and [m.text for m in store.inbox("B")] == ["hi"]


def beat(store, key, project, label=""):
    store.heartbeat("pc", [{"key": key, "tool": "claude-code", "project": project, "label": label}])


def test_send_by_label_case_insensitive(store):
    beat(store, "a", "x", "Proj-b-Dev")
    store.seen("b", "pc", "codex", "y")
    assert store.send("b", "proj-b-dev", "hi") == 1
    assert store.messages()[0].to == "pc/claude-code/x"


def test_same_label_refused_with_both_names(store):
    beat(store, "a", "x", "dup")
    beat(store, "b", "y", "dup")
    store.seen("c", "pc", "codex", "z")
    with pytest.raises(KaptyorkaError) as e:
        store.send("c", "dup", "hi")
    assert str(e.value) == "several sessions are named dup: pc/claude-code/x, pc/claude-code/y"


def test_unknown_name_lists_labels(store):
    beat(store, "a", "x", "nice")
    store.seen("c", "pc", "codex", "z")
    with pytest.raises(KaptyorkaError, match=r"pc/claude-code/x \(nice\)"):
        store.send("c", "nobody", "hi")


def test_rename_updates_label_on_next_heartbeat(store):
    beat(store, "a", "x", "old")
    assert store.agent("a").label == "old"
    beat(store, "a", "x", "new")
    assert store.agent("a").label == "new"
    beat(store, "a", "x", "")
    assert store.agent("a").label == ""


def test_old_db_without_label_column_opens(tmp_path):
    import sqlite3
    path = tmp_path / "hub.db"
    db = sqlite3.connect(str(path))
    db.executescript("""CREATE TABLE agents (
        key TEXT PRIMARY KEY, name TEXT NOT NULL, machine TEXT NOT NULL,
        tool TEXT NOT NULL, project TEXT NOT NULL, status TEXT NOT NULL DEFAULT '',
        activity TEXT NOT NULL DEFAULT '', last_seen REAL NOT NULL);
        INSERT INTO agents VALUES ('a','pc/codex/x','pc','codex','x','','',1000.0);""")
    db.commit(); db.close()
    s = Store(path, clock=lambda: 1001.0)
    assert s.agent("a").label == ""
    beat(s, "a", "x", "named")
    assert s.agent("a").label == "named"
    s.close()
    Store(path).close()  # reopening with the column present is fine


def test_resumed_session_keeps_its_name(store, clock):
    sess = [{"key": "claude:S1", "tool": "claude-code", "project": "proj-a"}]
    store.heartbeat("pc", sess, ["claude-code"]); clock.t += 5
    store.heartbeat("pc", sess, ["claude-code"])  # new pid, same session id: same key
    assert [a.name for a in store.online()] == ["pc/claude-code/proj-a"]


def test_session_missing_from_heartbeat_frees_its_name(store, clock):
    old = {"key": "claude:S1", "tool": "claude-code", "project": "proj-a"}
    other = {"key": "codex:T", "tool": "codex", "project": "proj-a"}
    store.heartbeat("pc", [old, other], ["claude-code", "codex"])
    store.seen("d", "box-2", "claude-code", "proj-a"); clock.t += 5
    store.heartbeat("pc", [other], ["claude-code"])  # codex not covered, box-2 is another machine
    assert {a.key for a in store.online()} == {"codex:T", "d"}
    store.heartbeat("pc", [{"key": "claude:S2", "tool": "claude-code", "project": "proj-a"}],
                    ["claude-code"])
    assert store.agent("claude:S2").name == "pc/claude-code/proj-a"


def test_send_from_web_lands_in_inbox_and_is_pushed(store):
    store.seen("k", "pc", "codex", "nav")
    assert store.send_from_web("alice", "pc/codex/nav", "hello") == 1
    assert [(m.sender, m.text) for m in store.inbox("k")] == [("alice", "hello")]
    store.send_from_web("alice", "pc/codex/nav", "again")
    assert store.offer("pc")[0]["text"] == "Kaptyorka message from alice (web page):\n\nagain"
    assert [m.from_web for m in store.messages()] == [True, True]


def test_send_from_web_to_all_and_no_rate_limit(store):
    store.seen("a", "pc", "codex", "nav")
    store.seen("b", "box-2", "codex", "nav")
    assert store.send_from_web("alice", "all", "hi") == 2
    for _ in range(40):
        store.send_from_web("alice", "pc/codex/nav", "x")


def test_send_from_web_errors(store):
    store.seen("a", "pc", "codex", "nav")
    with pytest.raises(KaptyorkaError, match="no session named"):
        store.send_from_web("alice", "nobody", "hi")
    with pytest.raises(KaptyorkaError, match="name"):
        store.send_from_web("  ", "pc/codex/nav", "hi")


def test_notes_write_from_web(store):
    store.seen("a", "pc", "codex", "nav")
    store.notes_write("a", "api", "v1")
    store.notes_write_from_web("alice", "api", "v2")
    assert [(n.author, n.text) for n in store.note_history("api")] == [("alice", "v2"), ("me", "v1")]
    with pytest.raises(KaptyorkaError, match="topic"):
        store.notes_write_from_web("alice", " ", "x")


def test_agent_replies_to_web_sender(store):
    store.seen("a", "pc", "codex", "nav")
    store.send_from_web("alice", "pc/codex/nav", "hi")
    store.inbox("a")
    with pytest.raises(KaptyorkaError, match="no door to alice"):  # a page name of no person
        store.send("a", "alice", "hello back")
    store.send_from_web("me", "pc/codex/nav", "hi", person_id=store.person_by_name("me")["id"])
    store.inbox("a")
    assert store.send("a", "me", "hello back") == 1  # the session's own person
    m = store.messages()[-1]
    assert (m.sender, m.to, m.read, m.to_web) == ("pc/codex/nav", "me", True, True)
    assert store.inbox("a") == [] and store.offer("pc") == []


def test_unknown_name_lists_people(store):
    store.seen("a", "pc", "codex", "nav")
    store.send_from_web("alice", "pc/codex/nav", "hi")
    with pytest.raises(KaptyorkaError, match="online: pc/codex/nav; people: alice"):
        store.send("a", "Bob", "hi")


def test_offline_label_match(store, clock):
    mgr, _ = person(store, "manager", "manager")
    beat(store, "m", "manager", "Manager")
    beat(store, "o", "x", "box")
    with store.lock:
        store.db.execute("UPDATE agents SET person_id=? WHERE key='m'", (mgr,))
    clock.t += 31  # both offline
    assert store.send_from_web("alice", "manager", "hi") == 1
    assert store.messages()[0].to == "pc/claude-code/manager"
    with pytest.raises(KaptyorkaError, match="no session named box"):
        store.send_from_web("alice", "box", "hi")
    beat(store, "a", "x", "box")
    beat(store, "b", "y", "box")
    with pytest.raises(KaptyorkaError, match="several sessions are named box"):
        store.send_from_web("alice", "box", "hi")
