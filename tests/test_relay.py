"""The relay of the secure design (src/kaptyorka/relay): the field filter both ways (design v2,
section 6), the caller check (SO_PEERCRED) and the pinned hub certificate, against the fake hub
in tests/fakes/relay_hub.py on loopback. The real units and the R-*/N-* attack rows run in a
throw-away container (secure-design/testbox/slice1.sh), never here."""
import ast
import base64
import hashlib
import hmac
import json
import os
import shutil
import socket
import ssl
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from fakes.relay_hub import FakeHub
from kaptyorka.relay import relay as R

RELAY_DIR = Path(R.__file__).parent
DEVICE = "dev-token-0123456789abcdef"
KEY = b"chat-key-for-tests-only-0123456789"


def make_relay(**conf):
    base = {"hub_ip": "127.0.0.1", "hub_port": 1, "pins": ["0" * 64], "name": "box-1",
            "version": "0.9.39", "idle_exit": 600}
    base.update(conf)
    logs = []
    return R.Relay(base, DEVICE, KEY, log=logs.append), logs


def out(call, args, person="alice"):
    relay, _ = make_relay()
    return relay.filter_out(person, call, args)[0]


def learned(relay, call, args, person="alice"):
    """filter_out, then keep its chats as call() does once the hub answered 200."""
    body, ctx = relay.filter_out(person, call, args)
    relay.learn(ctx)
    return body


def caller(relay=None, chat="claude-code:1111-2222", project="proj-a"):
    return {"chat": chat, "project": project}


# ---------------------------------------------------------------- the module stays on its own

def test_the_relay_imports_only_the_standard_library():
    tree = ast.parse((RELAY_DIR / "relay.py").read_text())
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, "no relative imports"
            names.add(node.module.split(".")[0])
    assert "kaptyorka" not in names
    assert names <= set(sys.stdlib_module_names), names - set(sys.stdlib_module_names)


def test_the_units_keep_the_slice0_changes_and_hard_limits():
    svc = (RELAY_DIR / "units" / "kaptyorka-relay.service").read_text()
    lines = [l.strip() for l in svc.splitlines() if l.strip() and not l.startswith("#")]
    assert not any(l.startswith(("ProtectControlGroups=", "PrivateUsers=")) for l in lines)  # D1, D2
    for need in ("DynamicUser=yes", "KillMode=control-group", "MemorySwapMax=0", "PrivateIPC=yes",
                 "TemporaryFileSystem=/etc:ro /var:ro /opt:ro", "TemporaryFileSystem=/dev/shm",
                 "SocketBindDeny=any",
                 "RestrictAddressFamilies=AF_INET AF_INET6", "IPAddressDeny=any",
                 "LoadCredential=device-token:/etc/kaptyorka/secret/device-token",
                 "LoadCredential=chat-key:/etc/kaptyorka/secret/chat-key"):
        assert need in lines, need
    assert any(l.startswith("RuntimeMaxSec=") for l in lines)
    # measured on systemd 255: "/" as a TemporaryFileSystem= is ignored, and a /sys tmpfs is
    # undone by ProtectKernelTunables=yes; /sys is hidden with InaccessiblePaths= instead
    assert not any(l.startswith("TemporaryFileSystem=/:") or l == "TemporaryFileSystem=/" for l in lines)
    assert not any(l.startswith("TemporaryFileSystem=/sys") for l in lines)
    assert any(l.startswith("InaccessiblePaths=/sys ") for l in lines)
    sock = (RELAY_DIR / "units" / "kaptyorka-relay-box@.socket").read_text()
    for need in ("ListenStream=/run/kaptyorka/relay/%i.sock", "SocketUser=root",
                 "SocketGroup=kap-%i", "SocketMode=0660", "FileDescriptorName=box-%i",
                 "Service=kaptyorka-relay.service"):
        assert need in sock, need


# ---------------------------------------------------------------- box to hub

def test_unknown_fields_are_dropped_and_the_chat_id_is_a_keyed_hash():
    body = out("send", {**caller(), "to": "bob", "text": "hi", "cwd": "/home/alice/x",
                        "user": "alice", "hostname": "box-1", "branch": "main"})
    assert set(body) == {"chat", "project", "to", "text"}
    assert body["chat"] != "claude-code:1111-2222" and len(body["chat"]) == 16
    assert body["chat"] == hmac.new(KEY, b"alice\0claude-code:1111-2222", hashlib.sha256).hexdigest()[:16]


def test_unknown_calls_and_missing_fields_are_refused():
    relay, _ = make_relay()
    for call in ("update", "config", "set_mode", "shared_device", "run", "read_path", "login"):
        with pytest.raises(R.Bad):
            relay.filter_out("alice", call, {})
    with pytest.raises(R.Bad, match="'text'"):
        relay.filter_out("alice", "send", {**caller(), "to": "bob"})
    with pytest.raises(R.Bad, match="'chat'"):
        relay.filter_out("alice", "inbox", {"project": "proj-a"})
    with pytest.raises(R.Bad):
        relay.filter_out("alice", "send", ["not", "an", "object"])


@pytest.mark.parametrize("project", ["../proj-b", "/home/alice", "proj a", "", "a" * 65, 7,
                                     ".hidden", "proj-a/../../etc"])
def test_a_project_is_a_plain_team_folder_name(project):
    with pytest.raises(R.Bad):
        out("inbox", {"chat": "s1", "project": project})


def test_report_sends_only_the_fields_of_6_2_and_cuts_the_name():
    body = out("report", {"chats": [{
        "chat": "s1", "project": "proj-a", "name": "\x1b]0;evil\x07title\x1b[31m red\n" + "x" * 200,
        "state": "busy", "last_activity": 1764140000.5, "cwd": "/home/alice", "tool": "claude",
        "pid": 4242, "branch": "main", "usage": {"five_hour": 12}}], "machine": "laptop"})
    [c] = body["chats"]
    assert set(c) == {"chat", "project", "name", "state", "last_activity"}
    assert c["name"].startswith("title red") and len(c["name"].encode()) <= 80
    assert "\x1b" not in c["name"] and "\n" not in c["name"]
    assert set(body) == {"chats"}
    for bad in ({"state": "running"}, {"last_activity": "yesterday"}, {"last_activity": True},
                {"last_activity": float("nan")}):
        with pytest.raises(R.Bad):
            out("report", {"chats": [{"chat": "s1", "project": "proj-a", "state": "idle", **bad}]})


def test_report_takes_at_most_64_chats():
    body = out("report", {"chats": [{"chat": f"s{i}", "project": "proj-a", "state": "idle"}
                                    for i in range(100)]})
    assert len(body["chats"]) == 64


def test_text_keeps_lines_drops_escapes_and_is_cut():
    body = out("notes_write", {**caller(), "topic": "proj-a",
                               "text": "line 1\r\nline\t2\x07\x1b[2J‮evil‬" + "y" * 70000})
    assert body["text"].startswith("line 1\nline\t2evil")
    assert len(body["text"].encode()) == 65536
    assert "‮" not in body["text"]


def test_a_send_target_with_control_characters_is_refused_not_cut():
    for to in ("bob\nmanager", "a" * 129, "", "\x1b[31mbob"):
        with pytest.raises(R.Bad):
            out("send", {**caller(), "to": to, "text": "x"})


def test_files_text_up_to_256k_binary_up_to_1m_never_cut():
    ok = out("send", {**caller(), "to": "bob", "text": "f", "file_name": "notes.md",
                      "file_content": "a" * (256 * 1024)})
    assert len(ok["file_content"]) == 256 * 1024
    with pytest.raises(R.Bad, match="256 KB"):
        out("send", {**caller(), "to": "bob", "text": "f", "file_name": "notes.md",
                     "file_content": "a" * (256 * 1024 + 1)})
    big = base64.b64encode(b"\x00" * (1024 * 1024)).decode()
    assert out("send", {**caller(), "to": "bob", "text": "f", "file_name": "a.png",
                        "file_content": "base64:" + big})["file_content"] == "base64:" + big
    with pytest.raises(R.Bad, match="1 MB"):
        out("send", {**caller(), "to": "bob", "text": "f", "file_name": "a.png",
                     "file_content": "base64:" + base64.b64encode(b"\x00" * (1024 * 1024 + 1)).decode()})
    with pytest.raises(R.Bad, match="base64"):
        out("send", {**caller(), "to": "bob", "text": "f", "file_name": "a.png",
                     "file_content": "base64:%%%"})
    for name in ("../../.ssh/id_ed25519", "/etc/passwd", ".bashrc", "a/b", "x\n.png"):
        with pytest.raises(R.Bad):
            out("send", {**caller(), "to": "bob", "text": "f", "file_name": name, "file_content": "x"})


@pytest.mark.parametrize("secret", [DEVICE, KEY.decode(), "sk-ant-oat01-" + "A" * 40,
                                    "ghp_" + "a" * 36, "-----BEGIN OPENSSH PRIVATE KEY-----",
                                    "glpat-" + "b" * 20, "AKIA" + "A" * 16])
def test_token_shaped_text_never_leaves(secret):
    with pytest.raises(R.Bad, match="key or token"):
        out("send", {**caller(), "to": "bob", "text": f"here: {secret} ok"})
    with pytest.raises(R.Bad, match="key or token"):
        out("notes_write", {**caller(), "topic": "t", "text": secret})
    with pytest.raises(R.Bad, match="key or token"):
        out("send", {**caller(), "to": "bob", "text": "f", "file_name": "k.bin",
                     "file_content": "base64:" + base64.b64encode(secret.encode()).decode()})


def test_join_is_not_a_call_of_the_box():
    with pytest.raises(R.Bad, match="unknown call"):
        out("join", {"name": "alice-laptop"})


def test_helper_requests_carry_project_prompt_and_chat_ids_only():
    body = out("work", {**caller(), "prompt": "fix it", "machine": "box-2", "path": "/home/alice"})
    assert set(body) == {"chat", "project", "prompt"}
    body = out("close_worker", {**caller(), "worker": "claude-code:w1", "reason": "x"})
    assert set(body) == {"chat", "project", "worker"} and len(body["worker"]) == 16


# ---------------------------------------------------------------- hub to box

def test_only_messages_for_this_box_s_chats_come_in_with_their_fields():
    relay, _ = make_relay()
    h = learned(relay, "report", {"chats": [{"chat": "s1", "project": "proj-a", "state": "idle"}]})["chats"][0]["chat"]
    got = relay.filter_in("alice", "report", {
        "messages": [
            {"id": 1, "chat": h, "text": "hello\x1b]52;c;ZXZpbA==\x07", "person": "bob",
             "machine": "box-2", "kind": "chat", "path": "/home/alice", "run": "rm -rf ~",
             "files": [{"id": 9, "name": "a.txt", "size": 3, "url": "http://evil"}]},
            {"id": 2, "chat": "0123456789abcdef", "text": "not ours"},
            {"id": 3, "chat": h, "text": "bad kind", "kind": "root"},
        ],
        "close": [{"chat": h}, {"chat": "fedcba9876543210"}],
        "helpers": [{"project": "proj-a", "prompt": "go"}, {"project": "/home/alice", "prompt": "x"}],
        "skills": [{"name": "review", "text": "t"}, {"name": "../../.bashrc", "text": "x"}],
        "update": {"to": "9.9.9"}, "config": {"hub": "http://evil"}, "mode": "team",
        "shared_device": True,
    })
    assert set(got) == {"messages", "close", "helpers", "skills"}
    assert got["messages"] == [{"id": 1, "chat": "s1", "text": "hello", "person": "bob",
                                "machine": "box-2", "kind": "chat",
                                "files": [{"id": 9, "name": "a.txt", "size": 3}]}]
    assert got["close"] == [{"chat": "s1"}]
    assert got["helpers"] == [{"project": "proj-a", "prompt": "go"}]
    assert got["skills"] == [{"name": "review", "text": "t"}]


def test_who_marks_this_box_s_chats_and_prefixes_every_other_id():
    relay, _ = make_relay()
    h = learned(relay, "report", {"chats": [{"chat": "s1", "project": "proj-a", "state": "idle"}]})["chats"][0]["chat"]
    got = relay.filter_in("alice", "who", {"chats": [
        {"chat": "box-2:abc", "person": "bob", "project": "proj-a", "name": "t\x1b[1m",
         "state": "busy", "cwd": "/home/bob", "tool": "codex"},
        {"chat": "s1", "person": "bob", "state": "ended"},   # shaped like one of the box's own ids
        {"chat": h, "state": "idle"},
        {"chat": "../x", "person": "bob"}]})
    assert got == {"chats": [
        {"chat": "hub:box-2:abc", "person": "bob", "project": "proj-a", "name": "t", "state": "busy",
         "mine": False},
        {"chat": "hub:s1", "person": "bob", "state": "ended", "mine": False},
        {"chat": "s1", "state": "idle", "mine": True}]}


def test_an_answer_of_the_wrong_shape_is_refused():
    relay, _ = make_relay()
    for call, ans in (("report", []), ("fetch_file", {"name": "a.txt"}), ("inbox", {"messages": 5}),
                      ("fetch_file", {"name": "../a", "content": "x"})):
        with pytest.raises(R.Bad):
            relay.filter_in("alice", call, ans)


# ---------------------------------------------------------------- the caller check

def test_peer_uid_is_the_kernel_s_answer():
    a, b = socket.socketpair(socket.AF_UNIX)
    try:
        assert R.peer_uid(a) == R.peer_uid(b) == os.getuid()
    finally:
        a.close()
        b.close()


def talk(relay, line: bytes, want_uid, person="alice"):
    a, b = socket.socketpair(socket.AF_UNIX)
    t = threading.Thread(target=relay.serve_conn, args=(b, person, want_uid))
    t.start()
    try:
        a.sendall(line)
    except BrokenPipeError:  # a refused caller is answered and dropped before it writes
        pass
    data = b""
    while not data.endswith(b"\n"):
        got = a.recv(65536)
        if not got:
            break
        data += got
    t.join(5)
    a.close()
    return json.loads(data)


def test_a_caller_that_is_not_the_box_user_is_refused_before_anything_is_read(certs):
    hub = FakeHub(certs["cert"], certs["key"])
    try:
        relay, logs = make_relay(hub_port=hub.port, pins=[certs["fp"]])
        line = json.dumps({"call": "inbox", "args": caller()}).encode() + b"\n"
        for want in (os.getuid() + 1, None):
            got = talk(relay, line, want)
            assert got == {"ok": False, "error": "refused: you are not this box's user"}
        assert hub.requests == []
        assert any("refused uid" in l for l in logs)
        assert talk(relay, line, os.getuid())["ok"] is True
        assert len(hub.requests) == 1
    finally:
        hub.stop()


def test_garbage_and_oversized_lines_get_one_plain_error():
    relay, _ = make_relay()
    assert talk(relay, b"not json\n", os.getuid()) == {"ok": False, "error": "the request is not JSON"}
    assert "call" in talk(relay, b"[1,2]\n", os.getuid())["error"]
    a, b = socket.socketpair(socket.AF_UNIX)
    with pytest.raises(R.Bad, match="too large"):
        threading.Thread(target=lambda: (a.sendall(b"x" * 300), a.close()), daemon=True).start()
        R.read_line(b, limit=100)
    b.close()


# ---------------------------------------------------------------- the pinned hub

@pytest.fixture(scope="module")
def certs(tmp_path_factory):
    if shutil.which("openssl") is None:
        pytest.skip("openssl not installed")
    d = tmp_path_factory.mktemp("relay-certs")
    out = {}
    for name in ("hub", "fake"):
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout",
                        str(d / f"{name}.key"), "-out", str(d / f"{name}.pem"), "-days", "2",
                        "-subj", "/CN=hub"], check=True, capture_output=True, timeout=60)
    der = subprocess.run(["openssl", "x509", "-in", str(d / "hub.pem"), "-outform", "der"],
                         check=True, capture_output=True, timeout=60).stdout
    out.update(cert=str(d / "hub.pem"), key=str(d / "hub.key"), fake_cert=str(d / "fake.pem"),
               fake_key=str(d / "fake.key"), fp=hashlib.sha256(der).hexdigest())
    return out


@pytest.fixture
def hub(certs):
    h = FakeHub(certs["cert"], certs["key"])
    yield h
    h.stop()


def test_a_call_reaches_the_pinned_hub_with_only_the_device_token_version_and_mode(hub, certs):
    relay, _ = make_relay(hub_port=hub.port, pins=["11" * 32, certs["fp"]])
    got = relay.call("alice", "send", {**caller(), "to": "bob", "text": "hi", "secret_path": "/home/alice"})
    assert got == {"ok": True, "result": {"ok": True}}  # the hub's "echo" was dropped
    [req] = hub.requests
    assert req["line"] == "POST /box/v1/send HTTP/1.1"
    assert set(req["headers"]) == {"host", "authorization", "x-kaptyorka-version",
                                   "x-kaptyorka-mode", "content-type", "content-length",
                                   "connection"}
    assert req["headers"]["authorization"] == "Bearer " + DEVICE
    assert req["headers"]["x-kaptyorka-mode"] == "personal"
    assert req["headers"]["x-kaptyorka-version"] == "0.9.39"
    assert set(json.loads(req["body"])) == {"chat", "project", "to", "text"}
    assert b"claude-code:1111-2222" not in req["raw"] and b"/home/alice" not in req["raw"]


def test_a_false_certificate_gets_nothing(certs):
    fake = FakeHub(certs["fake_cert"], certs["fake_key"])
    try:
        relay, logs = make_relay(hub_port=fake.port, pins=[certs["fp"]])
        got = relay.call("alice", "send", {**caller(), "to": "bob", "text": "CANARY-SEND"})
        assert got["ok"] is False and "certificate" in got["error"]
        assert not [r for r in fake.requests if r["line"]]   # no byte of a request reached it
        assert any("not the pinned one" in l for l in logs)
    finally:
        fake.stop()


def test_plain_http_gets_no_request(certs, tmp_path):
    plain = FakeHub(None, None, log_path=str(tmp_path / "log"), read_timeout=0.5)
    try:
        relay, _ = make_relay(hub_port=plain.port, pins=[certs["fp"]])
        got = relay.call("alice", "send", {**caller(), "to": "bob", "text": "CANARY-SEND"})
        assert got == {"ok": False, "error": "cannot reach the hub"}
        log = (tmp_path / "log").read_bytes() if (tmp_path / "log").exists() else b""
        assert DEVICE.encode() not in log and b"CANARY-SEND" not in log
    finally:
        plain.stop()


def test_nothing_is_sent_when_the_filter_refuses(hub, certs):
    relay, _ = make_relay(hub_port=hub.port, pins=[certs["fp"]])
    got = relay.call("alice", "send", {**caller(), "to": "bob", "text": "my key " + DEVICE})
    assert got["ok"] is False
    got = relay.call("alice", "update", {"to": "9.9.9"})
    assert got == {"ok": False, "error": "unknown call"}
    assert hub.requests == []


@pytest.mark.parametrize("status,headers,body,want", [
    (302, {"Location": "http://evil/login"}, {"ok": True}, "redirect"),
    (401, {}, {"error": "\x1b]52;c;ZXZpbA==\x07sign in at http://evil\nsecond line"},
     "the hub does not accept this computer's key (401)"),
    (500, {"Content-Type": "text/html"}, "<html>oops</html>", "the hub has a problem, try again later (500)"),
    (200, {"Content-Type": "text/html"}, "<html>ok</html>", "not JSON"),
    (200, {}, "x" * (R.MAX_ANSWER + 10), "too large"),
    (200, {}, "{not json", "expected shape"),
    (200, {}, {"messages": "all of them"}, "expected shape"),
])
def test_hub_answers_outside_the_table_become_one_plain_line(hub, certs, status, headers, body, want):
    relay, _ = make_relay(hub_port=hub.port, pins=[certs["fp"]])
    hub.answers["inbox"] = (status, headers, body)
    got = relay.call("alice", "inbox", caller())
    assert got["ok"] is False and want in got["error"], got
    assert "evil" not in got["error"] and "html" not in got["error"]
    assert "\n" not in got["error"] and "\x1b" not in got["error"]


def test_sign_in_requests_and_tool_changes_in_an_answer_are_dropped(hub, certs):
    relay, _ = make_relay(hub_port=hub.port, pins=[certs["fp"]])
    hub.answers["notes_write"] = (200, {}, {
        "ok": True, "reply": "saved", "auth_url": "http://evil/login", "elicitation": {"ask": "pw"},
        "tools": [{"name": "run_shell"}], "redirect": "http://evil"})
    assert relay.call("alice", "notes_write", {**caller(), "topic": "t", "text": "x"}) == \
        {"ok": True, "result": {"ok": True, "reply": "saved"}}


def test_fetch_file_brings_in_only_name_size_and_a_checked_content(hub, certs):
    relay, _ = make_relay(hub_port=hub.port, pins=[certs["fp"]])
    hub.answers["fetch_file"] = (200, {}, {"name": "a.txt", "size": 2, "content": "hi",
                                           "path": "/home/alice/.ssh/id_ed25519"})
    learned(relay, "report", {"chats": [{"chat": caller()["chat"], "project": "proj-a", "state": "idle"}]})
    h = relay.hash_chat("alice", caller()["chat"])
    relay.filter_in("alice", "inbox", {"messages": [{"id": 1, "chat": h, "text": "file",
                                                     "files": [{"id": 3, "name": "a.txt", "size": 2}]}]})
    assert relay.call("alice", "fetch_file", {**caller(), "id": 3}) == \
        {"ok": True, "result": {"name": "a.txt", "size": 2, "content": "hi"}}


# ---------------------------------------------------------------- the config

def test_the_config_wants_an_address_pins_a_name_and_a_version(tmp_path):
    p = tmp_path / "relay.conf"
    good = "hub_ip=10.77.0.1\nhub_port=8443\nhub_sha256=%s\nname=box-1\nversion=0.9.39\n" % ("ab" * 32)
    p.write_text(good)
    c = R.load_conf(str(p))
    assert c["pins"] == ["ab" * 32] and c["hub_ip"] == "10.77.0.1" and c["idle_exit"] == 600
    for bad in (good.replace("10.77.0.1", "hub.example"), good.replace("ab" * 32, "abc"),
                good.replace("name=box-1", "name=a\x1bb"), good.replace("version=0.9.39\n", "")):
        p.write_text(bad)
        with pytest.raises((ValueError, KeyError, OSError)):
            R.load_conf(str(p))


def test_no_sockets_from_systemd_means_no_start():
    with pytest.raises(SystemExit):
        R.doors_from_systemd({"LISTEN_PID": "1", "LISTEN_FDS": "1"})


# ---------------------------------------------------------------- the security review (09)

def _report(relay, person, chat, hub_answer=None):
    """A report of one chat through `person`'s door, as call() handles a 200 answer."""
    learned(relay, "report", {"chats": [{"chat": chat, "project": "proj-a", "state": "idle"}]}, person)
    return relay.hash_chat(person, chat)


def test_one_relay_two_doors_bob_cannot_see_ack_fetch_or_name_alice_s_chats():
    relay, _ = make_relay()
    ha = _report(relay, "alice", "claude-code:alice-1")
    assert relay.hash_chat("bob", "claude-code:alice-1") != ha   # the person is in the hash
    msg = {"messages": [{"id": 5, "chat": ha, "text": "for alice only",
                         "files": [{"id": 9, "name": "a.txt", "size": 1}]}]}
    assert relay.filter_in("bob", "inbox", msg) == {"messages": []}
    assert relay.filter_in("bob", "report", {"close": [{"chat": ha}]}) == {"close": []}
    for call, args in (("ack", {"id": 5}), ("fetch_file", {**caller(), "id": 9})):
        with pytest.raises(R.Bad, match="this box received"):
            relay.filter_out("bob", call, args)
    # alice's own door gets the message, and then may ack it and fetch its file
    assert relay.filter_in("alice", "inbox", msg)["messages"][0]["chat"] == "claude-code:alice-1"
    assert relay.filter_out("alice", "ack", {"id": 5})[0] == {"id": 5}
    assert relay.filter_out("alice", "fetch_file", {**caller(), "id": 9})[0]["id"] == 9
    # bob sending as alice's session id goes out as another hash, unknown to alice's door
    hb = relay.filter_out("bob", "send", {"chat": "claude-code:alice-1", "project": "proj-a",
                                          "to": "x", "text": "hi"})[0]["chat"]
    assert hb != ha and relay.unhash_chat("alice", hb) is None


def test_the_chat_map_learns_only_from_calls_the_hub_took(hub, certs):
    relay, _ = make_relay(hub_port=hub.port, pins=[certs["fp"]])
    refused = {"chat": "claude-code:never", "project": "proj-a", "to": "bob", "text": "ghp_" + "a" * 36}
    assert relay.call("alice", "send", refused)["ok"] is False
    assert relay.unhash_chat("alice", relay.hash_chat("alice", "claude-code:never")) is None
    down, _ = make_relay(hub_port=1, pins=[certs["fp"]])
    assert down.call("alice", "inbox", caller())["error"] == "cannot reach the hub"
    assert down.unhash_chat("alice", down.hash_chat("alice", caller()["chat"])) is None
    hub.answers["inbox"] = (500, {}, {"error": "x"})
    assert relay.call("alice", "inbox", caller())["ok"] is False
    assert relay.unhash_chat("alice", relay.hash_chat("alice", caller()["chat"])) is None
    del hub.answers["inbox"]
    assert relay.call("alice", "inbox", caller())["ok"] is True
    assert relay.unhash_chat("alice", relay.hash_chat("alice", caller()["chat"])) == caller()["chat"]


def _slow_hub(certs, mode):
    """A hub with the right certificate that never finishes: it trickles one byte at a time,
    or sends 100 Continue over and over."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(certs["cert"], certs["key"])
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(4)
    stop = threading.Event()

    def one(c):
        try:
            c = ctx.wrap_socket(c, server_side=True)
            c.settimeout(5)
            c.recv(65536)
            if mode == "trickle":
                c.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 999999\r\n\r\n")
            while not stop.is_set():
                c.sendall(b"x" if mode == "trickle" else b"HTTP/1.1 100 Continue\r\n\r\n")
                time.sleep(0.1)
        except OSError:
            pass

    def loop():
        while not stop.is_set():
            try:
                c, _ = srv.accept()
            except OSError:
                return
            threading.Thread(target=one, args=(c,), daemon=True).start()
    threading.Thread(target=loop, daemon=True).start()
    return srv, stop


@pytest.mark.parametrize("mode", ["trickle", "continue"])
def test_one_deadline_for_the_whole_hub_exchange(certs, monkeypatch, mode):
    monkeypatch.setattr(R, "HUB_TOTAL", 1.0)
    srv, stop = _slow_hub(certs, mode)
    try:
        relay, _ = make_relay(hub_port=srv.getsockname()[1], pins=[certs["fp"]])
        t0 = time.monotonic()
        got = relay.call("alice", "inbox", caller())
        assert got == {"ok": False, "error": "cannot reach the hub"}
        assert time.monotonic() - t0 < 4
    finally:
        stop.set()
        srv.close()


def test_a_line_of_mostly_structure_is_refused_before_it_is_parsed(monkeypatch):
    relay, _ = make_relay()
    junk = b'{"call":"send","args":[' + b"[]," * 50000 + b"[]]}\n"
    assert talk(relay, junk, os.getuid()) == {"ok": False, "error": "the request has too many parts"}
    deep = b'{"call":"send","args":' + b"[" * 100000 + b"]" * 100000 + b"}\n"
    assert talk(relay, deep, os.getuid()) == {"ok": False, "error": "the request has too many parts"}
    nums = b'{"call":"send","args":[' + b"1," * 40000 + b"1]}\n"
    assert talk(relay, nums, os.getuid())["error"] == "the request has too many parts"
    with pytest.raises(R.Bad):
        R.parse_json(b"[" * 100000)


def test_hub_answers_of_mostly_structure_are_dropped(hub, certs):
    relay, _ = make_relay(hub_port=hub.port, pins=[certs["fp"]])
    for junk in ('{"messages":[' + "[]," * 50000 + "[]]}", "[" * 100000 + "]" * 100000):
        hub.answers["inbox"] = (200, {}, junk)
        assert "expected shape" in relay.call("alice", "inbox", caller())["error"]


def test_only_a_few_large_requests_at_once(monkeypatch):
    relay, _ = make_relay()
    monkeypatch.setattr(R, "BIG_WAIT", 0.1)
    relay.big = threading.BoundedSemaphore(1)
    relay.big.acquire()   # the one slot is taken
    big = json.dumps({"call": "inbox", "args": {**caller(), "pad": "x" * (R.BIG + 1)}}).encode() + b"\n"
    assert talk(relay, big, os.getuid()) == R.BUSY
    relay.big.release()
    assert talk(relay, big, os.getuid())["error"] == "cannot reach the hub"   # its turn: it goes on


def test_a_hub_refusal_is_the_relay_s_fixed_line_and_the_hub_s_words_apart(hub, certs):
    relay, _ = make_relay(hub_port=hub.port, pins=[certs["fp"]])
    hub.answers["who"] = (401, {}, {"error": "\x1b[31msign in at http://evil/login\nnow"})
    assert relay.call("alice", "who", caller()) == {
        "ok": False, "error": "the hub does not accept this computer's key (401)",
        "hub_said": "sign in at http://evil/login"}
    hub.answers["who"] = (400, {}, {"error": "bad Authorization: Bearer " + DEVICE + " key " + KEY.decode()})
    got = relay.call("alice", "who", caller())
    assert DEVICE not in json.dumps(got) and KEY.decode() not in json.dumps(got)
    assert got["hub_said"] == "bad Authorization: Bearer [removed] key [removed]"
    hub.answers["who"] = (418, {}, "")
    assert relay.call("alice", "who", caller()) == {"ok": False, "error": "the hub refused the call (418)"}


def test_the_device_token_and_chat_key_never_come_in():
    relay, _ = make_relay()
    h = _report(relay, "alice", "s1")
    got = relay.filter_in("alice", "brief", {"text": "token is " + DEVICE + ", key " + KEY.hex()})
    assert got == {"text": "token is [removed], key [removed]"}
    got = relay.filter_in("alice", "inbox", {"messages": [
        {"id": 1, "chat": h, "text": "a", "person": DEVICE},
        {"id": 2, "chat": h, "text": "b"}]})
    assert [m["id"] for m in got["messages"]] == [2]
    with pytest.raises(R.Bad):
        relay.filter_in("alice", "fetch_file", {"name": "a.txt", "content": "k=" + DEVICE})
    assert relay.filter_in("alice", "projects", {"projects": [DEVICE, "proj-a"]}) == {"projects": ["proj-a"]}


INVISIBLE = ["\U000e0041\U000e0042", "\u200b", "\u2060", "\ufeff", "\u00ad", "\u180e", "\u2028",
             "\u2029", "\u202e", "\u2066", "\ufff9", "\ue000", "\ud800"]


@pytest.mark.parametrize("ch", INVISIBLE)
def test_invisible_characters_are_dropped_both_ways(ch):
    assert out("notes_write", {**caller(), "topic": "t", "text": f"a{ch}b\nc"})["text"] == "a" + "b\nc"
    with pytest.raises(R.Bad):
        out("send", {**caller(), "to": f"bob{ch}manager", "text": "x"})
    relay, _ = make_relay()
    h = _report(relay, "alice", "s1")
    got = relay.filter_in("alice", "inbox", {"messages": [
        {"id": 1, "chat": h, "text": f"hi{ch} there"},
        {"id": 2, "chat": h, "text": "x", "person": f"bob{ch}"}]})
    assert got == {"messages": [{"id": 1, "chat": "s1", "text": "hi there"}]}


def test_joiners_stay_in_text_but_not_in_names():
    family = "\U0001f468\u200d\U0001f469\u200d\U0001f467"
    assert out("notes_write", {**caller(), "topic": "t", "text": family})["text"] == family
    with pytest.raises(R.Bad):
        out("send", {**caller(), "to": "bo\u200db", "text": "x"})


def test_an_unclosed_escape_does_not_eat_the_rest_of_the_text():
    assert R.plain("keep \x1b]0; all of this\nand this line too", True) == "keep \nand this line too"


def test_strange_inputs_get_one_plain_answer_line(hub, certs):
    relay, logs = make_relay(hub_port=hub.port, pins=[certs["fp"]])
    huge = {"chats": [{"chat": "s1", "project": "proj-a", "state": "idle", "last_activity": 10 ** 400}]}
    with pytest.raises(R.Bad, match="not a number"):
        relay.filter_out("alice", "report", huge)
    line = b'{"call":"\\ud800","args":{}}\n'
    assert talk(relay, line, os.getuid()) == {"ok": False, "error": "unknown call"}
    assert any(l == "relay: alice unknown refused" for l in logs)
    hub.answers["brief"] = (200, {}, '{"text": "a\\ud800b"}')
    assert relay.call("alice", "brief", caller()) == {"ok": True, "result": {"text": "ab"}}


def test_an_unexpected_error_is_one_line_and_the_class_name_only(monkeypatch):
    relay, logs = make_relay()

    def boom(*a, **k):
        raise RuntimeError("secret detail")
    monkeypatch.setattr(relay, "call", boom)
    line = json.dumps({"call": "inbox", "args": caller()}).encode() + b"\n"
    assert talk(relay, line, os.getuid()) == R.FAILED
    assert logs[-1] == "relay: alice inbox failed: RuntimeError"


@pytest.mark.parametrize("field,value", [
    ("topic", "ghp_" + "a" * 36), ("file_name", "sk-ant-oat01-" + "A" * 90),
    ("text", "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.x"), ("text", "hf_" + "a" * 34),
    ("text", "AIza" + "B" * 35), ("text", "npm_" + "c" * 36), ("text", "sk_live_" + "d" * 24),
    ("text", "xapp-1-" + "E" * 20), ("text", "pypi-AgEIcHlwaS5vcmc" + "f" * 50),
    ("text", "b3BlbnNzaC1rZXktdjEAAAAA")])
def test_the_token_shape_check_covers_names_and_more_shapes(field, value):
    args = {**caller(), "to": "bob", "text": "x", "topic": "t", "file_name": "a.txt", "file_content": "x"}
    args[field] = value
    call = "notes_write" if field == "topic" else "send"
    with pytest.raises(R.Bad, match="key or token"):
        out(call, args)


def test_each_door_has_its_own_slots_and_a_full_door_says_busy(tmp_path):
    relay, _ = make_relay(idle_exit=1, box_uids={"alice": os.getuid(), "bob": os.getuid()})
    doors = []
    for p in ("alice", "bob"):
        s = socket.socket(socket.AF_UNIX)
        s.bind(str(tmp_path / f"{p}.sock"))
        s.listen(32)
        doors.append((s, p))
    threading.Thread(target=R.serve, args=(relay, doors), daemon=True).start()
    held = []
    try:
        for _ in range(R.MAX_PER_DOOR):
            c = socket.socket(socket.AF_UNIX)
            c.connect(str(tmp_path / "alice.sock"))
            held.append(c)
        c = socket.socket(socket.AF_UNIX)
        c.settimeout(5)
        c.connect(str(tmp_path / "alice.sock"))
        assert json.loads(c.recv(4096)) == R.BUSY
        c.close()
        c = socket.socket(socket.AF_UNIX)
        c.settimeout(5)
        c.connect(str(tmp_path / "bob.sock"))
        c.sendall(b"not json\n")
        assert json.loads(c.recv(4096)) == {"ok": False, "error": "the request is not JSON"}
        c.close()
    finally:
        for c in held:
            c.close()


def test_the_config_checks_its_numbers_and_box_uids(tmp_path):
    p = tmp_path / "relay.conf"
    good = "hub_ip=10.77.0.1\nhub_port=8443\nhub_sha256=%s\nname=box-1\nversion=0.9.39\n" % ("ab" * 32)
    p.write_text(good + "box_uids=alice:1001, bob:1002\n")
    c = R.load_conf(str(p))
    assert c["box_uids"] == {"alice": 1001, "bob": 1002}
    assert R.box_uid("alice", c) == 1001 and R.box_uid("carol", c) is None
    for bad in ("hub_port=0", "hub_port=70000", "idle_exit=nan", "idle_exit=-1", "idle_exit=inf",
                "box_uids=alice:0", "box_uids=Alice:1001", "box_uids=alice"):
        k = bad.split("=")[0]
        p.write_text("\n".join(l for l in good.splitlines() if not l.startswith(k + "=")) + "\n" + bad + "\n")
        with pytest.raises(ValueError):
            R.load_conf(str(p))


def test_the_chat_key_file_is_hex_text():
    assert R.read_chat_key(b"ab" * 32 + b"\n") == bytes.fromhex("ab" * 32)
    for bad in (b"\x00 binary \n" * 4, b"abc", b"ab" * 8):
        with pytest.raises(ValueError):
            R.read_chat_key(bad)
