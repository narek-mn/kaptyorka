import pytest

from kubrik.store import KubrikError, Store


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
    return Store(tmp_path / "hub.db", clock=clock)


def test_names_get_suffix_when_shared(store):
    a = store.seen("claude:1", "pc", "claude-code", "murmur")
    b = store.seen("claude:2", "pc", "claude-code", "murmur")
    assert (a.name, b.name) == ("pc/claude-code/murmur", "pc/claude-code/murmur-2")


def test_offline_after_30s(store, clock):
    store.seen("k", "pc", "codex", "nav"); clock.t += 31
    assert store.online() == []


def test_send_unknown_name_lists_online(store):
    store.seen("k", "pc", "codex", "nav")
    with pytest.raises(KubrikError, match="pc/codex/nav"): store.send("k", "dgx/codex/typo", "hi")


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
    assert o["key"] == "t" and o["text"] == "Kubrik message from pc/codex/a:\n\nhi"
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
    with pytest.raises(KubrikError, match="too long"):
        store.send("s", "dgx/codex/b", "x" * 32_001)
    for _ in range(30):
        store.send("s", "dgx/codex/b", "hi")
    with pytest.raises(KubrikError, match="rate"):
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
    s2 = Store(path)
    assert s2.notes_read("api").text == "v1"
    assert [m.text for m in s2.messages()] == ["hi"]
