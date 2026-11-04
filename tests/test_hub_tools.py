from kubrik import http
from helpers import call, call_error, run, tool_names

PC = {"key": "claude:1", "machine": "pc", "tool": "claude-code", "project": "murmur"}
DGX = {"key": "claude:2", "machine": "dgx", "tool": "claude-code", "project": "backend"}


def test_lists_exactly_seven_tools(hub):
    assert tool_names(hub) == {"who", "send", "inbox", "status",
                               "notes_list", "notes_read", "notes_write"}


def test_wrong_token_401(hub):
    assert http.request("POST", hub.url + "/mcp", "bad", {})[0] == 401


def test_missing_identity_refused(hub):
    assert "kubrik join" in call_error(hub, {}, "who")


def test_send_then_inbox_between_two_sessions(hub):
    run(call(hub, DGX, "who"))
    assert run(call(hub, PC, "send", to="dgx/claude-code/backend", text="move api?")) \
        == "sent to 1 session"
    msgs = run(call(hub, DGX, "inbox"))
    assert msgs[0]["from"] == "pc/claude-code/murmur"
    assert msgs[0]["text"] == "move api?"


def test_status_shows_in_who(hub):
    run(call(hub, PC, "status", text="working on /move"))
    rows = run(call(hub, PC, "who"))
    me = next(r for r in rows if r["name"] == "pc/claude-code/murmur")
    assert me["status"] == "working on /move"
    assert me["you"] is True


def test_notes_roundtrip(hub):
    run(call(hub, PC, "notes_write", topic="api", text="v1 at /v1"))
    note = run(call(hub, DGX, "notes_read", topic="api"))
    assert note["text"] == "v1 at /v1"
    assert note["author"] == "pc/claude-code/murmur"
    assert run(call(hub, DGX, "notes_read", topic="x")) == "no note named x"
