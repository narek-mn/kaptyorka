"""Each caller acts only for its own person: bridge routes, heartbeats, session keys, names."""
import pytest

from conftest import start_hub
from helpers import person
from kaptyorka import http
from kaptyorka.store import ONLINE_S, KaptyorkaError, Store


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


@pytest.fixture
def two(tmp_path):
    """A store with alice, bob and an admin root, each with one device and one session."""
    c = Clock()
    s = Store(tmp_path / "hub.db", clock=c)
    _, ta = person(s, "alice")
    _, tb = person(s, "bob")
    _, tr = person(s, "root", "admin")
    pa, pb, pr = (s.authenticate(t) for t in (ta, tb, tr))
    s.seen("a1", "alice@pc", "codex", "proj-a", principal=pa)
    s.seen("b1", "bob@pc", "codex", "proj-b", principal=pb)
    s.seen("r1", "root@srv", "codex", "p", principal=pr)
    return s, c, (ta, tb, tr), (pa, pb, pr)


def test_offer_only_to_own_sessions(two):
    s, _, _, (pa, pb, _) = two
    s.send("r1", "alice@pc/codex/proj-a", "for alice")
    assert s.offer("alice@pc", "", pb) == []  # bob names alice's machine: nothing
    items = s.offer("alice@pc", "", pa)
    assert [i["text"].endswith("for alice") for i in items] == [True]


def test_offer_respects_the_device(two, tmp_path):
    s, _, _, (pa, _, _) = two
    import hashlib
    with s.lock:  # a second device of alice's on another machine
        s.db.execute("INSERT INTO devices (person_id, name, token_hash, created) VALUES (?,?,?,?)",
                     (pa.person_id, "alice-desk", hashlib.sha256(b"D2").hexdigest(), 0))
    other = s.authenticate("D2")
    s.send("r1", "alice@pc/codex/proj-a", "hi")
    assert s.offer("alice@pc", "", other) == []
    assert len(s.offer("alice@pc", "", pa)) == 1


def test_ack_only_own(two):
    s, _, _, (pa, pb, _) = two
    s.send("r1", "alice@pc/codex/proj-a", "for alice")
    mid = s.offer("alice@pc", "", pa)[0]["id"]
    assert s.ack(mid, True, pb) is False
    assert not s.messages()[-1].read
    assert s.ack(mid, True, pa) is True
    assert s.messages()[-1].read


def test_bridge_routes_scoped(tmp_path):
    s = Store(tmp_path / "hub.db")
    _, ta = person(s, "alice")
    _, tb = person(s, "bob")
    _, tr = person(s, "root", "admin")
    s.seen("a1", "alice@pc", "codex", "proj-a", principal=s.authenticate(ta))
    s.seen("r1", "root@srv", "codex", "p", principal=s.authenticate(tr))
    s.send("r1", "alice@pc/codex/proj-a", "secret for alice")
    stop = start_hub(s)
    url = f"http://127.0.0.1:{stop.port}"
    try:
        status, items = http.request("GET", f"{url}/bridge/poll?machine=alice@pc&wait=0", tb)
        assert (status, items) == (200, [])
        mid = s.messages()[-1].id
        assert http.request("POST", url + "/bridge/ack", tb, {"id": mid, "ok": True})[0] == 404
        assert not s.messages()[-1].read
        status, items = http.request("GET", f"{url}/bridge/poll?machine=alice@pc&wait=0", ta)
        assert [i["id"] for i in items] == [mid]
        assert http.request("POST", url + "/bridge/ack", ta, {"id": mid, "ok": True})[0] == 204
    finally:
        stop()


def test_heartbeat_leaves_other_people_alone(two):
    s, _, _, (pa, pb, _) = two
    s.set_status("a1", "busy")
    # bob claims alice's machine with an empty complete list, and names her session key
    s.heartbeat("alice@pc", [], ["codex"], principal=pb)
    s.heartbeat("bob@pc", [{"key": "a1", "tool": "codex", "project": "zzz", "label": "pwned"}],
                ["codex"], principal=pb)
    a = s.agent("a1")
    assert a.online and a.label == "" and a.person_id == pa.person_id and a.machine == "alice@pc"
    with s.lock:
        assert s.db.execute("SELECT device_id FROM agents WHERE key='a1'").fetchone()[0] \
            == pa.device_id


def test_heartbeat_still_expires_own(two):
    s, _, _, (pa, _, _) = two
    s.heartbeat("alice@pc", [], ["codex"], principal=pa)
    assert not s.agent("a1").online


def test_seen_refuses_online_key_of_another_person(two):
    s, c, _, (pa, pb, _) = two
    with pytest.raises(KaptyorkaError, match="session key in use by another person"):
        s.seen("a1", "bob@pc", "codex", "x", principal=pb)
    assert s.agent("a1").person_id == pa.person_id
    c.t += ONLINE_S + 1  # alice's session is long gone: the key may be used again
    assert s.seen("a1", "bob@pc", "codex", "x", principal=pb).person_id == pb.person_id


def test_held_page_message_needs_a_door_at_delivery(tmp_path):
    c = Clock()
    s = Store(tmp_path / "hub.db", clock=c)
    pa, _ = person(s, "alice")
    pb, tb = person(s, "bob")
    _, tc = person(s, "cat")
    s.open_door_group(pa, pb, "alice")  # alice may write to bob's sessions, not to cat's
    s.seen("b1", "m", "codex", "p", principal=s.authenticate(tb))
    assert s.send_from_web("alice", "m/codex/p", "for bob", person_id=pa) == 1
    c.t += ONLINE_S + 1  # bob's session goes offline; the page message is held
    s.seen("c1", "m", "codex", "p", principal=s.authenticate(tc))  # cat takes the name
    assert s.agent("c1").name == "m/codex/p"
    assert s.inbox("c1") == []
    assert [m.text for m in s.inbox("b1")] == []  # offline: not delivered
    s.seen("b1", "m", "codex", "p", principal=s.authenticate(tb))
    assert [m.text for m in s.inbox("b1")] == ["for bob"]


def test_brief_and_errors_mask_private_folders(tmp_path):
    s = Store(tmp_path / "hub.db")
    _, ta = person(s, "alice")
    _, tb = person(s, "bob")
    s.seen("a1", "alice@pc", "codex", "secret-folder", principal=s.authenticate(ta))
    stop = start_hub(s)
    url = f"http://127.0.0.1:{stop.port}"
    h = {"X-Kaptyorka-Key": "b1", "X-Kaptyorka-Machine": "bob@pc",
         "X-Kaptyorka-Tool": "codex", "X-Kaptyorka-Project": "q"}
    try:
        status, text = http.request("GET", url + "/api/brief?event=start", tb, headers=h)
        assert status == 200 and "alice@pc/codex/(private)" in text
        assert "secret-folder" not in text
    finally:
        stop()
    with pytest.raises(KaptyorkaError) as e:
        s.send("b1", "nobody", "hi")
    assert "secret-folder" not in str(e.value) and "(private)" in str(e.value)
