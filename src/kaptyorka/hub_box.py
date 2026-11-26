"""The hub's side of a personal laptop (secure design v2, sections 6 and 9.1): the 16 calls its
relay posts as POST /box/v1/<call>, with the computer's device token.

A personal laptop's device joined with a "personal" invite (store.PERSONAL). Such a device
reaches nothing on the hub but these routes (hub.page_gate), and these routes take no other
device. Its chats are box chats: agent rows keyed "box:<device id>:<chat>", where <chat> is the
relay's keyed hash of the tool's session id (16 hex characters; the hub never sees the real
id). They show on the page and in `who` like any other chat (tool "box").

Nothing is ever pushed to a box chat. A message to one waits in the store until the box's helper
collects it in a `report` (or `inbox`) answer, delivers it inside the box, and acks it. The same
holds for the page's close orders, the helper chats a box chat asked for with `work`, and the
team skills: they ride on `report` answers only. The hub starts nothing on a personal laptop by
itself: a helper chat starts only for a box chat of that same box that asked for it.

Every text a box chat sends goes through the same store calls as any session's, so the rights
checks and the trick filter (tricks.py, through store.send) apply."""
import base64
import re
import threading
from typing import Callable, Dict, List, Optional

from kaptyorka import brief as brief_mod, paths
from kaptyorka.memory import check_state
from kaptyorka.store import (ONLINE_S, KaptyorkaError, MessageHeld, Principal, Store, clean,
                              clean_title, session_id)
from kaptyorka.tricks import check_tricks

PREFIX = "box:"
TOOL = "box"  # the relay sends no tool name (design v2, 6.4): every box chat is a "box" chat
CHAT_RX = re.compile(r"[0-9a-f]{16}")
NAME_RX = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._-]{0,63}")
HUB_ID_RX = re.compile(r"hub:([0-9a-f]{8})")
CALLS = ("report", "ack", "brief", "send", "notes_write", "state_write", "status", "inbox", "who",
         "projects", "notes_read", "notes_list", "fetch_file", "work", "close_worker")
MAX_HELPERS = 5  # helper chats waiting to start, per box
MAX_CHATS = 64
TEXT_TYPES = ("txt", "md", "log", "json", "csv", "svg")
DELIVERY = ("a box chat on a personal laptop gets it from its box helper at the helper's next"
            " report (seconds), or reads it on its next turn")
NOT_BOX = "this route is for a personal laptop's relay only"
NOT_TEAM = "{project} is not a team project of yours; the chat stays off the hub"


def box_key(device_id: int, chat: str) -> str:
    return f"{PREFIX}{device_id}:{chat}"


def is_box(key) -> bool:
    return isinstance(key, str) and key.startswith(PREFIX)


def chat_of(key: str, device_id: int) -> Optional[str]:
    """The relay's chat hash of a box chat of this device, else None."""
    head = f"{PREFIX}{device_id}:"
    return key[len(head):] if isinstance(key, str) and key.startswith(head) else None


class Refused(Exception):
    """A box call the hub does not take: (HTTP status, one line)."""

    def __init__(self, status: int, line: str):
        super().__init__(line)
        self.status = status


def _hash(v) -> str:
    if not isinstance(v, str) or not CHAT_RX.fullmatch(v):
        raise Refused(400, "not a chat id")
    return v


def _name(v, what: str = "name") -> str:
    if not isinstance(v, str) or not NAME_RX.fullmatch(v):
        raise Refused(400, f"not a valid {what}")
    return v


def _text(v, cap: int = 65536) -> str:
    if not isinstance(v, str):
        raise Refused(400, "not text")
    return v[:cap]


class BoxHub:
    """The /box/v1 calls. `tell(key, text)` and `wake()` are the hub's own; `brief_for(agent,
    principal)` composes a session's brief; `send(key, to, text, file_id, held)` is the hub's
    send (the manager's routing first, then the store)."""

    def __init__(self, store: Store, closing, rs, ms, send: Callable, wake: Callable,
                 brief_for: Callable):
        self.store, self.closing, self.rs, self.ms = store, closing, rs, ms
        self._send, self.wake, self.brief_for = send, wake, brief_for
        self.lock = threading.Lock()
        self.helpers: Dict[int, List[dict]] = {}   # device id -> helper chats to start
        self.skills_sent: Dict[int, str] = {}      # device id -> the skills version it has

    # ------------------------------------------------------------------ who is calling

    def allowed(self, principal: Principal) -> set:
        """The team projects the person has a role in (archived ones left out)."""
        return {p["name"] for p in self.store.projects_for(principal) if not p.get("archived")}

    def _gate(self, principal: Principal, project: str, allowed: Optional[set] = None) -> bool:
        if not self.store.team_project(project):
            return False
        return paths.team_name(project) in (self.allowed(principal) if allowed is None else allowed)

    def _upsert(self, principal: Principal, chat: str, project: str, now: float,
                state: Optional[str] = None, title: Optional[str] = None,
                active: Optional[float] = None) -> str:
        """The box chat's agent row, made or refreshed (seen now). Lock held."""
        s = self.store
        key = box_key(principal.device_id, chat)
        row = s.db.execute("SELECT person_id, device_id FROM agents WHERE key=?", (key,)).fetchone()
        if row is not None and (row["device_id"] != principal.device_id
                                or row["person_id"] != principal.person_id):
            raise Refused(403, "that chat is not this computer's")
        machine = clean(principal.device_name, 100)
        s._touch(key, machine, TOOL, project, now, None if state is None else state,
                 title=None if title is None else clean_title(title, 80))
        s.db.execute("UPDATE agents SET person_id=?, device_id=?, box=1, closable=1, cwd='',"
                     " host='', os_user='' WHERE key=?",
                     (principal.person_id, principal.device_id, key))
        if active is not None:
            s.db.execute("UPDATE agents SET active=? WHERE key=?", (active, key))
        return key

    def caller(self, principal: Principal, body: dict, create: bool = True) -> str:
        """The calling box chat's key; its row is made if it is new (a chat's first call may
        come before the helper's first report). Refused for a project the person has no role
        in: such a chat never reaches the hub."""
        chat, project = _hash(body.get("chat")), _name(body.get("project"), "project")
        if not self._gate(principal, project):
            raise Refused(403, NOT_TEAM.format(project=project))
        with self.store.lock:
            key = box_key(principal.device_id, chat)
            row = self.store.db.execute("SELECT project FROM agents WHERE key=?", (key,)).fetchone()
            if row is not None and row["project"] != project:
                raise Refused(403, "that chat runs in another team folder")
            if row is None and not create:
                raise Refused(404, "no such chat of this computer")
            return self._upsert(principal, chat, project, self.store.clock())

    # ------------------------------------------------------------------ the calls

    def handle(self, principal: Principal, call: str, body) -> dict:
        if principal is None or not principal.personal:
            raise Refused(403, NOT_BOX)
        if call not in CALLS:
            raise Refused(404, "no such call")
        if not isinstance(body, dict):
            raise Refused(400, "send one JSON object")
        try:
            return getattr(self, "c_" + call)(principal, body)
        except MessageHeld as e:
            return {"ok": False, "reply": str(e)[:500]}
        except KaptyorkaError as e:
            raise Refused(400, str(e)) from None

    def c_report(self, principal: Principal, body: dict) -> dict:
        chats = body.get("chats")
        if not isinstance(chats, list) or len(chats) > MAX_CHATS:
            raise Refused(400, "chats must be a list")
        allowed = self.allowed(principal)
        s = self.store
        ended, seen, take = [], set(), []
        for c in chats:  # checked before the lock: store.team_project takes it
            if not isinstance(c, dict):
                continue
            try:
                chat, project = _hash(c.get("chat")), _name(c.get("project"), "project")
            except Refused:
                continue
            if c.get("state") in ("busy", "idle", "ended") and self._gate(principal, project, allowed):
                take.append((chat, project, c))  # any other chat never reaches the hub
        with s.lock:
            now = s.clock()
            for chat, project, c in take:
                state = c["state"]
                key = box_key(principal.device_id, chat)
                row = s.db.execute("SELECT person_id, device_id, project FROM agents WHERE key=?",
                                   (key,)).fetchone()
                if row is not None and (row["device_id"] != principal.device_id
                                        or row["person_id"] != principal.person_id):
                    continue
                if state == "ended":
                    if row is not None:
                        s.db.execute("UPDATE agents SET last_seen=MIN(last_seen, ?), activity='idle'"
                                     " WHERE key=?", (now - ONLINE_S, key))
                        ended.append(key)
                    continue
                last = c.get("last_activity")
                active = None
                if isinstance(last, (int, float)) and not isinstance(last, bool) and last == last:
                    active = min(now, max(now - 30 * 86400, float(last)))  # the laptop's clock
                title = c.get("name") if isinstance(c.get("name"), str) else None
                self._upsert(principal, chat, project, now, state, title, active)
                seen.add(key)
            # the helper's list is the whole box: a chat it no longer names has ended
            for r in s.db.execute("SELECT key FROM agents WHERE device_id=? AND box=1 AND"
                                  " last_seen>?", (principal.device_id, now - ONLINE_S)).fetchall():
                if r["key"] not in seen:
                    s.db.execute("UPDATE agents SET last_seen=MIN(last_seen, ?) WHERE key=?",
                                 (now - ONLINE_S, r["key"]))
                    ended.append(r["key"])
        ended = list(dict.fromkeys(ended))
        if ended:  # a close the page asked for is done once its chat is gone
            self.closing.done(s, principal.device_id, {k: "" for k in ended}, principal.person_name)
        out: dict = {"messages": self._messages(principal)}
        close = [{"chat": chat_of(o["key"], principal.device_id)}
                 for o in self.closing.for_device(principal.device_id)
                 if chat_of(o["key"], principal.device_id)]
        if close:
            out["close"] = close
        with self.lock:
            helpers = self.helpers.pop(principal.device_id, [])
        if helpers:
            out["helpers"] = helpers
        version = self.rs.skills_version()
        if self.skills_sent.get(principal.device_id) != version:
            self.skills_sent[principal.device_id] = version
            out["skills"] = [{"name": k["name"], "text": k["text"]} for k in self.rs.skills()
                             if isinstance(k.get("name"), str) and NAME_RX.fullmatch(k["name"])][:50]
        return out

    def _origin(self, m) -> dict:
        """Who a message is from, for the box's origin line (design 8.4). Lock held."""
        key = m["sender_key"] or ""
        if key.startswith("web:"):
            return {"person": clean(m["sender_name"], 64) or "someone", "kind": "page"}
        if key.startswith("hub:"):
            return {"person": "hub", "kind": "manager"}
        row = self.store.db.execute(
            "SELECT a.machine, p.name, p.role FROM agents a LEFT JOIN people p ON p.id=a.person_id"
            " WHERE a.key=?", (key,)).fetchone()
        if row is None:
            return {"kind": "chat"}
        out = {"kind": "manager" if row["role"] == "manager" else "chat"}
        for k, v in (("person", row["name"]), ("machine", row["machine"])):
            v = clean(v, 64)
            if v and v.isprintable():
                out[k] = v
        return out

    def _messages(self, principal: Principal) -> List[dict]:
        """Messages for this box's chats that wait for delivery, each offered once a minute
        until its ack (store.offer's lease)."""
        s = self.store
        items = [o for o in s.offer(clean(principal.device_name, 100), "", principal)
                 if o.get("kind") == "message" and chat_of(o["key"], principal.device_id)]
        out = []
        with s.lock:
            for o in items[:50]:
                m = s.db.execute("SELECT * FROM messages WHERE id=?", (o["id"],)).fetchone()
                f = s._file_info(m["file_id"]) if m is not None else None
                out.append({"id": o["id"], "chat": chat_of(o["key"], principal.device_id),
                            "text": o["text"], **(self._origin(m) if m is not None else {}),
                            **({"files": [{"id": f["id"], "name": f["name"], "size": f["size"]}]}
                               if f else {})})
        return out

    def c_ack(self, principal: Principal, body: dict) -> dict:
        mid = body.get("id")
        if isinstance(mid, bool) or not isinstance(mid, int):
            raise Refused(400, "id must be a number")
        with self.store.lock:
            row = self.store.db.execute("SELECT to_key FROM messages WHERE id=?", (mid,)).fetchone()
        if row is None or chat_of(row["to_key"], principal.device_id) is None:
            raise Refused(404, "no such message for this computer")
        ok = body.get("ok", True) is True
        self.store.ack(mid, ok, principal, "" if ok else "the box helper could not deliver it")
        return {"ok": True}

    def c_brief(self, principal: Principal, body: dict) -> dict:
        key = self.caller(principal, body)
        agent = self.store.agent(key)
        return {"text": self.brief_for(agent, principal)}

    def c_send(self, principal: Principal, body: dict) -> dict:
        key = self.caller(principal, body)
        to, text = body.get("to"), _text(body.get("text"))
        if not isinstance(to, str) or not to.strip() or len(to) > 128:
            raise Refused(400, "to must name a chat")
        m = HUB_ID_RX.fullmatch(to)
        to = "#" + m.group(1) if m else to
        file_id = None
        if body.get("file_name") is not None:
            name, content = _text(body.get("file_name"), 200), body.get("file_content")
            if not isinstance(content, str):
                raise Refused(400, "a file needs its content")
            data = (base64.b64decode(content[7:], validate=True) if content.startswith("base64:")
                    else content.encode("utf-8"))
            if name.rsplit(".", 1)[-1].lower() in TEXT_TYPES and check_tricks(
                    data.decode("utf-8", "replace")) is not None:
                return {"ok": False, "reply": "not sent: the file holds a pattern the hub holds for"
                                              " a person to look at; send it another way"}
            file_id = self.store.file_add(name, data, principal.person_id)
        held: list = []
        n = self._send(key, to, text, file_id, held)
        self.wake()
        if held:
            reply = "; ".join(f"held: {name} was last seen {s} s ago; it gets the message when it"
                              " next reports" for name, s in held)
        else:
            reply = f"sent to {n} session" + ("" if n == 1 else "s")
        if self._to_box(key, to):
            reply += "; " + DELIVERY
        return {"ok": True, "reply": reply[:500]}

    def _to_box(self, sender_key: str, to: str) -> bool:
        s = self.store
        with s.lock:
            try:
                targets = s._targets(sender_key, to, s.clock(), None)
            except KaptyorkaError:
                return False
        return any(is_box(t["key"]) for t in targets)

    def _project(self, key: str) -> str:
        a = self.store.agent(key)
        return a.project if a is not None else ""

    def c_notes_write(self, principal: Principal, body: dict) -> dict:
        key = self.caller(principal, body)
        topic, text, project = _name(body.get("topic"), "topic"), _text(body.get("text")), self._project(key)
        if topic == "state":
            problem = check_state(text)
            if problem:
                raise Refused(400, problem)
        self.store.notes_write(key, topic, text, project)
        if topic == "state":
            self.ms.state_written(key, project, principal.person_id, self.store.clock())
        return {"ok": True, "reply": f"saved note {topic} of {project}"}

    def c_state_write(self, principal: Principal, body: dict) -> dict:
        return self.c_notes_write(principal, {**body, "topic": "state"})

    def c_status(self, principal: Principal, body: dict) -> dict:
        key = self.caller(principal, body)
        text = _text(body.get("text"), 500)
        self.store.set_status(key, text)
        self.ms.status_signal(key, text, self._project(key))
        return {"ok": True, "reply": "status set"}

    def c_inbox(self, principal: Principal, body: dict) -> dict:
        key = self.caller(principal, body)
        chat = chat_of(key, principal.device_id)
        out = []
        for m in self.store.inbox(key)[:50]:  # read now: the chat itself asked
            out.append({"id": m.id, "chat": chat, "kind": "page" if m.from_web else "chat",
                        "text": brief_mod.message_text(m),
                        **({"files": [{"id": m.file["id"], "name": m.file["name"],
                                       "size": m.file["size"]}]} if m.file else {})})
        return {"messages": out}

    def c_who(self, principal: Principal, body: dict) -> dict:
        from kaptyorka import admin_ops
        self.caller(principal, body)
        s = self.store
        names = admin_ops.names(s)
        rows = []
        for a in s.online():
            if not s.listed(principal.person_id, a.key, a.person_id):
                continue
            mine = chat_of(a.key, principal.device_id)
            row = {"chat": mine or session_id(a.key)[1:],
                   "state": "busy" if a.activity in ("busy", "shell", "waiting") else "idle"}
            person = clean(names.get(a.person_id, ""), 64)
            if person and person.isprintable():
                row["person"] = person
            machine = clean(a.machine, 64)
            if machine and machine.isprintable():
                row["machine"] = machine
            from kaptyorka.hub_feed import visible
            if visible(s, principal, a.person_id, a.project) and NAME_RX.fullmatch(a.project or ""):
                row["project"] = a.project
                row["name"] = clean(a.title or a.label or a.name, 80)
            else:
                row["name"] = session_id(a.key)
            rows.append(row)
        return {"chats": rows[:200]}

    def c_projects(self, principal: Principal, body: dict) -> dict:
        self.caller(principal, body)
        return {"projects": sorted(p for p in self.allowed(principal) if NAME_RX.fullmatch(p))[:200]}

    def c_notes_read(self, principal: Principal, body: dict) -> dict:
        key = self.caller(principal, body)
        topic = _name(body.get("topic"), "topic")
        note = self.store.notes_read(topic, principal, self._project(key))
        return {"topic": topic, "text": note.text if note else f"no note named {topic}"}

    def c_notes_list(self, principal: Principal, body: dict) -> dict:
        key = self.caller(principal, body)
        return {"topics": [n["topic"] for n in self.store.notes_list(principal, self._project(key))
                           if isinstance(n["topic"], str) and NAME_RX.fullmatch(n["topic"])][:500]}

    def c_fetch_file(self, principal: Principal, body: dict) -> dict:
        self.caller(principal, body)
        fid = body.get("id")
        if isinstance(fid, bool) or not isinstance(fid, int):
            raise Refused(400, "id must be a number")
        info, data = self.store.file_get(fid, principal)
        ext = info["name"].rsplit(".", 1)[-1].lower() if "." in info["name"] else ""
        try:
            content = data.decode("utf-8") if ext in TEXT_TYPES and len(data) <= 256 * 1024 else None
        except UnicodeDecodeError:
            content = None
        if content is None:
            if len(data) > 1024 * 1024:
                raise Refused(413, "the file is larger than a box takes (1 MB)")
            content = "base64:" + base64.b64encode(data).decode()
        return {"name": info["name"], "size": len(data), "content": content}

    def c_work(self, principal: Principal, body: dict) -> dict:
        """A helper chat in the caller's own team folder, in its own box: it rides on the next
        report. Nothing else may start one on a personal laptop."""
        key = self.caller(principal, body)
        prompt = _text(body.get("prompt"), 32768)
        if not prompt.strip():
            raise Refused(400, "the prompt is empty")
        project = self._project(key)
        with self.lock:
            q = self.helpers.setdefault(principal.device_id, [])
            if len(q) >= MAX_HELPERS:
                raise Refused(429, "helper chats are already waiting to start; try again soon")
            q.append({"project": project, "prompt": prompt})
        self.store.log_add(principal.person_name, "box_helper", f"{project} on {principal.device_name}")
        return {"ok": True, "reply": f"a helper chat starts in {project} on this computer within"
                                     " seconds; it shows in who() as one of yours"}

    def c_close_worker(self, principal: Principal, body: dict) -> dict:
        self.caller(principal, body)
        worker = _hash(body.get("worker"))
        a = self.store.agent(box_key(principal.device_id, worker))
        if a is None or not a.online:
            raise Refused(404, "no such chat of this computer")
        self.closing.ask(a, principal.person_name, True, self.store.clock())
        return {"ok": True, "reply": "it is closed at the helper's next report"}


def note_for(store: Store, key: str) -> str:
    """For the hub's own send answer: how a message to this target is delivered ("" when it is
    no box chat)."""
    return DELIVERY if is_box(key) else ""
