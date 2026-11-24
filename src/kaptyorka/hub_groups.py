"""Sessions working together: those that wrote to each other both ways in the last 30 minutes,
and a session with the workers it started. Linked sessions form one group (connected parts of
that graph); a group goes away when its sessions go quiet, a worker stays with its boss while
it is live. The manager is never in a group: it talks to everyone and would join all of them.

A member sees only links they could already read (_reader): those of their own sessions, and
those between two team machine sessions they see by project role; never a link between two
other people's sessions, even behind a door. A partner of their own session they may not see
is an "other sessions" count, with nothing of it (name, machine, project, messages). The
admin sees every link."""
from typing import Dict, List, Optional, Set, Tuple

from kaptyorka.store import Agent, Principal, Store, session_id
from kaptyorka.workers import LIVE

WINDOW_S = 30 * 60


class Links:
    """The hub's links right now, for every viewer: sessions, edges and when each was used."""

    def __init__(self, agents: Dict[str, Agent], edges: Dict[Tuple[str, str], Tuple[float, int]],
                 bosses: Dict[str, str]):
        self.agents = agents  # key -> online session (no manager)
        self.edges = edges  # (a, b) sorted -> (last time, last message id, 0 for a worker link)
        self.bosses = bosses  # worker key -> boss key


def links(store: Store, manager_id: Optional[int] = None) -> Links:
    now = store.clock()
    agents = {a.key: a for a in store.online()
              if manager_id is None or a.person_id != manager_id}
    with store.lock:
        said = store.db.execute(
            "SELECT sender_key, to_key, MAX(created) AS t, MAX(id) AS id FROM messages"
            " WHERE kind='message' AND created > ? AND to_key IS NOT NULL"
            " GROUP BY sender_key, to_key", (now - WINDOW_S,)).fetchall()
        workers = store.db.execute(
            "SELECT key, boss_key, COALESCE(up_at, created) AS t FROM workers WHERE key IS NOT NULL"
            f" AND state IN ({','.join('?' * len(LIVE))})", LIVE).fetchall()
    one_way = {(r["sender_key"], r["to_key"]): (r["t"], r["id"]) for r in said
               if r["sender_key"] in agents and r["to_key"] in agents}
    edges: Dict[Tuple[str, str], Tuple[float, int]] = {}
    for (a, b), (t, i) in one_way.items():
        back = one_way.get((b, a))
        if back is not None and a < b:
            edges[(a, b)] = (max(t, back[0]), max(i, back[1]))
    bosses = {}
    for w in workers:
        if w["key"] in agents and w["boss_key"] in agents and w["key"] != w["boss_key"]:
            bosses[w["key"]] = w["boss_key"]
            pair = tuple(sorted((w["key"], w["boss_key"])))
            t, i = edges.get(pair, (0.0, 0))
            edges[pair] = (max(t, w["t"] or 0.0), i)
    return Links(agents, edges, bosses)


def _seen(store: Store, principal: Principal, ln: Links) -> Set[str]:
    if principal.is_admin():
        return set(ln.agents)
    return {k for k, a in ln.agents.items()
            if store.listed(principal.person_id, k, a.person_id)}


def _reader(store: Store, principal: Principal):
    """ok(side_a, side_b): may this viewer read what passed between two sessions (each side
    {key, person_id})? The admin: always. Anyone else, what they can already read: a message
    their own person sent or received (store.messages_for), or one between two team machine
    sessions they see by project role (store.listed). A door to someone shows that person's
    sessions, never what they say to others."""
    if principal.is_admin():
        return lambda a, b: True
    me, team = principal.person_id, {}

    def is_team(pid):
        if pid not in team:
            team[pid] = pid is not None and store.is_team(pid)
        return team[pid]

    def ok(a, b):
        if a["person_id"] == me or b["person_id"] == me:
            return True
        return all(is_team(s["person_id"]) and store.listed(me, s["key"], s["person_id"])
                   for s in (a, b))
    return ok


def _parts(nodes: Set[str], pairs) -> List[Set[str]]:
    up = {n: n for n in nodes}

    def root(n):
        while up[n] != n:
            up[n] = up[up[n]]
            n = up[n]
        return n

    for a, b in pairs:
        up[root(a)] = root(b)
    out: Dict[str, Set[str]] = {}
    for n in nodes:
        out.setdefault(root(n), set()).add(n)
    return list(out.values())


def for_viewer(store: Store, principal: Principal, ln: Links, now: float) -> List[dict]:
    """The groups this viewer may see: {id, project, members: [{id, key, boss?}], others,
    active_s, last_id}. `key` is the hub's own and is removed before the page gets it."""
    seen = _seen(store, principal, ln)
    ok = _reader(store, principal)
    side = {k: {"key": k, "person_id": a.person_id} for k, a in ln.agents.items()}
    edges = {p: v for p, v in ln.edges.items() if ok(side[p[0]], side[p[1]])}  # only readable links
    linked = {k for pair in edges for k in pair}
    inside = [(a, b) for a, b in edges if a in seen and b in seen]
    out = []
    for part in _parts(seen & linked, inside):
        # a partner outside the part is one this viewer may not see (else it would be inside)
        hidden = {b if a in part else a for a, b in edges if (a in part) != (b in part)}
        if len(part) < 2 and not hidden:
            continue
        touching = [v for p, v in edges.items() if p[0] in part or p[1] in part]
        own = [v for p, v in edges.items() if p[0] in part and p[1] in part]
        order = sorted(part, key=lambda k: (ln.agents[k].started or 0.0, k))
        members = []
        for k in order:  # bosses first, each followed by its workers (the page draws a tree)
            if ln.bosses.get(k) in part:
                continue
            members.append({"id": session_id(k), "key": k})
            members += [{"id": session_id(w), "key": w, "boss": session_id(k)}
                        for w in order if ln.bosses.get(w) == k]
        projects = {ln.agents[k].project for k in part}
        project = next(iter(projects)) if len(projects) == 1 else "mixed"
        if project != "mixed" and not principal.is_admin() and not all(
                ln.agents[k].person_id == principal.person_id
                or store.project_role(project, principal.person_id) is not None for k in part):
            project = "mixed"  # a project this viewer may not read: not named
        out.append({"id": "g" + session_id(order[0])[1:], "project": project or "mixed",
                    "members": members, "others": len(hidden),
                    "active_s": round(now - max(t for t, _ in touching)),
                    "last_id": max([i for _, i in own] or [0])})
    out.sort(key=lambda g: g["active_s"])
    return out


def feed_groups(store: Store, principal: Principal, manager_id: Optional[int]) -> List[dict]:
    """The groups as the feed carries them: no session keys."""
    groups = for_viewer(store, principal, links(store, manager_id), store.clock())
    for g in groups:
        for m in g["members"]:
            m.pop("key")
    return groups


def conversation(store: Store, principal: Principal, gid: str,
                 manager_id: Optional[int]) -> Optional[List[dict]]:
    """Every message between the sessions of the viewer's group `gid`, oldest first, masked as
    the feed masks them, and only those the viewer may read (_reader); None when the viewer
    has no such group. A hidden partner is never in the group, so its messages never come back."""
    from kaptyorka.hub_feed import manager_sides, masked

    group = next((g for g in for_viewer(store, principal, links(store, manager_id), store.clock())
                  if g["id"] == gid), None)
    if group is None:
        return None
    keys, ok = [m["key"] for m in group["members"]], _reader(store, principal)
    return [masked(store, principal, manager_sides(m, manager_id),
                   (("_from", "sender"), ("_to", "to")))
            for m in store.messages_among(keys) if ok(m["_from"], m["_to"])]

