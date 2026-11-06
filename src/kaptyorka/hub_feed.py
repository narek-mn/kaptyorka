"""What the page shows one viewer: the feed filtered by their person (the admin sees all)."""
from dataclasses import asdict

from kaptyorka.store import Agent, Principal, Store

PRIVATE = "(private)"


def agent_row(a: Agent, now: float) -> dict:
    return {"name": a.name, "machine": a.machine, "tool": a.tool, "project": a.project,
            "status": a.status, "activity": a.activity, "label": a.label,
            "last_seen_s": round(now - a.last_seen)}


def private_name(name: str, machine: str, tool: str, project: str) -> str:
    """The session name with its project hidden: machine/tool/(private), keeping a -N suffix."""
    base = f"{machine}/{tool}/{project}"
    suffix = name[len(base):] if name.startswith(base) else ""
    return f"{machine}/{tool}/{PRIVATE}{suffix}"


def visible(store: Store, principal: Principal, person_id, project: str) -> bool:
    """Your own sessions show in full. An unregistered folder is only a name two people can
    share by chance, so another person's session shows its project only when it is a
    registered project you have a role in."""
    return person_id == principal.person_id or \
        store.project_role(project, principal.person_id) is not None


def session_row(store: Store, a: Agent, principal: Principal, names: dict, now: float) -> dict:
    """One online session as the viewer may see it (the who() rule)."""
    mine = a.person_id == principal.person_id
    row = {**agent_row(a, now), "mine": mine, "person": names.get(a.person_id, "")}
    if not visible(store, principal, a.person_id, a.project):
        row["project"] = PRIVATE
        row["name"] = private_name(a.name, a.machine, a.tool, a.project)
    if mine:
        row["doors"] = [d["with"] or d["person"] for d in store.doors_for(a.key)]
    return row


def masked(store: Store, principal: Principal, row: dict, sides) -> dict:
    """Pop each (side key, name field) pair's session from `row` and hide its project in the
    name when the viewer may not see it."""
    for side, field in sides:
        s = row.pop(side)
        if s is not None and not visible(store, principal, s["person_id"], s["project"]):
            row[field] = private_name(row[field], s["machine"], s["tool"], s["project"])
    return row


def knocks(store: Store, principal: Principal, to_me_only: bool = False) -> list:
    """Pending knocks the viewer may see, the knocking session masked."""
    return [masked(store, principal, k, (("_from", "from_session"),))
            for k in store.knocks(principal, sides=True)
            if not to_me_only or principal.is_admin() or k["to"] == principal.person_name]


def _notes(store: Store, principal: Principal) -> list:
    out = []
    for project in [None] + [p["name"] for p in store.projects_for(principal)]:
        for n in store.notes_list(principal, project):
            out.append({"project": project, "topic": n["topic"],
                        "history": [asdict(h) for h in store.note_history(n["topic"], principal,
                                                                          project)]})
    return out


def feed(store: Store, principal: Principal) -> dict:
    now = store.clock()
    people = store.people()
    names = {p["id"]: p["name"] for p in people}
    admin = principal.is_admin()
    me = principal.person_name
    doors = [masked(store, principal, d, (("_a", "a_session"), ("_b", "b_session")))
             for d in store.doors(sides=True) if admin or me in (d["a_person"], d["b_person"])]
    out = {
        "agents": [session_row(store, a, principal, names, now) for a in store.online()],
        "messages": [masked(store, principal, m, (("_from", "sender"), ("_to", "to")))
                     for m in store.messages_for(principal, 500)],
        "notes": _notes(store, principal),
        "knocks": knocks(store, principal, to_me_only=True),
        "doors": doors,
    }
    if admin:
        members, grants = store.project_members(), store.machine_grants()
        out.update({
            "people": [{**p, "devices": store.devices(p["id"])} for p in people],
            "requests": store.requests(),
            "legacy": {"unassigned": store.legacy_unassigned(), "enabled": store.legacy_enabled(),
                       "ready_to_disable": store.legacy_ready_to_disable()},
            "machines": [{**m, "grants": grants.get(m["host"], [])}
                         for m in store.machines(principal)],
            "projects": [{**p, "members": members.get(p["name"], [])}
                         for p in store.projects_for(principal)],
            "log": store.log(200),
            "settings": {"retention_days": store.retention_days()},
        })
    return out
