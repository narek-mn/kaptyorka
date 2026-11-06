"""ASGI auth: each device token resolves to a principal; failed tokens are rate-limited."""
import hashlib
import time
from typing import Callable, Dict, Hashable, List
from urllib.parse import unquote

from starlette.responses import PlainTextResponse
from starlette.websockets import WebSocketClose


class RateLimiter:
    """Counts failures per key (client address and token hash) over a sliding window."""

    def __init__(self, limit: int = 10, window_s: float = 60,
                 clock: Callable[[], float] = time.monotonic):
        self.limit, self.window_s, self.clock = limit, window_s, clock
        self.failures: Dict[Hashable, List[float]] = {}

    def _recent(self, addr: Hashable) -> List[float]:
        now = self.clock()
        recent = [t for t in self.failures.get(addr, []) if now - t < self.window_s]
        if recent:
            self.failures[addr] = recent
        else:
            self.failures.pop(addr, None)
        return recent

    def blocked(self, addr: Hashable) -> bool:
        return len(self._recent(addr)) >= self.limit

    def hit(self, addr: Hashable) -> bool:
        """Record a failure; True when the address is still allowed."""
        recent = self._recent(addr)
        recent.append(self.clock())
        self.failures[addr] = recent
        return len(recent) <= self.limit


def bearer(headers) -> str:
    auth = headers.get("authorization") or ""
    return auth[7:] if auth[:7].lower() == "bearer " else ""


class Auth:
    """ASGI wrapper: open routes pass; everything else needs a device token, whose
    principal goes into scope["state"]["principal"]."""

    def __init__(self, app, store, open_paths=("/",), open_prefixes=("/i/",),
                 limiter: "RateLimiter | None" = None):
        self.app, self.store = app, store
        self.open_paths, self.open_prefixes = tuple(open_paths), tuple(open_prefixes)
        self.limiter = limiter or RateLimiter()

    def _open(self, scope) -> bool:
        if scope["type"] != "http":
            return False
        path = scope["path"]
        if path.startswith(self.open_prefixes):
            return True
        return scope["method"] == "GET" and path in self.open_paths

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan" or self._open(scope):
            await self.app(scope, receive, send)
            return
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
        token = bearer(headers)
        machine = unquote(headers.get("x-kaptyorka-machine") or "") or None
        principal = self.store.authenticate(token, machine=machine) if token else None
        if principal is None:
            # keyed by address and token, so one revoked device cannot lock out a shared host
            client = scope.get("client")
            key = (client[0] if client else "?", hashlib.sha256(token.encode()).hexdigest())
            if self.limiter.blocked(key):
                deny = (PlainTextResponse("too many failed logins", status_code=429)
                        if scope["type"] == "http" else WebSocketClose(1008))
            else:
                self.limiter.hit(key)
                deny = (PlainTextResponse("unauthorized", status_code=401)
                        if scope["type"] == "http" else WebSocketClose(1008))
            await deny(scope, receive, send)
            return
        scope.setdefault("state", {})["principal"] = principal
        await self.app(scope, receive, send)
