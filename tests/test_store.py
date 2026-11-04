import pytest

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


def test_names_get_suffix_when_shared(store):
    a = store.seen("claude:1", "pc", "claude-code", "murmur")
    b = store.seen("claude:2", "pc", "claude-code", "murmur")
    assert (a.name, b.name) == ("pc/claude-code/murmur", "pc/claude-code/murmur-2")


def test_offline_after_30s(store, clock):
    store.seen("k", "pc", "codex", "nav"); clock.t += 31
    assert store.online() == []


def test_send_unknown_name_lists_online(store):
    store.seen("k", "pc", "codex", "nav")
    with pytest.raises(KaptyorkaError, match="pc/codex/nav"): store.send("k", "dgx/codex/typo", "hi")


def test_send_all_online_only(store, clock):
    for i, m in enumerate(["pc", "dgx", "jet"]): store.seen(f"k{i}", m, "codex", "p")
    clock.t += 31; store.seen("k0", "pc", "codex", "p"); store.seen("k1", "dgx", "codex", "p")
    assert store.send("k0", "all", "hi") == 1


def test_held_message_goes_to_next_session_same_project(store, clock):
    store.seen("s", "pc", "codex", "a"); store.seen("old", "dgx", "claude-code", "murmur")
    store.send("s", "dgx/claude-code/murmur", "hi"); clock.t += 31
    store.seen("new", "dgx", "claude-code", "murmur")
    assert [m.text for m in store.inbox("new")] == ["hi"]
    assert store.inbox("new") == []


def test_message_to_online_dash2_not_taken_by_first(store):
    store.seen("s", "pc", "codex", "a")
    store.seen("one", "dgx", "codex", "m"); store.seen("two", "dgx", "codex", "m")
    store.send("s", "dgx/codex/m-2", "hi")
    assert store.inbox("one") == [] and len(store.inbox("two")) == 1


def test_offer_frames_and_ack_marks_read(store):
    store.seen("s", "pc", "codex", "a"); store.seen("t", "dgx", "codex", "b")
    store.send("s", "dgx/codex/b", "hi")
    [o] = store.offer("dgx")
    assert o["key"] == "t" and o["text"] == "Kaptyorka message from pc/codex/a:\n\nhi"
    store.ack(o["id"], True); assert store.inbox("t") == [] and store.offer("dgx") == []


def test_failed_push_stays_for_pull(store):
    store.seen("s", "pc", "codex", "a"); store.seen("t", "dgx", "codex", "b")
    store.send("s", "dgx/codex/b", "hi")
    [o] = store.offer("dgx")
    store.ack(o["id"], False)
    assert store.offer("dgx") == []
    assert [m.text for m in store.inbox("t")] == ["hi"]


def test_offer_lease_reoffers_unacked(store, clock):
    store.seen("s", "pc", "codex", "a"); store.seen("t", "dgx", "codex", "b")
    store.send("s", "dgx/codex/b", "hi")
    [o] = store.offer("dgx")
    assert store.offer("dgx") == []
    clock.t += 61
    store.seen("s", "pc", "codex", "a"); store.seen("t", "dgx", "codex", "b")
    assert [x["id"] for x in store.offer("dgx")] == [o["id"]]


def test_text_and_rate_limits(store):
    store.seen("s", "pc", "codex", "a"); store.seen("t", "dgx", "codex", "b")
    with pytest.raises(KaptyorkaError, match="too long"):
        store.send("s", "dgx/codex/b", "x" * 32_001)
    for _ in range(30):
        store.send("s", "dgx/codex/b", "hi")
    with pytest.raises(KaptyorkaError, match="rate"):
        store.send("s", "dgx/codex/b", "hi")


def test_notes_keep_history(store):
    store.seen("a", "pc", "codex", "x"); store.notes_write("a", "api", "v1"); store.notes_write("a", "api", "v2")
    assert store.notes_read("api").text == "v2"
    assert [n.text for n in store.note_history("api")] == ["v2", "v1"]
    assert store.notes_list()[0]["topic"] == "api"


def test_survives_reopen(tmp_path):
    path = tmp_path / "hub.db"
    s = Store(path)
    s.seen("a", "pc", "codex", "x"); s.seen("b", "dgx", "codex", "y")
    s.notes_write("a", "api", "v1")
    s.send("a", "dgx/codex/y", "hi")
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
    store.seen("s", "pc", "codex", "a"); store.seen("old", "dgx", "claude-code", "murmur")
    clock.t += 31; store.seen("s", "pc", "codex", "a")
    assert store.send("s", "dgx/claude-code/murmur", "hi") == 1
    store.seen("new", "dgx", "claude-code", "murmur")
    assert [m.text for m in store.inbox("new")] == ["hi"]


def test_returning_agent_gets_suffix_if_name_taken(store, clock):
    store.seen("s", "pc", "codex", "a"); store.seen("A", "dgx", "codex", "m")
    clock.t += 31; store.seen("s", "pc", "codex", "a")
    assert store.seen("B", "dgx", "codex", "m").name == "dgx/codex/m"
    assert store.seen("A", "dgx", "codex", "m").name == "dgx/codex/m-2"
    store.send("s", "dgx/codex/m", "hi")
    assert store.inbox("A") == [] and [m.text for m in store.inbox("B")] == ["hi"]


def beat(store, key, project, label=""):
    store.heartbeat("pc", [{"key": key, "tool": "claude-code", "project": project, "label": label}])


def test_send_by_label_case_insensitive(store):
    beat(store, "a", "x", "Backend-Dev")
    store.seen("b", "pc", "codex", "y")
    assert store.send("b", "backend-dev", "hi") == 1
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
