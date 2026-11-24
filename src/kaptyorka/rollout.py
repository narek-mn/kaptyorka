"""Staged updates. When the hub runs a new version, bridges do not all update at once: a first
group updates (the accounts on the hub's own computer, and one team machine: the least busy
online one, never one with an overnight session or a waiting background task when another can
go), the rest wait SOAK_S and update only if the first group is healthy: each of its online
bridges reports the new version, none reports a failed update, and one that listed sessions
before still lists some. Unhealthy: the rest stay on the old version and the admins and the
manager hear why, once. `kaptyorka fleet rollout go` releases the rest at once, `hold` keeps
them back; `kaptyorka update` on a machine always updates it by hand.

The state is one JSON row in the settings table; the bridges learn it through the heartbeat's
`auto_update` (true only when this device may update now). No new service, no new route for
the bridge. A machine offline during the window updates when it comes back, if it passed."""
import json
import re
from typing import List, Optional

from kaptyorka import paths
from kaptyorka.store import Store, machine_name

SOAK_S = 20 * 60  # the first group runs the new version this long before the rest follow
ONLINE_S = 600  # a bridge silent longer than this is offline (workers.OFFLINE_S)
KEY = "rollout"


def _numbers(version) -> tuple:
    return tuple(int(n) for n in re.findall(r"\d+", str(version or "")))


def _get(store: Store) -> Optional[dict]:
    with store.lock:
        row = store.db.execute("SELECT value FROM settings WHERE key=?", (KEY,)).fetchone()
    try:
        return json.loads(row["value"]) if row else None
    except ValueError:
        return None


def _put(store: Store, state: dict) -> None:
    with store.lock:
        store.db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                         (KEY, json.dumps(state)))


def _devices(store: Store) -> List[dict]:
    with store.lock:
        rows = store.db.execute(
            "SELECT d.id, d.host, d.os_user, d.name, d.token_hash, d.last_used, d.version,"
            " p.team, f.facts FROM devices d JOIN people p ON p.id=d.person_id"
            " LEFT JOIN device_facts f ON f.device_id=d.id"
            " WHERE d.revoked_at IS NULL AND d.legacy=0 AND p.removed_at IS NULL").fetchall()
        busy = {r["device_id"]: (r["n"], r["working"]) for r in store.db.execute(
            "SELECT device_id, COUNT(*) AS n, SUM(activity NOT IN ('', 'idle')) AS working"
            " FROM agents WHERE last_seen > ? AND device_id IS NOT NULL GROUP BY device_id",
            (store.clock() - 30,))}
        night = {r["device_id"] for r in store.db.execute(
            "SELECT device_id FROM workers WHERE overnight=1 AND state IN"
            " ('starting','up','unattended','abandoned')")}
        waiting = {r["device_id"] for r in store.db.execute(
            "SELECT device_id FROM agents WHERE last_seen > ? AND activity='shell'",
            (store.clock() - 30,))}
    out, hub_key = [], paths.hub_key_digest()
    for r in rows:
        facts = json.loads(r["facts"]) if r["facts"] else {}
        n, working = busy.get(r["id"], (0, 0))
        out.append({"id": r["id"], "host": r["host"], "os_user": r["os_user"], "team": bool(r["team"]),
                    "hub_key": hub_key is not None and r["token_hash"] == hub_key, "last_used": r["last_used"],
                    "version": facts.get("version") or r["version"] or "",
                    "update_failed": facts.get("update_failed") or "", "sessions": n,
                    "working": working or 0, "night": r["id"] in night or r["id"] in waiting})
    return out


def _name(d: dict) -> str:
    return machine_name("", d["os_user"], d["host"], d["host"] or str(d["id"]))


def pick_first(store: Store, now: float) -> List[int]:
    """The first group: every account on the hub's computer, and one online team machine (the
    least busy; one with an overnight session or a waiting background task only if no other
    is online). No hub account and no team machine online: the least busy online account."""
    devs = _devices(store)
    online = [d for d in devs if d["last_used"] is not None and d["last_used"] > now - ONLINE_S]
    hub_hosts = {d["host"] for d in devs if d["hub_key"] and d["host"]}
    first = [d["id"] for d in devs if d["host"] and d["host"] in hub_hosts]
    calm = sorted((d for d in online if d["team"] and d["host"] not in hub_hosts),
                  key=lambda d: (d["night"], d["working"], d["sessions"], d["id"]))
    if calm:
        first.append(calm[0]["id"])
    if not first:
        rest = sorted(online, key=lambda d: (d["night"], d["working"], d["sessions"], d["id"]))
        first = [rest[0]["id"]] if rest else []
    return sorted(set(first))


def state(store: Store, hub_version: str, now: Optional[float] = None) -> dict:
    """The rollout of `hub_version`, started now when the hub first runs it."""
    now = store.clock() if now is None else now
    st = _get(store)
    if st is None or st.get("version") != hub_version:
        first = pick_first(store, now)
        st = {"version": hub_version, "started": now, "first": first,
              "baseline": {str(d["id"]): d["sessions"] for d in _devices(store) if d["id"] in first},
              "state": "soak" if first else "passed", "reason": "" if first else "no machine online to go first",
              "decided_at": None if first else now, "by": "" if first else "hub", "told": False}
        _put(store, st)
    return st


def allows(store: Store, device_id: int, hub_version: str) -> bool:
    """May this device's bridge update to the hub's version now?"""
    st = state(store, hub_version)
    return st["state"] == "passed" or device_id in st["first"]


def health(store: Store, st: dict, now: float) -> Optional[str]:
    """None while the first group looks healthy, else the reason in a few words. Offline
    bridges are not judged (no word either way); with none online, it waits."""
    devs = {d["id"]: d for d in _devices(store)}
    judged = 0
    for i in st["first"]:
        d = devs.get(i)
        if d is None or d["last_used"] is None or d["last_used"] <= now - ONLINE_S:
            continue
        judged += 1
        if d["update_failed"]:
            return f"{_name(d)}: {d['update_failed']}"
        if _numbers(d["version"]) < _numbers(st["version"]):
            return f"{_name(d)} still runs {d['version'] or 'an unknown version'}"
        if st["baseline"].get(str(i), 0) > 0 and d["sessions"] == 0:
            return f"{_name(d)} lists no sessions since its update"
    return None if judged else "waiting"


def tick(store: Store, hub_version: str, now: float) -> List[str]:
    """Move the rollout on; the messages to send to the admins and the manager (at most one)."""
    st = state(store, hub_version, now)
    if st["state"] != "soak" or now - st["started"] < SOAK_S:
        return []
    why = health(store, st, now)
    if why == "waiting":
        return []  # no bridge of the first group online yet: keep waiting
    if why is None:
        st.update(state="passed", decided_at=now, by="hub", reason="")
        _put(store, st)
        return []
    st.update(state="held", decided_at=now, by="hub", reason=why)
    told = st["told"]
    st["told"] = True
    _put(store, st)
    if told:
        return []
    return [f"Update to kaptyorka {hub_version} held: the machines that went first show a problem"
            f" ({why}). The other machines stay on their version. Run `kaptyorka fleet rollout go`"
            " to update them anyway, or `kaptyorka update` on one machine to update it by hand."]


def decide(store: Store, hub_version: str, go: bool, by: str) -> dict:
    """The admin: go (release everyone now) or hold (keep the rest back until go)."""
    st = state(store, hub_version)
    st.update(state="passed" if go else "held", decided_at=store.clock(), by=by,
              reason="" if go else f"held by {by}", told=True)
    _put(store, st)
    return status(store, hub_version)


def status(store: Store, hub_version: str, now: Optional[float] = None) -> dict:
    now = store.clock() if now is None else now
    st = state(store, hub_version, now)
    devs = _devices(store)
    names = {d["id"]: _name(d) for d in devs}
    behind = [_name(d) for d in devs if d["version"] and _numbers(d["version"]) < _numbers(hub_version)]
    return {"version": hub_version, "state": st["state"], "first": [names.get(i, str(i)) for i in st["first"]],
            "started": st["started"], "rest_in_s": max(0, round(st["started"] + SOAK_S - now))
            if st["state"] == "soak" else 0, "reason": st["reason"], "by": st["by"], "behind": behind}


def line(s: dict) -> str:
    """One line for `kaptyorka status` and the Machines page."""
    first = ", ".join(s["first"]) or "none"
    if s["state"] == "soak":
        wait = f"in {max(1, round(s['rest_in_s'] / 60))} min" if s["rest_in_s"] else "once they report"
        return f"update {s['version']}: first {first}; the rest follow {wait} if they are healthy"
    if s["state"] == "held":
        return f"update {s['version']} held: {s['reason']}; `kaptyorka fleet rollout go` releases it"
    left = f"; {len(s['behind'])} machine(s) not updated yet" if s["behind"] else ""
    return f"update {s['version']}: released to every machine{left}"

