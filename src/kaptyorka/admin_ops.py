"""The admin's changes to people, projects and invites; a member's own access; the log read
as plain sentences."""
import json
import re
from typing import Dict, List, Optional, Tuple

from kaptyorka.store import MAX_ABOUT, KaptyorkaError, Principal

LIVE = ("starting", "up", "unattended", "abandoned")  # workers.LIVE; workers imports the store
MANAGER = "manager"  # manager.PERSON; manager imports the store


# every column that holds a page key `web:<person name>`; a removed person's are renamed with them
WEB_KEYS = [("messages", "sender_key"), ("messages", "to_key"), ("workers", "boss_key"),
            ("doors", "a_key"), ("doors", "b_key"), ("knocks", "from_key"), ("knocks", "to_key"),
            ("work_requests", "boss_key"), ("state_writes", "key"), ("done_checks", "key"),
            ("proposals", "session_key"), ("conflicts", "session_key")]


def ensure_schema(db) -> None:
    for table, col in (("people", "removed_at"), ("projects", "archived_at"),
                       ("notes", "deleted_at")):
        if col not in [r[1] for r in db.execute(f"PRAGMA table_info({table})")]:
            db.execute(f"ALTER TABLE {table} ADD COLUMN {col} REAL")


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
        new = f"{name} (removed)"
        if db.execute("SELECT 1 FROM people WHERE name=?", (new,)).fetchone():
            new = f"{new} #{pid}"
        db.execute("BEGIN IMMEDIATE")  # all of it or none of it
        try:
            counts = _remove(store, pid, name, new, now)
            db.execute("COMMIT")
        except BaseException:
            db.execute("ROLLBACK")
            raise
    workers, still_running = 0, []
    with ws.lock:  # the close order a person's close sends; then the row says why
        for w in ws.db.execute(f"SELECT * FROM workers WHERE boss_person=? AND state IN"
                               f" ({','.join('?' * len(LIVE))})", (pid, *LIVE)).fetchall():
            if "may still run" in ws._close(w, w["name"]).get("note", ""):
                still_running.append({"worker": w["name"], "host": w["host"]})
            ws.db.execute("UPDATE workers SET state='closed', error='boss removed',"
                          " closed_at=COALESCE(closed_at, ?) WHERE name=?", (now, w["name"]))
            workers += 1
    store.log_add(by.person_name, "person_removed", name)
    return {"devices": counts[0], "workers": workers, "doors": counts[1], "grants": counts[2],
            "requests": counts[3], "work_requests": counts[4], "machines_unowned": counts[5],
            "still_running": still_running}


def _remove(store, pid: int, name: str, new: str, now: float) -> tuple:
    """remove_person's writes, under the lock: (devices, doors, grants, requests, work requests,
    hosts they owned)."""
    db = store.db
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
    work = db.execute("UPDATE work_requests SET decided_at=?, decision='person removed'"
                      " WHERE person_id=? AND decided_at IS NULL", (now, pid)).rowcount
    owned = [r["host"] for r in db.execute("SELECT host FROM machines WHERE owner_person_id=?"
                                           " ORDER BY host", (pid,))]
    db.execute("UPDATE machines SET owner_person_id=NULL WHERE owner_person_id=?", (pid,))
    db.execute("UPDATE people SET removed_at=?, name=? WHERE id=?", (now, new, pid))
    for table, col in WEB_KEYS:  # a new person of the old name must not get their page rows
        db.execute(f"UPDATE {table} SET {col}=? WHERE {col}=?", (f"web:{new}", f"web:{name}"))
    return devices, doors, grants, requests, work, owned


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
    if db.execute("SELECT 1 FROM projects WHERE name=? AND archived_at IS NULL",
                  (name,)).fetchone() is None:
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
    """Archive a project: its grants, pending requests and the folder each machine learned for
    it go. Its notes stay, readable by the admin alone; registering the name again restores it."""
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
        db.execute("UPDATE projects SET archived_at=? WHERE name=?", (store.clock(), name))
    store.log_add(by.person_name, "project_removed", name)
    return {"grants": grants, "requests": requests, "folders": folders}


# notes: delete and undo

UNDO_S = 600  # a deleted note can come back this long


def may_delete_note(store, principal: Principal, project: Optional[str], topic: str,
                    undo: bool = False) -> None:
    """Raise unless the principal may delete (or undo the deletion of) this note. Who started
    a team note is read from its live versions, or for an undo from the deleted ones."""
    if principal.legacy or principal.person_id is None:
        raise KaptyorkaError("use your own device token to delete notes")
    project = project or None
    with store.lock:
        if project is not None:
            store._check_project(project, principal, write=True)
            return
        if principal.is_admin():
            return
        stamp = _last_deleted(store, project, topic)
        first = store.db.execute(
            "SELECT author FROM notes WHERE project IS NULL AND topic=? AND deleted_at IS ?"
            " ORDER BY id LIMIT 1", (topic, stamp if undo else None)).fetchone()
    if first is None or first["author"] != principal.person_name:
        raise KaptyorkaError("only the admin or the person who started a team note can delete it")


def _last_deleted(store, project: Optional[str], topic: str) -> Optional[float]:
    """When the note was last deleted, if that was within the undo time. Under the lock."""
    row = store.db.execute("SELECT MAX(deleted_at) AS t FROM notes WHERE topic=? AND project IS ?"
                           " AND deleted_at>=?", (topic, project, store.clock() - UNDO_S)).fetchone()
    return row["t"]


def _note_name(project: Optional[str], topic: str) -> str:
    return f"{project or 'team'}:{topic}"


def note_delete(store, principal: Principal, topic: str, project: Optional[str]) -> int:
    """Hide every version of a note; it can come back for ten minutes, then it is purged."""
    project = project or None
    may_delete_note(store, principal, project, topic)
    with store.lock:
        n = store.db.execute("UPDATE notes SET deleted_at=? WHERE topic=? AND project IS ? AND"
                             " deleted_at IS NULL", (store.clock(), topic, project)).rowcount
    if n == 0:
        raise KaptyorkaError(f"no note named {topic}")
    store.log_add(principal.person_name, "note_delete", _note_name(project, topic))
    return n


def note_undo(store, principal: Principal, topic: str, project: Optional[str]) -> int:
    """Bring back the versions of the last deletion of a note, within ten minutes."""
    project = project or None
    may_delete_note(store, principal, project, topic, undo=True)
    with store.lock:
        if store.db.execute("SELECT 1 FROM notes WHERE topic=? AND project IS ? AND"
                            " deleted_at IS NULL", (topic, project)).fetchone():
            raise KaptyorkaError(f"a new note named {topic} exists;"
                                 " delete it first to bring the old one back")
        stamp = _last_deleted(store, project, topic)
        n = store.db.execute("UPDATE notes SET deleted_at=NULL WHERE topic=? AND project IS ? AND"
                             " deleted_at=?", (topic, project, stamp)).rowcount if stamp else 0
    if n == 0:
        raise KaptyorkaError(f"nothing to undo for {topic}")
    store.log_add(principal.person_name, "note_undo", _note_name(project, topic))
    return n


def purge_deleted_notes(store, now: float) -> int:
    """Remove for good the notes deleted more than ten minutes ago."""
    with store.lock:
        return store.db.execute("DELETE FROM notes WHERE deleted_at<?", (now - UNDO_S,)).rowcount


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
        return f"{who} turned off {_device(m[1], m[2])}"
    return None


LEGACY_DEVICE = "the shared token (old machines)"  # the `legacy` device, as the admin reads it


def _device(name: str, owner: str) -> str:
    return LEGACY_DEVICE if name == "legacy" else f"{owner}'s device {name}"


def _device_added(who, d):
    m = re.fullmatch(r"device (.+) of (.+)", d)
    if m is None:
        return None
    if m[1] == "legacy":
        return f"{who} added {LEGACY_DEVICE} for {m[2]}"
    return f"{who} added a device {m[1]} for {m[2]}"


def _device_shared(who, d):
    m = re.fullmatch(r"device (.+) of (.+) (on|off)", d)
    return f"{who} turned sharing {m[3]} for {_device(m[1], m[2])}" if m else None


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
    "device": _device_added,
    "invite": _match(r"invite for (.+)", "{0} made an invite link for {1}"),
    "invite_cancel": _match(r"invite for (.+)", "{0} cancelled the invite link for {1}"),
    "device_shared": _device_shared,
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
    "note_delete": _match(r"(.+)", "{0} deleted the note {1}"),
    "note_undo": _match(r"(.+)", "{0} brought back the note {1}"),
}

KNOWN_KINDS = tuple(SENTENCES)

# the plain groups of the log page, in order, and the raw kinds each covers
LOG_GROUPS = [
    ("people", ["person", "person_removed", "role", "invite", "invite_cancel", "device",
                "device_shared", "join"]),
    ("access", ["grant", "revoke", "request", "request_decided"]),
    ("projects", ["project_register", "project_updated", "project_removed"]),
    ("machines", ["machine_owner", "legacy_toggle", "legacy_assign", "legacy_used", "rekeyed",
                  "backup", "auto_update"]),
    ("messages", ["settings", "retention", "door_open", "door_close", "knock", "knock_answer",
                  "note_delete", "note_undo"]),
    ("rules", ["rules", "skills", "propose", "proposal", "conflict"]),
    ("workers", ["manager", "work_decide"]),
    ("sign-ins", ["page_login"]),
]


def log_kinds() -> List[Tuple[str, List[str]]]:
    return [(g, list(ks)) for g, ks in LOG_GROUPS]


def log_page(store, limit: int, person: Optional[str], kind: Optional[str],
             before: Optional[int]) -> List[dict]:
    """Log rows newest first with their sentence; `kind` is a group name, `before` a row id."""
    limit = max(1, min(int(limit), 1000))
    groups = dict(LOG_GROUPS)
    sql, args = "SELECT id, time, person, kind, detail FROM log WHERE 1=1", []
    if person:
        sql, args = sql + " AND person=?", args + [person]
    if before:
        sql, args = sql + " AND id<?", args + [int(before)]
    if kind == "other":
        every = [k for ks in groups.values() for k in ks]
        sql, args = sql + f" AND kind NOT IN ({','.join('?' * len(every))})", args + every
    elif kind:
        if kind not in groups:
            raise KaptyorkaError(f"no log group named {kind}")
        sql, args = sql + f" AND kind IN ({','.join('?' * len(groups[kind]))})", args + groups[kind]
    with store.lock:
        rows = store.db.execute(sql + " ORDER BY id DESC LIMIT ?", (*args, limit)).fetchall()
    return [{**dict(r), "sentence": log_sentence(dict(r))} for r in rows]


def log_sentence(row: dict) -> str:
    """One log row as a plain sentence; a kind without a sentence reads `kind: detail`."""
    kind, detail = row.get("kind", ""), row.get("detail", "")
    make = SENTENCES.get(kind)
    text = make(row.get("person", ""), detail) if make else None
    return text or f"{kind}: {detail}"
