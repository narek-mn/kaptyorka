import hashlib
import http.client
import json
import ssl
import urllib.error
import urllib.request
from typing import Any, List, Optional, Tuple


class HubUnreachable(Exception):
    def __init__(self, url: str):
        super().__init__(f"hub unreachable: {url}")
        self.url = url


class CertMismatch(OSError):
    """The hub presented a certificate whose fingerprint this machine does not accept."""

    def __init__(self, got: str):
        super().__init__(f"the hub's certificate ({got[:16]}...) is not the one this machine"
                         " joined with")
        self.got = got


class HubCertMismatch(HubUnreachable):
    """Refused before anything was sent: someone may be in between, or the hub's certificate
    changed without telling this machine. Ask the admin; `kaptyorka join` with a new link."""

    def __init__(self, url: str, got: str):
        super().__init__(url)
        self.got = got
        self.args = (f"refused {url}: its certificate {got[:16]}... is not the one this machine"
                     " joined with; ask your admin, then kaptyorka join <new invite link>",)


def unreachable(url: str) -> str:
    """The one sentence every command prints when the hub does not answer."""
    return f"cannot reach the hub at {url}; is it running, and is the address right?"


class _PinnedConnection(http.client.HTTPSConnection):
    """An https connection that accepts the server only by its certificate's fingerprint."""

    def __init__(self, *a, pins: List[str] = (), **kw):
        super().__init__(*a, **kw)
        self._pins = list(pins)

    def connect(self):
        super().connect()
        got = hashlib.sha256(self.sock.getpeercert(binary_form=True)).hexdigest()
        if got not in self._pins:  # before any byte of the request (and its token) goes out
            self.sock.close()
            raise CertMismatch(got)


class _PinnedHandler(urllib.request.HTTPSHandler):
    def __init__(self, pins: List[str]):
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE  # the fingerprint check in _PinnedConnection decides
        super().__init__(context=ctx)
        self._pins, self._ctx = pins, ctx

    def https_open(self, req):
        pins = self._pins
        return self.do_open(lambda *a, **kw: _PinnedConnection(*a, pins=pins, **kw), req,
                            context=self._ctx)


def _open(req, timeout: float, pins: Optional[List[str]]):
    """urlopen, pinned for an https hub whose fingerprints this machine knows (explicit pins,
    else the config's for its own hub); a CA-signed hub without pins is checked as usual."""
    url = req.full_url
    if pins is None and url.startswith("https://"):
        from kaptyorka import tls

        pins = tls.pins_for(url)
    if url.startswith("https://") and pins:
        return urllib.request.build_opener(_PinnedHandler(pins)).open(req, timeout=timeout)
    return urllib.request.urlopen(req, timeout=timeout)


def _unreachable(url: str, e: Exception) -> HubUnreachable:
    reason = getattr(e, "reason", e)
    if isinstance(reason, CertMismatch):
        return HubCertMismatch(url, reason.got)
    return HubUnreachable(url)


def request(method: str, url: str, token: str, body: "dict | None" = None,
            headers: "dict | None" = None, timeout: float = 10,
            pins: Optional[List[str]] = None) -> "tuple[int, Any]":
    h = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        h["Content-Type"] = "application/json"
    h.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    try:
        with _open(req, timeout, pins) as r:
            return r.status, _decode(r.read(), r.headers.get("Content-Type", ""))
    except urllib.error.HTTPError as e:
        return e.code, _decode(e.read(), e.headers.get("Content-Type", ""))
    except (urllib.error.URLError, OSError, http.client.HTTPException) as e:
        raise _unreachable(url, e) from None


def _decode(raw: bytes, ctype: str) -> Any:
    text = raw.decode("utf-8", "replace")
    if ctype.startswith("text/"):
        return text
    if not text:
        return None
    try:
        return json.loads(text)
    except ValueError:
        return text


def request_raw(method: str, url: str, token: str, data: Optional[bytes] = None,
                headers: "dict | None" = None, timeout: float = 30,
                pins: Optional[List[str]] = None) -> Tuple[int, bytes, str]:
    """Bytes in, bytes out: (status, body, content type). For file upload and download."""
    h = {"Authorization": f"Bearer {token}"}
    if data is not None:
        h["Content-Type"] = "application/octet-stream"
    h.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    try:
        with _open(req, timeout, pins) as r:
            return r.status, r.read(), r.headers.get("Content-Type", "")
    except urllib.error.HTTPError as e:
        return e.code, e.read(), e.headers.get("Content-Type", "")
    except (urllib.error.URLError, OSError, http.client.HTTPException) as e:
        raise _unreachable(url, e) from None
