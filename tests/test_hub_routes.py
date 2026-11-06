import getpass
import pytest
import asyncio
import threading
import time

from kaptyorka import http
from kaptyorka.auth import Auth
from helpers import call, call_error, run

PC = {"key": "claude:1", "machine": "pc", "tool": "claude-code", "project": "proj-a"}
BOX2 = {"key": "claude:2", "machine": "box-2", "tool": "claude-code", "project": "proj-b"}
BOX2_NAME = "box-2/claude-code/proj-b"
ADMIN_NAME = getpass.getuser()  # the hub fixture's token "T" is this admin's legacy device


class _Thread(threading.Thread):
    def __init__(self, fn):
        super().__init__(daemon=True)
        self.fn, self.result = fn, None

    def run(self):
        self.result = self.fn()

    def join_result(self):
        self.join()
        return self.result


def start_thread(fn):
    th = _Thread(fn)
    th.start()
    return th


def hub_send(hub, ident, to, text):
    return run(call(hub, ident, "send", to=to, text=text))


def ident_headers(ident):
    return {f"X-Kaptyorka-{k.capitalize()}": v for k, v in ident.items()}


def brief(hub, ident, event):
    status, body = http.request("GET", f"{hub.url}/api/brief?event={event}", "T",
                                headers=ident_headers(ident))
    assert status == 200
    return body


def heartbeat(hub, machine, sessions):
    return http.request("POST", hub.url + "/bridge/heartbeat", "T",
                        {"machine": machine, "sessions": sessions})[0]


def feed(hub):
    status, body = http.request("GET", hub.url + "/api/feed", "T")
    assert status == 200
    return body


def box2_online(hub, activity="idle"):
    assert heartbeat(hub, "box-2", [{"key": BOX2["key"], "tool": "claude-code",
                                   "project": "proj-b", "activity": activity}]) == 200


def test_heartbeat_makes_sessions_online(hub):
    assert heartbeat(hub, "box-2", [
        {"key": "claude:2", "tool": "claude-code", "project": "proj-b", "activity": "busy"},
        {"key": "codex:3", "tool": "codex", "project": "proj-b", "activity": "idle"}]) == 200
    agents = feed(hub)["agents"]
    assert {a["name"] for a in agents} == {"box-2/claude-code/proj-b", "box-2/codex/proj-b"}
    assert {a["activity"] for a in agents} == {"busy", "idle"}


def test_poll_returns_immediately_when_queued(hub):
    box2_online(hub)
    hub_send(hub, PC, BOX2_NAME, "hi")
    t0 = time.time()
    status, items = http.request("GET", f"{hub.url}/bridge/poll?machine=box-2&wait=25", "T",
                                 timeout=30)
    assert status == 200 and len(items) == 1 and time.time() - t0 < 1
    assert items[0]["key"] == BOX2["key"]
    assert items[0]["text"] == "Kaptyorka message from pc/claude-code/proj-a:\n\nhi"


def test_poll_wakes_on_send(hub):
    box2_online(hub)
    t0 = time.time()
    th = start_thread(lambda: http.request("GET", f"{hub.url}/bridge/poll?machine=box-2&wait=10",
                                           "T", timeout=15))
    time.sleep(0.5)
    hub_send(hub, PC, BOX2_NAME, "hi")
    status, items = th.join_result()
    assert status == 200 and len(items) == 1 and time.time() - t0 < 3


def test_poll_empty_after_wait(hub):
    t0 = time.time()
    assert http.request("GET", f"{hub.url}/bridge/poll?machine=box-2&wait=1", "T") == (200, [])
    assert 0.9 < time.time() - t0 < 3


def test_ack_false_leaves_message_for_brief(hub):
    box2_online(hub)
    hub_send(hub, PC, BOX2_NAME, "move api?")
    _, items = http.request("GET", f"{hub.url}/bridge/poll?machine=box-2&wait=0", "T")
    assert http.request("POST", hub.url + "/bridge/ack", "T",
                        {"id": items[0]["id"], "ok": False})[0] == 204
    assert brief(hub, BOX2, "prompt") == \
        "Kaptyorka message from pc/claude-code/proj-a:\n\nmove api?"
    assert brief(hub, BOX2, "prompt") == ""


def test_ack_true_marks_read(hub):
    box2_online(hub)
    hub_send(hub, PC, BOX2_NAME, "hi")
    _, items = http.request("GET", f"{hub.url}/bridge/poll?machine=box-2&wait=0", "T")
    assert http.request("POST", hub.url + "/bridge/ack", "T",
                        {"id": items[0]["id"], "ok": True})[0] == 204
    assert brief(hub, BOX2, "prompt") == ""


def test_ack_string_false_is_not_ok(hub):
    box2_online(hub)
    hub_send(hub, PC, BOX2_NAME, "hi")
    _, items = http.request("GET", f"{hub.url}/bridge/poll?machine=box-2&wait=0", "T")
    assert http.request("POST", hub.url + "/bridge/ack", "T",
                        {"id": items[0]["id"], "ok": "false"})[0] == 204
    assert "hi" in brief(hub, BOX2, "prompt")


def raw_post(hub, path, data: bytes):
    import urllib.error
    import urllib.request
    req = urllib.request.Request(hub.url + path, data=data, method="POST",
                                 headers={"Authorization": "Bearer T",
                                          "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


def test_bad_input_is_400(hub):
    assert raw_post(hub, "/bridge/heartbeat", b"{not json") == 400
    assert raw_post(hub, "/bridge/ack", b"{not json") == 400
    assert http.request("POST", hub.url + "/bridge/heartbeat", "T",
                        {"machine": "box-2", "sessions": [{"key": "k"}]})[0] == 400
    assert http.request("POST", hub.url + "/bridge/ack", "T", {"ok": True})[0] == 400
    assert http.request("GET", hub.url + "/bridge/poll?wait=0", "T")[0] == 400
    for wait in ("abc", "nan", "inf"):
        assert http.request("GET", f"{hub.url}/bridge/poll?machine=box-2&wait={wait}",
                            "T")[0] == 400


def test_brief_start_summary_exact(hub):
    box2_online(hub)
    run(call(hub, PC, "who"))
    run(call(hub, BOX2, "notes_write", topic="proj-a-api", text="v1"))
    hub_send(hub, BOX2, "pc/claude-code/proj-a", "ping")
    assert brief(hub, PC, "start") == (
        f"You are `pc/claude-code/proj-a`, {ADMIN_NAME}, on pc. 1 other online:"
        " box-2/claude-code/proj-b (idle). 1 unread message. Board topics: proj-a-api.\n\n"
        "Unread messages:\n\nKaptyorka message from box-2/claude-code/proj-b:\n\nping")


def test_brief_start_alone_plural_no_topics(hub):
    assert brief(hub, PC, "start") == \
        f"You are `pc/claude-code/proj-a`, {ADMIN_NAME}, on pc. No one else online. 0 unread messages."
    box2_online(hub, activity="")
    heartbeat(hub, "box-1", [{"key": "oc:1", "tool": "opencode", "project": "nav",
                               "activity": "busy"}])
    run(call(hub, PC, "notes_write", topic="a", text="1"))
    run(call(hub, PC, "notes_write", topic="b", text="2"))
    hub_send(hub, BOX2, "pc/claude-code/proj-a", "one")
    hub_send(hub, BOX2, "pc/claude-code/proj-a", "two")
    assert brief(hub, PC, "start") == (  # a new message breaks the 10 s cache
        f"You are `pc/claude-code/proj-a`, {ADMIN_NAME}, on pc. 2 others online:"
        " box-2/claude-code/proj-b, box-1/opencode/nav (busy). 2 unread messages."
        " Board topics: b, a.\n\nUnread messages:\n\nKaptyorka message from box-2/claude-code/proj-b:\n\none\n\n"
        "Kaptyorka message from box-2/claude-code/proj-b:\n\ntwo")


def test_brief_prompt_empty_when_nothing_new(hub):
    assert brief(hub, PC, "prompt") == ""


def test_brief_without_identity_refused(hub):
    status, body = http.request("GET", f"{hub.url}/api/brief?event=start", "T")
    assert status == 400 and "kaptyorka join" in body


def test_feed_lists_messages_and_note_history(hub):
    box2_online(hub)
    hub_send(hub, PC, BOX2_NAME, "hi")
    run(call(hub, PC, "notes_write", topic="api", text="v1"))
    run(call(hub, PC, "notes_write", topic="api", text="v2"))
    f = feed(hub)
    assert [(m["sender"], m["to"], m["text"]) for m in f["messages"]] == \
        [("pc/claude-code/proj-a", BOX2_NAME, "hi")]
    assert [n["topic"] for n in f["notes"]] == ["api"]
    assert [h["text"] for h in f["notes"][0]["history"]] == ["v2", "v1"]


def test_feed_requires_token(hub):
    assert http.request("GET", hub.url + "/api/feed", "bad")[0] == 401


def test_routes_require_token(hub):
    assert http.request("POST", hub.url + "/bridge/heartbeat", "bad",
                        {"machine": "x", "sessions": []})[0] == 401
    assert http.request("GET", hub.url + "/bridge/poll?machine=x&wait=0", "bad")[0] == 401
    assert http.request("POST", hub.url + "/bridge/ack", "bad", {"id": 1, "ok": True})[0] == 401
    assert http.request("GET", hub.url + "/api/brief?event=start", "bad")[0] == 401


def test_websocket_without_token_closed(tmp_path):
    sent = []

    async def app(scope, receive, send):
        raise AssertionError("must not be reached")

    async def receive():
        return {"type": "websocket.connect"}

    async def send(msg):
        sent.append(msg)

    from kaptyorka.store import Store

    store = Store(tmp_path / "hub.db", legacy_token="T")
    asyncio.run(Auth(app, store)({"type": "websocket", "path": "/mcp", "headers": []},
                                 receive, send))
    assert sent[0]["type"] == "websocket.close" and sent[0]["code"] == 1008


@pytest.mark.parametrize("event", ["stop", "tool"])
def test_brief_stop_and_tool_are_unread_only(hub, event):
    box2_online(hub)
    hub_send(hub, PC, BOX2_NAME, "hi")
    assert brief(hub, BOX2, event) == "Kaptyorka message from pc/claude-code/proj-a:\n\nhi"
    assert brief(hub, BOX2, event) == ""


def test_label_in_feed_brief_and_cli_rows(hub):
    assert heartbeat(hub, "box-2", [{"key": "claude:2", "tool": "claude-code",
                                   "project": "proj-b", "label": "api-dev"},
                                  {"key": "codex:3", "tool": "codex", "project": "proj-b"}]) == 200
    labels = {a["name"]: a["label"] for a in feed(hub)["agents"]}
    assert labels == {"box-2/claude-code/proj-b": "api-dev", "box-2/codex/proj-b": ""}
    ident = {"key": "codex:3", "machine": "box-2", "tool": "codex", "project": "proj-b"}
    assert "api-dev (box-2/claude-code/proj-b)" in brief(hub, ident, "start")


def test_web_send_lands_in_inbox_with_person_name(hub):
    box2_online(hub)
    s, body = http.request("POST", hub.url + "/api/send", "T",
                           {"to": BOX2_NAME, "text": "from the page"})
    assert s == 200 and body == {"sent": 1}
    assert [(m["from"], m["text"]) for m in run(call(hub, BOX2, "inbox"))] == [(ADMIN_NAME, "from the page")]
    assert feed(hub)["messages"][-1]["from_web"] is True


def test_web_send_all_and_unknown_name(hub):
    box2_online(hub)
    hub_send(hub, PC, BOX2_NAME, "make PC online")
    s, body = http.request("POST", hub.url + "/api/send", "T", {"to": "all", "text": "hi"})
    assert s == 200 and body == {"sent": 2}
    s, body = http.request("POST", hub.url + "/api/send", "T", {"to": "nobody", "text": "hi"})
    assert s == 400 and "no session named nobody" in body
    assert http.request("POST", hub.url + "/api/send", "T", {"to": "all"})[0] == 400


def test_web_note_write(hub):
    s, _ = http.request("POST", hub.url + "/api/notes", "T", {"topic": "api", "text": "v1"})
    assert s == 204
    assert feed(hub)["notes"][0]["history"][0]["author"] == ADMIN_NAME


def test_web_routes_require_token(hub):
    assert http.request("POST", hub.url + "/api/send", "bad", {"to": "all", "text": "x"})[0] == 401
    assert http.request("POST", hub.url + "/api/notes", "bad", {"topic": "a", "text": "x"})[0] == 401


def test_agent_reply_to_web_sender_goes_to_page_not_inbox(hub):
    box2_online(hub)
    http.request("POST", hub.url + "/api/send", "T", {"to": BOX2_NAME, "text": "hi"})
    assert http.request("GET", hub.url + "/bridge/poll?machine=box-2&wait=0", "T")[1][0]["text"] == \
        f"Kaptyorka message from {ADMIN_NAME} (web page):\n\nhi"
    assert "sent to 1 session" in str(hub_send(hub, BOX2, ADMIN_NAME, "on it"))
    last = feed(hub)["messages"][-1]
    assert (last["to"], last["to_web"], last["read"]) == (ADMIN_NAME, True, True)
    texts = [m["text"] for i in (PC, BOX2) for m in run(call(hub, i, "inbox"))]
    assert "on it" not in texts
    assert f"people: {ADMIN_NAME}" in call_error(hub, BOX2, "send", to="Bob", text="x")
