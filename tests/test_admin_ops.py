"""The admin's store operations: removing people and projects, roles, invites, a member's own
access and the log read as sentences."""
import hashlib
import sqlite3
from dataclasses import replace

import pytest

from helpers import person
from kaptyorka import admin_ops
from kaptyorka.hub_feed import feed
from kaptyorka.manager import ManagerStore
from kaptyorka.store import KaptyorkaError, Principal, Store
from kaptyorka.workers import WorkerStore


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
def ids(store):
    return {name: person(store, name, role)[0]
            for name, role in (("alice", "admin"), ("bob", "member"), ("carol", "member"))}


def who(store, name):
    p = next(x for x in admin_ops.names(store).items() if x[1] == name)
    role = store.db.execute("SELECT role FROM people WHERE id=?", (p[0],)).fetchone()["role"]
    return Principal(p[0], name, role, 0, "laptop", False, False)


def shared_device(store, person_id, host="box-1"):
    """A device with a signing secret whose bridge was seen just now."""
    with store.lock:
        return store.db.execute(
            "INSERT INTO devices (person_id, name, token_hash, secret_hash, created, last_used, host)"
            " VALUES (?,?,?,?,?,?,?)",
            (person_id, host, hashlib.sha256(host.encode()).hexdigest(), "s" * 64,
             store.clock(), store.clock(), host)).lastrowid


def worker(store, name, boss, device_id, project="proj-a", state="up"):
    with store.lock:
        store.db.execute(
            "INSERT INTO workers (name, project, task, boss_key, boss_person, device_id, host,"
            " tool, key, state, created) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (name, project, "t", f"claude:{name}-boss", boss, device_id, "box-1", "claude-code",
             f"claude:{name}", state, store.clock()))


def test_remove_person_closes_workers_and_doors(store, ws, ids):
    s, bob = store, ids["bob"]
    s.project_register("proj-a", by="alice")
    s.grant_project("proj-a", bob, "write", "alice")
    s.db.execute("INSERT INTO machines (host, first_seen) VALUES ('box-1', 1)")
    s.grant_machine("box-1", bob, "alice")
    s.project_register("proj-b", by="alice")
    s.request_access(bob, "project", "proj-b", "to read")
    s.invite_create(bob)
    did = s.open_door_group(bob, ids["carol"], "alice")
    s.db.execute("INSERT INTO knocks (from_key, from_person, to_person, about, created)"
                 " VALUES ('claude:c', ?, ?, 'hi', 1)", (ids["carol"], bob))
    dev = shared_device(s, ids["alice"])
    worker(s, "w1", bob, dev)
    worker(s, "w2", bob, dev, state="closed")
    worker(s, "w3", ids["carol"], dev)
    own = shared_device(s, bob, host="box-2")  # their own machine: its bridge goes with them
    worker(s, "w4", bob, own)
    s.db.execute("UPDATE workers SET host='box-2' WHERE name='w4'")
    s.db.execute("INSERT INTO machines (host, first_seen, owner_person_id) VALUES ('box-2', 1, ?)",
                 (bob,))
    mgr = ManagerStore(s)
    open_req = mgr.request_add(bob, "claude:b", "proj-a", "t", "claude-code", None, "box-2", "",
                               "/p")
    done_req = mgr.request_add(bob, "claude:b", "proj-a", "t", "claude-code", None, "box-2", "", "/p")
    mgr.request_decide(done_req, "box-1", "ok")
    carol_req = mgr.request_add(ids["carol"], "claude:c", "proj-a", "t", "claude-code", None,
                                "box-1", "", "/p")
    done = admin_ops.remove_person(s, ws, "bob", who(s, "alice"))
    assert done == {"devices": 2, "workers": 2, "doors": 1, "grants": 2, "requests": 1,
                    "work_requests": 1, "machines_unowned": ["box-2"],
                    "still_running": [{"worker": "w4", "host": "box-2"}]}
    assert s.db.execute("SELECT owner_person_id FROM machines WHERE host='box-2'"
                        ).fetchone()[0] is None
    assert mgr.request(open_req)["decision"] == "person removed"
    assert mgr.request(open_req)["decided_at"] == s.clock()
    assert mgr.request(done_req)["decision"] == "box-1"
    assert mgr.request(carol_req)["decided_at"] is None
    row = s.db.execute("SELECT * FROM people WHERE id=?", (bob,)).fetchone()
    assert row["name"] == "bob (removed)" and row["removed_at"] == s.clock()
    assert all(d["revoked_at"] for d in s.devices(bob))
    assert s.db.execute("SELECT closed_at FROM doors WHERE id=?", (did,)).fetchone()[0]
    assert s.db.execute("SELECT answered_at FROM knocks").fetchone()[0]
    assert s.db.execute("SELECT COUNT(*) FROM invites WHERE person_id=?", (bob,)).fetchone()[0] == 0
    w1 = s.db.execute("SELECT state, error FROM workers WHERE name='w1'").fetchone()
    assert tuple(w1) == ("closed", "boss removed")
    assert s.db.execute("SELECT state FROM workers WHERE name='w3'").fetchone()[0] == "up"
    orders = s.db.execute("SELECT worker, kind FROM launches").fetchall()
    assert [tuple(o) for o in orders] == [("w1", "close")]  # w4's bridge is gone: no order
    assert s.log(1)[0]["kind"] == "person_removed" and s.log(1)[0]["detail"] == "bob"


def test_remove_refusals(store, ws, ids):
    s = store
    person(s, "manager", "manager")
    admin = who(s, "alice")
    with pytest.raises(KaptyorkaError, match="you cannot remove yourself"):
        admin_ops.remove_person(s, ws, "alice", admin)
    other_admin = Principal(999, "x", "admin", 0, "d", False, False)
    with pytest.raises(KaptyorkaError, match="the admin cannot be removed"):
        admin_ops.remove_person(s, ws, "alice", other_admin)
    with pytest.raises(KaptyorkaError, match="the manager account cannot be removed; use"
                                             " kaptyorka start --no-manager"):
        admin_ops.remove_person(s, ws, "manager", admin)
    with pytest.raises(KaptyorkaError, match="no person named dave"):
        admin_ops.remove_person(s, ws, "dave", admin)
    with pytest.raises(KaptyorkaError, match="admin only"):
        admin_ops.remove_person(s, ws, "carol", who(s, "bob"))
    assert s.person_by_name("carol") is not None


def test_remove_refuses_while_shared_token_speaks_for_them(store, ws, ids):
    store.db.execute("INSERT INTO settings (key, value) VALUES ('legacy:box-1', ?)",
                     (str(ids["bob"]),))
    with pytest.raises(KaptyorkaError, match="turn the shared token off first"):
        admin_ops.remove_person(store, ws, "bob", who(store, "alice"))
    store.set_legacy_enabled(False, "alice")
    admin_ops.remove_person(store, ws, "bob", who(store, "alice"))


def test_removed_name_can_be_reused(store, ws, ids):
    old = who(store, "bob")
    ws.notify("web:bob", "a private notice for the old bob")
    store.db.execute("INSERT INTO messages (sender_key, sender_name, to_key, to_name, machine, tool,"
                     " project, text, created) VALUES ('web:bob', 'bob', 'web:alice', 'alice',"
                     " '', '', '', 'hello from the old bob', 1)")
    dev = shared_device(store, ids["alice"])
    worker(store, "w1", ids["bob"], dev, state="closed")
    store.db.execute("UPDATE workers SET boss_key='web:bob'")
    admin_ops.remove_person(store, ws, "bob", who(store, "alice"))
    new = store.person_create("bob", by="alice")
    new_bob = Principal(new, "bob", "member", 0, "web", False, False)
    assert store.messages_for(new_bob) == []
    keys = [tuple(r) for r in store.db.execute("SELECT sender_key, to_key FROM messages ORDER BY id")]
    assert ("hub:workers", "web:bob (removed)") in keys
    assert all(k != "web:bob" for pair in keys for k in pair)
    assert store.db.execute("SELECT boss_key FROM workers").fetchone()[0] == "web:bob (removed)"
    renamed = replace(old, person_name="bob (removed)")
    assert len(store.messages_for(renamed)) == 2  # the removed row still reaches its own
    assert new != ids["bob"] and store.person_by_name("bob")["id"] == new
    admin_ops.remove_person(store, ws, "bob", who(store, "alice"))
    names = admin_ops.names(store)
    assert names[ids["bob"]] == "bob (removed)" and names[new] == f"bob (removed) #{new}"


def test_removed_person_hidden_everywhere(store, ws, ids):
    s = store
    s.project_register("proj-a", by="alice")
    s.grant_project("proj-a", ids["bob"], "read", "alice")
    s.db.execute("INSERT INTO agents (key, name, machine, tool, project, last_seen, person_id)"
                 " VALUES ('claude:b1', 'bob-pc', 'pc', 'claude-code', 'p', ?, ?)",
                 (s.clock(), ids["bob"]))
    admin_ops.remove_person(s, ws, "bob", who(s, "alice"))
    assert [p["name"] for p in s.people()] == ["alice", "carol"]
    assert s.person_by_name("bob") is None and s.person_by_name("bob (removed)") is None
    assert s.project_members().get("proj-a", []) == []
    out = feed(s, who(s, "alice"))
    assert [p["name"] for p in out["people"]] == ["alice", "carol"]
    assert [a["person"] for a in out["agents"]] == ["bob (removed)"]  # an old session


def test_set_role_bounds(store, ids):
    admin = who(store, "alice")
    admin_ops.set_role(store, "bob", "manager", admin)
    assert store.person_by_name("bob")["role"] == "manager"
    admin_ops.set_role(store, "bob", "member", admin)
    assert store.person_by_name("bob")["role"] == "member"
    assert store.log(1)[0]["kind"] == "role"
    for name, role in (("bob", "admin"), ("alice", "member")):
        with pytest.raises(KaptyorkaError, match="the admin role cannot be given or taken here"):
            admin_ops.set_role(store, name, role, admin)
    with pytest.raises(KaptyorkaError, match="role must be member or manager"):
        admin_ops.set_role(store, "bob", "boss", admin)
    with pytest.raises(KaptyorkaError, match="admin only"):
        admin_ops.set_role(store, "carol", "manager", who(store, "bob"))


def test_invites_list_and_cancel(store, ws, ids, clock):
    code = store.invite_create(ids["bob"])
    store.invite_create(ids["carol"], ttl_s=10)
    used = store.invite_create(ids["carol"])
    store.invite_redeem(used, "pc")
    clock[0] += 20
    rows = admin_ops.invites_open(store)
    assert [(r["code"], r["person"]) for r in rows] == [(code, "bob")]
    assert rows[0]["expires"] == 1_000_000.0 + 7 * 86400
    admin_ops.invite_cancel(store, code, who(store, "alice"))
    assert admin_ops.invites_open(store) == [] and not store.invite_valid(code)
    with pytest.raises(KaptyorkaError, match="no open invite"):
        admin_ops.invite_cancel(store, code, who(store, "alice"))
    with pytest.raises(KaptyorkaError, match="no open invite"):
        admin_ops.invite_cancel(store, used, who(store, "alice"))


def test_project_update_and_remove_keeps_notes(store, ws, ids):
    s, admin = store, who(store, "alice")
    s.project_register("proj-a", "old", by="alice")
    s.grant_project("proj-a", ids["bob"], "write", "alice")
    s.request_access(ids["carol"], "project", "proj-a", "")
    s.notes_write_from_web("alice", "state", "where it stands", project="proj-a")
    dev = shared_device(s, ids["alice"])
    ws.facts_seen(dev, {}, {"proj-a": "/w/proj-a", "proj-b": "/w/proj-b"})
    admin_ops.project_update(s, "proj-a", "new words", admin)
    assert s.projects_for(admin)[0]["description"] == "new words"
    done = admin_ops.project_remove(s, "proj-a", admin)
    assert done == {"grants": 1, "requests": 1, "folders": 1}
    assert [(p["name"], p.get("archived"), p["role"]) for p in s.projects_for(admin)] == \
        [("proj-a", True, "read")]
    assert s.projects_for(who(s, "bob")) == [] and s.project_role("proj-a", ids["bob"]) is None
    assert ws.folders(dev) == {"proj-b": "/w/proj-b"}
    assert s.db.execute("SELECT text FROM notes WHERE project='proj-a'").fetchone()[0] == \
        "where it stands"
    assert [r["kind"] for r in s.log(3)][:2] == ["project_removed", "project_updated"]
    with pytest.raises(KaptyorkaError, match="no project named proj-a"):
        admin_ops.project_update(s, "proj-a", "x", admin)
    with pytest.raises(KaptyorkaError, match="no project named proj-a"):
        admin_ops.project_remove(s, "proj-a", admin)
    with pytest.raises(KaptyorkaError, match="no project named proj-a"):
        s.grant_project("proj-a", ids["bob"], "read", "alice")
    with pytest.raises(KaptyorkaError, match="no project named proj-a"):
        s.request_access(ids["bob"], "project", "proj-a")


def test_removed_project_notes_are_the_admins_alone(store, ws, ids):
    s, admin = store, who(store, "alice")
    s.project_register("proj-a", "the app", by="alice")
    s.grant_project("proj-a", ids["carol"], "write", "alice")
    s.notes_write_from_web("alice", "state", "where it stands", project="proj-a")
    admin_ops.project_remove(s, "proj-a", admin)
    c = who(s, "carol")  # her grant is gone; she has a session in a folder named proj-a
    s.seen("claude:c1", "pc", "claude-code", "proj-a", principal=c)
    for fn in (lambda: s.notes_list(c, "proj-a"), lambda: s.notes_read("state", c, "proj-a")):
        with pytest.raises(KaptyorkaError, match="was removed"):
            fn()
    with pytest.raises(KaptyorkaError, match="was removed"):
        s.notes_write("claude:c1", "state", "mine now", project="proj-a")
    with pytest.raises(KaptyorkaError, match="was removed"):
        s.notes_write_from_web("alice", "state", "edit", project="proj-a", principal=admin)
    assert not s.may_read_project("proj-a", c) and not s.may_write_project("proj-a", c)
    assert s.notes_read("state", admin, "proj-a").text == "where it stands"
    out = feed(s, admin)
    assert [n["project"] for n in out["notes"]] == ["proj-a"]
    assert [p["name"] for p in out["archived_projects"]] == ["proj-a"] and out["projects"] == []
    assert "proj-a" not in [n["project"] for n in feed(s, c)["notes"]]
    s.project_register("proj-a", by="alice")  # comes back with its notes and no roles
    assert [(p["name"], p.get("archived"), p["description"]) for p in s.projects_for(admin)] == \
        [("proj-a", None, "the app")]
    assert s.project_role("proj-a", ids["carol"]) is None
    assert s.notes_read("state", admin, "proj-a").text == "where it stands"


def test_project_remove_refuses_live_worker(store, ws, ids):
    store.project_register("proj-a", by="alice")
    worker(store, "w1", ids["bob"], shared_device(store, ids["alice"]))
    with pytest.raises(KaptyorkaError, match="close its workers first"):
        admin_ops.project_remove(store, "proj-a", who(store, "alice"))
    store.db.execute("UPDATE workers SET state='closed'")
    admin_ops.project_remove(store, "proj-a", who(store, "alice"))


def test_my_access_member_view(store, ws, ids):
    s, bob = store, ids["bob"]
    s.project_register("proj-a", "the app", by="alice")
    s.project_register("proj-b", by="alice")
    s.grant_project("proj-a", bob, "write", "alice")
    s.db.execute("INSERT INTO machines (host, first_seen) VALUES ('box-1', 1)")
    s.db.execute("INSERT INTO machines (host, first_seen) VALUES ('box-2', 1)")
    s.grant_machine("box-1", bob, "alice")
    s.request_access(bob, "project", "proj-b", "to help")
    got = admin_ops.my_access(s, ws, who(s, "bob"))
    assert got["projects"] == [{"name": "proj-a", "role": "write", "description": "the app"}]
    assert got["machines"] == [{"host": "box-1", "owner": None, "shared": False}]
    assert [(r["target"], r["status"]) for r in got["requests"]] == [("proj-b", "pending")]


def test_log_sentences():
    cases = [
        ("grant", "project:proj-a:write -> bob", "alice gave bob write access to proj-a"),
        ("grant", "machine:box-1 -> bob", "alice let bob use the machine box-1"),
        ("revoke", "project:proj-a -> bob", "alice took bob's access to proj-a"),
        ("revoke", "device laptop of bob", "alice turned off bob's device laptop"),
        ("person", "bob created as member", "alice added bob with the role member"),
        ("device", "device laptop of bob", "alice added a device laptop for bob"),
        ("invite", "invite for bob", "alice made an invite link for bob"),
        ("project_register", "proj-a", "alice registered the project proj-a"),
        ("project_removed", "proj-a", "alice removed the project proj-a"),
        ("person_removed", "bob", "alice removed bob from the team"),
        ("role", "bob -> manager", "alice made bob a manager"),
        ("page_login", "web", "alice opened the page (web)"),
        ("rules", "team rules v3 by alice", "alice saved team rules version 3"),
        ("proposal", "proposal 4 accepted", "alice accepted rule proposal 4"),
        ("conflict", "conflict 2 in proj-a", "alice reported a rule conflict (2) in proj-a"),
        ("request_decided", "project:proj-a -> bob: approved (already write)",
         "alice approved bob's request for the project proj-a"),
        ("settings", "retention_days=60", "alice set message keeping to 60 days"),
        ("machine_owner", "box-1 -> bob", "alice made bob the owner of box-1"),
        ("door_open", "group #3 bob <-> carol", "alice opened a door between bob and carol"),
        ("door_open", "web:alice <-> box-1/claude-code/proj-a",
         "alice opened a door between alice and box-1/claude-code/proj-a"),
        ("door_close", "box-1/claude-code/proj-a -> box-2/codex/proj-a",
         "alice closed the door between box-1/claude-code/proj-a and box-2/codex/proj-a"),
        ("door_close", "#3", "alice closed door 3"),
        ("knock", "#5 box-1/claude-code/proj-a -> carol", "alice knocked for carol (knock 5)"),
        ("knock_answer", "#5 yes by box-2/codex/proj-a", "alice said yes to knock 5"),
        ("knock_answer", "#5 no on the web page", "alice declined knock 5"),
        ("propose", "proposal 4: Run the linter before every commit.",
         "alice proposed a rule: Run the linter before every commit."),
        ("propose", "proposal 4: two\nlines", "alice proposed a rule: two\nlines"),
        ("legacy_toggle", "off", "alice switched the shared token off"),
        ("legacy_toggle", "on", "alice switched the shared token on"),
        ("legacy_assign", "box-3 -> bob", "alice gave box-3 (old shared token) to bob"),
        ("legacy_used", "box-3", "box-3 still uses the old shared token"),
        ("rekeyed", "box-3", "box-3 took its own token in place of the shared one"),
        ("auto_update", "box-1 on", "alice turned auto-update on for box-1"),
        ("auto_update", "box-1 off", "alice turned auto-update off for box-1"),
        ("auto_update", "box-1 (bob) off", "alice turned auto-update off for box-1 (bob)"),
        ("auto_update", "box-1 default", "alice set auto-update for box-1 back to the hub's default"),
        ("something_new", "x y", "something_new: x y"),
        ("grant", "odd detail", "grant: odd detail"),
    ]
    for kind, detail, want in cases:
        assert admin_ops.log_sentence({"person": "alice", "kind": kind, "detail": detail}) == want
    for kind, detail, want in [  # the shared token's device is never called "legacy"
            ("revoke", "device legacy of alice", "alice turned off the shared token (old machines)"),
            ("device_shared", "device legacy of alice on",
             "alice turned sharing on for the shared token (old machines)"),
            ("device", "device legacy of alice", "alice added the shared token (old machines) for alice")]:
        got = admin_ops.log_sentence({"person": "alice", "kind": kind, "detail": detail})
        assert got == want and "legacy" not in got
    for kind, detail, want in [
            ("work_decide", "request 1: wait — busy", "manager decided work request 1: wait — busy"),
            ("manager", "closed: switched off", "The manager session: closed: switched off")]:
        assert admin_ops.log_sentence({"person": "manager", "kind": kind, "detail": detail}) == want


def test_schema_adds_removed_at_to_an_older_people_table(tmp_path):
    db = sqlite3.connect(str(tmp_path / "x.db"))
    db.execute("CREATE TABLE people (id INTEGER PRIMARY KEY, name TEXT)")
    db.execute("CREATE TABLE projects (name TEXT PRIMARY KEY)")
    db.execute("CREATE TABLE notes (id INTEGER PRIMARY KEY, topic TEXT)")
    admin_ops.ensure_schema(db)
    admin_ops.ensure_schema(db)
    assert "deleted_at" in [r[1] for r in db.execute("PRAGMA table_info(notes)")]
    assert "removed_at" in [r[1] for r in db.execute("PRAGMA table_info(people)")]
    assert "archived_at" in [r[1] for r in db.execute("PRAGMA table_info(projects)")]


# notes: delete and undo, the log in groups

@pytest.fixture
def board(store, ids):
    store.project_register("proj-a", by="alice")
    store.grant_project("proj-a", ids["bob"], "write", "alice")
    store.grant_project("proj-a", ids["carol"], "read", "alice")
    return {n: who(store, n) for n in ("alice", "bob", "carol")}


def test_note_delete_hides_everywhere(store, board):
    alice = board["alice"]
    store.notes_write_from_web("alice", "plan", "v1", "proj-a", principal=alice)
    store.notes_write_from_web("alice", "plan", "v2", "proj-a", principal=alice)
    assert admin_ops.note_delete(store, alice, "plan", "proj-a") == 2
    assert store.notes_list(alice, "proj-a") == []
    assert store.notes_read("plan", alice, "proj-a") is None
    assert store.log(5)[0]["kind"] == "note_delete"
    with pytest.raises(KaptyorkaError, match="no note named plan"):
        admin_ops.note_delete(store, alice, "plan", "proj-a")


def test_note_undo_restores_all_versions(store, board, clock):
    alice = board["alice"]
    store.notes_write_from_web("alice", "plan", "v1", "proj-a", principal=alice)
    store.notes_write_from_web("alice", "plan", "v2", "proj-a", principal=alice)
    admin_ops.note_delete(store, alice, "plan", "proj-a")
    assert admin_ops.note_undo(store, board["bob"], "plan", "proj-a") == 2
    history = store.note_history("plan", alice, "proj-a")
    assert len(history) == 2 and history[0].text == "v2"
    admin_ops.note_delete(store, alice, "plan", "proj-a")
    clock[0] += admin_ops.UNDO_S + 1
    with pytest.raises(KaptyorkaError, match="nothing to undo for plan"):
        admin_ops.note_undo(store, alice, "plan", "proj-a")


def test_note_delete_needs_write(store, board):
    store.notes_write_from_web("alice", "plan", "v1", "proj-a", principal=board["alice"])
    with pytest.raises(KaptyorkaError, match="no access"):
        admin_ops.note_delete(store, board["carol"], "plan", "proj-a")
    assert admin_ops.note_delete(store, board["bob"], "plan", "proj-a") == 1


def test_team_note_delete_admin_or_author(store, board):
    bob, carol, alice = board["bob"], board["carol"], board["alice"]
    store.notes_write_from_web("bob", "idea", "v1")
    store.notes_write_from_web("carol", "idea", "v2")
    with pytest.raises(KaptyorkaError, match="only the admin or the person who started"):
        admin_ops.note_delete(store, carol, "idea", None)
    assert admin_ops.note_delete(store, bob, "idea", None) == 2
    admin_ops.note_undo(store, alice, "idea", None)
    assert admin_ops.note_delete(store, alice, "idea", None) == 2
    shared = replace(alice, legacy=True)
    with pytest.raises(KaptyorkaError, match="own device token"):
        admin_ops.note_undo(store, shared, "idea", None)


def test_purge_removes_only_old_deleted_notes(store, board, clock):
    alice = board["alice"]
    for t in ("old", "recent", "live"):
        store.notes_write_from_web("alice", t, "x", "proj-a", principal=alice)
    admin_ops.note_delete(store, alice, "old", "proj-a")
    clock[0] += 9 * 60
    admin_ops.note_delete(store, alice, "recent", "proj-a")
    clock[0] += 2 * 60
    assert admin_ops.purge_deleted_notes(store, clock[0]) == 1
    rows = store.db.execute("SELECT topic FROM notes ORDER BY topic").fetchall()
    assert [r["topic"] for r in rows] == ["live", "recent"]
    assert any(r["kind"] == "note_delete" for r in store.log(10))


def test_log_kinds_cover_every_sentence_kind():
    groups = dict(admin_ops.log_kinds())
    seen = [k for ks in groups.values() for k in ks]
    assert sorted(seen) == sorted(set(seen)) and set(admin_ops.KNOWN_KINDS) <= set(seen)
    assert list(groups)[0] == "people" and list(groups)[-1] == "sign-ins"


def test_log_page_before_and_kind(store):
    for who_, kind in (("alice", "person"), ("alice", "grant"), ("alice", "role"),
                       ("bob", "mystery"), ("alice", "page_login")):
        store.log_add(who_, kind, "x")
    rows = store.log(5)
    third = rows[2]["id"]
    older = admin_ops.log_page(store, 10, None, None, third)
    assert [r["id"] for r in older] == [r["id"] for r in rows[3:]] and "sentence" in older[0]
    assert {r["kind"] for r in admin_ops.log_page(store, 10, None, "people", None)} == {"person", "role"}
    assert [r["kind"] for r in admin_ops.log_page(store, 10, None, "other", None)] == ["mystery"]
    assert len(admin_ops.log_page(store, 2, "alice", None, None)) == 2
    with pytest.raises(KaptyorkaError, match="no log group"):
        admin_ops.log_page(store, 10, None, "nope", None)


def test_undo_refuses_when_a_new_note_took_the_name(store, board):
    alice = board["alice"]
    store.notes_write_from_web("alice", "plan", "old", "proj-a", principal=alice)
    admin_ops.note_delete(store, alice, "plan", "proj-a")
    store.notes_write_from_web("alice", "plan", "new", "proj-a", principal=alice)
    with pytest.raises(KaptyorkaError, match="a new note named plan exists; delete it first"):
        admin_ops.note_undo(store, alice, "plan", "proj-a")
    assert [n.text for n in store.note_history("plan", alice, "proj-a")] == ["new"]


def test_team_note_owner_ignores_deleted_versions(store, board, clock):
    bob, carol = board["bob"], board["carol"]
    store.notes_write_from_web("bob", "idea", "old")
    admin_ops.note_delete(store, bob, "idea", None)
    clock[0] += 5
    store.notes_write_from_web("carol", "idea", "new")
    with pytest.raises(KaptyorkaError, match="only the admin or the person who started"):
        admin_ops.note_delete(store, bob, "idea", None)
    assert admin_ops.note_delete(store, carol, "idea", None) == 1
    with pytest.raises(KaptyorkaError, match="only the admin or the person who started"):
        admin_ops.note_undo(store, bob, "idea", None)
    assert admin_ops.note_undo(store, carol, "idea", None) == 1  # her own deletion


def test_undo_brings_back_only_the_last_deletion(store, board, clock):
    alice = board["alice"]
    store.notes_write_from_web("alice", "plan", "v1", "proj-a", principal=alice)
    admin_ops.note_delete(store, alice, "plan", "proj-a")
    clock[0] += 5
    store.notes_write_from_web("alice", "plan", "v2", "proj-a", principal=alice)
    admin_ops.note_delete(store, alice, "plan", "proj-a")
    assert admin_ops.note_undo(store, alice, "plan", "proj-a") == 1
    assert [n.text for n in store.note_history("plan", alice, "proj-a")] == ["v2"]
