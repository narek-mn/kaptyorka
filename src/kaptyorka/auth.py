"""ASGI auth: each device token resolves to a principal; failed tokens are rate-limited."""
import hashlib
import logging
import sys
import time
from typing import Callable, Dict, Hashable, List
from urllib.parse import parse_qs, unquote

from starlette.responses import PlainTextResponse
from starlette.websockets import WebSocketClose

# what a machine still on the old shared token is told once that token is off
LEGACY_OFF = ("the old shared sign-in is switched off (no machine used it for 7 days); to switch it"
              " back on, run on the hub machine: kaptyorka shared-token on. Better: join this"
              " machine with an invite link: kaptyorka join <link>")


class RateLimiter:
    """Counts failures per key (client address and token hash) over a sliding window."""

    def __init__(self, limit: int = 10, window_s: float = 60,
                 clock: Callable[[], float] = time.monotonic):
        self.limit, self.window_s, self.clock = limit, window_s, clock
        self.failures: Dict[Hashable, List[float]] = {}

    def _prune(self, now: float) -> None:
        """Forget keys whose window has fully passed, so made-up tokens cannot grow the map."""
        for k in [k for k, ts in self.failures.items() if not ts or now - ts[-1] >= self.window_s]:
            del self.failures[k]

    def _recent(self, addr: Hashable) -> List[float]:
        now = self.clock()
        self._prune(now)
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

    def __init__(self, app, store, open_paths=("/",), open_prefixes=("/i/", "/fonts/"),
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
        if not token and scope.get("method") == "GET" and scope["path"].startswith("/api/files/"):
            # an <img> on the page cannot send a header: only file downloads take ?t=<token>
            token = parse_qs(scope.get("query_string", b"").decode("latin-1")).get("t", [""])[0]
        machine = unquote(headers.get("x-kaptyorka-machine") or "") or None
        if machine is None and scope.get("path") == "/bridge/poll":
            # a bridge sends no identity header: its machine is in the query. /bridge/heartbeat
            # has it in the JSON body instead, so the hub re-resolves a legacy caller there.
            machine = parse_qs(scope.get("query_string", b"").decode("latin-1")).get(
                "machine", [None])[0] or None
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
                deny = (PlainTextResponse(LEGACY_OFF if self.store.legacy_switched_off(token)
                                          else "unauthorized", status_code=401)
                        if scope["type"] == "http" else WebSocketClose(1008))
            await deny(scope, receive, send)
            return
        scope.setdefault("state", {})["principal"] = principal
        await self.app(scope, receive, send)


NO_WEBSOCKET = "this hub serves no WebSocket"
UPGRADE_WARNINGS = ("Unsupported upgrade request.", "No supported WebSocket library detected")
UPGRADE_NOTE_S = 3600  # one line per client and path an hour


class _QuietUpgrades(logging.Filter):
    """Drops uvicorn's two warnings per WebSocket upgrade request; NoWebSocket logs them."""

    def filter(self, record: logging.LogRecord) -> bool:
        return not str(record.getMessage()).startswith(UPGRADE_WARNINGS)


def quiet_upgrade_warnings() -> None:
    log = logging.getLogger("uvicorn.error")
    if not any(isinstance(f, _QuietUpgrades) for f in log.filters):
        log.addFilter(_QuietUpgrades())


class NoWebSocket:
    """ASGI: a request that asks for a WebSocket gets a plain 400, and one stderr line per
    client and path an hour names who asks (the hub has no WebSocket route)."""

    def __init__(self, app, clock: Callable[[], float] = time.monotonic, report=None):
        self.app, self.clock = app, clock
        self.report = report or (lambda line: print(line, file=sys.stderr, flush=True))
        self.noted: Dict[Hashable, float] = {}

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
            if headers.get("upgrade", "").lower() == "websocket":
                client = scope.get("client")
                who = client[0] if client else "?"
                key, now = (who, scope["path"]), self.clock()
                if now - self.noted.get(key, -UPGRADE_NOTE_S) >= UPGRADE_NOTE_S:
                    self.noted = {k: t for k, t in self.noted.items() if now - t < UPGRADE_NOTE_S}
                    self.noted[key] = now
                    agent = headers.get("user-agent", "")[:120] or "no user agent"
                    self.report(f"kaptyorka: refused WebSocket requests from {who} to"
                                f" {scope['path'][:200]} ({agent}); once an hour")
                await PlainTextResponse(NO_WEBSOCKET, status_code=400)(scope, receive, send)
                return
        await self.app(scope, receive, send)
