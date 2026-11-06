import pytest

from conftest import start_hub  # noqa: F401
from helpers import call, call_error, person, run
from kaptyorka import http, paths
from kaptyorka.store import MAX_FILE, KaptyorkaError

ADMIN = "T"


def ident(key, project="p"):
    return {"key": key, "machine": "pc", "tool": "claude-code", "project": project}


def upload(hub, token, name, data):
    return http.request_raw("POST", hub.url + "/api/files", token, data,
                            {"X-File-Name": name})


def setup(hub):
    """ann (member) with a session, bob (member) with a session; ann may message bob."""
    ann, ann_t = person(hub.store, "ann", "admin")  # an admin sender needs no door
    bob, bob_t = person(hub.store, "bob")
    ia, ib = ident("claude:a"), ident("claude:b")
    for i, t in ((ia, ann_t), (ib, bob_t)):
        hub.token = t
        run(__import__("helpers").call_result(hub, i, "who"))
    hub.token = ADMIN
    return ann, ann_t, ia, bob, bob_t, ib


def test_upload_limit_exact_and_over(hub, home):
    pid, tok = person(hub.store, "ann")
    status, body, _ = upload(hub, tok, "a.png", b"x" * MAX_FILE)
    assert status == 200 and b'"size":5000000' in body.replace(b" ", b"")
    with pytest.raises(KaptyorkaError) as e:  # over HTTP the hub answers before the body is sent
        hub.store.file_add("a.png", b"x" * (MAX_FILE + 1), pid)
    assert str(e.value) == "file too large: 5000001 bytes, limit 5000000"
    assert MAX_FILE == 5_000_000


def test_traversal_name_stored_by_id(hub, home):
    pid, tok = person(hub.store, "ann")
    fid = hub.store.file_add("../../etc/evil.txt", b"hi", pid)
    assert (paths.files_dir() / str(fid)).read_bytes() == b"hi"
    assert not (home / "etc").exists()
    assert oct(paths.files_dir().stat().st_mode & 0o777) == "0o700"


def test_bad_extension_refused(hub, home):
    pid, tok = person(hub.store, "ann")
    with pytest.raises(KaptyorkaError, match=r"file type \.exe not allowed"):
        hub.store.file_add("a.exe", b"x", pid)
    with pytest.raises(KaptyorkaError, match=r"file type \. not allowed"):
        hub.store.file_add("noext", b"x", pid)
    assert hub.store.file_add("A.PNG", b"x", pid)
    status, body, _ = upload(hub, tok, "a.sh", b"x")
    assert status == 400 and b"not allowed" in body


def test_file_visible_to_sender_receiver_admin_only(hub, home):
    ann, ann_t, ia, bob, bob_t, ib = setup(hub)
    cat, cat_t = person(hub.store, "cat")
    fid = hub.store.file_add("n.txt", b"secret", ann)
    hub.token = ann_t
    hub.store.send("claude:a", "claude:b" if False else hub.store.agent("claude:b").name, "see", fid)
    for tok, ok in ((ann_t, True), (bob_t, True), (ADMIN, True), (cat_t, False)):
        status, body, ctype = http.request_raw("GET", f"{hub.url}/api/files/{fid}", tok)
        assert (status, body) == ((200, b"secret") if ok else (404, b"no such file")), tok
    status, _, ctype = http.request_raw("GET", f"{hub.url}/api/files/999", cat_t)
    assert status == 404
    assert ctype.startswith("text/plain")
    # not yours / already attached
    with pytest.raises(KaptyorkaError, match="not yours"):
        hub.store.send("claude:b", hub.store.agent("claude:a").name, "x", fid)
    with pytest.raises(KaptyorkaError, match="not yours"):
        hub.store.send("claude:a", hub.store.agent("claude:b").name, "x", fid)


def test_download_headers(hub, home):
    pid, tok = person(hub.store, "ann", "admin")
    fid = hub.store.file_add('we"ird name.json', b"{}", pid)
    import urllib.request
    req = urllib.request.Request(f"{hub.url}/api/files/{fid}",
                                 headers={"Authorization": f"Bearer {ADMIN}"})
    with urllib.request.urlopen(req) as r:
        assert r.headers["Content-Type"].startswith("application/json")
        assert r.headers["Content-Disposition"] == 'attachment; filename="weirdname.json"'


def test_inbox_carries_file(hub, home):
    ann, ann_t, ia, bob, bob_t, ib = setup(hub)
    status, body, _ = upload(hub, ann_t, "pic.png", b"png")
    assert status == 200
    import json
    fid = json.loads(body)["id"]
    hub.token = ann_t
    bname = hub.store.agent("claude:b").name
    assert call_error(hub, ia, "send", to=bname, text="x", file="/tmp/a.png").endswith("attach a file by passing its local path; the kaptyorka tool uploads it")
    assert run(call(hub, ia, "send", to=bname, text="look", file=f"id:{fid}")) == "sent to 1 session"
    hub.token = bob_t
    rows = run(call(hub, ib, "inbox"))
    assert rows[0]["file"] == {"id": fid, "name": "pic.png", "size": 3}
    info = run(call(hub, ib, "fetch_file", id=fid))
    assert info == {"id": fid, "name": "pic.png", "size": 3, "url": f"/api/files/{fid}"}
    assert run(call(hub, ib, "inbox")) == []
    hub.token = ADMIN
    assert run(call(hub, ident("claude:adm"), "send", to=bname, text="plain")) == "sent to 1 session"
    hub.token = bob_t
    assert run(call(hub, ib, "inbox"))[0]["file"] is None
    cat, cat_t = person(hub.store, "cat")
    hub.token = cat_t
    assert call_error(hub, ident("claude:c"), "fetch_file", id=fid).endswith("no such file")


def test_oversize_content_length_refused_unread(hub, home):
    import socket
    pid, tok = person(hub.store, "ann")
    host, port = hub.url[len("http://"):].split(":")
    with socket.create_connection((host, int(port))) as s:
        s.sendall((f"POST /api/files HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer {tok}\r\n"
                   f"X-File-Name: a.png\r\nContent-Length: 5000001\r\n\r\n").encode())  # no body sent
        s.settimeout(5)
        reply = b""
        # read the whole reply: headers and body may arrive in separate packets
        while True:
            head, sep, body = reply.partition(b"\r\n\r\n")
            if sep and b"content-length:" in head.lower():
                length = int(head.lower().split(b"content-length:")[1].split(b"\r\n")[0])
                if len(body) >= length:
                    break
            chunk = s.recv(4096)
            if not chunk:
                break
            reply += chunk
        reply = reply.decode()
    assert reply.startswith("HTTP/1.1 400") and "file too large: 5000001 bytes" in reply
    assert hub.store.db.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 0


def test_oversize_chunked_stops_at_limit(hub, home):
    import urllib.request
    pid, tok = person(hub.store, "ann")

    def chunks():
        for _ in range(6):
            yield b"x" * 1_000_000
    req = urllib.request.Request(hub.url + "/api/files", data=chunks(), method="POST",
                                 headers={"Authorization": f"Bearer {tok}", "X-File-Name": "a.png",
                                          "Transfer-Encoding": "chunked"})
    import urllib.error
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(req)
    assert e.value.code == 400 and b"file too large" in e.value.read()
    assert hub.store.db.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 0
