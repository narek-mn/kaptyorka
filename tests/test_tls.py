"""Encrypted connections, in a sandbox: a hub on https with a certificate made by the real
`openssl`, machines pinning its fingerprint (join from an invite link, heartbeat, the MCP
shim's tool call), a browser that installed the hub's CA, a wrong fingerprint refused before
anything is sent, the move announced then followed (fake clock for the signature's age),
plain http in "only" mode, rollback and rotation. Never the real ~/.kaptyorka or hub."""
import json
import shutil
import socket
import ssl
import threading
import time
import urllib.request

import anyio
import pytest

from helpers import person
from kaptyorka import http, join, paths, tls
from kaptyorka.manager import ManagerStore
from kaptyorka.store import Store

pytestmark = pytest.mark.skipif(shutil.which("openssl") is None, reason="openssl not installed")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Hub:
    """The hub's own listeners (tls.servers) in a thread, on two free ports."""

    def __init__(self, store, port):
        from kaptyorka.hub import build_app

        self.store, self.port = store, port
        self.app = build_app(store)
        self.start()

    def start(self):
        self.servers = tls.servers(self.app, self.port, log_level="warning", timeout_graceful_shutdown=1)
        self.thread = threading.Thread(target=tls.run, args=(self.servers,), daemon=True)
        self.thread.start()
        deadline = time.monotonic() + 10
        while not all(s.started for s in self.servers):
            assert self.thread.is_alive() and time.monotonic() < deadline, "hub did not start"
            time.sleep(0.02)

    def stop(self):
        self.servers[0].should_exit = True
        self.thread.join(10)

    def restart(self):
        """As the service does: a new process, so a new app on the same store."""
        from kaptyorka.hub import build_app

        self.stop()
        self.app = build_app(self.store)
        self.start()


@pytest.fixture
def sandbox(home):
    """A hub home in the test's temp folder: hub.json with its port, the certificate for
    127.0.0.1, alice (admin) with a device token, the hub running in mode both (https used)."""
    store = Store(paths.home() / "hub.db")
    ManagerStore(store).set_enabled(False, "test")
    _, token = person(store, "alice", "admin")
    port, tport = free_port(), free_port()
    (paths.home() / "hub.json").write_text(json.dumps({"port": port}))
    fp = tls.make_certificate(["localhost"], ["127.0.0.1"])
    tls.save_state({"mode": "both", "use": "https", "port": tport, "since": time.time()})
    hub = Hub(store, port)
    yield type("S", (), {"store": store, "token": token, "fp": fp, "port": port, "tport": tport,
                         "plain": f"http://127.0.0.1:{port}", "https": f"https://127.0.0.1:{tport}",
                         "hub": hub})
    hub.stop()
    store.close()


def test_the_certificate_is_signed_by_the_hub_ca_and_names_its_addresses(sandbox):
    """What a browser that installed the hub's CA checks: the chain and the address."""
    ctx = ssl.create_default_context(cafile=str(tls.tls_dir() / "ca.crt"))  # hostname checked too
    with urllib.request.urlopen(sandbox.https + "/", context=ctx, timeout=10) as r:
        assert r.status == 200 and b"<html" in r.read().lower()
    assert len(sandbox.fp) == 64 and tls.cert_fingerprint(tls.files()[0]) == sandbox.fp
    end = tls.expires(tls.files()[0])
    assert 800 * 86400 < end - time.time() <= tls.LEAF_DAYS * 86400 + 60
    for f in ("ca.key", "hub.key"):
        assert (tls.tls_dir() / f).stat().st_mode & 0o077 == 0


def test_the_certificate_passes_strict_verification(sandbox):
    """Python 3.13 turns VERIFY_X509_STRICT on by default (it wants the key identifiers);
    set it here so every Python version runs the check."""
    ctx = ssl.create_default_context(cafile=str(tls.tls_dir() / "ca.crt"))
    ctx.verify_flags |= ssl.VERIFY_X509_STRICT
    with urllib.request.urlopen(sandbox.https + "/", context=ctx, timeout=10) as r:
        assert r.status == 200


def test_pinned_calls_work_and_a_wrong_fingerprint_is_refused(sandbox):
    assert http.request("GET", sandbox.https + "/api/me", sandbox.token, pins=[sandbox.fp])[0] == 200
    with pytest.raises(http.HubCertMismatch) as e:
        http.request("GET", sandbox.https + "/api/me", sandbox.token, pins=["0" * 64])
    assert "is not the one this machine joined with" in str(e.value)
    with pytest.raises(http.HubUnreachable):  # no pin: checked against the system's CAs, refused
        http.request("GET", sandbox.https + "/api/me", sandbox.token, pins=[])
    # the config's pins are used for its own hub
    paths.save_config({"hub": sandbox.https, "token": sandbox.token, "hub_pins": [sandbox.fp]})
    assert http.request("GET", sandbox.https + "/api/me", sandbox.token)[0] == 200


def test_join_from_an_https_invite_pins_the_link_s_fingerprint(sandbox):
    pid = sandbox.store.db.execute("SELECT id FROM people WHERE name='alice'").fetchone()["id"]
    status, inv = http.request("POST", sandbox.https + "/api/invites", sandbox.token,
                               {"person": "alice"}, pins=[sandbox.fp])
    assert status == 200 and inv["url"] == f"{sandbox.https}/i/{inv['code']}#sha256={sandbox.fp}"
    # a link whose fingerprint was changed: refused before the invite is sent, so not spent
    bad = inv["url"].replace(sandbox.fp, "f" * 64)
    assert join.redeem(bad, "box-1") is None
    hub, token, secret, _ = join.redeem(inv["url"], "box-1")
    assert hub == sandbox.https and secret
    assert http.request("GET", hub + "/api/me", token, pins=[sandbox.fp])[0] == 200
    # an old plain-http link still parses and works the same
    code = sandbox.store.invite_create(pid)
    assert join.redeem(f"{sandbox.plain}/i/{code}", "box-2")[0] == sandbox.plain


def _bridge(sandbox, url):
    """A machine joined over plain http before the move: its token and signing secret."""
    pid = sandbox.store.db.execute("SELECT id FROM people WHERE name='alice'").fetchone()["id"]
    code = sandbox.store.invite_create(pid)
    hub, token, secret, _ = join.redeem(f"{url}/i/{code}", "box-1")
    cfg = {"hub": hub, "token": token, "secret": secret, "machine": "u@box-1"}
    paths.save_config(cfg)
    return cfg


def beat(cfg, pins=None):
    return http.request("POST", cfg["hub"] + "/bridge/heartbeat", cfg["token"], {
        "machine": "u@box-1", "sessions": [], "tools": [], "bridge": True, "folders": {},
        "facts": {"host": "box-1", "os_user": "u", **tls.machine_facts(cfg)}}, pins=pins)


def test_announce_then_follow_then_only_then_rollback(sandbox):
    cfg = _bridge(sandbox, sandbox.plain)
    status, answer = beat(cfg)
    a = answer["tls"]
    assert status == 200 and a["use"] == "https" and a["url"] == sandbox.https and a["pins"] == [sandbox.fp]
    assert a["plain"] == sandbox.plain
    # the signature: forged, or replayed later, is not followed
    assert tls.check({**a, "url": "https://10.9.9.9:1"}, cfg["secret"], time.time()) == "bad signature"
    assert tls.check(a, cfg["secret"], a["issued"] + tls.ANNOUNCE_MAX_AGE_S + 1) == "too old"
    assert tls.check(a, "another secret", time.time()) == "bad signature"
    logs = []
    cfg = tls.follow(cfg, answer, logs.append)
    assert cfg["hub"] == sandbox.https and cfg["hub_plain"] == sandbox.plain and cfg["hub_pins"] == [sandbox.fp]
    assert paths.load_config()["hub"] == sandbox.https  # every other client reads it there
    assert beat(cfg)[0] == 200  # over https, pinned from the config
    # everyone moved: only. Plain http still serves this computer (the manager) itself...
    assert tls.admin("only", sandbox.store, log=logs.append) == 0
    assert http.request("GET", sandbox.plain + "/api/me", sandbox.token)[0] == 200
    # ...and anything else is told it moved; a browser's plain GET goes to https
    assert _asgi(sandbox, "POST", "/bridge/heartbeat", auth=True) == (403, None)
    assert _asgi(sandbox, "GET", "/settings", auth=False) == (301, f"https://10.0.0.5:{sandbox.tport}/settings")
    # rollback: machines move back to plain http, then off
    assert tls.admin("off", sandbox.store, log=logs.append) == 0
    assert tls.state()["use"] == "http"
    status, answer = beat(cfg)
    cfg = tls.follow(cfg, answer, logs.append)
    assert cfg["hub"] == sandbox.plain
    assert beat(cfg)[0] == 200  # reports itself on http now
    assert tls.admin("off", sandbox.store, log=logs.append) == 0
    assert tls.state() == {"mode": "off"} and "tls" not in beat(cfg)[1]


def test_off_waits_for_machines_still_on_https(sandbox):
    cfg = _bridge(sandbox, sandbox.plain)
    cfg = tls.follow(cfg, beat(cfg)[1], lambda m: None)
    beat(cfg)  # it reports https
    out = []
    assert tls.admin("off", sandbox.store, log=out.append) == 0  # back to http first
    assert tls.admin("off", sandbox.store, log=out.append) == 1 and "still on https: u@box-1" in out[-1]
    assert tls.admin("off", sandbox.store, force=True, log=out.append) == 0


def test_only_waits_for_machines_still_on_http(sandbox):
    cfg = _bridge(sandbox, sandbox.plain)
    beat(cfg)
    out = []
    assert tls.admin("only", sandbox.store, log=out.append) == 1 and "still on plain http: u@box-1" in out[-1]
    assert tls.state()["mode"] == "both"


def _asgi(sandbox, method, path, auth):
    """One request to the plain http listener as if from another computer (10.0.0.5)."""
    got = {}

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(m):
        if m["type"] == "http.response.start":
            got["status"] = m["status"]
            got["location"] = dict(m["headers"]).get(b"location")

    headers = [(b"host", f"10.0.0.5:{sandbox.port}".encode())]
    if auth:
        headers.append((b"authorization", b"Bearer x"))
    scope = {"type": "http", "method": method, "path": path, "query_string": b"", "headers": headers,
             "client": ("10.0.0.5", 5555)}
    anyio.run(tls.http_guard(sandbox.hub.app), scope, receive, send)
    loc = got.get("location")
    return got["status"], loc.decode() if loc else None


def test_the_shim_s_tool_call_over_https_trusts_only_the_pinned_certificate(sandbox):
    from kaptyorka import shim

    paths.save_config({"hub": sandbox.https, "token": sandbox.token, "hub_pins": [sandbox.fp]})

    async def call(url):
        async with anyio.create_task_group() as tg:
            relay = shim.Relay(url, {"Authorization": f"Bearer {sandbox.token}"}, tg)
            r = await relay.use(lambda c: c.list_tools())
            await relay.drop(relay.client)
            return r
    tools = anyio.run(call, sandbox.https)
    assert tools is not None and any(t.name == "who" for t in tools.tools)
    # the bridge moved the config while the shim ran: it follows on its next connect
    paths.save_config({"hub": sandbox.https, "hub_plain": "http://10.9.9.9:1", "token": sandbox.token,
                       "hub_pins": [sandbox.fp]})
    assert tls.current_url("http://10.9.9.9:1") == sandbox.https
    paths.save_config({"hub": sandbox.https, "token": sandbox.token, "hub_pins": ["0" * 64]})
    assert anyio.run(call, sandbox.https) is None  # a certificate it never pinned: no call


def test_rotation_announces_first_then_moves(sandbox):
    cfg = _bridge(sandbox, sandbox.plain)
    cfg = tls.follow(cfg, beat(cfg)[1], lambda m: None)
    out = []
    assert tls.admin("rotate", sandbox.store, log=out.append) == 0
    nxt = tls.cert_fingerprint(tls.files(True)[0])
    assert nxt and nxt != sandbox.fp
    beat(cfg)  # reports its old pins
    assert tls.admin("rotate", sandbox.store, log=out.append) == 1 and "not pinned yet" in out[-1]
    status, answer = beat(cfg)
    assert answer["tls"]["pins"] == [sandbox.fp, nxt]
    cfg = tls.follow(cfg, answer, out.append)
    beat(cfg)  # now with both
    assert tls.admin("rotate", sandbox.store, restart=sandbox.hub.restart, log=out.append) == 0
    assert tls.cert_fingerprint(tls.files()[0]) == nxt and not tls.files(True)[0].exists()
    assert beat(cfg)[0] == 200  # the new certificate, already pinned
    with pytest.raises(http.HubCertMismatch):  # the old one is no longer what the hub shows
        http.request("GET", sandbox.https + "/api/me", sandbox.token, pins=[sandbox.fp])


def test_links_move_to_https_with_the_fingerprint(sandbox):
    assert tls.base_url("http", "10.0.0.5:8765") == f"https://10.0.0.5:{sandbox.tport}"
    assert tls.base_url("https", f"10.0.0.5:{sandbox.tport}") == f"https://10.0.0.5:{sandbox.tport}"
    assert tls.invite_url("https://h:1", "abc") == f"https://h:1/i/abc#sha256={sandbox.fp}"
    assert tls.invite_url("http://h:1", "abc") == "http://h:1/i/abc"
    assert tls.split_invite(f"https://h:1/i/abc#sha256={sandbox.fp.upper()}") == ("https://h:1/i/abc", sandbox.fp)
    tls.save_state(None)
    assert tls.base_url("http", "10.0.0.5:8765") == "http://10.0.0.5:8765"


def test_the_admins_hear_once_before_the_certificate_ends(sandbox):
    end = tls.expires(tls.files()[0])
    assert tls.expiry_news(sandbox.store, end - 40 * 86400) == []
    told = tls.expiry_news(sandbox.store, end - 10 * 86400)
    assert len(told) == 1 and "ends in 10 days" in told[0] and "kaptyorka tls rotate" in told[0]
    assert tls.expiry_news(sandbox.store, end - 9 * 86400) == []  # once per certificate


def test_off_is_the_default_and_says_nothing(home):
    assert tls.state() == {"mode": "off"}
    assert tls.announcement("key", "10.0.0.5:8765", time.time()) is None
    assert tls.base_url("http", "10.0.0.5:8765") == "http://10.0.0.5:8765"


def test_a_real_bridge_moves_on_its_heartbeat_and_reports_it(sandbox, monkeypatch):
    from kaptyorka import identity
    from kaptyorka.bridge import Bridge

    monkeypatch.setattr(identity, "facts", lambda: {"host": "box-1", "os_user": "u", "version": "0.9.35"})
    cfg = _bridge(sandbox, sandbox.plain)
    lines = []
    b = Bridge(cfg, [], lines.append)
    b.heartbeat()
    assert b.cfg["hub"] == sandbox.https and paths.load_config()["hub"] == sandbox.https
    assert any("encrypted connections" in line for line in lines)
    b._facts_at = None
    b.heartbeat()  # now over https, pinned; its facts say so
    out = []
    tls.admin("status", sandbox.store, log=out.append)
    assert any(line.endswith("@box-1: https, pinned") for line in out), out
