import http.client
import json
import urllib.error
import urllib.request
from typing import Any, Optional, Tuple


class HubUnreachable(Exception):
    def __init__(self, url: str):
        super().__init__(f"hub unreachable: {url}")
        self.url = url


def request(method: str, url: str, token: str, body: "dict | None" = None,
            headers: "dict | None" = None, timeout: float = 10) -> "tuple[int, Any]":
    h = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        h["Content-Type"] = "application/json"
    h.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, _decode(r.read(), r.headers.get("Content-Type", ""))
    except urllib.error.HTTPError as e:
        return e.code, _decode(e.read(), e.headers.get("Content-Type", ""))
    except (urllib.error.URLError, OSError, http.client.HTTPException):
        raise HubUnreachable(url) from None


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
                headers: "dict | None" = None, timeout: float = 30) -> Tuple[int, bytes, str]:
    """Bytes in, bytes out: (status, body, content type). For file upload and download."""
    h = {"Authorization": f"Bearer {token}"}
    if data is not None:
        h["Content-Type"] = "application/octet-stream"
    h.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read(), r.headers.get("Content-Type", "")
    except urllib.error.HTTPError as e:
        return e.code, e.read(), e.headers.get("Content-Type", "")
    except (urllib.error.URLError, OSError, http.client.HTTPException):
        raise HubUnreachable(url) from None
