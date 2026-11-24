"""The feed names each message's session sides by handle (sender_id, to_id), so the page keys
chats by a session's handle, never by its name: two sessions that swap or reuse a name keep
their own histories. The handle is the opaque one each session row already carries; no raw
key reaches the page, and a hidden session stays masked."""
import json
import time

from helpers import person
from kaptyorka import hub_feed
from kaptyorka.manager import ManagerStore
from kaptyorka.store import Store, session_id
from test_working_together import said


def to_page(store, key, person_name, text):
    with store.lock:
        s = store.db.execute("SELECT * FROM agents WHERE key=?", (key,)).fetchone()
        store.db.execute(
            "INSERT INTO messages (sender_key, sender_name, to_key, to_name, machine, tool, project,"
            " text, created) VALUES (?,?,?,?,?,?,?,?,?)",
            (key, s["name"], "web:" + person_name, person_name, s["machine"], s["tool"], s["project"],
             text, store.clock()))


def setup(tmp_path):
    s = Store(tmp_path / "hub.db")
    ManagerStore(s).set_enabled(False, "test")
    person(s, "alice", "admin")
    bob, btok = person(s, "bob")
    carol, ctok = person(s, "carol")
    s.project_register("proj-a", by="alice")
    s.project_register("proj-c", by="alice")
    s.grant_project("proj-a", bob, "write", "alice")
    s.grant_project("proj-c", carol, "write", "alice")
    s.heartbeat("box-2", [{"key": "opencode:o1", "tool": "opencode", "project": "proj-a"},
                          {"key": "opencode:o2", "tool": "opencode", "project": "proj-a"}],
                principal=s.authenticate(btok), facts={"host": "box-2", "os_user": "bob"})
    s.heartbeat("box-4", [{"key": "codex:c1", "tool": "codex", "project": "proj-c"}],
                principal=s.authenticate(ctok), facts={"host": "box-4", "os_user": "carol"})
    return s, bob


def test_every_session_side_carries_its_handle_never_its_key(tmp_path):
    s, bob = setup(tmp_path)
    s.send_from_web("bob", s.agent("opencode:o1").name, "for one", bob)
    to_page(s, "opencode:o2", "bob", "from two")
    said(s, "opencode:o2", "codex:c1", "to carol's hidden session")
    f = hub_feed.feed(s, s.authenticate(_token(s, "bob")))
    by_text = {m["text"]: m for m in f["messages"]}
    assert by_text["for one"]["to_id"] == session_id("opencode:o1") and "sender_id" not in by_text["for one"]
    assert by_text["from two"]["sender_id"] == session_id("opencode:o2") and "to_id" not in by_text["from two"]
    hidden = by_text["to carol's hidden session"]
    assert hidden["to_id"] == session_id("codex:c1") and "proj-c" not in hidden["to"]  # still masked
    rows = {a["id"] for a in f["agents"]}
    assert session_id("opencode:o1") in rows  # the same handle the session row has
    raw = json.dumps(f)
    for key in ("opencode:o1", "opencode:o2", "codex:c1"):
        assert key not in raw
    s.close()


def test_a_gone_session_keeps_its_handle_in_old_messages(tmp_path):
    s, bob = setup(tmp_path)
    to_page(s, "opencode:o2", "bob", "before it closed")
    with s.lock:
        s.db.execute("DELETE FROM agents WHERE key='opencode:o2'")
    f = hub_feed.feed(s, s.authenticate(_token(s, "bob")))
    m = [m for m in f["messages"] if m["text"] == "before it closed"][0]
    assert m["sender_id"] == session_id("opencode:o2")
    s.close()


def _token(s, name):
    """A fresh device token for `name` (person() keeps its own)."""
    pid = s.db.execute("SELECT id FROM people WHERE name=?", (name,)).fetchone()["id"]
    return s.device_add(pid, f"pc-{name}-{time.time_ns()}")
