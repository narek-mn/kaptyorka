"""Signed launch orders: the hub signs, the bridge verifies."""
import hashlib
import hmac
import json
from typing import Collection, Optional

MAX_AGE_S = 600


def signing_key(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def _mac(order: dict, key: str) -> str:
    body = json.dumps(order, sort_keys=True, separators=(",", ":"))
    return hmac.new(key.encode(), body.encode(), hashlib.sha256).hexdigest()


def sign(order: dict, key: str) -> dict:
    body = {k: v for k, v in order.items() if k != "sig"}
    return {**body, "sig": _mac(body, key)}


def verify(order: dict, key: str, now: float, seen_ids: Collection[int]) -> Optional[str]:
    if not isinstance(order, dict) or not all(f in order for f in ("sig", "id", "issued")):
        return "malformed order"
    issued = order["issued"]
    if isinstance(issued, bool) or not isinstance(issued, (int, float)):
        return "malformed order"
    try:
        hash(order["id"])
    except TypeError:
        return "malformed order"
    body = {k: v for k, v in order.items() if k != "sig"}
    if not hmac.compare_digest(str(order["sig"]).encode(), _mac(body, key).encode()):
        return "bad signature"
    if abs(now - issued) > MAX_AGE_S:  # a future-dated order is as suspect as an old one
        return "order too old"
    if order["id"] in seen_ids:
        return "order already run"
    return None
