"""Rules: versioned team rules, skills, proposals, conflicts."""
import pytest

from helpers import person
from kaptyorka.rules import MAX_OPEN, RulesStore, check_name, rules_text
from kaptyorka.store import KaptyorkaError, Principal, Store


@pytest.fixture
def clock():
    return [1_000_000.0]


@pytest.fixture
def store(tmp_path, clock):
    s = Store(tmp_path / "hub.db", clock=lambda: clock[0])
    yield s
    s.close()


@pytest.fixture
def rs(store):
    return RulesStore(store)


@pytest.fixture
def alice(store):
    pid = person(store, "alice")[0]
    return Principal(pid, "alice", "member", 1, "alice-laptop", False, False)


def test_rules_versions_increment_and_history(rs, clock):
    assert rs.current() == {"text": "", "version": 0, "author": "", "time": 0.0}
    assert rs.set_rules("one", "admin") == 1
    clock[0] += 1
    assert rs.set_rules("two", "admin") == 2
    assert rs.current()["text"] == "two"
    assert [h["version"] for h in rs.history()] == [2, 1]
    assert rules_text("") == ""
    assert "two" in rules_text("two")


def test_set_rules_same_text_is_no_new_version(rs):
    rs.set_rules("one", "admin")
    assert rs.set_rules(" one \n", "admin") == 1
    assert len(rs.history()) == 1


def test_set_rules_rejects_project_scope(rs):
    with pytest.raises(KaptyorkaError, match="project rules live"):
        rs.set_rules("x", "admin", scope="proj-a")
    with pytest.raises(KaptyorkaError, match="too long"):
        rs.set_rules("x" * 20001, "admin")


def test_skills_replace_set_and_version_hash(rs, clock):
    assert rs.skills_version() == ""
    r = rs.set_skills({"a": "1", "b": "2"}, "admin")
    assert r == {"added": ["a", "b"], "changed": [], "removed": []}
    v1 = rs.skills_version()
    assert len(v1) == 12
    t = rs.skills()[0]["time"]
    clock[0] += 5
    r = rs.set_skills({"a": "1", "b": "3"}, "admin")
    assert r["changed"] == ["b"]
    assert rs.skills()[0]["time"] == t  # unchanged row keeps its time
    assert rs.skills_version() != v1
    assert rs.set_skills({"b": "3"}, "admin")["removed"] == ["a"]
    assert [s["name"] for s in rs.skills()] == ["b"]
    rs.set_skills({}, "admin")
    assert rs.skills_version() == ""
    with pytest.raises(KaptyorkaError, match="too many"):
        rs.set_skills({f"s{i}": "x" for i in range(51)}, "admin")


def test_skill_name_rules():
    for bad in ("Team", "a--b", "-a", "a" * 49, ""):
        with pytest.raises(KaptyorkaError):
            check_name(bad)
    for ok in ("a", "my-skill-2", "a" * 48):
        assert check_name(ok) == ok


def test_propose_caps_and_open_limit(rs, alice):
    with pytest.raises(KaptyorkaError):
        rs.propose(alice, "k", "x" * 2001, "e")
    with pytest.raises(KaptyorkaError):
        rs.propose(alice, "k", "x", "e" * 4001)
    for i in range(MAX_OPEN):
        rs.propose(alice, "k", f"r{i}", "e")
    with pytest.raises(KaptyorkaError, match="20 open proposals"):
        rs.propose(alice, "k", "more", "e")
    assert len(rs.proposals("open")) == MAX_OPEN
    assert rs.proposals()[0]["person"] == "alice"


def test_accept_appends_new_rules_version(rs, alice):
    rs.set_rules("base", "admin")
    pid = rs.propose(alice, "k1", "new rule", "ev")
    row = rs.decide(pid, True, "ok", "admin")
    assert row["session_key"] == "k1" and row["status"] == "accepted"
    cur = rs.current()
    assert cur["version"] == 2 and cur["text"] == "base\n\nnew rule"
    pid = rs.propose(alice, "k1", "bad", "ev")
    assert rs.decide(pid, False, "no", "admin")["status"] == "rejected"
    assert rs.current()["version"] == 2


def test_decide_twice_raises(rs, alice):
    pid = rs.propose(alice, "k", "r", "e")
    rs.decide(pid, False, "", "admin")
    with pytest.raises(KaptyorkaError, match="already decided"):
        rs.decide(pid, True, "", "admin")
    with pytest.raises(KaptyorkaError, match="no such proposal"):
        rs.decide(999, True, "", "admin")


def test_conflict_logged(rs, store, alice):
    cid = rs.conflict(alice, "k", "proj-a", "a", "b")
    c = rs.conflicts()[0]
    assert c["id"] == cid and c["person"] == "alice" and c["project"] == "proj-a"
    assert any(e["kind"] == "conflict" for e in store.log())
    with pytest.raises(KaptyorkaError):
        rs.conflict(alice, "k", "proj-a", "a" * 1001, "b")
