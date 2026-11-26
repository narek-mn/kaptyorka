"""kaptyorka relay: the only kaptyorka code on a personal laptop outside the box.

It holds the computer's hub key and forwards a fixed list of calls from the box to the hub
over TLS, accepting the hub only by its pinned certificate fingerprint. It is a filter, not a
pipe: every field of every call is checked both ways and anything not in the table below is
dropped (secure design v2, sections 4.3 and 6).

Each door belongs to one person's box. Chats, messages and files are kept per door: a box
sees, acks and fetches only what came in through its own door, and its chat ids are hashed
with the person inside, so one box can never act for another person's chats.

Python standard library only. It imports nothing from the rest of kaptyorka, so it can be read
and audited on its own. It runs as `python3 -I relay.py /etc/kaptyorka/relay.conf` under
kaptyorka-relay.service, with its doors passed in by systemd (one Unix socket per person,
kaptyorka-relay-box@<person>.socket) and its secrets passed in with LoadCredential=.

The box speaks one JSON line per connection: {"call": "<name>", "args": {...}}. The relay
answers one JSON line, {"ok": true, "result": {...}} or {"ok": false, "error": "<one line>"}
(a refusal by the hub may add "hub_said": the hub's own words, cleaned, never the relay's),
and closes. To the hub it sends POST /box/v1/<call> with a JSON body built only from the
filtered fields, the device token, the version and the mode.
"""
import base64
import hashlib
import hmac
import http.client
import json
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
import unicodedata
from typing import Any, Callable, Dict, List, Optional, Tuple

MAX_REQUEST = 2_621_440      # one line from the box: a 1 MiB file in base64 plus text
MAX_ANSWER = 2_097_152       # one answer from the hub
MAX_CLIENTS = 16             # connections handled at once; more get "busy" and are closed
MAX_PER_DOOR = 6             # of those, at most this many from one door
BIG = 256 * 1024             # a request or answer larger than this is "large"
MAX_BIG = 2                  # large requests handled at once (MemoryMax=200M)
BIG_WAIT = 20                # seconds a large request may wait for its turn
MAX_BARE = 65536             # bytes of JSON outside strings: numbers, keys' quotes, brackets
MAX_PARTS = 2000             # objects and lists in one JSON line
READ_TIMEOUT = 15            # seconds for the box to send its line
HUB_TIMEOUT = 20             # seconds for each read from the hub
HUB_TOTAL = 30               # seconds for the whole hub exchange, connect to last byte
SEEN_MAX = 4096              # chats, message ids and file ids kept per door
TEXT_FILE_MAX = 256 * 1024   # 6.3: text files up to 256 KB
BIN_FILE_MAX = 1024 * 1024   # 6.3: other files up to 1 MB
PERSON_RX = re.compile(r"[a-z_][a-z0-9_-]{0,30}")   # same rule as `enter` (4.7)


class Bad(ValueError):
    """A field or a whole call that the table does not allow. The text is one plain line."""


BUSY = {"ok": False, "error": "the relay is busy, try again"}
FAILED = {"ok": False, "error": "the relay could not handle this request"}


# ---------------------------------------------------------------------------- text cleaning

_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]"                         # CSI: colours, cursor
                   r"|\x1b\][^\x07\x1b\n]{0,256}(?:\x07|\x1b\\)?"       # OSC: titles, clipboard
                   r"|\x1b[PX^_][^\x1b\n]{0,256}(?:\x1b\\)?"            # DCS, SOS, PM, APC
                   r"|\x1b[ -/]*[0-~]?")                                # any other escape
# Invisible and line-breaking characters: every format character (Cf: direction marks and
# overrides, zero-width spaces and joiners, soft hyphen, word joiner, BOM, ...) and the line
# and paragraph separators (Zl, Zp). No Cf, Zl or Zp lies above U+1FFFF except the tag block,
# which _HIDDEN covers whole. Unassigned characters (Cn) stay: Python's Unicode table is older
# than the newest emoji.
_INVISIBLE = {cp: None for cp in range(0x20000) if unicodedata.category(chr(cp)) in ("Cf", "Zl", "Zp")}
# Lone surrogates (cannot be sent as UTF-8), private use, and the tag characters U+E0000-E007F
# that show nothing on screen but read as letters to an AI model.
_HIDDEN = re.compile("[\ud800-\udfff-\U000e0000-\U000e007f\U000f0000-\U0010ffff]+")
_CTRL_ALL = {c: None for c in list(range(0, 32)) + list(range(127, 160))}
_CTRL_ALL.update(_INVISIBLE)
_CTRL_KEEP_LINES = {k: v for k, v in _CTRL_ALL.items() if k not in (9, 10, 0x200C, 0x200D)}


def plain(s: str, lines: bool) -> str:
    """Terminal escape codes, control characters, direction overrides and invisible characters
    removed. With lines, tabs and line breaks stay, and the zero-width joiners that some
    scripts and emoji need."""
    s = _ANSI.sub("", s.replace("\r\n", "\n"))
    return _HIDDEN.sub("", s.translate(_CTRL_KEEP_LINES if lines else _CTRL_ALL))


def cut(s: str, max_bytes: int) -> str:
    """At most max_bytes of UTF-8, never half a character."""
    b = s.encode("utf-8", "replace")
    return s if len(b) <= max_bytes else b[:max_bytes].decode("utf-8", "ignore")


_STRING = re.compile(rb'"[^"\\]*(?:\\.[^"\\]*)*"')


def parse_json(raw: bytes) -> Any:
    """json.loads, after a cheap check that the line is not mostly structure: each "[" or "{"
    costs far more memory than its one byte. Raises Bad."""
    bare = _STRING.sub(b'""', raw)
    if len(bare) > MAX_BARE or bare.count(b"[") + bare.count(b"{") > MAX_PARTS:
        raise Bad("the request has too many parts")
    try:
        return json.loads(raw.decode("utf-8"))
    except (ValueError, RecursionError):
        raise Bad("the request is not JSON") from None


# ---------------------------------------------------------------------------- field kinds
# Each kind is a function (value, ctx, way) -> cleaned value; it raises Bad when the value
# is not allowed. way is "out" (box to hub) or "in" (hub to box). ctx is the call's Context.

class Context:
    def __init__(self, relay: "Relay", person: str):
        self.relay, self.person = relay, person
        self.strings: List[str] = []           # every outgoing string, for the token-shape check
        self.chats: Dict[str, str] = {}        # hash -> session id named in this call (out)
        self.got_msgs: List[int] = []          # message ids in this answer (in)
        self.got_files: List[int] = []         # file ids in this answer (in)
        self.unknown_chats = 0                 # items dropped for a chat this door never named

    def holds_secret(self, s: str) -> bool:
        return any(x in s for x in self.relay.secrets)

    def scrub(self, s: str) -> str:
        for x in self.relay.secrets:
            s = s.replace(x, "[removed]")
        return s


def Id(rx: str) -> Callable:
    pat = re.compile(rx)

    def f(v, ctx, way):
        if not isinstance(v, str) or not pat.fullmatch(v):
            raise Bad("not a valid name or id")
        if way == "out":
            ctx.strings.append(v)
        elif ctx.holds_secret(v):
            raise Bad("not a valid name or id")
        return v
    return f


NAME = Id(r"[A-Za-z0-9_][A-Za-z0-9._-]{0,63}")       # team folder, topic, skill
HUBID = Id(r"[A-Za-z0-9_][A-Za-z0-9._:@-]{0,127}")   # an id the hub hands out


def Text(max_bytes: int, lines: bool = True) -> Callable:
    def f(v, ctx, way):
        if not isinstance(v, str):
            raise Bad("not text")
        v = plain(v, lines)
        if way == "out":
            v = cut(v, max_bytes)
            ctx.strings.append(v)
            return v
        return cut(ctx.scrub(v), max_bytes)
    return f


def Label(max_chars: int) -> Callable:
    """A short one-line name (a person, a machine, a send target): refused, not cut, when it
    is too long or holds control or invisible characters, because a cut name names someone
    else."""
    def f(v, ctx, way):
        if not isinstance(v, str) or not v or len(v) > max_chars or plain(v, False) != v:
            raise Bad("not a valid name")
        if way == "out":
            ctx.strings.append(v)
        elif ctx.holds_secret(v):
            raise Bad("not a valid name")
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
    # the range test first: it is exact for any int and false for nan and inf
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not 0 <= v < 2 ** 53:
        raise Bad("not a number")
    return v


def Bool(v, ctx, way):
    if not isinstance(v, bool):
        raise Bad("not true or false")
    return v


def Received(kind: str) -> Callable:
    """A message or file id. Out: only one that came in through this door (ack, fetch_file)."""
    def f(v, ctx, way):
        v = Int(v, ctx, way)
        if way == "out" and not ctx.relay.has_seen(ctx.person, kind, v):
            raise Bad(f"not a {kind} this box received")
        return v
    return f


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
        elif ctx.holds_secret(raw.decode("latin-1")):
            raise Bad("the file holds this computer's key")
        return "base64:" + base64.b64encode(raw).decode()
    try:
        size = len(v.encode("utf-8"))
    except UnicodeEncodeError:
        raise Bad("text file with a broken character") from None
    if size > TEXT_FILE_MAX:
        raise Bad("text file larger than 256 KB")
    if way == "out":
        ctx.strings.append(v)
    elif ctx.holds_secret(v):
        raise Bad("the file holds this computer's key")
    return v


CHAT_RX = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._:@-]{0,127}")   # a tool's session id, in the box
HASH_RX = re.compile(r"[0-9a-f]{16}")


def Chat(v, ctx, way):
    """A chat of this box. Out: the tool's session id, sent as a keyed hash (16 characters).
    In: a hash this door's box sent, turned back into the session id; any other is refused."""
    if way == "out":
        if not isinstance(v, str) or not CHAT_RX.fullmatch(v):
            raise Bad("not a valid chat id")
        h = ctx.relay.hash_chat(ctx.person, v)
        ctx.chats[h] = v
        return h
    raw = ctx.relay.unhash_chat(ctx.person, v) if isinstance(v, str) else None
    if raw is None:
        ctx.unknown_chats += 1
        raise Bad("not a chat of this box")
    return raw


def AnyChat(v, ctx, way):
    """A chat the hub names (in: only): ("mine", session id) for one of this box, else
    ("hub", the hub's opaque id)."""
    raw = ctx.relay.unhash_chat(ctx.person, v) if isinstance(v, str) else None
    return ("mine", raw) if raw is not None else ("hub", HUBID(v, ctx, way))


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

_MESSAGE = Obj({  # 6.5: a message for a chat
    "id": R(Int), "chat": R(Chat), "text": R(TEXT),
    # the sender's own words (the helper fences them) and the name an answer goes to
    "body": O(TEXT), "reply": O(Text(200, lines=False)),
    "person": O(Label(64)), "machine": O(Label(64)), "kind": O(Choice("chat", "page", "manager")),
    "files": O(List_(Obj({"id": R(Int), "name": R(FILE_NAME), "size": R(Int)}), 10, True)),
})


def MESSAGE(v, ctx, way):
    """A message that passed; its id and its files' ids are what this door may ack and fetch."""
    m = _MESSAGE(v, ctx, way)
    ctx.got_msgs.append(m["id"])
    ctx.got_files.extend(f["id"] for f in m.get("files", []))
    return m


MESSAGES = List_(MESSAGE, 50, True)

_WHO_ROW = Obj({
    "chat": R(AnyChat), "person": O(Label(64)), "machine": O(Label(64)), "project": O(NAME),
    "name": O(Text(80, lines=False)), "state": O(Choice("busy", "idle", "ended"))})


def WHO_ROW(v, ctx, way):
    """One chat of the team. A chat of this box comes back as its session id with mine: true;
    any other as "hub:<the hub's id>", so the two can never be mixed up."""
    row = _WHO_ROW(v, ctx, way)
    whose, chat = row["chat"]
    row["chat"] = chat if whose == "mine" else "hub:" + chat
    row["mine"] = whose == "mine"
    return row


# call: (what the box may send, what the hub may answer). No `join`: that is the installer's
# step (6.1), never a chat's.
CALLS: Dict[str, Tuple[Callable, Callable]] = {
    "report": (
        Obj({"chats": R(List_(Obj({
            "chat": R(Chat), "project": R(NAME), "name": O(Text(80, lines=False)),
            "state": R(Choice("busy", "idle", "ended")), "last_activity": O(Num)}), 64, False))}),
        Obj({"messages": O(MESSAGES),
             "close": O(List_(Obj({"chat": R(Chat)}), 20, True)),
             "helpers": O(List_(Obj({"project": R(NAME), "prompt": R(Text(32768))}), 5, True)),
             "skills": O(List_(Obj({"name": R(NAME), "text": R(TEXT)}), 50, True))})),
    "ack": (Obj({"id": R(Received("message")), "ok": O(Bool)}), OK),
    "brief": (Obj(CALLER), Obj({"text": O(TEXT)})),
    "send": (Obj({**CALLER, "to": R(Label(128)), "text": R(TEXT),
                  "file_name": O(FILE_NAME), "file_content": O(FileContent)}),
             Obj({"ok": O(Bool), "id": O(Int), "reply": O(ONE_LINE)})),
    "notes_write": (Obj({**CALLER, "topic": R(NAME), "text": R(TEXT)}), OK),
    "state_write": (Obj({**CALLER, "topic": O(NAME), "text": R(TEXT)}), OK),
    "status": (Obj({**CALLER, "topic": O(NAME), "text": R(ONE_LINE)}), OK),
    "inbox": (Obj(CALLER), Obj({"messages": O(MESSAGES)})),
    "who": (Obj(CALLER), Obj({"chats": O(List_(WHO_ROW, 200, True))})),
    "projects": (Obj(CALLER), Obj({"projects": O(List_(NAME, 200, True))})),
    "notes_read": (Obj({**CALLER, "topic": R(NAME)}),
                   Obj({"topic": O(NAME), "text": O(Text(TEXT_FILE_MAX))})),
    "notes_list": (Obj(CALLER), Obj({"topics": O(List_(NAME, 500, True))})),
    "fetch_file": (Obj({**CALLER, "id": R(Received("file"))}),
                   Obj({"name": R(FILE_NAME), "size": O(Int), "content": R(FileContent)})),
    "work": (Obj({**CALLER, "prompt": R(Text(32768))}), OK),
    "close_worker": (Obj({**CALLER, "worker": R(Chat)}), OK),
}

# Token shapes that must never leave in any outgoing string (K-5): text, names, file names
# and file contents. The device token and the chat key themselves are checked too.
TOKEN_RX = re.compile(
    r"sk-ant-[A-Za-z0-9_-]{20,}|\bsk-(?:proj-)?[A-Za-z0-9_-]{32,}|\bgh[pousr]_[A-Za-z0-9]{30,}"
    r"|github_pat_[A-Za-z0-9_]{40,}|glpat-[A-Za-z0-9_-]{20,}|xox[abprs]-[A-Za-z0-9-]{10,}"
    r"|\bAKIA[0-9A-Z]{16}\b|-----BEGIN [A-Z ]*PRIVATE KEY-----"
    r"|\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}"         # a JWT
    r"|\bAIza[0-9A-Za-z_-]{35}|\bhf_[A-Za-z0-9]{30,}|\bnpm_[A-Za-z0-9]{36}"
    r"|\bpypi-AgE[A-Za-z0-9_-]{50,}|\b[sr]k_live_[0-9A-Za-z]{24,}|\bxapp-\d-[A-Za-z0-9-]{10,}"
    r"|b3BlbnNzaC1rZXktdjE")                                   # the body of an OpenSSH key


# ---------------------------------------------------------------------------- a box's own sign-ins (red team round 3, finding 2)
# The tools' sign-in files in a box (OpenCode, Codex, Cursor) can be read by the chat's own
# commands, and TOKEN_RX knows only public token shapes: a base64, hex or opaque copy passed.
# So the relay is told each box's actual secret values (root writes them per box door into
# SECRETS_DIR/<box>, from enter and kaptyorka-guard; the relay never reads a box file) and
# refuses outgoing text that holds one raw, reversed, in hex (any case, any separators) or in
# base64 (standard or URL-safe, at any alignment, wrapped or not). Matching is by aligned
# pieces of CHUNK characters: any run of 2 * CHUNK - 1 characters of such a form is found.
#
# This is a backstop, not a wall: a tricked model that cuts a value into short pieces, or
# encodes it in a way not listed here (another alphabet, a cipher, one character per message),
# gets it out. The standing rule and the trick filter are the first line; ending the box's
# sign-ins at the vendor after a suspected leak is the remedy.

SECRETS_DIR = "/run/kaptyorka/box-secrets"
SECRETS_FILE_MAX = 4 * 1024 * 1024
MAX_SECRET_VALUES = 256
CHUNK = 16
_WS = re.compile(r"\s+")
_NOT_HEX = re.compile(r"[^0-9a-f]")
_URLSAFE = str.maketrans("+/", "-_")


def secret_forms(value: bytes) -> Tuple[List[str], List[str]]:
    """(forms to find in the text as it is, forms to find in its hex digits) of one value."""
    text = value.decode("latin-1")
    forms = [text, text[::-1]]
    for k in (0, 1, 2):   # base64 of the value at each of the three byte offsets in a longer text
        n = k + len(value)
        e = base64.b64encode(b"\0" * k + value).decode()
        core = e[(0, 2, 3)[k]:4 * (n // 3) + (8 * (n % 3)) // 6]
        forms += [core, core.translate(_URLSAFE)]
    return forms, [value.hex()]


def _chunks(form: str) -> List[str]:
    if len(form) < CHUNK:
        return []
    return [form[i:i + CHUNK] for i in range(0, len(form) - CHUNK + 1, CHUNK)] + [form[-CHUNK:]]


def _scan(t: str, pieces: set) -> bool:
    return any(t[i:i + CHUNK] in pieces for i in range(len(t) - CHUNK + 1))


class SecretGuard:
    """The known secret values of one door, as pieces to look for."""

    def __init__(self, values: List[bytes]):
        self.plain: set = set()
        self.hex: set = set()
        for v in values:
            forms, hexes = secret_forms(v)
            for f in forms:
                self.plain.update(_chunks(f))
            for f in hexes:
                self.hex.update(_chunks(f))

    def holds(self, s: str) -> bool:
        if not self.plain:
            return False
        squeezed = _WS.sub("", s)
        if _scan(s, self.plain) or (squeezed != s and _scan(squeezed, self.plain)):
            return True
        return _scan(_NOT_HEX.sub("", s.lower()), self.hex)


def parse_secrets(raw: bytes) -> List[bytes]:
    """The values in one box's file, strictly: {"values": [text, ...]}. Raises ValueError."""
    data = json.loads(raw.decode("utf-8"))
    vals = data.get("values") if isinstance(data, dict) else None
    if not isinstance(vals, list) or len(vals) > MAX_SECRET_VALUES:
        raise ValueError("not a list of values")
    out = []
    for v in vals:
        if not isinstance(v, str) or not 8 <= len(v) <= 8192:
            raise ValueError("a value is not text of 8 to 8192 characters")
        out.append(v.encode("utf-8"))
    return out


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
        der = self.sock.getpeercert(binary_form=True)
        if not der:  # a cipher without a certificate
            self.sock.close()
            raise CertMismatch("none")
        got = hashlib.sha256(der).hexdigest()
        if not any(hmac.compare_digest(got, p) for p in self._pins):
            self.sock.close()
            raise CertMismatch(got)


HUB_REFUSALS = {400: "the hub did not accept the request",
                401: "the hub does not accept this computer's key",
                403: "the hub does not accept this computer's key",
                404: "the hub does not know this call",
                413: "the request is too large for the hub",
                429: "the hub is busy, try again later"}


def hub_error(status: int, body: bytes, ctx: Context) -> Dict[str, Any]:
    """The box's answer to a hub's refusal: a fixed line written by the relay, and the hub's
    own words apart in "hub_said", one plain line with this computer's key removed."""
    if status in HUB_REFUSALS:
        line = HUB_REFUSALS[status]
    elif status >= 500:
        line = "the hub has a problem, try again later"
    else:
        line = "the hub refused the call"
    answer = {"ok": False, "error": f"{line} ({status})"}
    text = ""
    try:
        data = json.loads(body.decode("utf-8"))
        if isinstance(data, dict) and isinstance(data.get("error"), str):
            text = data["error"]
    except (ValueError, RecursionError):
        text = body.decode("utf-8", "replace")
    text = text.strip().split("\n", 1)[0] if text.strip() else ""
    text = cut(ctx.scrub(plain(text, False)), 200).strip()
    if text:
        answer["hub_said"] = text
    return answer


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
    if not 1 <= out["hub_port"] <= 65535:
        raise ValueError("hub_port must be 1 to 65535")
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
    if not 1 <= out["idle_exit"] <= 86400:   # also false for nan
        raise ValueError("idle_exit must be 1 to 86400 seconds")
    # box_uids = alice:1001, bob:1002 (written by the installer): then no user list is read
    if "box_uids" in conf:
        uids = {}
        for item in conf["box_uids"].split(","):
            person, _, uid = item.strip().partition(":")
            if not PERSON_RX.fullmatch(person) or not uid.isdigit() or int(uid) == 0:
                raise ValueError("box_uids must be person:uid, ...")
            uids[person] = int(uid)
        out["box_uids"] = uids
    return out


def read_chat_key(raw: bytes) -> bytes:
    """The chat key file holds hex text (the installer writes `openssl rand -hex 32`)."""
    text = raw.decode("ascii", "replace").strip()
    if not re.fullmatch(r"(?:[0-9a-fA-F]{2}){16,}", text):
        raise ValueError("the chat key must be at least 32 hex characters")
    return bytes.fromhex(text)


class _Door:
    """What one door's box has named or received, each bounded, oldest out first."""

    def __init__(self):
        self.chats: Dict[str, str] = {}    # hash -> session id
        self.message: Dict[int, None] = {}
        self.file: Dict[int, None] = {}


def _remember(d: Dict, k, v=None) -> None:
    d.pop(k, None)
    d[k] = v
    while len(d) > SEEN_MAX:
        d.pop(next(iter(d)))


class Relay:
    def __init__(self, conf: Dict[str, Any], device_token: str, chat_key: bytes,
                 log: Callable[[str], None] = lambda s: print(s, flush=True), secrets_dir: str = SECRETS_DIR):
        if not re.fullmatch(r"[\x21-\x7e]{16,}", device_token):
            raise ValueError("the device token is shorter than 16 or not plain ASCII")
        if len(chat_key) < 16:
            raise ValueError("the chat key is too short")
        self.conf, self.token, self.key, self.log = conf, device_token, chat_key, log
        self.secrets = tuple(s for s in (device_token, chat_key.decode("latin-1"), chat_key.hex()) if s)
        self.own = [device_token.encode(), chat_key, chat_key.hex().encode()]
        self.secrets_dir = secrets_dir
        self.guards: Dict[str, Tuple[Any, SecretGuard]] = {}
        self.doors: Dict[str, _Door] = {}
        self.lock = threading.Lock()
        self.big = threading.BoundedSemaphore(MAX_BIG)

    def _door(self, person: str) -> _Door:
        # callers hold self.lock
        if person not in self.doors:
            self.doors[person] = _Door()
        return self.doors[person]

    # chat ids: one keyed hash per person and chat, so a session id guessed by another box
    # gives another hash, and each door looks only in its own map
    def hash_chat(self, person: str, raw: str) -> str:
        return hmac.new(self.key, person.encode() + b"\0" + raw.encode(), hashlib.sha256).hexdigest()[:16]

    def unhash_chat(self, person: str, h: str) -> Optional[str]:
        if not HASH_RX.fullmatch(h):
            return None
        with self.lock:
            d = self.doors.get(person)
            return d.chats.get(h) if d else None

    def learn(self, ctx: Context) -> None:
        """The chats a call named, kept once the hub took the call (200), so calls that never
        reach the hub cannot push real chats out."""
        with self.lock:
            d = self._door(ctx.person)
            for h, raw in ctx.chats.items():
                _remember(d.chats, h, raw)

    def has_seen(self, person: str, kind: str, i: int) -> bool:
        with self.lock:
            d = self.doors.get(person)
            return d is not None and i in getattr(d, kind)

    def guard(self, person: str) -> SecretGuard:
        """This door's secret values: its box's file (read again when it changed) and the relay's own."""
        path = os.path.join(self.secrets_dir, person)
        try:
            st = os.stat(path)
            key: Any = (st.st_ino, st.st_mtime_ns, st.st_size)
        except OSError:
            key = None
        with self.lock:
            have = self.guards.get(person)
        if have and have[0] == key:
            return have[1]
        values: List[bytes] = []
        if key is not None:
            try:
                with open(path, "rb") as f:
                    values = parse_secrets(f.read(SECRETS_FILE_MAX + 1)[:SECRETS_FILE_MAX])
            except (OSError, ValueError, RecursionError) as e:
                self.log(f"relay: {person}'s sign-in values cannot be read ({type(e).__name__})")
        g = SecretGuard(values + self.own)
        with self.lock:
            self.guards[person] = (key, g)
        return g

    # one call
    def filter_out(self, person: str, call: str, args: Any) -> Tuple[Dict[str, Any], Context]:
        """The body for the hub, built only from allowed fields. Raises Bad."""
        if call not in CALLS:
            raise Bad("unknown call")
        ctx = Context(self, person)
        body = CALLS[call][0](args, ctx, "out")
        guard = self.guard(person)
        for s in ctx.strings:
            if ctx.holds_secret(s) or TOKEN_RX.search(s):
                raise Bad("refused: the text holds something shaped like a key or token")
            if guard.holds(s):
                raise Bad("refused: the text holds a sign-in of this computer or its box (as it is or encoded)")
        return body, ctx

    def filter_in(self, person: str, call: str, answer: Any) -> Dict[str, Any]:
        """What the box may see of the hub's answer. Raises Bad. The message and file ids that
        pass become what this door may ack and fetch."""
        ctx = Context(self, person)
        result = CALLS[call][1](answer, ctx, "in")
        with self.lock:
            d = self._door(person)
            for i in ctx.got_msgs:
                _remember(d.message, i)
            for i in ctx.got_files:
                _remember(d.file, i)
        if ctx.unknown_chats:
            self.log(f"relay: {person} {call}: {ctx.unknown_chats} item(s) for unknown chats dropped")
        return result

    def ask_hub(self, call: str, body: Dict[str, Any]) -> Tuple[int, bytes, str]:
        c = PinnedConnection(self.conf["hub_ip"], self.conf["hub_port"], self.conf["pins"],
                             HUB_TIMEOUT)
        late = threading.Event()

        def stop():  # the whole exchange is over HUB_TOTAL: cut the socket under http.client
            late.set()
            sock = c.sock
            if sock is not None:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
        killer = threading.Timer(HUB_TOTAL, stop)
        killer.daemon = True
        killer.start()
        try:
            c.connect()
            if late.is_set():
                raise OSError("the hub took too long")
            data = json.dumps(body, ensure_ascii=True).encode()
            ip = self.conf["hub_ip"]
            c.putrequest("POST", "/box/v1/" + call, skip_host=True, skip_accept_encoding=True)
            for k, v in (("Host", f"[{ip}]" if ":" in ip else ip),
                         ("Authorization", "Bearer " + self.token),
                         ("X-Kaptyorka-Version", self.conf["version"]),
                         ("X-Kaptyorka-Mode", "personal"), ("Content-Type", "application/json"),
                         ("Content-Length", str(len(data))), ("Connection", "close")):
                c.putheader(k, v)
            c.endheaders(data)
            r = c.getresponse()
            raw = r.read(MAX_ANSWER + 1)
            if late.is_set():
                raise OSError("the hub took too long")
            return r.status, raw, r.getheader("Content-Type", "") or ""
        finally:
            killer.cancel()
            c.close()

    def call(self, person: str, call: str, args: Any, holding_big: bool = False) -> Dict[str, Any]:
        """One whole call: filter out, ask the hub, filter in. Always returns the box's answer.
        Error lines name only the table's call name, never what the box sent as the name."""
        try:
            body, ctx = self.filter_out(person, call, args)
        except Bad as e:
            if call not in CALLS:
                return {"ok": False, "error": "unknown call"}
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
            return hub_error(status, raw[:4096], ctx)
        self.learn(ctx)
        if len(raw) > MAX_ANSWER:
            return {"ok": False, "error": "the hub's answer is too large; dropped"}
        if not ctype.split(";")[0].strip().lower() == "application/json":
            return {"ok": False, "error": "the hub's answer is not JSON; dropped"}
        took = len(raw) > BIG and not holding_big
        if took and not self.big.acquire(timeout=BIG_WAIT):
            return BUSY
        try:
            return {"ok": True, "result": self.filter_in(person, call, parse_json(raw))}
        except Bad:
            return {"ok": False, "error": "the hub's answer did not have the expected shape; dropped"}
        finally:
            if took:
                self.big.release()

    # one connection
    def serve_conn(self, conn: socket.socket, person: str, want_uid: Optional[int]) -> None:
        name = "-"
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
                line = read_line(conn)
            except Bad as e:
                self._answer(conn, {"ok": False, "error": str(e)})
                return
            big = len(line) > BIG
            if big and not self.big.acquire(timeout=BIG_WAIT):
                self._answer(conn, BUSY)
                return
            try:
                try:
                    req = parse_json(line)
                    if not isinstance(req, dict) or not isinstance(req.get("call"), str):
                        raise Bad("send one JSON object with a call and its args")
                    call, args = req["call"], req.get("args", {})
                except Bad as e:
                    self._answer(conn, {"ok": False, "error": str(e)})
                    return
                name = call if call in CALLS else "unknown"
                answer = self.call(person, call, args, holding_big=big)
            finally:
                if big:
                    self.big.release()
            self.log(f"relay: {person} {name} {'ok' if answer['ok'] else 'refused'}")
            self._answer(conn, answer)
        except OSError:
            pass
        except Exception as e:  # one plain line for the box, the class name for the journal
            self.log(f"relay: {person} {name} failed: {type(e).__name__}")
            try:
                self._answer(conn, FAILED)
            except OSError:
                pass
        finally:
            conn.close()

    @staticmethod
    def _answer(conn: socket.socket, answer: Dict[str, Any]) -> None:
        conn.settimeout(READ_TIMEOUT)
        conn.sendall(json.dumps(answer, ensure_ascii=False).encode("utf-8", "replace") + b"\n")


# ---------------------------------------------------------------------------- systemd

def doors_from_systemd(env=os.environ, log: Callable[[str], None] = lambda s: print(s, flush=True)
                       ) -> List[Tuple[socket.socket, str]]:
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
            log(f"relay: socket {i + 1} of {n} is not a box door; skipped")
            s.close()
            continue
        doors.append((s, person))
    return doors


def box_uid(person: str, conf: Optional[Dict[str, Any]] = None) -> Optional[int]:
    if conf and "box_uids" in conf:
        return conf["box_uids"].get(person)
    try:
        return pwd.getpwnam("kap-" + person).pw_uid
    except KeyError:
        return None


def _say_busy(conn: socket.socket) -> None:
    try:
        conn.setblocking(False)
        conn.send(json.dumps(BUSY).encode() + b"\n")
    except OSError:
        pass
    conn.close()


def serve(relay: Relay, doors: List[Tuple[socket.socket, str]]) -> None:
    sel = selectors.DefaultSelector()
    for s, person in doors:
        s.setblocking(False)
        sel.register(s, selectors.EVENT_READ, person)
    busy = [0]
    per_door: Dict[str, int] = {}
    last = [time.monotonic()]
    lock = threading.Lock()

    def run(conn, person, uid):
        try:
            relay.serve_conn(conn, person, uid)
        finally:
            with lock:
                busy[0] -= 1
                per_door[person] -= 1
                last[0] = time.monotonic()

    relay.log(f"relay: up, doors for {', '.join(p for _, p in doors) or 'nobody'}")
    while True:
        for key, _ in sel.select(timeout=5):
            try:
                conn, _ = key.fileobj.accept()
            except (BlockingIOError, InterruptedError):
                continue
            person = key.data
            with lock:
                full = busy[0] >= MAX_CLIENTS or per_door.get(person, 0) >= MAX_PER_DOOR
                if not full:
                    busy[0] += 1
                    per_door[person] = per_door.get(person, 0) + 1
            if full:
                _say_busy(conn)
                continue
            conn.setblocking(True)
            threading.Thread(target=run, args=(conn, person, box_uid(person, relay.conf)),
                             daemon=True).start()
        with lock:
            idle = busy[0] == 0 and time.monotonic() - last[0] > relay.conf["idle_exit"]
        if idle:  # no chat is talking: no connection, nothing running. systemd starts it again.
            relay.log("relay: idle, exiting")
            return


# What this program may see (its unit's RootDirectory= and binds). ExecStartPre= runs
# `relay.py --viewcheck` inside the same sandbox: anything more in these folders means a setting
# systemd dropped without a word, and the relay does not start.
SYS_HIDDEN = ("block", "bus", "class", "dev", "devices", "firmware", "hypervisor", "kernel", "module", "power")
VIEW = [("/", {"bin", "dev", "etc", "lib", "lib64", "opt", "proc", "root", "run", "sbin", "sys", "tmp", "usr", "var"}),
        ("/etc", {"alternatives", "kaptyorka", "ld.so.cache", "passwd"}),
        ("/etc/kaptyorka", {"relay.conf"}),
        ("/opt", {"kaptyorka"}), ("/opt/kaptyorka", {"relay"}), ("/var", {"tmp"}),
        ("/run", {"credentials", "host", "kaptyorka", "systemd"}), ("/run/kaptyorka", {"box-secrets"}),
        ("/usr/local", set()), ("/sys", set(SYS_HIDDEN) | {"fs"}),
        ("/sys/fs/cgroup", set())] + [("/sys/" + d, set()) for d in SYS_HIDDEN]


def view_problem(view) -> str:
    """'' when no folder in view shows more than it may; else the first extra."""
    for d, may in view:
        try:
            extra = set(os.listdir(d)) - may
        except FileNotFoundError:
            continue
        except OSError as e:
            return f"{d} cannot be listed ({e.strerror})"
        if extra:
            return f"{d} shows {', '.join(sorted(extra)[:8])}"
    return ""


def main(argv: List[str]) -> int:
    if argv[1:2] == ["--viewcheck"]:
        why = view_problem(VIEW)
        if why:
            print(f"relay: its sandbox is not as set: {why}. The relay was not started.", file=sys.stderr)
            return 1
        return 0
    conf = load_conf(argv[1] if len(argv) > 1 else "/etc/kaptyorka/relay.conf")
    cred = os.environ.get("CREDENTIALS_DIRECTORY", "")
    if not cred:
        raise SystemExit("relay: no credentials (LoadCredential=) from systemd")
    with open(os.path.join(cred, "device-token"), encoding="ascii") as f:
        token = f.read().strip()
    with open(os.path.join(cred, "chat-key"), "rb") as f:
        key = read_chat_key(f.read())
    relay = Relay(conf, token, key)
    serve(relay, doors_from_systemd())
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
