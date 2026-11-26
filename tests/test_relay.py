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
import subprocess
import sys
import threading
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


def out(call, args):
    relay, _ = make_relay()
    return relay.filter_out(call, args)


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
                 "TemporaryFileSystem=/sys/fs/cgroup:ro", "TemporaryFileSystem=/dev/shm",
                 "RestrictAddressFamilies=AF_INET AF_INET6", "IPAddressDeny=any",
                 "LoadCredential=device-token:/etc/kaptyorka/secret/device-token",
                 "LoadCredential=chat-key:/etc/kaptyorka/secret/chat-key"):
        assert need in lines, need
    assert any(l.startswith("RuntimeMaxSec=") for l in lines)
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
    assert body["chat"] == hmac.new(KEY, b"claude-code:1111-2222", hashlib.sha256).hexdigest()[:16]


def test_unknown_calls_and_missing_fields_are_refused():
    relay, _ = make_relay()
    for call in ("update", "config", "set_mode", "shared_device", "run", "read_path", "login"):
        with pytest.raises(R.Bad):
            relay.filter_out(call, {})
    with pytest.raises(R.Bad, match="'text'"):
        relay.filter_out("send", {**caller(), "to": "bob"})
    with pytest.raises(R.Bad, match="'chat'"):
        relay.filter_out("inbox", {"project": "proj-a"})
    with pytest.raises(R.Bad):
        relay.filter_out("send", ["not", "an", "object"])


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


def test_join_sends_only_the_chosen_name():
    assert out("join", {"name": "alice-laptop", "hostname": "x", "user": "alice"}) == {"name": "box-1"}


def test_helper_requests_carry_project_prompt_and_chat_ids_only():
    body = out("work", {**caller(), "prompt": "fix it", "machine": "box-2", "path": "/home/alice"})
    assert set(body) == {"chat", "project", "prompt"}
    body = out("close_worker", {**caller(), "worker": "claude-code:w1", "reason": "x"})
    assert set(body) == {"chat", "project", "worker"} and len(body["worker"]) == 16


# ---------------------------------------------------------------- hub to box

def test_only_messages_for_this_box_s_chats_come_in_with_their_fields():
    relay, _ = make_relay()
    h = relay.filter_out("report", {"chats": [{"chat": "s1", "project": "proj-a", "state": "idle"}]})["chats"][0]["chat"]
    got = relay.filter_in("report", {
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


def test_who_passes_other_machines_chat_ids_as_opaque_ids_only():
    relay, _ = make_relay()
    got = relay.filter_in("who", {"chats": [
        {"chat": "box-2:abc", "person": "bob", "project": "proj-a", "name": "t\x1b[1m",
         "state": "busy", "cwd": "/home/bob", "tool": "codex"},
        {"chat": "../x", "person": "bob"}]})
    assert got == {"chats": [{"chat": "box-2:abc", "person": "bob", "project": "proj-a",
                              "name": "t", "state": "busy"}]}


def test_an_answer_of_the_wrong_shape_is_refused():
    relay, _ = make_relay()
    for call, ans in (("report", []), ("fetch_file", {"name": "a.txt"}), ("inbox", {"messages": 5}),
                      ("fetch_file", {"name": "../a", "content": "x"})):
        with pytest.raises(R.Bad):
            relay.filter_in(call, ans)


# ---------------------------------------------------------------- the caller check

def test_peer_uid_is_the_kernel_s_answer():
    a, b = socket.socketpair(socket.AF_UNIX)
    try:
        assert R.peer_uid(a) == R.peer_uid(b) == os.getuid()
    finally:
        a.close()
        b.close()


def talk(relay, line: bytes, want_uid):
    a, b = socket.socketpair(socket.AF_UNIX)
    t = threading.Thread(target=relay.serve_conn, args=(b, "alice", want_uid))
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
    got = relay.call("send", {**caller(), "to": "bob", "text": "hi", "secret_path": "/home/alice"})
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
        got = relay.call("send", {**caller(), "to": "bob", "text": "CANARY-SEND"})
        assert got["ok"] is False and "certificate" in got["error"]
        assert fake.requests == []
        assert any("not the pinned one" in l for l in logs)
    finally:
        fake.stop()


def test_plain_http_gets_no_request(certs, tmp_path):
    plain = FakeHub(None, None, log_path=str(tmp_path / "log"))
    try:
        relay, _ = make_relay(hub_port=plain.port, pins=[certs["fp"]])
        got = relay.call("send", {**caller(), "to": "bob", "text": "CANARY-SEND"})
        assert got == {"ok": False, "error": "cannot reach the hub"}
        log = (tmp_path / "log").read_bytes() if (tmp_path / "log").exists() else b""
        assert DEVICE.encode() not in log and b"CANARY-SEND" not in log
    finally:
        plain.stop()


def test_nothing_is_sent_when_the_filter_refuses(hub, certs):
    relay, _ = make_relay(hub_port=hub.port, pins=[certs["fp"]])
    got = relay.call("send", {**caller(), "to": "bob", "text": "my key " + DEVICE})
    assert got["ok"] is False
    got = relay.call("update", {"to": "9.9.9"})
    assert got == {"ok": False, "error": "update: unknown call 'update'"}
    assert hub.requests == []


@pytest.mark.parametrize("status,headers,body,want", [
    (302, {"Location": "http://evil/login"}, {"ok": True}, "redirect"),
    (401, {}, {"error": "\x1b]52;c;ZXZpbA==\x07sign in at http://evil\nsecond line"},
     "the hub refused (401): sign in at http://evil"),
    (500, {"Content-Type": "text/html"}, "<html>oops</html>", "the hub refused (500): <html>oops</html>"),
    (200, {"Content-Type": "text/html"}, "<html>ok</html>", "not JSON"),
    (200, {}, "x" * (R.MAX_ANSWER + 10), "too large"),
    (200, {}, "{not json", "expected shape"),
    (200, {}, {"messages": "all of them"}, "expected shape"),
])
def test_hub_answers_outside_the_table_become_one_plain_line(hub, certs, status, headers, body, want):
    relay, _ = make_relay(hub_port=hub.port, pins=[certs["fp"]])
    hub.answers["inbox"] = (status, headers, body)
    got = relay.call("inbox", caller())
    assert got["ok"] is False and want in got["error"], got
    assert "\n" not in got["error"] and "\x1b" not in got["error"]


def test_sign_in_requests_and_tool_changes_in_an_answer_are_dropped(hub, certs):
    relay, _ = make_relay(hub_port=hub.port, pins=[certs["fp"]])
    hub.answers["notes_write"] = (200, {}, {
        "ok": True, "reply": "saved", "auth_url": "http://evil/login", "elicitation": {"ask": "pw"},
        "tools": [{"name": "run_shell"}], "redirect": "http://evil"})
    assert relay.call("notes_write", {**caller(), "topic": "t", "text": "x"}) == \
        {"ok": True, "result": {"ok": True, "reply": "saved"}}


def test_fetch_file_brings_in_only_name_size_and_a_checked_content(hub, certs):
    relay, _ = make_relay(hub_port=hub.port, pins=[certs["fp"]])
    hub.answers["fetch_file"] = (200, {}, {"name": "a.txt", "size": 2, "content": "hi",
                                           "path": "/home/alice/.ssh/id_ed25519"})
    assert relay.call("fetch_file", {**caller(), "id": 3}) == \
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
