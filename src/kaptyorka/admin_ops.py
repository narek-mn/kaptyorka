"""The admin's changes to people, projects and invites; a member's own access; the log read
as plain sentences."""
import json
import re
from typing import Dict, List, Optional

from kaptyorka.store import MAX_ABOUT, KaptyorkaError, Principal

LIVE = ("starting", "up", "unattended", "abandoned")  # workers.LIVE; workers imports the store
MANAGER = "manager"  # manager.PERSON; manager imports the store


def ensure_schema(db) -> None:
    cols = [r[1] for r in db.execute("PRAGMA table_info(people)")]
    if "removed_at" not in cols:
        db.execute("ALTER TABLE people ADD COLUMN removed_at REAL")


def _admin(by: Principal) -> None:
    if not by.is_admin():
        raise KaptyorkaError("admin only")


def _live_person(db, name: str):
    row = db.execute("SELECT * FROM people WHERE name=? AND removed_at IS NULL",
                     (name.strip(),)).fetchone()
    if row is None:
        raise KaptyorkaError(f"no person named {name.strip()}")
    return row


def names(store) -> Dict[int, str]:
    """{person id: name} for everyone, removed people too: their old sessions keep a name."""
    with store.lock:
        return {r["id"]: r["name"] for r in store.db.execute("SELECT id, name FROM people")}


# people

def remove_person(store, ws, name: str, by: Principal) -> dict:
    """Take a person off the team: devices stop, access and open requests go, their workers
    close. The row stays, renamed "<name> (removed)", so the log and old sessions read right."""
    _admin(by)
    db, now = store.db, store.clock()
    with store.lock:
        row = _live_person(db, name)
        pid, name = row["id"], row["name"]
        if pid == by.person_id:
            raise KaptyorkaError("you cannot remove yourself")
        if row["role"] == "admin":
            raise KaptyorkaError("the admin cannot be removed")
        if name == MANAGER and row["role"] == "manager":
            raise KaptyorkaError("the manager account cannot be removed;"
                                 " use kaptyorka start --no-manager")
        on = db.execute("SELECT value FROM settings WHERE key='legacy_enabled'").fetchone()
        mapped = db.execute("SELECT key FROM settings WHERE key LIKE 'legacy:%' AND value=?",
                            (str(pid),)).fetchone()
        if mapped is not None and (on is None or on["value"] != "0"):
            # the shared token speaks for them on that machine, devices or not
            raise KaptyorkaError(f"{name} still uses the old shared token on"
                                 f" {mapped['key'][len('legacy:'):]}; turn the shared token off first")
        for dev in db.execute("SELECT * FROM devices WHERE person_id=? AND revoked_at IS NULL",
                              (pid,)).fetchall():
            if store._is_rekey_device(dev):  # its token must not be handed out
                db.execute("DELETE FROM rekeys WHERE device_name=?", (dev["name"],))
        devices = db.execute("UPDATE devices SET revoked_at=? WHERE person_id=? AND"
                             " revoked_at IS NULL", (now, pid)).rowcount
        db.execute("DELETE FROM invites WHERE person_id=? AND used_at IS NULL", (pid,))
        doors = db.execute("UPDATE doors SET closed_at=? WHERE closed_at IS NULL AND"
                           " (a_person=? OR b_person=?)", (now, pid, pid)).rowcount
        db.execute("UPDATE knocks SET answered_at=?, answer='no' WHERE answered_at IS NULL AND"
                   " (from_person=? OR to_person=?)", (now, pid, pid))
        grants = db.execute("DELETE FROM project_access WHERE person_id=?", (pid,)).rowcount
        grants += db.execute("DELETE FROM machine_access WHERE person_id=?", (pid,)).rowcount
        requests = db.execute("DELETE FROM requests WHERE person_id=? AND decided_at IS NULL",
                              (pid,)).rowcount
        new = f"{name} (removed)"
        if db.execute("SELECT 1 FROM people WHERE name=?", (new,)).fetchone():
            new = f"{new} #{pid}"
        db.execute("UPDATE people SET removed_at=?, name=? WHERE id=?", (now, new, pid))
    workers = 0
    with ws.lock:  # the close order a person's close sends; then the row says why
        for w in ws.db.execute(f"SELECT * FROM workers WHERE boss_person=? AND state IN"
                               f" ({','.join('?' * len(LIVE))})", (pid, *LIVE)).fetchall():
            ws._close(w, w["name"])
            ws.db.execute("UPDATE workers SET state='closed', error='boss removed',"
                          " closed_at=COALESCE(closed_at, ?) WHERE name=?", (now, w["name"]))
            workers += 1
    store.log_add(by.person_name, "person_removed", name)
    return {"devices": devices, "workers": workers, "doors": doors, "grants": grants,
            "requests": requests}


def set_role(store, name: str, role: str, by: Principal) -> None:
    _admin(by)
    if role == "admin":
        raise KaptyorkaError("the admin role cannot be given or taken here")
    if role not in ("member", "manager"):
        raise KaptyorkaError("role must be member or manager")
    with store.lock:
        row = _live_person(store.db, name)
        if row["role"] == "admin":
            raise KaptyorkaError("the admin role cannot be given or taken here")
        if row["name"] == MANAGER and row["role"] == "manager":
            raise KaptyorkaError("the manager account keeps its role")
        store.db.execute("UPDATE people SET role=? WHERE id=?", (role, row["id"]))
    store.log_add(by.person_name, "role", f"{row['name']} -> {role}")


# invites

def invite_expires(store, code: str) -> Optional[float]:
    with store.lock:
        row = store.db.execute("SELECT expires FROM invites WHERE code=?", (code,)).fetchone()
    return row["expires"] if row else None


def invites_open(store) -> List[dict]:
    """Unused, unexpired invites of people still on the team, oldest first."""
    with store.lock:
        rows = store.db.execute(
            "SELECT i.code, p.name AS person, i.created, i.expires FROM invites i"
            " JOIN people p ON p.id=i.person_id WHERE i.used_at IS NULL AND i.expires>?"
            " AND p.removed_at IS NULL ORDER BY i.created", (store.clock(),)).fetchall()
    return [dict(r) for r in rows]


def invite_cancel(store, code: str, by: Principal) -> None:
    _admin(by)
    with store.lock:
        row = store.db.execute("SELECT p.name FROM invites i JOIN people p ON p.id=i.person_id"
                               " WHERE i.code=? AND i.used_at IS NULL", (code,)).fetchone()
        if row is None:
            raise KaptyorkaError("no open invite with that code")
        store.db.execute("DELETE FROM invites WHERE code=?", (code,))
    store.log_add(by.person_name, "invite_cancel", f"invite for {row['name']}")


# projects

def _project(db, name: str) -> None:
    if db.execute("SELECT 1 FROM projects WHERE name=?", (name,)).fetchone() is None:
        raise KaptyorkaError(f"no project named {name}")


def project_update(store, name: str, description: str, by: Principal) -> None:
    _admin(by)
    description = description.strip()
    if len(description) > MAX_ABOUT:
        raise KaptyorkaError(f"description is too long (max {MAX_ABOUT} characters)")
    with store.lock:
        _project(store.db, name)
        store.db.execute("UPDATE projects SET description=? WHERE name=?", (description, name))
    store.log_add(by.person_name, "project_updated", name)


def project_remove(store, name: str, by: Principal) -> dict:
    """Unregister a project: its grants, pending requests and the folder each machine learned
    for it go. Its notes stay."""
    _admin(by)
    db = store.db
    with store.lock:
        _project(db, name)
        if db.execute(f"SELECT 1 FROM workers WHERE project=? AND state IN"
                      f" ({','.join('?' * len(LIVE))})", (name, *LIVE)).fetchone():
            raise KaptyorkaError(f"workers are running in {name}; close its workers first")
        grants = db.execute("DELETE FROM project_access WHERE project=?", (name,)).rowcount
        requests = db.execute("DELETE FROM requests WHERE kind='project' AND target=? AND"
                              " decided_at IS NULL", (name,)).rowcount
        folders = 0
        for r in db.execute("SELECT device_id, folders FROM device_facts").fetchall():
            known = json.loads(r["folders"])
            if name in known:
                del known[name]
                db.execute("UPDATE device_facts SET folders=? WHERE device_id=?",
                           (json.dumps(known), r["device_id"]))
                folders += 1
        db.execute("DELETE FROM projects WHERE name=?", (name,))
    store.log_add(by.person_name, "project_removed", name)
    return {"grants": grants, "requests": requests, "folders": folders}


# a member's own view

def my_access(store, ws, principal: Principal) -> dict:
    return {
        "projects": [{"name": p["name"], "role": p["role"], "description": p["description"]}
                     for p in store.projects_for(principal)],
        "machines": [{"host": m["host"], "owner": m["owner"],
                      "shared": any(d["shared"] for d in m["devices"])}
                     for m in store.machines(principal)],
        "requests": [{**r, "status": "pending"} for r in store.pending_for(principal.person_id)],
    }


# the log in words

def _grant(who, d):
    m = re.fullmatch(r"project:([^:]+):(\w+) -> (.+)", d)
    if m:
        return f"{who} gave {m[3]} {m[2]} access to {m[1]}"
    m = re.fullmatch(r"machine:(.+) -> (.+)", d)
    if m:
        return f"{who} let {m[2]} use the machine {m[1]}"
    return None


def _revoke(who, d):
    m = re.fullmatch(r"project:([^:]+) -> (.+)", d)
    if m:
        return f"{who} took {m[2]}'s access to {m[1]}"
    m = re.fullmatch(r"machine:(.+) -> (.+)", d)
    if m:
        return f"{who} took {m[2]}'s use of the machine {m[1]}"
    m = re.fullmatch(r"device (.+) of (.+)", d)
    if m:
        return f"{who} turned off {m[2]}'s device {m[1]}"
    return None


def _match(pattern: str, template: str):
    """A sentence maker: `template` gets the person, then the pattern's groups."""
    def make(who, d):
        m = re.fullmatch(pattern, d)
        return template.format(who, *m.groups()) if m else None
    return make


SENTENCES = {
    "grant": _grant,
    "revoke": _revoke,
    "person": _match(r"(.+) created as (\w+)", "{0} added {1} with the role {2}"),
    "device": _match(r"device (.+) of (.+)", "{0} added a device {1} for {2}"),
    "invite": _match(r"invite for (.+)", "{0} made an invite link for {1}"),
    "invite_cancel": _match(r"invite for (.+)", "{0} cancelled the invite link for {1}"),
    "join": _match(r"device (.+)", "{0} joined with the device {1}"),
    "project_register": _match(r"(.+)", "{0} registered the project {1}"),
    "project_updated": _match(r"(.+)", "{0} changed the description of {1}"),
    "project_removed": _match(r"(.+)", "{0} removed the project {1}"),
    "person_removed": _match(r"(.+)", "{0} removed {1} from the team"),
    "role": _match(r"(.+) -> (\w+)", "{0} made {1} a {2}"),
    "page_login": _match(r"(.*)", "{0} opened the page ({1})"),
    "request": _match(r"(\w+):(.+)", "{0} asked for access to the {1} {2}"),
    "request_decided": _match(r"(\w+):(.+) -> (.+): (\w+).*",
                              "{0} {4} {3}'s request for the {1} {2}"),
    "work_decide": _match(r"request (\d+): (.+)", "{0} decided work request {1}: {2}"),
    "manager": _match(r"(.+)", "The manager session: {1}"),
    "rules": _match(r"team rules v(\d+).*", "{0} saved team rules version {1}"),
    "skills": _match(r"(.*)", "{0} updated the team skills ({1})"),
    "proposal": _match(r"proposal (\d+) (\w+)", "{0} {2} rule proposal {1}"),
    "conflict": _match(r"conflict (\d+) in (.+)", "{0} reported a rule conflict ({1}) in {2}"),
    "backup": _match(r"(.*)", "{0} made a backup: {1}"),
}


def log_sentence(row: dict) -> str:
    """One log row as a plain sentence; a kind without a sentence reads `kind: detail`."""
    kind, detail = row.get("kind", ""), row.get("detail", "")
    make = SENTENCES.get(kind)
    text = make(row.get("person", ""), detail) if make else None
    return text or f"{kind}: {detail}"
