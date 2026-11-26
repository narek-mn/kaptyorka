"""Encrypted connections to the hub, optional (docs/superpowers/specs/2026-11-25-hub-tls.md).

The hub makes its own small certificate authority and a server certificate signed by it
(`openssl`, on the hub machine only). Machines never trust a CA: they pin the server
certificate's SHA-256 fingerprint, which reaches them inside the invite link
(`https://<hub>:<port>/i/<code>#sha256=<hex>`) or, for a machine already joined, in its
heartbeat answer signed with its own signing secret. Browsers either accept the certificate
once or install the hub's CA (`kaptyorka tls ca`).

The hub's mode, in hub.json "tls" (plain http stays the default):
- off: http only.
- both: http on the usual port and https on the TLS port; the heartbeat answer tells each
  bridge which to use ("use"), and a bridge moves only after a pinned call to the other works.
- only: https; the http port still serves this computer itself (127.0.0.1: the manager), and
  answers anything else with "moved" (a browser's plain GET is sent to https). A machine that
  was off during the move needs a new invite link: a fingerprint learned over plain http could
  be an attacker's.

Every hub connection goes through http.py (pinned there) except the MCP shim's, which gets a
context from `client_context` that trusts exactly the pinned certificate."""
import hashlib
import hmac
import json
import os
import re
import socket
import ssl
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Callable, List, Optional, Tuple
from urllib.parse import urlsplit

from kaptyorka import paths

LEAF_DAYS = 825  # Apple's limit for a TLS server certificate, also one trusted by hand
CA_DAYS = 3650
WARN_DAYS = 30  # the admin hears it this long before the certificate ends
ANNOUNCE_MAX_AGE_S = 600
FP = re.compile(r"^[0-9a-f]{64}$")
MOVED = ("this hub moved to encrypted connections: {url}. This machine missed the move; get a"
         " new invite link from your admin and run `kaptyorka join <link>` (the link carries the"
         " hub's certificate fingerprint).")


# --- files and state (the hub machine) --------------------------------------------------

def tls_dir() -> Path:
    d = paths.home() / "tls"
    d.mkdir(mode=0o700, exist_ok=True)
    return d


def files(next_: bool = False) -> Tuple[Path, Path]:
    d = tls_dir()
    return (d / "next.crt", d / "next.key") if next_ else (d / "hub.crt", d / "hub.key")


def _hub_json() -> dict:
    try:
        got = json.loads((paths.home() / "hub.json").read_text())
    except (OSError, ValueError):
        return {}
    return got if isinstance(got, dict) else {}


def state() -> dict:
    """{"mode": off|both|only, "use": https|http, "port": the TLS port, "since": time}."""
    st = _hub_json().get("tls")
    if not isinstance(st, dict) or st.get("mode") not in ("both", "only"):
        return {"mode": "off"}
    return st


def save_state(st: Optional[dict]) -> None:
    saved = _hub_json()
    if st is None or st.get("mode") == "off":
        saved.pop("tls", None)
    else:
        saved["tls"] = st
    (paths.home() / "hub.json").write_text(json.dumps(saved))


def fingerprint(der: bytes) -> str:
    return hashlib.sha256(der).hexdigest()


def cert_fingerprint(path: Path) -> Optional[str]:
    try:
        return fingerprint(ssl.PEM_cert_to_DER_cert(path.read_text()))
    except (OSError, ValueError):
        return None


def _openssl(args: List[str], run: Callable) -> None:
    try:
        r = run(["openssl", *args], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as e:
        raise RuntimeError(f"openssl did not run ({e}); install openssl on the hub machine") from e
    if r.returncode != 0:
        raise RuntimeError(f"openssl {args[0]} failed: {(r.stderr or r.stdout).strip()[:300]}")


def make_certificate(names: List[str], ips: List[str], next_: bool = False,
                     run: Callable = subprocess.run) -> str:
    """The hub's CA (once, kept) and a server certificate signed by it for these names and
    addresses, valid LEAF_DAYS; its fingerprint. `next_`: the one `rotate` moves to."""
    d = tls_dir()
    ca, ca_key = d / "ca.crt", d / "ca.key"
    if not (ca.exists() and ca_key.exists()):
        _openssl(["req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(ca_key),
                  "-out", str(ca), "-days", str(CA_DAYS), "-subj",
                  f"/CN=kaptyorka hub CA {socket.gethostname()[:40]}",
                  "-addext", "basicConstraints=critical,CA:TRUE",
                  "-addext", "keyUsage=critical,keyCertSign,cRLSign",
                  "-addext", "subjectKeyIdentifier=hash"], run)
        os.chmod(ca_key, 0o600)
    crt, key = files(next_)
    alt = ",".join([f"DNS:{n}" for n in dict.fromkeys(names) if n]
                   + [f"IP:{i}" for i in dict.fromkeys(ips) if i])
    with tempfile.TemporaryDirectory(dir=d) as tmp:
        csr, ext = Path(tmp) / "hub.csr", Path(tmp) / "ext.cnf"
        ext.write_text(f"subjectAltName={alt}\nbasicConstraints=critical,CA:FALSE\n"
                       "keyUsage=critical,digitalSignature,keyEncipherment\n"
                       "extendedKeyUsage=serverAuth\nsubjectKeyIdentifier=hash\n"
                       "authorityKeyIdentifier=keyid:always\n")
        _openssl(["req", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key), "-out", str(csr),
                  "-subj", f"/CN={(names or ['kaptyorka-hub'])[0][:60]}"], run)
        os.chmod(key, 0o600)
        _openssl(["x509", "-req", "-in", str(csr), "-CA", str(ca), "-CAkey", str(ca_key),
                  "-CAcreateserial", "-days", str(LEAF_DAYS), "-sha256", "-out", str(crt),
                  "-extfile", str(ext)], run)
    return cert_fingerprint(crt) or ""


def expires(path: Path, run: Callable = subprocess.run) -> Optional[float]:
    """The certificate's notAfter as a time, or None."""
    try:
        r = run(["openssl", "x509", "-enddate", "-noout", "-in", str(path)],
                capture_output=True, text=True, timeout=10)
        return ssl.cert_time_to_seconds(r.stdout.strip().split("=", 1)[1])
    except (OSError, subprocess.SubprocessError, IndexError, ValueError):
        return None


def default_names() -> Tuple[List[str], List[str]]:
    """What the server certificate names: this computer's name, localhost, its addresses."""
    host = socket.gethostname()
    names = [host, host.split(".")[0], "localhost"]
    ips = ["127.0.0.1"]
    try:
        ips.append(paths.outbound_ip())
    except Exception:
        pass
    saved = _hub_json().get("host")
    if saved:
        (ips if re.fullmatch(r"[\d.]+|[0-9a-fA-F:]+", saved) else names).append(saved)
    return names, ips


# --- what the hub says ------------------------------------------------------------------

def _with_port(host: str, port: int) -> str:
    """"10.0.0.5:8765" -> "10.0.0.5:<port>" (IPv6 in brackets kept)."""
    if host.startswith("[") and "]" in host:
        h = host[:host.index("]") + 1]
    elif host.count(":") == 1:
        h = host.split(":", 1)[0]
    else:
        h = host  # no port given (or a bare IPv6 address)
    return f"{h}:{port}"


def base_url(scheme: str, host: str) -> str:
    """The hub's address for links it hands out: https on the TLS port once it uses https,
    also when the request came over plain http."""
    st = state()
    if st["mode"] != "off" and st.get("use") == "https":
        return f"https://{_with_port(host, int(st['port']))}" if scheme == "http" else f"https://{host}"
    return f"{scheme}://{host}"


def invite_url(base: str, code: str) -> str:
    """An invite link; over https it carries the fingerprint: the machine pins it before it
    sends anything (old http links stay as they were)."""
    fp = cert_fingerprint(files()[0]) if base.startswith("https://") else None
    return f"{base}/i/{code}" + (f"#sha256={fp}" if fp else "")


_fp_cache: dict = {}


def _pins_now() -> List[str]:
    out = []
    for nxt in (False, True):
        p = files(nxt)[0]
        try:
            m = p.stat().st_mtime
        except OSError:
            continue
        hit = _fp_cache.get(p)
        if hit is None or hit[0] != m:
            _fp_cache[p] = hit = (m, cert_fingerprint(p))
        if hit[1]:
            out.append(hit[1])
    return out


def _mac(body: dict, key: str) -> str:
    raw = json.dumps(body, sort_keys=True, separators=(",", ":"))
    return hmac.new(key.encode(), raw.encode(), hashlib.sha256).hexdigest()


def announcement(signing_key: Optional[str], host: str, now: float) -> Optional[dict]:
    """The heartbeat answer's "tls": where to connect and which certificates to accept, signed
    with the device's signing key (its secret's hash), so an announcement over plain http
    cannot be forged by someone between without that secret. None while off, or for a
    device without a secret (a legacy shared token: it needs a new invite)."""
    st = state()
    if st["mode"] == "off" or not signing_key:
        return None
    pins = _pins_now()
    if not pins:
        return None
    plain_port = _hub_json().get("port", 8765)
    body = {"use": st.get("use", "https"), "url": f"https://{_with_port(host, int(st['port']))}",
            "plain": f"http://{_with_port(host, int(plain_port))}", "pins": pins, "issued": now}
    return {**body, "sig": _mac(body, signing_key)}


# --- what a machine does ----------------------------------------------------------------

def _signing(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()  # orders.signing_key


def check(a, secret: Optional[str], now: float) -> Optional[str]:
    """Why an announcement is not to be followed, or None."""
    if not isinstance(a, dict) or not secret:
        return "no announcement or no signing secret"
    body = {k: v for k, v in a.items() if k != "sig"}
    if not hmac.compare_digest(str(a.get("sig", "")).encode(), _mac(body, _signing(secret)).encode()):
        return "bad signature"
    issued = a.get("issued")
    if isinstance(issued, bool) or not isinstance(issued, (int, float)) or abs(now - issued) > ANNOUNCE_MAX_AGE_S:
        return "too old"
    pins = a.get("pins")
    if not isinstance(pins, list) or not pins or not all(isinstance(p, str) and FP.match(p) for p in pins):
        return "bad pins"
    for k in ("url", "plain"):
        if not isinstance(a.get(k), str):
            return "bad address"
    if not a["url"].startswith("https://") or not a["plain"].startswith("http://") or a.get("use") not in ("https", "http"):
        return "bad address"
    return None


def follow(cfg: dict, answer, log: Callable[[str], None] = print,
           now: Optional[float] = None, probe: Optional[Callable] = None) -> dict:
    """The bridge, after each heartbeat: keep the pins the hub announced, and move this
    machine's hub address to https (or back) once a call there works. Returns the config,
    saved when it changed. Any other client reads the address from the config."""
    from kaptyorka import http

    a = answer.get("tls") if isinstance(answer, dict) else None
    if a is None or not cfg.get("hub"):
        return cfg
    why = check(a, cfg.get("secret"), time.time() if now is None else now)
    if why:
        log(f"tls: ignored the hub's announcement: {why}")
        return cfg

    def works(url: str, pins: List[str]) -> bool:
        if probe is not None:
            return probe(url, pins)
        try:
            return http.request("GET", url + "/api/me", cfg["token"], timeout=10, pins=pins)[0] == 200
        except http.HubUnreachable:
            return False
    new = dict(cfg)
    new["hub_pins"] = a["pins"]
    cur = cfg["hub"].rstrip("/")
    if a["use"] == "https" and cur.startswith("http://"):
        if works(a["url"], a["pins"]):
            new["hub_plain"], new["hub"] = cur, a["url"]
            log(f"tls: the hub now uses encrypted connections: {a['url']}")
        else:
            log(f"tls: the hub announced {a['url']}, which did not answer yet; staying on {cur}")
    elif a["use"] == "http" and cur.startswith("https://"):
        plain = cfg.get("hub_plain") or a["plain"]
        if works(plain, []):
            new["hub"] = plain
            log(f"tls: the hub went back to plain http: {plain}")
    if new != cfg:  # onto the file as it is now: another command may have written it meanwhile
        disk = paths.load_config()
        if disk.get("token") == cfg.get("token"):
            paths.save_config({**disk, **{k: new[k] for k in ("hub", "hub_pins", "hub_plain") if k in new}})
    return new


def pins_for(url: str, cfg: Optional[dict] = None) -> List[str]:
    """The fingerprints this machine accepts for `url`: the config's, when url is its hub."""
    cfg = paths.load_config() if cfg is None else cfg
    pins = cfg.get("hub_pins") or []
    hub = cfg.get("hub") or ""
    if not url.startswith("https://") or not pins:
        return []
    if urlsplit(url).netloc != urlsplit(hub).netloc:
        return []
    return [p for p in pins if isinstance(p, str) and FP.match(p)]


def peer_der(url: str, timeout: float = 10) -> bytes:
    """The certificate the server at url presents (not verified here: the caller pins it)."""
    u = urlsplit(url)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE  # verified by its fingerprint, which the caller compares
    with socket.create_connection((u.hostname, u.port or 443), timeout=timeout) as raw:
        with ctx.wrap_socket(raw, server_hostname=u.hostname) as s:
            return s.getpeercert(binary_form=True)


def client_context(url: str, pins: List[str]) -> ssl.SSLContext:
    """A context that trusts exactly the server's pinned certificate (the MCP shim's httpx
    client has no hook after the handshake): read it, check its fingerprint, then trust that
    certificate alone. A swapped certificate fails the second handshake."""
    der = peer_der(url)
    if fingerprint(der) not in pins:
        raise ssl.SSLError(f"the hub's certificate {fingerprint(der)[:16]}... is not the one this"
                           " machine joined with")
    ctx = ssl.create_default_context(cadata=ssl.DER_cert_to_PEM_cert(der))
    ctx.check_hostname = False  # the pin, not the name: a LAN address can change
    if hasattr(ssl, "VERIFY_X509_STRICT"):
        ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT
    if hasattr(ssl, "VERIFY_X509_PARTIAL_CHAIN"):
        ctx.verify_flags |= ssl.VERIFY_X509_PARTIAL_CHAIN  # a leaf trusted as its own anchor
    return ctx


def current_url(url: str) -> str:
    """A long-running client's hub address, followed when the bridge moved the config."""
    cfg = paths.load_config()
    hub = (cfg.get("hub") or "").rstrip("/")
    if hub and url.rstrip("/") in (hub, (cfg.get("hub_plain") or "").rstrip("/")):
        return hub
    return url


def split_invite(link: str) -> Tuple[str, Optional[str]]:
    """(link without its fragment, the sha256 fingerprint it carries or None)."""
    base, _, frag = link.strip().partition("#")
    m = re.fullmatch(r"sha256=([0-9a-fA-F]{64})", frag)
    return base, (m.group(1).lower() if m else None)


def machine_facts(cfg: dict) -> dict:
    """What a machine says about its connection: the scheme it uses and its pins (short)."""
    hub = cfg.get("hub") or ""
    return {"hub_tls": {"scheme": "https" if hub.startswith("https://") else "http",
                        "pins": [p[:16] for p in (cfg.get("hub_pins") or []) if isinstance(p, str)][:2]}}


# --- the hub's listeners ----------------------------------------------------------------

def _loopback(scope) -> bool:
    client = scope.get("client") or ("", 0)
    return client[0] in ("127.0.0.1", "::1", "localhost") or str(client[0]).startswith("127.")


def http_guard(app):
    """The plain http listener: everything while the mode is off or both; in "only" this
    computer itself (the manager) still, a browser's plain GET is sent to https, and anything
    else is told it moved (it already sent its token in clear: that is why bridges move first)."""
    async def guarded(scope, receive, send):
        st = state()
        if scope["type"] not in ("http", "websocket") or st["mode"] != "only" or _loopback(scope):
            return await app(scope, receive, send)
        headers = dict(scope.get("headers") or [])
        host = headers.get(b"host", b"").decode("latin-1") or "localhost"
        target = f"https://{_with_port(host, int(st['port']))}"
        path = scope.get("path", "/")
        if scope["type"] == "http" and scope.get("method") in ("GET", "HEAD") \
                and b"authorization" not in headers \
                and not path.startswith(("/api", "/bridge", "/mcp", "/i/")):
            qs = scope.get("query_string", b"").decode("latin-1")
            loc = target + path + (f"?{qs}" if qs else "")
            await send({"type": "http.response.start", "status": 301,
                        "headers": [(b"location", loc.encode("latin-1")), (b"content-length", b"0")]})
            await send({"type": "http.response.body", "body": b""})
            return
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        body = MOVED.format(url=target).encode()
        await send({"type": "http.response.start", "status": 403,
                    "headers": [(b"content-type", b"text/plain; charset=utf-8"),
                                (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})
    return guarded


def servers(app, port: int, **kw) -> list:
    """The uvicorn servers for this mode: plain http always (the lifespan runs there once),
    https on the TLS port while the mode is both or only."""
    import uvicorn

    st = state()
    out = [uvicorn.Server(uvicorn.Config(http_guard(app), host="0.0.0.0", port=port, **kw))]
    if st["mode"] != "off":
        import contextlib

        class Second(uvicorn.Server):
            """A second listener in the same process: the first one handles the signals."""
            def capture_signals(self):
                return contextlib.nullcontext()
        crt, key = files()
        out.append(Second(uvicorn.Config(app, host="0.0.0.0", port=int(st["port"]), lifespan="off",
                                         ssl_certfile=str(crt), ssl_keyfile=str(key), **kw)))
    return out


def run(servers_: list) -> None:
    """Serve them all until the first stops (a signal), then stop the others."""
    import asyncio

    async def main():
        rest = [asyncio.create_task(s.serve()) for s in servers_[1:]]
        try:
            await servers_[0].serve()
        finally:
            for s in servers_[1:]:
                s.should_exit = True
            await asyncio.gather(*rest, return_exceptions=True)
    asyncio.run(main())


# --- the admin's commands (the hub machine) ---------------------------------------------

ONLINE_S = 3600  # a machine seen within this long counts for "everyone moved"


def machines(store) -> List[dict]:
    """Each live device with a bridge: who, online, the scheme it uses, its pins (short)."""
    now = store.clock()
    with store.lock:
        rows = store.db.execute(
            "SELECT d.host, d.os_user, d.bridge_seen, f.facts FROM devices d"
            " LEFT JOIN device_facts f ON f.device_id=d.id"
            " WHERE d.revoked_at IS NULL AND d.legacy=0 AND d.mode!='personal'"
            " AND d.bridge_seen IS NOT NULL").fetchall()
    out = []
    for r in rows:
        try:
            t = (json.loads(r["facts"]) if r["facts"] else {}).get("hub_tls") or {}
        except ValueError:
            t = {}
        out.append({"who": f"{r['os_user']}@{r['host']}" if r["os_user"] else str(r["host"]),
                    "online": r["bridge_seen"] > now - ONLINE_S,
                    "scheme": t.get("scheme", "http"), "pins": t.get("pins") or []})
    return sorted(out, key=lambda m: m["who"])


def _short(fp: Optional[str]) -> str:
    return (fp or "")[:16]


def admin(action: str, store, force: bool = False, port: Optional[int] = None,
          restart: Callable[[], None] = lambda: None, log: Callable[[str], None] = print,
          run: Callable = subprocess.run) -> int:
    """`kaptyorka tls status|on|only|off|rotate|ca` on the hub machine."""
    st, now = state(), time.time()
    crt, nxt = files()[0], files(True)[0]
    cur_fp, next_fp = cert_fingerprint(crt), cert_fingerprint(nxt)
    ms = machines(store)
    plain_port = int(_hub_json().get("port", 8765))
    if action == "status":
        mode = st["mode"] if st["mode"] != "both" else f"both (machines use {st.get('use')})"
        log(f"mode: {mode}" + (f"; https on port {st['port']}" if st["mode"] != "off" else ""))
        if cur_fp:
            end = expires(crt, run)
            left = (f", ends {time.strftime('%Y-%m-%d', time.localtime(end))}"
                    f" ({int((end - now) // 86400)} days)") if end else ""
            log(f"certificate sha256: {cur_fp}{left}")
        if next_fp:
            log(f"next certificate sha256: {next_fp} (announced; `kaptyorka tls rotate` again moves to it)")
        for m in ms:
            has = "next pinned" if next_fp and _short(next_fp) in m["pins"] else \
                "pinned" if cur_fp and _short(cur_fp) in m["pins"] else "no pin"
            log(f"  {m['who']}: {m['scheme']}, {has}{'' if m['online'] else ', offline'}")
        return 0
    if action == "ca":
        ca = tls_dir() / "ca.crt"
        if not ca.exists():
            log("no certificate yet: `kaptyorka tls on` makes one")
            return 1
        log(f"the hub's CA certificate: {ca}")
        log("install it in a browser (or the system) as a trusted authority to open the page"
            " without a warning; never copy ca.key anywhere")
        return 0
    if action == "on":
        if st["mode"] != "off" and st.get("use") == "https":
            log("already on; `kaptyorka tls status` shows who moved")
            return 0
        if not cur_fp:
            names, ips = default_names()
            cur_fp = make_certificate(names, ips, run=run)
            log(f"made the hub's certificate: sha256 {cur_fp}")
        tport = port or int(st.get("port") or plain_port + 1)
        save_state({"mode": "both", "use": "https", "port": tport, "since": now})
        restart()
        log(f"https on port {tport} beside http on {plain_port}. Each machine's bridge moves on its"
            " next heartbeat, after a test call with the pinned certificate works.")
        log("next: `kaptyorka tls status` until every machine says https, then `kaptyorka tls only`")
        return 0
    if action == "only":
        if st["mode"] == "off" or st.get("use") != "https":
            log("run `kaptyorka tls on` first, and let the machines move")
            return 1
        behind = [m["who"] for m in ms if m["online"] and m["scheme"] != "https"]
        if behind and not force:
            log("still on plain http: " + ", ".join(behind) + ". Wait a minute, or --force (they"
                " then need a new invite link)")
            return 1
        save_state({**st, "mode": "only"})
        log("plain http now serves only this computer; others are told the hub moved (a browser"
            " is sent to https). Undo: `kaptyorka tls off`")
        return 0
    if action == "off":
        if st["mode"] == "off":
            log("already off")
            return 0
        if st.get("use") == "https":
            save_state({**st, "mode": "both", "use": "http"})
            log("plain http is open again and machines move back on their next heartbeat; run"
                " `kaptyorka tls off` again once `kaptyorka tls status` shows them on http")
            return 0
        behind = [m["who"] for m in ms if m["online"] and m["scheme"] == "https"]
        if behind and not force:
            log("still on https: " + ", ".join(behind) + ". Wait a minute, or --force")
            return 1
        save_state(None)
        restart()
        log("off: plain http only, as before. The certificate is kept for a later `kaptyorka tls on`")
        return 0
    if action == "rotate":
        if not cur_fp:
            log("no certificate yet: `kaptyorka tls on` makes one")
            return 1
        if not next_fp:
            names, ips = default_names()
            next_fp = make_certificate(names, ips, next_=True, run=run)
            log(f"made the next certificate: sha256 {next_fp}. Machines learn it on their next"
                " heartbeat; run `kaptyorka tls rotate` again once `kaptyorka tls status` shows"
                " every machine with it")
            return 0
        behind = [m["who"] for m in ms if m["online"] and _short(next_fp) not in m["pins"]]
        if behind and not force:
            log("not pinned yet: " + ", ".join(behind) + ". Wait a minute, or --force (they then"
                " need a new invite link)")
            return 1
        for new, old in zip(files(True), files()):
            os.replace(new, old)
        restart()
        log(f"the hub now uses sha256 {next_fp}. Browsers that accepted the old certificate by"
            " hand ask once more; those with the hub's CA installed do not")
        return 0
    log(f"unknown action {action}")
    return 2


_end_cache: dict = {}


def expiry_news(store, now: float, run: Callable = subprocess.run) -> List[str]:
    """Once per certificate, WARN_DAYS before it ends: the admins' line with the command."""
    if state()["mode"] == "off":
        return []
    crt = files()[0]
    fp = cert_fingerprint(crt)
    if not fp:
        return []
    if _end_cache.get("fp") != fp:
        _end_cache.update(fp=fp, end=expires(crt, run))
    end = _end_cache["end"]
    if end is None or end - now > WARN_DAYS * 86400:
        return []
    with store.lock:
        row = store.db.execute("SELECT value FROM settings WHERE key='tls_expiry_told'").fetchone()
        if row and row["value"] == fp:
            return []
        store.db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('tls_expiry_told', ?)", (fp,))
    days = max(0, int((end - now) // 86400))
    return [f"the hub's https certificate ends in {days} days: on the hub machine run `kaptyorka tls"
            " rotate`, then again once `kaptyorka tls status` shows every machine with the new one"]
