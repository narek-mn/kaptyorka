import pytest
import asyncio
import threading
import time

from kaptyorka import http
from kaptyorka.auth import Auth
from helpers import call, call_error, run

PC = {"key": "claude:1", "machine": "pc", "tool": "claude-code", "project": "murmur"}
DGX = {"key": "claude:2", "machine": "dgx", "tool": "claude-code", "project": "backend"}
DGX_NAME = "dgx/claude-code/backend"


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


def dgx_online(hub, activity="idle"):
    assert heartbeat(hub, "dgx", [{"key": DGX["key"], "tool": "claude-code",
                                   "project": "backend", "activity": activity}]) == 204


def test_heartbeat_makes_sessions_online(hub):
    assert heartbeat(hub, "dgx", [
        {"key": "claude:2", "tool": "claude-code", "project": "backend", "activity": "busy"},
        {"key": "codex:3", "tool": "codex", "project": "backend", "activity": "idle"}]) == 204
    agents = feed(hub)["agents"]
    assert {a["name"] for a in agents} == {"dgx/claude-code/backend", "dgx/codex/backend"}
    assert {a["activity"] for a in agents} == {"busy", "idle"}


def test_poll_returns_immediately_when_queued(hub):
    dgx_online(hub)
    hub_send(hub, PC, DGX_NAME, "hi")
    t0 = time.time()
    status, items = http.request("GET", f"{hub.url}/bridge/poll?machine=dgx&wait=25", "T",
                                 timeout=30)
    assert status == 200 and len(items) == 1 and time.time() - t0 < 1
    assert items[0]["key"] == DGX["key"]
    assert items[0]["text"] == "Kaptyorka message from pc/claude-code/murmur:\n\nhi"


def test_poll_wakes_on_send(hub):
    dgx_online(hub)
    t0 = time.time()
    th = start_thread(lambda: http.request("GET", f"{hub.url}/bridge/poll?machine=dgx&wait=10",
                                           "T", timeout=15))
    time.sleep(0.5)
    hub_send(hub, PC, DGX_NAME, "hi")
    status, items = th.join_result()
    assert status == 200 and len(items) == 1 and time.time() - t0 < 3


def test_poll_empty_after_wait(hub):
    t0 = time.time()
    assert http.request("GET", f"{hub.url}/bridge/poll?machine=dgx&wait=1", "T") == (200, [])
    assert 0.9 < time.time() - t0 < 3


def test_ack_false_leaves_message_for_brief(hub):
    dgx_online(hub)
    hub_send(hub, PC, DGX_NAME, "move api?")
    _, items = http.request("GET", f"{hub.url}/bridge/poll?machine=dgx&wait=0", "T")
    assert http.request("POST", hub.url + "/bridge/ack", "T",
                        {"id": items[0]["id"], "ok": False})[0] == 204
    assert brief(hub, DGX, "prompt") == \
        "Kaptyorka message from pc/claude-code/murmur:\n\nmove api?"
    assert brief(hub, DGX, "prompt") == ""


def test_ack_true_marks_read(hub):
    dgx_online(hub)
    hub_send(hub, PC, DGX_NAME, "hi")
    _, items = http.request("GET", f"{hub.url}/bridge/poll?machine=dgx&wait=0", "T")
    assert http.request("POST", hub.url + "/bridge/ack", "T",
                        {"id": items[0]["id"], "ok": True})[0] == 204
    assert brief(hub, DGX, "prompt") == ""


def test_ack_string_false_is_not_ok(hub):
    dgx_online(hub)
    hub_send(hub, PC, DGX_NAME, "hi")
    _, items = http.request("GET", f"{hub.url}/bridge/poll?machine=dgx&wait=0", "T")
    assert http.request("POST", hub.url + "/bridge/ack", "T",
                        {"id": items[0]["id"], "ok": "false"})[0] == 204
    assert "hi" in brief(hub, DGX, "prompt")


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
                        {"machine": "dgx", "sessions": [{"key": "k"}]})[0] == 400
    assert http.request("POST", hub.url + "/bridge/ack", "T", {"ok": True})[0] == 400
    assert http.request("GET", hub.url + "/bridge/poll?wait=0", "T")[0] == 400
    for wait in ("abc", "nan", "inf"):
        assert http.request("GET", f"{hub.url}/bridge/poll?machine=dgx&wait={wait}",
                            "T")[0] == 400


def test_brief_start_summary_exact(hub):
    dgx_online(hub)
    run(call(hub, PC, "who"))
    run(call(hub, DGX, "notes_write", topic="murmur-api", text="v1"))
    hub_send(hub, DGX, "pc/claude-code/murmur", "ping")
    assert brief(hub, PC, "start") == (
        "Kaptyorka: you are `pc/claude-code/murmur`. 1 other online: dgx/claude-code/backend"
        " (idle). 1 unread message. Board topics: murmur-api.\n\n"
        "Kaptyorka message from dgx/claude-code/backend:\n\nping")


def test_brief_start_alone_plural_no_topics(hub):
    assert brief(hub, PC, "start") == \
        "Kaptyorka: you are `pc/claude-code/murmur`. No one else online. 0 unread messages."
    dgx_online(hub, activity="")
    heartbeat(hub, "jetson", [{"key": "oc:1", "tool": "opencode", "project": "nav",
                               "activity": "busy"}])
    run(call(hub, PC, "notes_write", topic="a", text="1"))
    run(call(hub, PC, "notes_write", topic="b", text="2"))
    hub_send(hub, DGX, "pc/claude-code/murmur", "one")
    hub_send(hub, DGX, "pc/claude-code/murmur", "two")
    assert brief(hub, PC, "start") == (
        "Kaptyorka: you are `pc/claude-code/murmur`. 2 others online: dgx/claude-code/backend,"
        " jetson/opencode/nav (busy). 2 unread messages. Board topics: b, a.\n\n"
        "Kaptyorka message from dgx/claude-code/backend:\n\none\n\n"
        "Kaptyorka message from dgx/claude-code/backend:\n\ntwo")


def test_brief_prompt_empty_when_nothing_new(hub):
    assert brief(hub, PC, "prompt") == ""


def test_brief_without_identity_refused(hub):
    status, body = http.request("GET", f"{hub.url}/api/brief?event=start", "T")
    assert status == 400 and "kaptyorka join" in body


def test_feed_lists_messages_and_note_history(hub):
    dgx_online(hub)
    hub_send(hub, PC, DGX_NAME, "hi")
    run(call(hub, PC, "notes_write", topic="api", text="v1"))
    run(call(hub, PC, "notes_write", topic="api", text="v2"))
    f = feed(hub)
    assert [(m["sender"], m["to"], m["text"]) for m in f["messages"]] == \
        [("pc/claude-code/murmur", DGX_NAME, "hi")]
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
    dgx_online(hub)
    hub_send(hub, PC, DGX_NAME, "hi")
    assert brief(hub, DGX, event) == "Kaptyorka message from pc/claude-code/murmur:\n\nhi"
    assert brief(hub, DGX, event) == ""


def test_label_in_feed_brief_and_cli_rows(hub):
    assert heartbeat(hub, "dgx", [{"key": "claude:2", "tool": "claude-code",
                                   "project": "backend", "label": "api-dev"},
                                  {"key": "codex:3", "tool": "codex", "project": "backend"}]) == 204
    labels = {a["name"]: a["label"] for a in feed(hub)["agents"]}
    assert labels == {"dgx/claude-code/backend": "api-dev", "dgx/codex/backend": ""}
    ident = {"key": "codex:3", "machine": "dgx", "tool": "codex", "project": "backend"}
    assert "api-dev (dgx/claude-code/backend)" in brief(hub, ident, "start")


def test_web_send_lands_in_inbox_with_person_name(hub):
    dgx_online(hub)
    s, body = http.request("POST", hub.url + "/api/send", "T",
                           {"to": DGX_NAME, "text": "from the page", "from": "Narek"})
    assert s == 200 and body == {"sent": 1}
    assert [(m["from"], m["text"]) for m in run(call(hub, DGX, "inbox"))] == [("Narek", "from the page")]
    assert feed(hub)["messages"][-1]["from_web"] is True


def test_web_send_all_and_unknown_name(hub):
    dgx_online(hub)
    hub_send(hub, PC, DGX_NAME, "make PC online")
    s, body = http.request("POST", hub.url + "/api/send", "T", {"to": "all", "text": "hi", "from": "Narek"})
    assert s == 200 and body == {"sent": 2}
    s, body = http.request("POST", hub.url + "/api/send", "T", {"to": "nobody", "text": "hi", "from": "Narek"})
    assert s == 400 and "no session named nobody" in body
    assert http.request("POST", hub.url + "/api/send", "T", {"to": "all"})[0] == 400


def test_web_note_write(hub):
    s, _ = http.request("POST", hub.url + "/api/notes", "T", {"topic": "api", "text": "v1", "from": "Narek"})
    assert s == 204
    assert feed(hub)["notes"][0]["history"][0]["author"] == "Narek"


def test_web_routes_require_token(hub):
    assert http.request("POST", hub.url + "/api/send", "bad", {"to": "all", "text": "x", "from": "N"})[0] == 401
    assert http.request("POST", hub.url + "/api/notes", "bad", {"topic": "a", "text": "x", "from": "N"})[0] == 401


def test_agent_reply_to_web_sender_goes_to_page_not_inbox(hub):
    dgx_online(hub)
    http.request("POST", hub.url + "/api/send", "T", {"to": DGX_NAME, "text": "hi", "from": "Narek"})
    assert http.request("GET", hub.url + "/bridge/poll?machine=dgx&wait=0", "T")[1][0]["text"] == \
        "Kaptyorka message from Narek (web page):\n\nhi"
    assert "sent to 1 session" in str(hub_send(hub, DGX, "Narek", "on it"))
    last = feed(hub)["messages"][-1]
    assert (last["to"], last["to_web"], last["read"]) == ("Narek", True, True)
    texts = [m["text"] for i in (PC, DGX) for m in run(call(hub, i, "inbox"))]
    assert "on it" not in texts
    assert "people: Narek" in call_error(hub, DGX, "send", to="Bob", text="x")
