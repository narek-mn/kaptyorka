"""Machine events: the hub compares each team machine's bridge facts with the previous ones and
records only what the machine itself confirmed. One dated line per event goes to the team note
`machines` (its own section, the last NOTE_CAP lines) and one message to the manager; agent
tool updates go to the note only, one line for every machine that updated within TOOL_GROUP_S.

Only team machines and the hub's own computer: a personal laptop going offline is a closed
lid, and it sends no extra facts. Rules, strict: a changed value counts only after the
machine reported it in CONFIRM facts builds in a row (a value that flips back records
nothing); offline only after OFFLINE_S of silence, and machines that went silent within
GROUP_S of each other make one event; what changed while it was gone is recorded only once it
is back and reports it. Nothing is guessed: no plans, no reasons."""
import json
import time
from typing import Callable, Dict, List, Optional

from kaptyorka.store import Store

OFFLINE_S = 600
GROUP_S = 180
CONFIRM = 3
NEW_S = 600  # a device created this recently is new when first seen
COUNT_GAP_S = 55  # a bridge that sends no build number: one report a minute counts
NOTE_TOPIC = "machines"
NOTE_HEADER = "## Machine events (written by the hub)"
NOTE_CAP = 50
AUTHOR = "hub"
ORDER = ["boot", "os", "jetpack", "l4t", "kernel", "host", "ip"]  # then the tools, by name
LABEL = {"os": "OS", "l4t": "L4T", "jetpack": "JetPack", "kernel": "kernel", "host": "hostname",
         "ip": "address"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS machine_state (
    device_id INTEGER PRIMARY KEY, host TEXT NOT NULL, confirmed TEXT NOT NULL,
    pending TEXT NOT NULL DEFAULT '{}', seen REAL NOT NULL, seq INTEGER, counted REAL,
    offline_at REAL);
CREATE TABLE IF NOT EXISTS machine_tool_news (
    tool TEXT NOT NULL, old TEXT NOT NULL, new TEXT NOT NULL, host TEXT NOT NULL,
    at REAL NOT NULL, PRIMARY KEY(tool, old, new, host));
"""
TOOL_GROUP_S = 600  # agent tool updates confirmed this close together make one note line


def hm(t: float) -> str:
    return time.strftime("%H:%M", time.localtime(t))


def span(s: float) -> str:
    m = max(1, int(s // 60))
    if m < 60:
        return f"{m} min"
    h, m = divmod(m, 60)
    if h < 48:
        return f"{h} h" + (f" {m} min" if m and h < 3 else "")
    return f"{h // 24} days"


def values(facts: dict, host: str, scope: str) -> Dict[str, object]:
    """The tracked values in one facts report: OS, L4T, JetPack, kernel, boot time, the
    agent tools' versions, the hostname and, for a team machine, its address."""
    out: Dict[str, object] = {k: facts[k] for k in ("os", "l4t", "jetpack", "kernel", "boot")
                              if facts.get(k)}
    if host:
        out["host"] = host
    if scope == "team" and facts.get("ip"):
        out["ip"] = facts["ip"]
    for tool, version in (facts.get("tools") or {}).items():
        if isinstance(version, str) and version:
            out["tool:" + tool] = version
    return out


def describe(key: str, old, new) -> str:
    if key == "boot":
        return f"rebooted at {hm(float(new))}"
    name = key[5:] if key.startswith("tool:") else LABEL.get(key, key)
    return f"{name} {old} -> {new}"


class MachineEvents:
    def __init__(self, store: Store, hub_host: Callable[[], str]):
        self.store, self.db, self.lock = store, store.db, store.lock
        self.hub_host = hub_host
        # silence counts from the hub's start at the earliest: a hub that was down heard
        # nobody, which is no news about the machines
        self.started = store.clock()
        with self.lock:
            self.db.executescript(SCHEMA)

    def _device(self, device_id: int):
        return self.db.execute(
            "SELECT d.*, p.team FROM devices d JOIN people p ON p.id=d.person_id WHERE d.id=?",
            (device_id,)).fetchone()

    def _scope(self, dev) -> Optional[str]:
        if dev is None or dev["revoked_at"] is not None or dev["legacy"]:
            return None
        if dev["team"]:
            return "team"
        return "hub" if dev["host"] and dev["host"] == self.hub_host() else None

    def scope(self, device_id: int) -> Optional[str]:
        """"team" (a team machine: its address is tracked too), "hub" (the hub's own computer)
        or None: this device's bridge sends no machine facts and makes no events."""
        with self.lock:
            return self._scope(self._device(device_id))

    def tracked_team(self, device_id: int) -> bool:
        """A team machine's device these events watch: its going offline is a machine event,
        so the older "shared machine offline" notice is not sent as well."""
        with self.lock:
            return self._scope(self._device(device_id)) == "team" and self.db.execute(
                "SELECT 1 FROM machine_state WHERE device_id=?", (device_id,)).fetchone() is not None

    def observe(self, device_id: int, facts: dict, now: float) -> List[str]:
        """One facts report of a device's bridge; the event texts it confirms."""
        with self.lock:
            dev = self._device(device_id)
            scope = self._scope(dev)
            if scope is None:
                self.db.execute("DELETE FROM machine_state WHERE device_id=?", (device_id,))
                return []
            host = dev["host"]
            now_values = values(facts, host, scope)
            seq = facts.get("seq") if isinstance(facts.get("seq"), int) else None
            row = self.db.execute("SELECT * FROM machine_state WHERE device_id=?",
                                  (device_id,)).fetchone()
            if row is None:
                return self._first(dev, host, now_values, seq, now)
            events: List[str] = []
            if row["offline_at"] is not None:
                events.append(f"{host}: back online after {span(now - row['seen'])}"
                              f" (offline since {hm(row['seen'])})")
            confirmed, pending = json.loads(row["confirmed"]), json.loads(row["pending"])
            counted = row["counted"]
            if (seq is not None and seq != row["seq"]) or \
                    (seq is None and (counted is None or now - counted >= COUNT_GAP_S)):
                counted = now
                changes = self._count(confirmed, pending, now_values)
                for key, old, new in [c for c in changes if c[0].startswith("tool:")]:
                    # agent tools update often: the note only, one line for all machines
                    self.db.execute("INSERT OR REPLACE INTO machine_tool_news (tool, old, new,"
                                    " host, at) VALUES (?,?,?,?,?)",
                                    (key[5:], str(old), str(new), host, now))
                changes = [c for c in changes if not c[0].startswith("tool:")]
                if changes:
                    events.append(f"{host}: " + "; ".join(describe(*c) for c in changes))
            self.db.execute(
                "UPDATE machine_state SET host=?, confirmed=?, pending=?, seen=?, seq=?,"
                " counted=?, offline_at=NULL WHERE device_id=?",
                (host, json.dumps(confirmed), json.dumps(pending), now, seq, counted, device_id))
            return events

    @staticmethod
    def _count(confirmed: dict, pending: dict, now_values: dict) -> list:
        """One counted report: a value seen CONFIRM times in a row becomes confirmed; one
        that is back to the confirmed value drops its count. A value never known before is
        taken as it is (a bridge that just started sending it), with no event."""
        changes = []
        for key, v in now_values.items():
            old = confirmed.get(key)
            if old is None:
                confirmed[key] = v
            elif v == old:
                pending.pop(key, None)
            else:
                n = pending[key][1] + 1 if key in pending and pending[key][0] == v else 1
                if n >= CONFIRM:
                    changes.append((key, old, v))
                    confirmed[key] = v
                    pending.pop(key, None)
                else:
                    pending[key] = [v, n]
        for key in [k for k in pending if k not in now_values]:
            pending.pop(key)  # no longer reported: no run of reports
        return sorted(changes, key=lambda c: (ORDER.index(c[0]) if c[0] in ORDER else len(ORDER),
                                              c[0]))

    def _first(self, dev, host: str, now_values: dict, seq, now: float) -> List[str]:
        """Lock held. A device seen for the first time: a new one (created just now) is a new
        machine, another account on it, or the machine joined again as new after being gone
        (a reinstall); then what it had before is carried over, so the changes it reports
        are recorded once confirmed. An older device (tracking just started) is taken as it is."""
        events: List[str] = []
        confirmed = dict(now_values)
        # joins only for team machines: on the hub's own computer they are its setup (the
        # admin's and the manager's accounts), not news
        if dev["team"] and dev["created"] >= now - NEW_S:
            others = self.db.execute(
                "SELECT m.*, d.os_user FROM machine_state m JOIN devices d ON d.id=m.device_id"
                " WHERE m.host=? AND m.device_id!=? ORDER BY m.seen DESC",
                (host, dev["id"])).fetchall()
            same = [r for r in others if r["os_user"] == dev["os_user"]]  # its account again
            if same:
                last = same[0]
                # what it had: its changes are recorded once confirmed; a reinstall boots anew
                confirmed = {**json.loads(last["confirmed"]), "boot": now_values.get("boot")}
                confirmed = {k: v for k, v in confirmed.items() if v is not None}
                was = (f" after being offline since {hm(last['seen'])}"
                       if last["offline_at"] is not None or last["seen"] < now - OFFLINE_S else "")
                events.append(f"{host}: joined again as a new device{was}")
                for r in same:  # replaced: never offline again
                    self.db.execute("DELETE FROM machine_state WHERE device_id=?",
                                    (r["device_id"],))
            elif others:
                events.append(f"{host}: another account joined ({dev['os_user'] or 'unknown'})")
            else:
                events.append(f"{host}: new machine joined")
        pending: dict = {}
        self._count(confirmed, pending, now_values)  # its first report is the first of a run
        self.db.execute("INSERT INTO machine_state (device_id, host, confirmed, pending, seen, seq,"
                        " counted) VALUES (?,?,?,?,?,?,?)",
                        (dev["id"], host, json.dumps(confirmed), json.dumps(pending), now, seq, now))
        return events

    def tick(self, now: float) -> List[str]:
        """Offline: silent more than OFFLINE_S. Machines silent from within GROUP_S of each
        other make one event, written once the last of them could have joined the group."""
        events: List[str] = []
        with self.lock:
            rows = []
            for r in self.db.execute("SELECT * FROM machine_state WHERE offline_at IS NULL"
                                     " ORDER BY seen").fetchall():
                if self._scope(self._device(r["device_id"])) is None:
                    self.db.execute("DELETE FROM machine_state WHERE device_id=?",
                                    (r["device_id"],))
                elif max(r["seen"], self.started) < now - OFFLINE_S:
                    rows.append(r)

            def quiet(r):
                return max(r["seen"], self.started)
            while rows and now >= quiet(rows[0]) + OFFLINE_S + GROUP_S:
                group = [r for r in rows if quiet(r) <= quiet(rows[0]) + GROUP_S]
                rows = rows[len(group):]
                hosts = list(dict.fromkeys(r["host"] for r in group))
                for r in group:
                    self.db.execute("UPDATE machine_state SET offline_at=? WHERE device_id=?",
                                    (now, r["device_id"]))
                if len(hosts) == 1:
                    events.append(f"{hosts[0]}: offline since {hm(group[0]['seen'])}")
                else:
                    within = span(group[-1]["seen"] - group[0]["seen"])
                    events.append(f"{', '.join(hosts)} went offline within {within} (since"
                                  f" {hm(group[0]['seen'])}): likely network or power")
        return events

    def tool_news(self, now: float) -> List[str]:
        """Agent tool updates, for the note only: one line per update (tool, old, new) for
        every machine that confirmed it within TOOL_GROUP_S of the first, written once that
        window has passed: "Claude Code 2.1.331 -> 2.1.332 on box-1, box-2, box-3"."""
        from kaptyorka.adapters.base import TOOL_NAMES
        out: List[str] = []
        with self.lock:
            rows = self.db.execute("SELECT tool, old, new, MIN(at) AS first FROM"
                                   " machine_tool_news GROUP BY tool, old, new ORDER BY first"
                                   ).fetchall()
            for r in rows:
                if now < r["first"] + TOOL_GROUP_S:
                    continue
                key = (r["tool"], r["old"], r["new"])
                hosts = [h["host"] for h in self.db.execute(
                    "SELECT host FROM machine_tool_news WHERE tool=? AND old=? AND new=? AND"
                    " at<=? ORDER BY host", (*key, r["first"] + TOOL_GROUP_S))]
                self.db.execute("DELETE FROM machine_tool_news WHERE tool=? AND old=? AND new=?"
                                " AND at<=?", (*key, r["first"] + TOOL_GROUP_S))
                out.append(f"{TOOL_NAMES.get(r['tool'], r['tool'])} {r['old']} -> {r['new']}"
                           f" on {', '.join(hosts)}")
        return out

    def write_note(self, texts: List[str], now: float) -> None:
        """One dated line per event in the team note `machines`, in its own section at the end
        (the last NOTE_CAP lines); the rest of the note is kept as it is."""
        lines = [f"- {time.strftime('%Y-%m-%d %H:%M', time.localtime(now))} {t}" for t in texts]
        self.store.note_section(NOTE_TOPIC, NOTE_HEADER, lines, NOTE_CAP, AUTHOR)


def manager_text(event: str) -> str:
    return (f"machine event: {event}. Follow your machine-events rule: warn the sessions working"
            " on that machine, tidy the `machines` note, add a reason only once a session"
            " confirms the work is done.")
