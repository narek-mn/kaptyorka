"""What the page shows one viewer: the feed filtered by their person (the admin sees all)."""
from dataclasses import asdict
from typing import Optional

from kaptyorka import __version__, admin_ops
from kaptyorka.manager import ManagerStore
from kaptyorka.store import PRIVATE, Agent, Principal, Store, machine_name, private_name, session_id


def agent_row(a: Agent, now: float) -> dict:
    return {"name": a.name, "machine": a.machine, "tool": a.tool, "project": a.project,
            "status": a.status, "activity": a.activity, "label": a.label,
            "background": a.background, "title": a.title, "last_seen_s": round(now - a.last_seen),
            "idle_s": None if a.active is None else max(0, round(now - a.active))}


def visible(store: Store, principal: Principal, person_id, project: str) -> bool:
    """Your own sessions show in full. An unregistered folder is only a name two people can
    share by chance, so another person's session shows its project only when it is a
    registered project you have a role in."""
    return person_id == principal.person_id or \
        store.project_role(project, principal.person_id) is not None


def session_row(store: Store, a: Agent, principal: Principal, names: dict, now: float,
                workers: Optional[dict] = None, ws=None, manager_id: Optional[int] = None) -> dict:
    """One online session as the viewer may see it (the who() rule). `workers` maps the
    sessions of live workers to their names; a worker's name shows only with its project.
    `manager_id`: the manager person's id; their session gets role "manager"."""
    mine = a.person_id == principal.person_id
    row = {**agent_row(a, now), "id": session_id(a.key), "mine": mine,
           "person": names.get(a.person_id, ""),
           "where": store.device_label(a.device_id) or machine_name("", a.os_user, a.host, a.machine),
           "team": a.person_id is not None and store.is_team(a.person_id)}
    if not visible(store, principal, a.person_id, a.project):
        row["project"] = PRIVATE
        row["name"] = private_name(a.name, a.machine, a.tool, a.project)
        row["title"] = ""  # a hidden project: nothing of the session
    elif workers and a.key in workers:
        row["worker"] = workers[a.key]
    if manager_id is not None and a.person_id == manager_id:
        row["role"] = "manager"
        row["background"] = False  # the manager runs as a background job; it keeps its own look
    row["harness"] = None
    if ws is not None and a.device_id is not None:
        row["harness"] = ws.facts(a.device_id).get("harness", {}).get(a.tool)
    if not mine:  # a door from one of the viewer's sessions, not their page: read only here
        row["writable"] = store.page_may_message(principal.person_name, principal.person_id,
                                                 a.key, a.person_id)
    if mine:
        row["doors"] = [masked(store, principal, d, (("_side", "with"),))["with"] or d["person"]
                        for d in store.doors_for(a.key)]
    return row


def masked(store: Store, principal: Principal, row: dict, sides) -> dict:
    """Pop each (side key, name field) pair's session from `row` and hide its project in the
    name when the viewer may not see it; `<field>_id` is then its handle, to tell two hidden
    sessions apart and to write to one."""
    for side, field in sides:
        s = row.pop(side)
        if s is not None and not visible(store, principal, s["person_id"], s["project"]):
            row[field] = private_name(row[field], s["machine"], s["tool"], s["project"])
            if s.get("key"):
                row[field + "_id"] = session_id(s["key"])
    return row


def manager_sides(m: dict, manager_id: Optional[int]) -> dict:
    """Mark a message from or to the manager's sessions, so the page keeps it in the manager's
    chat while the manager sleeps and its name is masked for a member."""
    for side, flag in (("_from", "from_manager"), ("_to", "to_manager")):
        m[flag] = manager_id is not None and m[side] is not None and m[side]["person_id"] == manager_id
    return m


def knocks(store: Store, principal: Principal, to_me_only: bool = False) -> list:
    """Pending knocks the viewer may see, the knocking session masked, or left out when the
    viewer may not see that session at all (store.listed)."""
    out = []
    for k in store.knocks(principal, sides=True):
        if to_me_only and not principal.is_admin() and k["to"] != principal.person_name:
            continue
        side = k["_from"]
        if side is not None and not store.listed(principal.person_id, side["key"],
                                                 side["person_id"]):
            k["from_session"], k["_from"] = "", None
        out.append(masked(store, principal, k, (("_from", "from_session"),)))
    return out


def _notes(store: Store, principal: Principal) -> list:
    out = []
    for project in [None] + [p["name"] for p in store.projects_for(principal)]:
        for n in store.notes_list(principal, project):
            out.append({"project": project, "topic": n["topic"],
                        "history": [asdict(h) for h in store.note_history(n["topic"], principal,
                                                                          project)]})
    return out


def feed(store: Store, principal: Principal, ws=None, cs=None) -> dict:
    now = store.clock()
    people = store.people()
    names = admin_ops.names(store)  # removed people too: their old sessions keep a name
    admin = principal.is_admin()
    me = principal.person_name
    mid = ManagerStore(store).person_id()
    doors = [masked(store, principal, d, (("_a", "a_session"), ("_b", "b_session")))
             for d in store.doors(sides=True) if admin or me in (d["a_person"], d["b_person"])]
    live = ws.live_keys() if ws is not None else None  # workers never fold into Idle
    out = {
        "agents": [session_row(store, a, principal, names, now, live, ws=ws, manager_id=mid)
                   for a in store.online()
                   if store.listed(principal.person_id, a.key, a.person_id)],
        "messages": [masked(store, principal, manager_sides(m, mid), (("_from", "sender"), ("_to", "to")))
                     for m in store.messages_for(principal, 500)],
        "notes": _notes(store, principal),
        "knocks": knocks(store, principal) if admin else [],  # only the admin answers
        "doors": doors,
    }
    if not admin and ws is not None:  # only what blocks this person; the admin has /api/warnings
        out["warnings"] = [w for w in ws.warnings_for(principal, now, __version__)
                           if w["person"] == me]
    if admin:
        members, grants, hidden = store.project_members(), store.machine_grants(), store.team_hidden()
        sides = (("_from", "from_session"), ("_to", "target"))
        connects = [masked(store, principal, r, sides) for r in cs.requests()] if cs else []
        decided = [masked(store, principal, r, sides)
                   for r in cs.requests(pending_only=False)] if cs else []
        out.update({
            "people": [{**p, "devices": store.devices(p["id"]),
                        **({"hidden_from": hidden.get(p["name"], [])} if p["team"] else {})} for p in people],
            "requests": sorted(store.requests() + connects, key=lambda r: r["created"]),
            "decisions": sorted([r for r in store.requests(pending_only=False) if r["decided_at"]]
                                + decided, key=lambda r: r["decided_at"], reverse=True)[:10],
            "legacy": {"unassigned": store.legacy_unassigned(), "enabled": store.legacy_enabled(),
                       "ready_to_disable": store.legacy_ready_to_disable()},
            "machines": [{**m, "grants": grants.get(m["host"], [])}
                         for m in store.machines(principal)],
            "projects": [{**p, "members": members.get(p["name"], [])}
                         for p in store.projects_for(principal) if not p.get("archived")],
            "archived_projects": [p for p in store.projects_for(principal) if p.get("archived")],
            "log": [{**r, "sentence": admin_ops.log_sentence(r)} for r in store.log(200)],
            "settings": {"retention_days": store.retention_days()},
        })
    return out
