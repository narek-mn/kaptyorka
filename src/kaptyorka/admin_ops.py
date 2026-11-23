"""The admin's changes to people, projects and invites; a member's own access; the log read
as plain sentences."""
import json
import re
from typing import Dict, List, Optional, Tuple

from kaptyorka import paths
from kaptyorka.store import MAX_ABOUT, ONLINE_S, KaptyorkaError, Principal, machine_name
from kaptyorka.workers import device_tools

LIVE = ("starting", "up", "unattended", "abandoned")  # workers.LIVE; workers imports the store
MANAGER = "manager"  # manager.PERSON; manager imports the store


# every column that holds a page key `web:<person name>`; a removed person's are renamed with them
WEB_KEYS = [("messages", "sender_key"), ("messages", "to_key"), ("workers", "boss_key"),
            ("doors", "a_key"), ("doors", "b_key"), ("knocks", "from_key"), ("knocks", "to_key"),
            ("work_requests", "boss_key"), ("state_writes", "key"), ("done_checks", "key"),
            ("proposals", "session_key"), ("conflicts", "session_key"),
            ("connects", "from_key")]


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
    """{person id: name} for everyone, removed people too: their old sessions keep a name. A team
    machine reads "<name> (team)"."""
    with store.lock:
        return {r["id"]: r["name"] + (" (team)" if r["team"] else "")
                for r in store.db.execute("SELECT id, name, team FROM people")}


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
            raise KaptyorkaError(f"{name} is an admin; remove their admin role first")
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
    db.execute("DELETE FROM team_machine_hidden WHERE person_id=? OR team_person_id=?", (pid, pid))
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


ROLE_CHOICES = ("admin", "member", "manager")


def admins(store) -> List[str]:
    """The names of the hub's admins, oldest first."""
    with store.lock:
        return [r["name"] for r in store.db.execute(
            "SELECT name FROM people WHERE role='admin' AND removed_at IS NULL ORDER BY id")]


def set_role(store, name: str, role: str, by: Principal) -> dict:
    """Give a person a role. Several admins may be; the last one cannot go, nor, while sign-in
    with GitLab is on, the last one linked to GitLab, so the page stays usable. The hub's own
    manager account keeps its role. The hub key keeps its rights whatever its person's role.
    Returns {"role", "note"}; the note is "" (kept for the command line)."""
    _admin(by)
    if role not in ROLE_CHOICES:
        raise KaptyorkaError("role must be admin, member or manager")
    from kaptyorka import signin  # signin imports the store too; only needed here

    gitlab_on = signin.status(store) is None
    with store.lock:
        db = store.db
        row = _live_person(db, name)
        name, old = row["name"], row["role"]
        if name == MANAGER and old == "manager":
            raise KaptyorkaError("the hub's manager account keeps its role")
        if row["team"]:
            raise KaptyorkaError(f"{name} is a team machine; it has no role to change")
        if old == role:
            return {"role": role, "note": ""}
        if role == "admin":
            on = db.execute("SELECT value FROM settings WHERE key='legacy_enabled'").fetchone()
            mapped = db.execute("SELECT key FROM settings WHERE key LIKE 'legacy:%' AND value=?",
                                (str(row["id"]),)).fetchone()
            if mapped is not None and (on is None or on["value"] != "0"):
                # anyone holding the shared token on that machine would be an admin
                raise KaptyorkaError(f"{name} still uses the old shared token on"
                                     f" {mapped['key'][len('legacy:'):]}; turn the shared token"
                                     " off first")
        if old == "admin" and _last_admin(db, row["id"]):
            raise KaptyorkaError(("you are" if row["id"] == by.person_id else f"{name} is")
                                 + " the last admin; make someone else an admin first")
        if old == "admin" and gitlab_on and row["gitlab_user"] and db.execute(
                "SELECT 1 FROM people WHERE role='admin' AND removed_at IS NULL AND id<>?"
                " AND gitlab_user IS NOT NULL AND gitlab_user<>''", (row["id"],)).fetchone() is None:
            raise KaptyorkaError(("you are" if row["id"] == by.person_id else f"{name} is")
                                 + " the last admin who signs in with GitLab; make someone with"
                                 " GitLab an admin first")
        db.execute("UPDATE people SET role=? WHERE id=?", (role, row["id"]))
    if role == "admin":
        store.log_add(by.person_name, "admin_given", name)
    elif old == "admin":
        store.log_add(by.person_name, "admin_taken", name)
    if role != "admin" and old != "admin" or role == "manager":
        store.log_add(by.person_name, "role", f"{name} -> {role}")
    return {"role": role, "note": ""}


def _last_admin(db, pid) -> bool:
    """Lock held. True when no other admin person (team machines never count) is left."""
    return db.execute("SELECT 1 FROM people WHERE role='admin' AND removed_at IS NULL AND team=0"
                      " AND id<>?", (pid,)).fetchone() is None


def make_team_machine(store, name: str, by: Principal) -> dict:
    """Turn a person into a team machine, such as the person the hub made at install for its own
    OS account: not a human. Their devices stay and now speak for the team machine (the hub key
    keeps its rights); their admin role goes, and so does their ownership of hosts. Refused for
    someone who signs in with GitLab, and while the old shared token speaks for them."""
    _admin(by)
    with store.lock:
        db = store.db
        row = _live_person(db, name)
        pid, name = row["id"], row["name"]
        if row["team"]:
            raise KaptyorkaError(f"{name} is already a team machine")
        if name == MANAGER and row["role"] == "manager":
            raise KaptyorkaError("the hub's manager account stays as it is")
        if row["gitlab_user"]:
            raise KaptyorkaError(f"{name} signs in with GitLab, so is a person; unlink their"
                                 " GitLab name first")
        if db.execute("SELECT 1 FROM gitlab_sessions g JOIN devices d ON d.id=g.device_id"
                      " WHERE g.person_id=? AND d.revoked_at IS NULL", (pid,)).fetchone():
            raise KaptyorkaError(f"{name} has browsers signed in with GitLab, so is a person")
        on = db.execute("SELECT value FROM settings WHERE key='legacy_enabled'").fetchone()
        if (on is None or on["value"] != "0") and (db.execute(
                "SELECT 1 FROM devices WHERE person_id=? AND legacy=1 AND revoked_at IS NULL",
                (pid,)).fetchone() or db.execute(
                "SELECT 1 FROM settings WHERE key LIKE 'legacy:%' AND value=?",
                (str(pid),)).fetchone()):
            raise KaptyorkaError(f"the old shared token still speaks for {name}; turn it off"
                                 " first: kaptyorka shared-token off")
        if row["role"] == "admin" and _last_admin(db, pid):
            raise KaptyorkaError(("you are" if pid == by.person_id else f"{name} is")
                                 + " the last admin; make someone else an admin first")
        owned = [r["host"] for r in db.execute("SELECT host FROM machines WHERE"
                                               " owner_person_id=? ORDER BY host", (pid,))]
        db.execute("UPDATE machines SET owner_person_id=NULL WHERE owner_person_id=?", (pid,))
        db.execute("UPDATE people SET team=1, role='member' WHERE id=?", (pid,))
    store.log_add(by.person_name, "team_machine_from", name)
    return {"name": name, "was": row["role"], "machines_unowned": owned}


def undo_team_machine(store, name: str, by: Principal) -> dict:
    """make_team_machine backwards: the team machine is a person again, with the member role.
    Its devices stop counting as shared, so they host no one else's workers; who it was hidden
    from is forgotten. A former admin role and host ownership are not remembered: give them back
    with `kaptyorka people role` and `kaptyorka machine owner`. Refused while another person's
    worker runs on it (close those first: its bridge would stop reporting them)."""
    _admin(by)
    with store.lock:
        db = store.db
        row = _live_person(db, name)
        pid, name = row["id"], row["name"]
        if not row["team"]:
            raise KaptyorkaError(f"{name} is not a team machine")
        busy = [r["name"] for r in db.execute(
            "SELECT w.name FROM workers w JOIN devices d ON d.id=w.device_id WHERE d.person_id=?"
            " AND w.boss_person<>? AND w.state IN ('starting','up','unattended','abandoned')"
            " ORDER BY w.name", (pid, pid))]
        if busy:
            raise KaptyorkaError(f"other people's workers run on {name} ({', '.join(busy)});"
                                 " close them first")
        db.execute("UPDATE people SET team=0, role='member' WHERE id=?", (pid,))
        db.execute("DELETE FROM team_machine_hidden WHERE team_person_id=?", (pid,))
    store.log_add(by.person_name, "team_machine_undo", name)
    return {"name": name}


# invites

def invite_expires(store, code: str) -> Optional[float]:
    with store.lock:
        row = store.db.execute("SELECT expires FROM invites WHERE code=?", (code,)).fetchone()
    return row["expires"] if row else None


def invites_open(store) -> List[dict]:
    """Unexpired invites of people still on the team that can still do something, oldest first:
    unused ones (they join a machine), and used ones while their person has no GitLab link (they
    can still link one). `used` tells them apart."""
    with store.lock:
        rows = store.db.execute(
            "SELECT i.code, p.name AS person, i.created, i.expires, i.used_at IS NOT NULL AS used"
            " FROM invites i JOIN people p ON p.id=i.person_id WHERE i.expires>?"
            " AND p.removed_at IS NULL AND (i.used_at IS NULL OR p.gitlab_user IS NULL)"
            " ORDER BY i.created", (store.clock(),)).fetchall()
    return [{**dict(r), "used": bool(r["used"])} for r in rows]


def invite_cancel(store, code: str, by: Principal) -> None:
    """Delete an unexpired invite, used or not: a used one may still link a GitLab account, and
    has no other job, so deleting it costs nothing."""
    _admin(by)
    with store.lock:
        row = store.db.execute("SELECT p.name FROM invites i JOIN people p ON p.id=i.person_id"
                               " WHERE i.code=? AND i.expires>?", (code, store.clock())).fetchone()
        if row is None:
            raise KaptyorkaError("no open invite with that code")
        store.db.execute("DELETE FROM invites WHERE code=?", (code,))
    store.log_add(by.person_name, "invite_cancel", f"invite for {row['name']}")


def invite_renew(store, name: str, by: Principal) -> str:
    """The Team row's New invite link: the person's unexpired invites, used or not, stop
    working, and a fresh one is made."""
    _admin(by)
    with store.lock:
        row = _live_person(store.db, name)
        store.db.execute("DELETE FROM invites WHERE person_id=? AND expires>?",
                         (row["id"], store.clock()))
    return store.invite_create(row["id"], by=by.person_name, log_kind="invite_new")


SELF_INVITES = 3  # unused invites a person may hold at once from Add a machine
SELF_INVITE_TTL_S = 86400  # a link to add a machine is for now: it ends after a day


def can_add_machine(principal: Principal) -> bool:
    return not (principal.legacy or principal.shared
                or (principal.person_name, principal.role) == (MANAGER, MANAGER))


def invite_self(store, principal: Principal) -> str:
    """Add a machine: an invite for the caller's own person, never anyone else's."""
    if not can_add_machine(principal) or store.is_team(principal.person_id):
        raise KaptyorkaError("this sign-in cannot add a machine; ask the admin")
    return store.invite_create(principal.person_id, ttl_s=SELF_INVITE_TTL_S,
                               by=principal.person_name, log_kind="invite_self",
                               limit=SELF_INVITES)


def emergency_device(store) -> Optional[int]:
    """The hub key: the device whose token is in ~/.kaptyorka/admin-token on this (the hub)
    machine. It has admin rights whatever its person's role and still opens the page while GitLab
    is down. Its id while it is live, else None."""
    digest = paths.hub_key_digest()
    if digest is None:
        return None
    with store.lock:
        row = store.db.execute("SELECT id FROM devices WHERE token_hash=? AND revoked_at IS NULL"
                               " AND legacy=0", (digest,)).fetchone()
    return row["id"] if row else None


_MACHINE = "revoked_at IS NULL AND expires IS NULL AND legacy=0"  # a joined machine, still in
_BROWSER = ("revoked_at IS NULL AND id IN (SELECT device_id FROM gitlab_sessions)")  # a GitLab sign-in


def account(store, principal: Principal, ws=None) -> dict:
    """The account page: the caller's own person, machines, browsers signed in with GitLab and
    projects, nothing of anyone else's. `current` marks the device this page signed in with.
    With `ws` (a WorkerStore) each machine has its computer's GPU queue line (other people's
    names for an admin only, counts for anyone else)."""
    now, emergency = store.clock(), emergency_device(store)
    with store.lock:
        p = store.db.execute("SELECT name, role, gitlab_user FROM people WHERE id=?",
                             (principal.person_id,)).fetchone()
        devices = store.db.execute(
            f"SELECT id, name, label, os_user, host, last_used FROM devices WHERE person_id=? AND {_MACHINE}"
            " ORDER BY name", (principal.person_id,)).fetchall()
        tools = {d["id"]: device_tools(store.db, d["id"]) for d in devices}
        browsers = store.db.execute(
            f"SELECT id, created, last_used FROM devices WHERE person_id=? AND {_BROWSER}"
            " AND (expires IS NULL OR expires>?) ORDER BY created DESC",
            (principal.person_id, now)).fetchall()
        team = _team_machines(store, principal.person_id)
    queue: Dict[str, str] = {}

    def extra(d) -> dict:
        if ws is None:
            return {}
        if d["host"] not in queue:
            queue[d["host"]] = ws.queue_text(d["host"], now, principal.is_admin() and not principal.legacy)
        return {"gpu_queue": queue[d["host"]]}
    return {
        "person": p["name"], "role": p["role"], "gitlab_user": p["gitlab_user"],
        "machines": [{"id": d["id"], "name": machine_name(d["label"], d["os_user"], d["host"], d["name"]),
                      "machine": d["name"], "label": d["label"],
                      "auto": machine_name("", d["os_user"], d["host"], d["name"]),
                      "os_user": d["os_user"], "host": d["host"], "last_seen": d["last_used"],
                      "tools": tools[d["id"]],
                      "online": d["last_used"] is not None and d["last_used"] > now - ONLINE_S,
                      "current": d["id"] == principal.device_id,
                      "emergency": d["id"] == emergency, **extra(d)}
                     for d in devices],
        "browsers": [{"id": b["id"], "created": b["created"], "last_used": b["last_used"],
                      "current": b["id"] == principal.device_id} for b in browsers],
        "team_machines": [{"name": machine_name(d["label"], d["os_user"], d["host"], d["name"]),
                           "os_user": d["os_user"], "host": d["host"], "tools": d["tools"],
                           "last_seen": d["last_used"], "projects": d["projects"],
                           "online": d["last_used"] is not None and d["last_used"] > now - ONLINE_S,
                           **extra(d)}
                          for d in team],
        "projects": [{"name": x["name"], "role": x["role"]} for x in store.projects_for(principal)],
        "add_machine": can_add_machine(principal) and not store.is_team(principal.person_id),
    }


def _team_machines(store, person_id: int) -> List[dict]:
    """Lock held. The team machines the person may use: every joined team machine unless an
    admin hid it from them, with or without sessions. Each comes with only those of its
    projects the person has a role in (their sessions stay behind Store._team_reach)."""
    out = []
    for d in store.db.execute(
            "SELECT d.id, d.name, d.label, d.os_user, d.host, d.last_used, d.person_id FROM devices d"
            " JOIN people p ON p.id=d.person_id WHERE p.team=1 AND p.removed_at IS NULL"
            f" AND d.host!='' AND d.person_id!=? AND {_MACHINE} ORDER BY d.name",
            (person_id,)).fetchall():
        if store._hidden(d["person_id"], person_id):  # an admin turned it off for them
            continue
        projects = [r["project"] for r in store.db.execute(
            "SELECT DISTINCT project FROM agents WHERE person_id=? ORDER BY project", (d["person_id"],))
            if store._role(r["project"], person_id) is not None]
        out.append({**dict(d), "projects": projects, "tools": device_tools(store.db, d["id"])})
    return out


def _own_machine(store, principal: Principal, device_id: int):
    """One of the caller's own joined machines; anyone else's reads as missing."""
    row = store.db.execute(f"SELECT * FROM devices WHERE id=? AND person_id=? AND {_MACHINE}",
                           (device_id, principal.person_id)).fetchone()
    if row is None:
        raise KaptyorkaError("no such machine of yours")
    return row


def machine_label(store, principal: Principal, device_id: int, label: str) -> str:
    """Rename one of your machines on the page ("" goes back to its own name). Only the label
    changes: the machine name its sessions and bridge use stays."""
    label = " ".join(label.split())
    if len(label) > 80:
        raise KaptyorkaError("a machine name is at most 80 characters")
    with store.lock:
        row = _own_machine(store, principal, device_id)
        store.db.execute("UPDATE devices SET label=? WHERE id=?", (label, device_id))
    shown = row["label"] or row["name"]
    store.log_add(principal.person_name, "machine_label", f"{shown} -> {label or row['name']}")
    return machine_name(label, row["os_user"], row["host"], row["name"])


def machine_remove(store, principal: Principal, device_id: int) -> None:
    """Remove one of your machines: its token stops working until it joins again. Not the one
    this page signed in with (the admin token): that would lock the page out."""
    if device_id == principal.device_id:
        raise KaptyorkaError("this page is signed in with that machine's token; remove it from"
                             " another sign-in")
    if device_id == emergency_device(store):
        raise KaptyorkaError("this is the hub's own emergency sign-in; it can't be removed here")
    with store.lock:
        _own_machine(store, principal, device_id)
    store.device_revoke(device_id, by=principal.person_name)


def browsers_other(store, si, principal: Principal) -> int:
    """Sign out everywhere else: every GitLab sign-in of yours but this page's ends."""
    with store.lock:
        ids = [r["id"] for r in store.db.execute(
            f"SELECT id FROM devices WHERE person_id=? AND {_BROWSER} AND id<>?",
            (principal.person_id, principal.device_id))]
    for d in ids:
        si._end(d, "signed out from another browser")
    if ids:
        store.log_add(principal.person_name, "browsers_out", str(len(ids)))
    return len(ids)


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


def purge_personal(store) -> Dict[str, int]:
    """Delete what the hub holds from sessions outside the team projects: their rows in agents,
    the messages to or from them with their files, their knocks, doors, connection requests and
    done checks, and the history of folders that are no project at all (git facts, state writes,
    conflicts, finished workers, work requests, learned folders). Run at hub start and
    when a project is removed. The manager and the hub's live workers stay; the notes of a
    registered or removed project are never touched."""
    from kaptyorka import paths

    db, deleted = store.db, {}
    with store.lock:
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        team = {r["name"] for r in db.execute("SELECT name FROM projects WHERE archived_at IS NULL")}
        known = {r["name"] for r in db.execute("SELECT name FROM projects")}  # archived too
        mid = db.execute("SELECT id FROM people WHERE name=? AND role='manager'",
                         (MANAGER,)).fetchone()
        mid = mid["id"] if mid else None
        marks = ",".join("?" * len(LIVE))
        keep = {r["key"] for r in db.execute(
            f"SELECT key FROM workers WHERE key IS NOT NULL AND state IN ({marks})", LIVE)} \
            if "workers" in tables else set()

        def no_project(p) -> bool:  # neither a team project nor a removed one: a personal folder
            return p is not None and p not in (MANAGER, paths.COORDINATOR) \
                and paths.team_name(p) not in known

        keys = [r["key"] for r in db.execute("SELECT key, project, person_id FROM agents")
                if paths.team_name(r["project"]) not in team and r["key"] not in keep
                and r["project"] != paths.COORDINATOR  # joined with /hub team: kept
                and (mid is None or r["person_id"] != mid)]

        def by_keys(table: str, *cols: str) -> int:
            n = 0
            for i in range(0, len(keys), 400):
                chunk = keys[i:i + 400]
                q = ",".join("?" * len(chunk))
                n += db.execute(f"DELETE FROM {table} WHERE " + " OR ".join(
                    f"{c} IN ({q})" for c in cols), chunk * len(cols)).rowcount
            return n

        def by_project(table: str, extra: str = "") -> int:
            n = 0
            for r in db.execute(f"SELECT DISTINCT project FROM {table} WHERE project IS NOT NULL"
                                + extra).fetchall():
                if no_project(r["project"]):
                    n += db.execute(f"DELETE FROM {table} WHERE project=?" + extra,
                                    (r["project"],)).rowcount
            return n

        # messages: to or from such a session, or to a session of a folder that is no project
        ids = set()
        for i in range(0, len(keys), 400):
            chunk = keys[i:i + 400]
            q = ",".join("?" * len(chunk))
            ids |= {r["id"] for r in db.execute(
                f"SELECT id FROM messages WHERE sender_key IN ({q}) OR to_key IN ({q})", chunk * 2)}
        ids |= {r["id"] for r in db.execute(
            "SELECT id, project FROM messages WHERE to_key NOT LIKE 'web:%'"
            " AND to_key NOT LIKE 'hub:%'") if no_project(r["project"])}
        files = set()
        for fid in ids:
            files |= {r["id"] for r in db.execute("SELECT id FROM files WHERE message_id=?", (fid,))}
            db.execute("DELETE FROM messages WHERE id=?", (fid,))
        for fid in files:
            db.execute("DELETE FROM files WHERE id=?", (fid,))
            try:
                (paths.files_dir() / str(fid)).unlink()
            except FileNotFoundError:
                pass
        deleted.update(messages=len(ids), files=len(files))
        deleted["knocks"] = by_keys("knocks", "from_key", "to_key")
        deleted["doors"] = by_keys("doors", "a_key", "b_key")
        if "connects" in tables:
            deleted["connects"] = by_keys("connects", "from_key", "to_key")
        if "done_checks" in tables:
            deleted["done_checks"] = by_keys("done_checks", "key") + by_project("done_checks")
        for table in ("git_facts", "state_writes", "conflicts", "work_requests"):
            if table in tables:
                deleted[table] = by_project(table)
        if "workers" in tables:
            deleted["workers"] = by_project("workers", " AND manager=0"
                                                       " AND state IN ('closed','failed')")
        deleted["agents"] = by_keys("agents", "key")
        if "device_facts" in tables:
            n = 0
            for r in db.execute("SELECT device_id, facts, folders FROM device_facts").fetchall():
                folders = json.loads(r["folders"] or "{}")
                facts = json.loads(r["facts"] or "{}")
                kept = {p: f for p, f in folders.items() if paths.team_name(p) in team}
                git = facts.get("git") if isinstance(facts.get("git"), dict) else {}
                git_kept = {c: g for c, g in git.items() if c in kept.values()
                            or paths.team_name(c.rstrip("/").rsplit("/", 1)[-1]) in team}
                if kept != folders or git_kept != git:
                    if "git" in facts:
                        facts["git"] = git_kept
                    db.execute("UPDATE device_facts SET facts=?, folders=? WHERE device_id=?",
                               (json.dumps(facts), json.dumps(kept), r["device_id"]))
                    n += 1
            deleted["device_facts"] = n
    return deleted


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


def _match(pattern: str, template: str, flags: int = 0):
    """A sentence maker: `template` gets the person, then the pattern's groups."""
    def make(who, d):
        m = re.fullmatch(pattern, d, flags)
        return template.format(who, *m.groups()) if m else None
    return make


def _side(key: str) -> str:
    """A door end: a session name as is, the page as its person's name."""
    return key[4:] if key.startswith("web:") else key


def _door_open(who, d):
    m = re.fullmatch(r"group #\d+ (.+) <-> (.+)", d)
    if m:
        return f"{who} opened a door between {m.group(1)} and {m.group(2)}"
    m = re.fullmatch(r"(.+) <-> (.+)", d)
    return f"{who} opened a door between {_side(m.group(1))} and {_side(m.group(2))}" if m else None


def _door_close(who, d):
    m = re.fullmatch(r"#(\d+)", d)
    if m:
        return f"{who} closed door {m.group(1)}"
    m = re.fullmatch(r"(.+) -> (.+)", d)
    return f"{who} closed the door between {m.group(1)} and {m.group(2)}" if m else None


def _knock_answer(who, d):
    m = re.fullmatch(r"#(\d+) (yes|no) (?:by .+|on the web page)", d)
    if not m:
        return None
    return f"{who} {'said yes to' if m.group(2) == 'yes' else 'declined'} knock {m.group(1)}"


def _connect_request(who, d):
    m = re.fullmatch(r"#(\d+) (.+) -> (.+)", d)
    if not m:
        return None
    side = "their page" if m.group(2).startswith("web:") else m.group(2)
    return f"{who} asked to connect {side} to {m.group(3)} (request {m.group(1)})"


def _connect_decided(who, d):
    m = re.fullmatch(r"#(\d+) (.+): (approved|denied|offline)", d)
    if not m:
        return None
    if m.group(3) == "offline":
        return (f"{who} approved {m.group(2)}'s connection request {m.group(1)}, but a session"
                " was offline, so no door opened")
    return f"{who} {m.group(3)} {m.group(2)}'s connection request {m.group(1)}"


def _auto_update(who, d):
    m = re.fullmatch(r"(.+) (on|off|default)", d)
    if not m:
        return None
    if m.group(2) == "default":
        return f"{who} set auto-update for {m.group(1)} back to the hub's default"
    return f"{who} turned auto-update {m.group(2)} for {m.group(1)}"


SENTENCES = {
    "grant": _grant,
    "revoke": _revoke,
    "person": _match(r"(.+) created as (\w+)", "{0} added {1} with the role {2}"),
    "device": _device_added,
    "invite": _match(r"invite for (.+)", "{0} made an invite link for {1}"),
    "invite_new": _match(r"invite for (.+)", "{0} made a new invite link for {1}; the old ones"
                                              " no longer work"),
    "machine_label": _match(r"(.+) -> (.+)", "{0} renamed their machine {1} to {2}"),
    "browsers_out": lambda who, d: f"{who} signed out their other browsers ({d})",
    "invite_self": _match(r"invite for (.+)", "{0} made a link to add a machine"),
    "invite_cancel": _match(r"invite for (.+)", "{0} cancelled the invite link for {1}"),
    "device_shared": _device_shared,
    "join": _match(r"device (.+)", "{0} joined with the device {1}"),
    "project_register": _match(r"(.+)", "{0} registered the project {1}"),
    "project_updated": _match(r"(.+)", "{0} changed the description of {1}"),
    "project_removed": _match(r"(.+)", "{0} removed the project {1}"),
    "gitlab": lambda who, d: (f"{who} turned the GitLab project list off" if d == "off" else
                              _match(r"(\S+) (\S+)", "{0} set the GitLab project list to the"
                                                       " group {2} on {1}")(who, d)),
    "gitlab_user": lambda who, d: (_match(r"(.+) unlinked", "{0} unlinked {1} from GitLab")(who, d)
                                   if d.endswith(" unlinked") else
                                   _match(r"(.+) -> (.+)", "{0} linked {1} to the GitLab user {2}")(who, d)),
    "gitlab_link": _match(r"(.+)", "{0} linked the GitLab user {1}"),
    "gitlab_signin": _match(r"(.+)", "{0} signed in to the page with GitLab ({1})"),
    "gitlab_signout": _match(r"(.+)", "{0}'s GitLab sign-in ended: {1}"),
    "person_removed": _match(r"(.+)", "{0} removed {1} from the team"),
    "role": _match(r"(.+) -> (\w+)", "{0} made {1} a {2}"),
    "admin_given": _match(r"(.+)", "{0} made {1} an admin"),
    "admin_taken": _match(r"(.+)", "{0} removed {1}'s admin role"),
    "team_machine": _match(r"(.+)", "{0} added the team machine {1}"),
    "team_machine_from": _match(r"(.+)", "{0} made {1} a team machine"),
    "team_machine_undo": _match(r"(.+)", "{0} made {1} a person again"),
    "machine_move": _match(r"(.+): (.+) -> (.+)", "{0} moved the machine {1} from {2} to {3}"),
    "team_machine_hidden": _match(r"(.+) -> (.+)", "{0} hid {1} from {2}"),
    "team_machine_shown": _match(r"(.+) -> (.+)", "{0} showed {1} to {2}"),
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
    "door_open": _door_open,
    "door_close": _door_close,
    "knock": _match(r"#(\d+) (.+) -> (.+)", "{0} knocked for {3} (knock {1})"),
    "knock_answer": _knock_answer,
    "connect_request": _connect_request,
    "connect_decided": _connect_decided,
    "propose": _match(r"proposal (\d+): (.*)", "{0} proposed a rule: {2}", re.S),
    "legacy_toggle": _match(r"(on|off)", "{0} switched the shared token {1}"),
    "legacy_assign": _match(r"(.+) -> (.+)", "{0} gave {1} (old shared token) to {2}"),
    "legacy_used": _match(r"(.+)", "{1} still uses the old shared token"),
    "rekeyed": _match(r"(.+)", "{1} took its own token in place of the shared one"),
    "auto_update": _auto_update,
    "settings": _match(r"retention_days=(\d+)", "{0} set message keeping to {1} days"),
    "machine_owner": _match(r"(.+) -> (.+)", "{0} made {2} the owner of {1}"),
    "machine_owner_auto": _match(r"(.+)", "{0} owns {1}: the first to join from it"),
    "gpu_queue": lambda who, d: (_match(r"(.+): off", "{0} turned off the GPU queue of {1}")(who, d)
                                 if d.endswith(": off") else
                                 _match(r"(.+?): (.+) \((\d+) shares\)",
                                        "{0} set the GPU queue of {1} to {2} ({3} shares)")(who, d)),
    "machine_in_use": _match(r"(.+?) on (\S+): (.+)",
                             "{1} opened a session on {2}, which someone else uses heavily: {3}",
                             re.S),
    "legacy_auto_off": _match(r"(\d+) days", "The shared token switched itself off: no machine used it for {1} days"),
}

KNOWN_KINDS = tuple(SENTENCES)

# the plain groups of the log page, in order, and the raw kinds each covers
LOG_GROUPS = [
    ("people", ["person", "person_removed", "role", "admin_given", "admin_taken", "team_machine", "team_machine_from", "team_machine_undo", "invite", "invite_new", "invite_self",
                "machine_label", "browsers_out",
                "invite_cancel", "device",
                "device_shared", "join", "gitlab_user", "gitlab_link"]),
    ("access", ["grant", "revoke", "request", "request_decided", "connect_request",
                "connect_decided"]),
    ("projects", ["project_register", "project_updated", "project_removed", "gitlab"]),
    ("notes", ["note_delete", "note_undo"]),
    ("machines", ["machine_move", "team_machine_hidden", "team_machine_shown", "machine_owner", "machine_owner_auto", "machine_in_use", "gpu_queue", "legacy_auto_off", "legacy_toggle", "legacy_assign", "legacy_used", "rekeyed",
                  "backup", "auto_update"]),
    ("messages", ["settings", "retention", "door_open", "door_close", "knock", "knock_answer"]),
    ("rules", ["rules", "skills", "propose", "proposal", "conflict"]),
    ("workers", ["manager", "work_decide"]),
    ("sign-ins", ["page_login", "gitlab_signin", "gitlab_signout"]),
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
