"""kaptyorka relay: the only kaptyorka code on a personal laptop outside the box.

It holds the computer's hub key and forwards a fixed list of calls from the box to the hub
over TLS, accepting the hub only by its pinned certificate fingerprint. It is a filter, not a
pipe: every field of every call is checked both ways and anything not in the table below is
dropped (secure design v2, sections 4.3 and 6).

Python standard library only. It imports nothing from the rest of kaptyorka, so it can be read
and audited on its own. It runs as `python3 -I relay.py /etc/kaptyorka/relay.conf` under
kaptyorka-relay.service, with its doors passed in by systemd (one Unix socket per person,
kaptyorka-relay-box@<person>.socket) and its secrets passed in with LoadCredential=.

The box speaks one JSON line per connection: {"call": "<name>", "args": {...}}. The relay
answers one JSON line, {"ok": true, "result": {...}} or {"ok": false, "error": "<one line>"},
and closes. To the hub it sends POST /box/v1/<call> with a JSON body built only from the
filtered fields, the device token, the version and the mode.
"""
import base64
import hashlib
import hmac
import http.client
import json
import math
import os
import pwd
import re
import selectors
import socket
import ssl
import struct
import sys
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

MAX_REQUEST = 2_621_440      # one line from the box: a 1 MiB file in base64 plus text
MAX_ANSWER = 2_097_152       # one answer from the hub
MAX_CLIENTS = 16             # connections handled at once; more are closed at once
READ_TIMEOUT = 15            # seconds for the box to send its line
HUB_TIMEOUT = 20             # seconds for the hub to answer
TEXT_FILE_MAX = 256 * 1024   # 6.3: text files up to 256 KB
BIN_FILE_MAX = 1024 * 1024   # 6.3: other files up to 1 MB
PERSON_RX = re.compile(r"[a-z_][a-z0-9_-]{0,30}")   # same rule as `enter` (4.7)


class Bad(ValueError):
    """A field or a whole call that the table does not allow. The text is one plain line."""


# ---------------------------------------------------------------------------- text cleaning

_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]"            # CSI: colours, cursor, reports
                   r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?"  # OSC: titles, clipboard (52), links
                   r"|\x1b[PX^_][^\x1b]*(?:\x1b\\)?"       # DCS, SOS, PM, APC
                   r"|\x1b[ -/]*[0-~]?")                   # any other escape
_BIDI = "؜‎‏‪‫‬‭‮⁦⁧⁨⁩"
_CTRL_ALL = {c: None for c in list(range(0, 32)) + list(range(127, 160))}
_CTRL_ALL.update({ord(c): None for c in _BIDI})
_CTRL_KEEP_LINES = {k: v for k, v in _CTRL_ALL.items() if k not in (9, 10)}


def plain(s: str, lines: bool) -> str:
    """Terminal escape codes, control characters and direction overrides removed. With lines,
    tabs and line breaks stay."""
    s = _ANSI.sub("", s.replace("\r\n", "\n"))
    return s.translate(_CTRL_KEEP_LINES if lines else _CTRL_ALL)


def cut(s: str, max_bytes: int) -> str:
    """At most max_bytes of UTF-8, never half a character."""
    b = s.encode("utf-8", "replace")
    return s if len(b) <= max_bytes else b[:max_bytes].decode("utf-8", "ignore")


# ---------------------------------------------------------------------------- field kinds
# Each kind is a function (value, ctx, way) -> cleaned value; it raises Bad when the value
# is not allowed. way is "out" (box to hub) or "in" (hub to box). ctx is the call's Context.

class Context:
    def __init__(self, relay: "Relay"):
        self.relay = relay
        self.strings: List[str] = []  # every outgoing string, for the token-shape check


def Id(rx: str) -> Callable:
    pat = re.compile(rx)

    def f(v, ctx, way):
        if not isinstance(v, str) or not pat.fullmatch(v):
            raise Bad("not a valid name or id")
        return v
    return f


NAME = Id(r"[A-Za-z0-9_][A-Za-z0-9._-]{0,63}")       # team folder, topic, skill
HUBID = Id(r"[A-Za-z0-9_][A-Za-z0-9._:@-]{0,127}")   # an id the hub hands out


def Text(max_bytes: int, lines: bool = True) -> Callable:
    def f(v, ctx, way):
        if not isinstance(v, str):
            raise Bad("not text")
        v = cut(plain(v, lines), max_bytes)
        if way == "out":
            ctx.strings.append(v)
        return v
    return f


def Label(max_chars: int) -> Callable:
    """A short one-line name (a person, a machine, a send target): refused, not cut, when it
    is too long or holds control characters, because a cut name names someone else."""
    def f(v, ctx, way):
        if not isinstance(v, str) or not v or len(v) > max_chars or plain(v, False) != v:
            raise Bad("not a valid name")
        if way == "out":
            ctx.strings.append(v)
        return v
    return f


def Choice(*allowed: str) -> Callable:
    def f(v, ctx, way):
        if not isinstance(v, str) or v not in allowed:
            raise Bad("not one of " + ", ".join(allowed))
        return v
    return f


def Int(v, ctx, way):
    if isinstance(v, bool) or not isinstance(v, int) or not 0 <= v < 2 ** 53:
        raise Bad("not a whole number")
    return v


def Num(v, ctx, way):
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) \
            or not 0 <= v < 2 ** 53:
        raise Bad("not a number")
    return v


def Bool(v, ctx, way):
    if not isinstance(v, bool):
        raise Bad("not true or false")
    return v


FILE_NAME = Id(r"[A-Za-z0-9_][A-Za-z0-9._ -]{0,127}")


def FileContent(v, ctx, way):
    """Text up to 256 KB, or "base64:<data>" up to 1 MB once decoded. Refused, never cut: a cut
    file is a broken file."""
    if not isinstance(v, str):
        raise Bad("not text")
    if v.startswith("base64:"):
        try:
            raw = base64.b64decode(v[7:], validate=True)
        except ValueError:
            raise Bad("not valid base64") from None
        if len(raw) > BIN_FILE_MAX:
            raise Bad("file larger than 1 MB")
        if way == "out":
            ctx.strings.append(raw.decode("latin-1"))
        return "base64:" + base64.b64encode(raw).decode()
    if len(v.encode("utf-8", "replace")) > TEXT_FILE_MAX:
        raise Bad("text file larger than 256 KB")
    if way == "out":
        ctx.strings.append(v)
    return v


CHAT_RX = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._:@-]{0,127}")   # a tool's session id, in the box
HASH_RX = re.compile(r"[0-9a-f]{16}")


def Chat(v, ctx, way):
    """A chat of this box. Out: the tool's session id, sent as a keyed hash (16 characters).
    In: a hash this relay handed out, turned back into the session id; any other is refused."""
    if way == "out":
        if not isinstance(v, str) or not CHAT_RX.fullmatch(v):
            raise Bad("not a valid chat id")
        return ctx.relay.hash_chat(v)
    raw = ctx.relay.unhash_chat(v) if isinstance(v, str) else None
    if raw is None:
        raise Bad("not a chat of this box")
    return raw


def AnyChat(v, ctx, way):
    """A chat the hub names (in: only). One of this box is turned back; others stay opaque."""
    raw = ctx.relay.unhash_chat(v) if isinstance(v, str) else None
    return raw if raw is not None else HUBID(v, ctx, way)


def Obj(fields: Dict[str, Tuple[Callable, bool]]) -> Callable:
    """A JSON object with only these fields (kind, required). Unknown fields are dropped."""
    def f(v, ctx, way):
        if not isinstance(v, dict):
            raise Bad("not an object")
        out = {}
        for name, (kind, required) in fields.items():
            if name not in v or v[name] is None:
                if required:
                    raise Bad(f"field '{name}' is missing")
                continue
            try:
                out[name] = kind(v[name], ctx, way)
            except Bad as e:
                raise Bad(f"field '{name}': {e}") from None
        return out
    return f


def List_(kind: Callable, max_items: int, drop_bad: bool) -> Callable:
    """A list of at most max_items. Coming in, a bad item is dropped; going out, it refuses."""
    def f(v, ctx, way):
        if not isinstance(v, list):
            raise Bad("not a list")
        out = []
        for item in v[:max_items]:
            try:
                out.append(kind(item, ctx, way))
            except Bad:
                if not drop_bad:
                    raise
        return out
    return f


def R(kind):  # required
    return (kind, True)


def O(kind):  # optional
    return (kind, False)


# ---------------------------------------------------------------------------- the table (6)

TEXT = Text(65536)
ONE_LINE = Text(500, lines=False)
CALLER = {"chat": R(Chat), "project": R(NAME)}   # which chat of this box is calling (6.2)
OK = Obj({"ok": O(Bool), "reply": O(ONE_LINE)})

MESSAGE = Obj({  # 6.5: a message for a chat
    "id": R(Int), "chat": R(Chat), "text": R(TEXT),
    "person": O(Label(64)), "machine": O(Label(64)), "kind": O(Choice("chat", "page", "manager")),
    "files": O(List_(Obj({"id": R(Int), "name": R(FILE_NAME), "size": R(Int)}), 10, True)),
})
MESSAGES = List_(MESSAGE, 50, True)

# call: (what the box may send, what the hub may answer)
CALLS: Dict[str, Tuple[Callable, Callable]] = {
    "join": (Obj({}), OK),
    "report": (
        Obj({"chats": R(List_(Obj({
            "chat": R(Chat), "project": R(NAME), "name": O(Text(80, lines=False)),
            "state": R(Choice("busy", "idle", "ended")), "last_activity": O(Num)}), 64, False))}),
        Obj({"messages": O(MESSAGES),
             "close": O(List_(Obj({"chat": R(Chat)}), 20, True)),
             "helpers": O(List_(Obj({"project": R(NAME), "prompt": R(Text(32768))}), 5, True)),
             "skills": O(List_(Obj({"name": R(NAME), "text": R(TEXT)}), 50, True))})),
    "ack": (Obj({"id": R(Int), "ok": O(Bool)}), OK),
    "brief": (Obj(CALLER), Obj({"text": O(TEXT)})),
    "send": (Obj({**CALLER, "to": R(Label(128)), "text": R(TEXT),
                  "file_name": O(FILE_NAME), "file_content": O(FileContent)}),
             Obj({"ok": O(Bool), "id": O(Int), "reply": O(ONE_LINE)})),
    "notes_write": (Obj({**CALLER, "topic": R(NAME), "text": R(TEXT)}), OK),
    "state_write": (Obj({**CALLER, "topic": O(NAME), "text": R(TEXT)}), OK),
    "status": (Obj({**CALLER, "topic": O(NAME), "text": R(ONE_LINE)}), OK),
    "inbox": (Obj(CALLER), Obj({"messages": O(MESSAGES)})),
    "who": (Obj(CALLER), Obj({"chats": O(List_(Obj({
        "chat": R(AnyChat), "person": O(Label(64)), "machine": O(Label(64)), "project": O(NAME),
        "name": O(Text(80, lines=False)), "state": O(Choice("busy", "idle", "ended"))}),
        200, True))})),
    "projects": (Obj(CALLER), Obj({"projects": O(List_(NAME, 200, True))})),
    "notes_read": (Obj({**CALLER, "topic": R(NAME)}),
                   Obj({"topic": O(NAME), "text": O(Text(TEXT_FILE_MAX))})),
    "notes_list": (Obj(CALLER), Obj({"topics": O(List_(NAME, 500, True))})),
    "fetch_file": (Obj({**CALLER, "id": R(Int)}),
                   Obj({"name": R(FILE_NAME), "size": O(Int), "content": R(FileContent)})),
    "work": (Obj({**CALLER, "prompt": R(Text(32768))}), OK),
    "close_worker": (Obj({**CALLER, "worker": R(Chat)}), OK),
}

# Token shapes that must never leave in a chat's text (K-5). The device token and the chat key
# themselves are checked too.
TOKEN_RX = re.compile(
    r"sk-ant-[A-Za-z0-9_-]{20,}|\bsk-(?:proj-)?[A-Za-z0-9_-]{32,}|\bgh[pousr]_[A-Za-z0-9]{30,}"
    r"|github_pat_[A-Za-z0-9_]{40,}|glpat-[A-Za-z0-9_-]{20,}|xox[abprs]-[A-Za-z0-9-]{10,}"
    r"|\bAKIA[0-9A-Z]{16}\b|-----BEGIN [A-Z ]*PRIVATE KEY-----")


# ---------------------------------------------------------------------------- the box side

def peer_uid(conn: socket.socket) -> int:
    """The caller's user id, as the kernel names it (SO_PEERCRED)."""
    raw = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("iII"))
    return struct.unpack("iII", raw)[1]


def read_line(conn: socket.socket, limit: int = MAX_REQUEST, timeout: float = READ_TIMEOUT) -> bytes:
    deadline = time.monotonic() + timeout
    buf = b""
    while b"\n" not in buf:
        left = deadline - time.monotonic()
        if left <= 0:
            raise Bad("the request took too long")
        conn.settimeout(left)
        try:
            got = conn.recv(65536)
        except socket.timeout:
            raise Bad("the request took too long") from None
        if not got:
            break
        buf += got
        if len(buf) > limit:
            raise Bad("the request is too large")
    line = buf.split(b"\n", 1)[0]
    if len(line) > limit:
        raise Bad("the request is too large")
    return line


# ---------------------------------------------------------------------------- the hub side

class CertMismatch(OSError):
    pass


class PinnedConnection(http.client.HTTPSConnection):
    """https that accepts the hub only by its certificate's SHA-256 fingerprint, checked before
    any byte of the request (and its token) goes out. The same approach as kaptyorka's http.py."""

    def __init__(self, host: str, port: int, pins: List[str], timeout: float):
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE  # the fingerprint decides
        super().__init__(host, port, timeout=timeout, context=ctx)
        self._pins = pins

    def connect(self):
        super().connect()
        got = hashlib.sha256(self.sock.getpeercert(binary_form=True)).hexdigest()
        if not any(hmac.compare_digest(got, p) for p in self._pins):
            self.sock.close()
            raise CertMismatch(got)


def hub_error(status: int, body: bytes) -> str:
    """One plain line from a hub's refusal."""
    text = ""
    try:
        data = json.loads(body.decode("utf-8"))
        if isinstance(data, dict) and isinstance(data.get("error"), str):
            text = data["error"]
    except ValueError:
        text = body.decode("utf-8", "replace")
    text = plain(text.strip().split("\n", 1)[0] if text.strip() else "", False)[:200]
    return f"the hub refused ({status}): {text}" if text else f"the hub refused ({status})"


# ---------------------------------------------------------------------------- the relay

def load_conf(path: str) -> Dict[str, Any]:
    conf: Dict[str, str] = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                conf[k.strip()] = v.strip()
    out: Dict[str, Any] = {}
    socket.inet_pton(socket.AF_INET6 if ":" in conf["hub_ip"] else socket.AF_INET, conf["hub_ip"])
    out["hub_ip"] = conf["hub_ip"]   # an address: the relay has no DNS (IPAddressAllow= only)
    out["hub_port"] = int(conf["hub_port"])
    pins = [p.strip().lower().replace(":", "") for p in conf["hub_sha256"].split(",") if p.strip()]
    if not pins or not all(re.fullmatch(r"[0-9a-f]{64}", p) for p in pins):
        raise ValueError("hub_sha256 must be one or more SHA-256 fingerprints")
    out["pins"] = pins
    out["name"] = conf["name"]
    if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9._ -]{0,63}", out["name"]):
        raise ValueError("name must be a short plain name")
    out["version"] = conf["version"]
    if not re.fullmatch(r"[0-9A-Za-z.+-]{1,32}", out["version"]):
        raise ValueError("version is not a version")
    out["idle_exit"] = float(conf.get("idle_exit", "600"))
    return out


class Relay:
    def __init__(self, conf: Dict[str, Any], device_token: str, chat_key: bytes,
                 log: Callable[[str], None] = lambda s: print(s, flush=True)):
        if not device_token or not re.fullmatch(r"[\x21-\x7e]+", device_token):
            raise ValueError("the device token is empty or not plain ASCII")
        if len(chat_key) < 16:
            raise ValueError("the chat key is too short")
        self.conf, self.token, self.key, self.log = conf, device_token, chat_key, log
        self.chats: Dict[str, str] = {}   # hash -> session id, learned from what the box sent
        self.lock = threading.Lock()

    # chat ids
    def hash_chat(self, raw: str) -> str:
        h = hmac.new(self.key, raw.encode(), hashlib.sha256).hexdigest()[:16]
        with self.lock:
            self.chats.pop(h, None)
            self.chats[h] = raw
            while len(self.chats) > 4096:
                self.chats.pop(next(iter(self.chats)))
        return h

    def unhash_chat(self, h: str) -> Optional[str]:
        if not HASH_RX.fullmatch(h):
            return None
        with self.lock:
            return self.chats.get(h)

    # one call
    def filter_out(self, call: str, args: Any) -> Dict[str, Any]:
        """The body for the hub, built only from allowed fields. Raises Bad."""
        if call not in CALLS:
            raise Bad(f"unknown call {plain(str(call), False)[:40]!r}")
        ctx = Context(self)
        body = CALLS[call][0](args, ctx, "out")
        secrets = (self.token, self.key.decode("latin-1"))
        for s in ctx.strings:
            if any(x and x in s for x in secrets) or TOKEN_RX.search(s):
                raise Bad("refused: the text holds something shaped like a key or token")
        if call == "join":
            body = {"name": self.conf["name"]}
        return body

    def filter_in(self, call: str, answer: Any) -> Dict[str, Any]:
        """What the box may see of the hub's answer. Raises Bad."""
        return CALLS[call][1](answer, Context(self), "in")

    def ask_hub(self, call: str, body: Dict[str, Any]) -> Tuple[int, bytes, str]:
        c = PinnedConnection(self.conf["hub_ip"], self.conf["hub_port"], self.conf["pins"],
                             HUB_TIMEOUT)
        try:
            data = json.dumps(body, ensure_ascii=True).encode()
            c.putrequest("POST", "/box/v1/" + call, skip_host=True, skip_accept_encoding=True)
            for k, v in (("Host", self.conf["hub_ip"]), ("Authorization", "Bearer " + self.token),
                         ("X-Kaptyorka-Version", self.conf["version"]),
                         ("X-Kaptyorka-Mode", "personal"), ("Content-Type", "application/json"),
                         ("Content-Length", str(len(data))), ("Connection", "close")):
                c.putheader(k, v)
            c.endheaders(data)
            r = c.getresponse()
            raw = r.read(MAX_ANSWER + 1)
            return r.status, raw, r.getheader("Content-Type", "") or ""
        finally:
            c.close()

    def call(self, call: str, args: Any) -> Dict[str, Any]:
        """One whole call: filter out, ask the hub, filter in. Always returns the box's answer."""
        try:
            body = self.filter_out(call, args)
        except Bad as e:
            return {"ok": False, "error": f"{call}: {e}"}
        try:
            status, raw, ctype = self.ask_hub(call, body)
        except CertMismatch as e:
            self.log(f"relay: hub certificate {e.args[0][:16]}... is not the pinned one; nothing sent")
            return {"ok": False, "error": "the hub's certificate is not the one this computer "
                                          "joined with; nothing was sent"}
        except (OSError, http.client.HTTPException, ssl.SSLError):
            return {"ok": False, "error": "cannot reach the hub"}
        if status // 100 == 3:
            return {"ok": False, "error": "the hub answered with a redirect; dropped"}
        if status != 200:
            return {"ok": False, "error": hub_error(status, raw[:4096])}
        if len(raw) > MAX_ANSWER:
            return {"ok": False, "error": "the hub's answer is too large; dropped"}
        if not ctype.split(";")[0].strip().lower() == "application/json":
            return {"ok": False, "error": "the hub's answer is not JSON; dropped"}
        try:
            return {"ok": True, "result": self.filter_in(call, json.loads(raw.decode("utf-8")))}
        except (ValueError, Bad):
            return {"ok": False, "error": "the hub's answer did not have the expected shape; dropped"}

    # one connection
    def serve_conn(self, conn: socket.socket, person: str, want_uid: Optional[int]) -> None:
        try:
            try:
                uid = peer_uid(conn)
            except OSError:
                uid = -1
            if want_uid is None or uid != want_uid:
                self.log(f"relay: refused uid {uid} on {person}'s door")
                self._answer(conn, {"ok": False, "error": "refused: you are not this box's user"})
                return
            try:
                req = json.loads(read_line(conn).decode("utf-8"))
                if not isinstance(req, dict) or not isinstance(req.get("call"), str):
                    raise Bad("send one JSON object with a call and its args")
                call, args = req["call"], req.get("args", {})
            except (ValueError, Bad) as e:
                self._answer(conn, {"ok": False, "error": str(e) if isinstance(e, Bad)
                                    else "the request is not JSON"})
                return
            answer = self.call(call, args)
            self.log(f"relay: {person} {plain(call, False)[:40]} "
                     f"{'ok' if answer['ok'] else 'refused'}")
            self._answer(conn, answer)
        except OSError:
            pass
        finally:
            conn.close()

    @staticmethod
    def _answer(conn: socket.socket, answer: Dict[str, Any]) -> None:
        conn.settimeout(READ_TIMEOUT)
        conn.sendall(json.dumps(answer, ensure_ascii=False).encode() + b"\n")


# ---------------------------------------------------------------------------- systemd

def doors_from_systemd(env=os.environ) -> List[Tuple[socket.socket, str]]:
    """The listening sockets systemd passed in, with the person each one belongs to
    (FileDescriptorName=box-<person>)."""
    if env.get("LISTEN_PID") != str(os.getpid()):
        raise SystemExit("relay: no sockets from systemd (start it through its socket units)")
    n = int(env.get("LISTEN_FDS", "0"))
    names = env.get("LISTEN_FDNAMES", "").split(":")
    doors = []
    for i in range(n):
        name = names[i] if i < len(names) else ""
        s = socket.socket(fileno=3 + i)
        person = name[4:] if name.startswith("box-") else ""
        if s.family != socket.AF_UNIX or not PERSON_RX.fullmatch(person):
            s.close()
            continue
        doors.append((s, person))
    return doors


def box_uid(person: str) -> Optional[int]:
    try:
        return pwd.getpwnam("kap-" + person).pw_uid
    except KeyError:
        return None


def serve(relay: Relay, doors: List[Tuple[socket.socket, str]]) -> None:
    sel = selectors.DefaultSelector()
    for s, person in doors:
        s.setblocking(False)
        sel.register(s, selectors.EVENT_READ, person)
    slots = threading.BoundedSemaphore(MAX_CLIENTS)
    busy = [0]
    last = [time.monotonic()]
    lock = threading.Lock()

    def run(conn, person, uid):
        try:
            relay.serve_conn(conn, person, uid)
        finally:
            with lock:
                busy[0] -= 1
                last[0] = time.monotonic()
            slots.release()

    relay.log(f"relay: up, doors for {', '.join(p for _, p in doors) or 'nobody'}")
    while True:
        for key, _ in sel.select(timeout=5):
            try:
                conn, _ = key.fileobj.accept()
            except (BlockingIOError, InterruptedError):
                continue
            conn.setblocking(True)
            if not slots.acquire(blocking=False):
                conn.close()
                continue
            with lock:
                busy[0] += 1
            person = key.data
            threading.Thread(target=run, args=(conn, person, box_uid(person)), daemon=True).start()
        with lock:
            idle = busy[0] == 0 and time.monotonic() - last[0] > relay.conf["idle_exit"]
        if idle:  # no chat is talking: no connection, nothing running. systemd starts it again.
            relay.log("relay: idle, exiting")
            return


def main(argv: List[str]) -> int:
    conf = load_conf(argv[1] if len(argv) > 1 else "/etc/kaptyorka/relay.conf")
    cred = os.environ.get("CREDENTIALS_DIRECTORY", "")
    if not cred:
        raise SystemExit("relay: no credentials (LoadCredential=) from systemd")
    with open(os.path.join(cred, "device-token"), encoding="ascii") as f:
        token = f.read().strip()
    with open(os.path.join(cred, "chat-key"), "rb") as f:
        key = f.read().strip()
    relay = Relay(conf, token, key)
    serve(relay, doors_from_systemd())
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
