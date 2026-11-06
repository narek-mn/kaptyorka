import getpass
from kaptyorka import http
from helpers import call, call_error, run, tool_names

PC = {"key": "claude:1", "machine": "pc", "tool": "claude-code", "project": "proj-a"}
BOX = {"key": "claude:2", "machine": "box-1", "tool": "claude-code", "project": "proj-b"}


def test_lists_exactly_twenty_tools(hub):
    assert tool_names(hub) == {"who", "send", "inbox", "status",
                               "notes_list", "notes_read", "notes_write", "projects",
                               "knock", "answer", "door_close", "request_access", "fetch_file",
                               "work", "workers", "adopt", "close_worker", "state", "state_write", "decide"}


def test_wrong_token_401(hub):
    assert http.request("POST", hub.url + "/mcp", "bad", {})[0] == 401


def test_missing_identity_refused(hub):
    assert "kaptyorka join" in call_error(hub, {}, "who")


def test_send_then_inbox_between_two_sessions(hub):
    run(call(hub, BOX, "who"))
    assert run(call(hub, PC, "send", to="box-1/claude-code/proj-b", text="move api?")) \
        == "sent to 1 session"
    msgs = run(call(hub, BOX, "inbox"))
    assert msgs[0]["from"] == "pc/claude-code/proj-a"
    assert msgs[0]["text"] == "move api?"


def test_status_shows_in_who(hub):
    run(call(hub, PC, "status", text="working on /move"))
    rows = run(call(hub, PC, "who"))
    me = next(r for r in rows if r["name"] == "pc/claude-code/proj-a")
    assert me["status"] == "working on /move"
    assert me["you"] is True


def test_notes_roundtrip(hub):
    run(call(hub, PC, "notes_write", topic="api", text="v1 at /v1"))
    note = run(call(hub, BOX, "notes_read", topic="api"))
    assert note["text"] == "v1 at /v1"
    assert note["author"] == getpass.getuser()  # the person behind the hub fixture's token
    assert run(call(hub, BOX, "notes_read", topic="x")) == "no note named x"


from kaptyorka import memory  # noqa: E402

GOOD = ("# What it is\nx\n# Where it stands\nx\n# Decisions\nx\n# Open problems\nx\n"
        "# How to run and test\nx\n")


def _rows(hub):
    return [tuple(r) for r in hub.store.db.execute(
        "SELECT project, key, person_id FROM state_writes ORDER BY id")]


def _project(hub):
    hub.store.project_register("proj-a", by="admin")


def test_state_roundtrip_and_validation(hub):
    _project(hub)
    assert run(call(hub, PC, "state", project="proj-a")).startswith("no state note yet for proj-a;")
    assert "needs these headings" in call_error(hub, PC, "state_write", project="proj-a", text="nope")
    assert _rows(hub) == []
    assert run(call(hub, PC, "state_write", project="proj-a", text=GOOD)) == "state of proj-a updated"
    assert run(call(hub, BOX, "state", project="proj-a")) == GOOD
    assert _rows(hub) == [("proj-a", "claude:1", hub.store.db.execute(
        "SELECT person_id FROM agents WHERE key='claude:1'").fetchone()[0])]


def test_notes_write_state_topic_validated_and_recorded(hub):
    _project(hub)
    assert "needs these headings" in call_error(hub, PC, "notes_write", topic="state",
                                                text="x", project="proj-a")
    run(call(hub, PC, "notes_write", topic="state", text=GOOD, project="proj-a"))
    assert len(_rows(hub)) == 1
    run(call(hub, PC, "notes_write", topic="state", text="free text"))  # team note: no rule
    assert len(_rows(hub)) == 1


def test_state_write_needs_write_role(hub):
    from helpers import person
    _project(hub)
    pid, token = person(hub.store, "bob")
    hub.store.grant_project("proj-a", pid, "read", "admin")
    other = type(hub)(url=hub.url, token=token, store=hub.store)
    assert "no access" in call_error(other, PC, "state_write", project="proj-a", text=GOOD)
    assert _rows(hub) == []
    hub.store.grant_project("proj-a", pid, "write", "admin")
    run(call(other, PC, "state_write", project="proj-a", text=GOOD))
    assert len(_rows(hub)) == 1


def test_decide_appends_dated_lines(hub):
    _project(hub)
    hub.store.clock = lambda: 1_700_000_000.0  # 2023-11-14 UTC
    assert run(call(hub, PC, "decide", project="proj-a", text="use sqlite\nnot files")) \
        == "decision recorded"
    hub.store.clock = lambda: 1_700_090_000.0  # 2023-11-15
    run(call(hub, BOX, "decide", project="proj-a", text="x" * 600))
    text = run(call(hub, PC, "notes_read", topic="decisions", project="proj-a"))["text"]
    who = getpass.getuser()
    lines = text.splitlines()
    assert lines[0] == f"- 2023-11-14 {who}: use sqlite not files"
    assert lines[1] == f"- 2023-11-15 {who}: " + "x" * 500
    assert len(lines) == 2


def test_decide_team_note(hub):
    run(call(hub, PC, "decide", project="", text="ship it"))
    assert "ship it" in run(call(hub, PC, "notes_read", topic="decisions"))["text"]


def test_page_state_note_validated_and_recorded(hub):
    _project(hub)
    url = hub.url + "/api/notes"
    s, _ = http.request("POST", url, hub.token, {"topic": "state", "text": "bad", "project": "proj-a"})
    assert s == 400 and _rows(hub) == []
    s, _ = http.request("POST", url, hub.token, {"topic": "state", "text": GOOD, "project": "proj-a"})
    assert s == 204
    rows = _rows(hub)
    assert len(rows) == 1 and rows[0][0] == "proj-a" and rows[0][1] == f"web:{getpass.getuser()}"
