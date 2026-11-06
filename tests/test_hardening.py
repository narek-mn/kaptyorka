"""Client strings normalised at hub entry, and small hardening: rate limiter memory, download
headers, page parties on files, the shim's download errors."""
from urllib.parse import quote

from conftest import start_hub
from helpers import call, person, run
from kaptyorka import http, shim
from kaptyorka.auth import RateLimiter
from kaptyorka.store import Store


def test_heartbeat_strings_become_one_line(tmp_path):
    s = Store(tmp_path / "hub.db")
    _, ta = person(s, "alice")
    pa = s.authenticate(ta)
    s.heartbeat("box\n1", [{"key": "k1", "tool": "co\tdex", "project": "proj\n-a\r\nx" + "y" * 200,
                            "label": "l\nb" + "z" * 60, "activity": "a\n\nb" + "w" * 300}],
                ["codex"], principal=pa)
    s.set_status("k1", "doing\nthings" + "v" * 300)
    a = s.agent("k1")
    assert "\n" not in a.name and "\r" not in a.name and "\t" not in a.name
    assert a.machine == "box 1" and a.tool == "co dex" and a.project.startswith("proj -a x")
    assert len(a.project) == 100 and len(a.label) == 40
    assert a.activity.startswith("a b") and len(a.activity) == 200
    assert a.status.startswith("doing things") and len(a.status) == 200
    s.seen("r1", "root@srv", "codex", "p", principal=pa)  # alice's own other session
    s.send("k1", "root@srv/codex/p", "hi")
    [item] = s.offer("root@srv", "", pa)
    head = item["text"].split("\n\n", 1)[0]
    assert head == f"Kaptyorka message from {a.name}:" and "\n" not in head
    s.close()


def test_identity_headers_become_one_line(hub):
    ident = {"key": "claude:n", "machine": quote("pc\nx"), "tool": "claude-code",
             "project": quote("proj\n-a")}
    rows = run(call(hub, ident, "who"))
    me = [r for r in rows if r["you"]][0]
    assert me["machine"] == "pc x" and me["project"] == "proj -a"
    assert me["name"] == "pc x/claude-code/proj -a"


def test_rate_limiter_forgets_expired_keys():
    t = [0.0]
    r = RateLimiter(limit=2, window_s=60, clock=lambda: t[0])
    for i in range(50):
        r.hit(("1.2.3.4", str(i)))
    assert len(r.failures) == 50
    t[0] = 61
    assert not r.blocked(("1.2.3.4", "x"))
    assert r.failures == {}
    r.hit("a")
    t[0] = 200
    r.hit("b")
    assert list(r.failures) == ["b"]


def test_download_nosniff_and_page_party(tmp_path, home):
    s = Store(tmp_path / "hub.db")
    stop = start_hub(s)
    url = f"http://127.0.0.1:{stop.port}"
    try:
        ann, ann_t = person(s, "ann")
        ben, ben_t = person(s, "ben")
        _, cat_t = person(s, "cat")
        s.seen("b1", "pc", "codex", "p", principal=s.authenticate(ben_t))
        fid = s.file_add("n.txt", b"hi", ben)
        with s.lock:  # a message from ben's session to ann's page carrying the file
            s.db.execute("INSERT INTO messages (sender_key, sender_name, to_key, to_name, machine,"
                         " tool, project, text, created, file_id) VALUES ('b1','b','web:ann',"
                         "'ann','','','','x',0,?)", (fid,))
        status, data, _ = http.request_raw("GET", f"{url}/api/files/{fid}", ann_t)
        assert (status, data) == (200, b"hi")
        assert http.request_raw("GET", f"{url}/api/files/{fid}", cat_t)[0] == 404
        import urllib.request
        req = urllib.request.Request(f"{url}/api/files/{fid}",
                                     headers={"Authorization": f"Bearer {ben_t}"})
        with urllib.request.urlopen(req) as r:
            assert r.headers["X-Content-Type-Options"] == "nosniff"
    finally:
        stop()
        s.close()


def test_page_sender_sees_its_file(tmp_path):
    s = Store(tmp_path / "hub.db")
    ann, ann_t = person(s, "ann")
    fid = s.file_add("n.txt", b"hi", ann)
    with s.lock:
        s.db.execute("INSERT INTO messages (sender_key, sender_name, to_key, to_name, machine, tool,"
                     " project, text, created, file_id) VALUES ('web:ann','ann','k','x','m','t',"
                     "'p','x',0,?)", (fid,))
    assert s.file_visible(fid, s.authenticate(ann_t))
    s.close()


def test_shim_download_error_is_the_hubs_answer(monkeypatch):
    monkeypatch.setattr(http, "request_raw", lambda *a, **k: (404, b"no such file", "text/plain"))
    out = shim._download({"hub": "http://h", "token": "t"}, {}, {"url": "/api/files/1", "id": 1,
                                                                "name": "a", "size": 1})
    assert out == "no such file"
