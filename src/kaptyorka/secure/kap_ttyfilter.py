#!/usr/bin/python3 -I
"""tty-filter (secure design v2, 4.8): everything a team chat prints passes through here on its
way to the person's terminal, which runs as the person (finding 10).

It drops what lets a program in the box act through the terminal: the clipboard (OSC 52), files
and "run" codes (OSC 1337, kitty's and iTerm2's), window titles, notifications, colour changes
and every other OSC, links unless plain (OSC 8 with an http(s) address that is also the text
shown), DCS, APC, PM and SOS strings, report-back questions that answer with text the box can
choose or that tell about the screen (title, colours, screen checksums, terminal name), window
moves and resizes, printing, concealed text (SGR 8), 8-bit controls and the C0 controls a
terminal acts on (answerback, shift-out). It keeps what the tools need to draw: printable text,
colours and styles, cursor moves, erasing, scroll regions, the alternate screen, mouse and paste
modes, synchronized output, the kitty keyboard flags, and the questions whose answer is a fixed
row of digits (cursor position, device attributes, mode reports). It never passes bracketed
paste off (CSI ? 2004 l): a chat has no need to turn the person's paste guard off.

Nothing the box sets outlives the chat (red team round 5, finding 1): the filter keeps track of
every lasting mode it lets through (mouse reports, focus events, the alternate screen, cursor
keys and keypad, insert mode, scroll region, cursor shape and visibility, the kitty keyboard
flags, modifyOtherKeys, synchronized output, the line-drawing character sets) and, when the
output ends for any reason (the box side closed, the person's side closed, SIGTERM from its
unit's time limit or from enter, an error), it sends what puts each one back to how a shell's
terminal has it between programs. enter sends a fixed reset of its own after the filter ended,
for a filter that was killed outright. Two passed modes are left as the box set them: 12 (cursor
blink) and 1007 (wheel as arrow keys on the alternate screen): their resting state is each
terminal's own setting, so no fixed value is right, and neither breaks the shell.

Started by `enter` (root) as its own unit: a fresh user of its own (DynamicUser=), no network,
no files of anyone, no capabilities and a system-call filter (TIOCSTI into the person's terminal
needs it to be the caller's own terminal or CAP_SYS_ADMIN: neither), holding only two open
devices from systemd-run --pipe:
  tty-filter pty    fd 0 = the person's terminal, fd 1 = the box side (the pty enter made):
                    the person's keys go in unchanged, the box's output comes back filtered
  tty-filter pipe   fd 0 = the box's output, fd 1 = the person's terminal: filtered, one way
Python standard library only. Installed as /usr/local/libexec/kaptyorka/tty-filter.
"""
import codecs
import errno
import os
import re
import select
import signal
import sys

PLAIN = re.compile(r"[^\x00-\x1f\x7f-\x9f]+")
KEEP_C0 = "\x07\x08\x09\x0a\x0b\x0c\x0d"
C1_INTRO = {"\x9b": "[", "\x9d": "]", "\x90": "P", "\x9f": "_", "\x9e": "^", "\x98": "X"}
STR_INTRO = {"]": "osc", "P": "dcs", "_": "apc", "^": "pm", "X": "sos"}
ESC_KEEP = set("78=>MDEH")
CHARSET_INTRO = set("()*+")
CHARSET_KEEP = set("0AB")
MAX_CSI = 64
MAX_OSC = 2048
MAX_LINK_TEXT = 2048
MAX_LINK_HOLD = 8192
# CSI finals kept with no private marker and no intermediate (the rest: by their own rule)
CSI_PLAIN = set("@ABCDEFGHIJKLMPSTXZ`abdefgrsu")
DEC_MODES = {"1", "6", "7", "12", "25", "47", "1000", "1002", "1003", "1004", "1005", "1006", "1007",
             "1015", "1016", "1047", "1048", "1049", "2004", "2026", "2027"}
# The lasting DEC modes the filter restores at the end, with the state a shell's terminal has between
# programs (2004 is not here: it is never switched off, so it is on at the end whenever it was on).
MODE_REST = {"1": "l", "6": "l", "7": "h", "25": "h", "47": "l", "1000": "l", "1002": "l", "1003": "l", "1004": "l",
             "1005": "l", "1006": "l", "1015": "l", "1016": "l", "1047": "l", "1049": "l", "2026": "l", "2027": "l"}
SCREEN_MODES = ("1049", "1047", "47")       # left first: the alternate screen's cursor and contents go with it
SEQ_RX = re.compile(r"\x1b\[([<=>?]?)([0-9;:]*)([ !$]?)([@-~])")
LINK_RX = re.compile(r"https?://[\x21-\x7e]{1,2000}")
LINK_PARAMS_RX = re.compile(r"(id=[A-Za-z0-9_.:-]{1,64})?")


def _sgr(params: str) -> str:
    """SGR without 8 (concealed text), colour arguments left alone; None if nothing remains."""
    if not params:
        return ""
    out, toks, i = [], params.split(";"), 0
    while i < len(toks):
        t = toks[i]
        head = t.split(":", 1)[0]
        if head in ("38", "48", "58") and ":" not in t and i + 1 < len(toks):
            n = 3 if toks[i + 1] == "5" else 5 if toks[i + 1] == "2" else 1
            out += toks[i:i + n]
            i += n
            continue
        if head != "8":
            out.append(t)
        i += 1
    return ";".join(out) if out else None


def csi_ok(params: str, inter: str, final: str):
    """The sequence to send for CSI params inter final, or None to drop it."""
    if any(c not in "0123456789;:<=>?" for c in params) or any(c in "<=>?" for c in params[1:]):
        return None
    priv = params[:1] if params[:1] in ("<", "=", ">", "?") else ""
    body = params[len(priv):]
    if not inter and not priv:
        if final == "m":
            s = _sgr(body)
            return None if s is None else f"\x1b[{s}m"
        if final in CSI_PLAIN:
            return f"\x1b[{body}{final}"
        if final in "hl":
            keep = [p for p in body.split(";") if p == "4"]
            return f"\x1b[{';'.join(keep)}{final}" if keep else None
        if final == "n" and body in ("5", "6"):
            return f"\x1b[{body}n"
        if final == "c" and body in ("", "0"):
            return f"\x1b[{body}c"
        return None
    if priv == "?" and not inter:
        if final in "hl":
            keep = [p for p in body.split(";") if p in DEC_MODES and not (p == "2004" and final == "l")]
            return f"\x1b[?{';'.join(keep)}{final}" if keep else None
        if final in "JK" or (final == "u" and body == ""):
            return f"\x1b[?{body}{final}"
        return None
    if priv == ">" and not inter:
        if final == "c" and body in ("", "0"):
            return f"\x1b[>{body}c"
        if final in "um":      # kitty keyboard flags push; modifyOtherKeys
            return f"\x1b[>{body}{final}"
        return None
    if priv in ("<", "=") and not inter and final == "u":
        return f"\x1b[{priv}{body}u"
    if inter == "$" and final == "p" and priv in ("", "?"):      # mode report: digits back
        return f"\x1b[{priv}{body}$p"
    if inter == " " and final == "q" and not priv:               # cursor shape
        return f"\x1b[{body} q"
    if inter == "!" and final == "p" and not priv and not body:  # soft reset
        return "\x1b[!p"
    return None


class Filter:
    """Bytes in, bytes out; the state carries over between calls (a sequence may be split)."""

    def __init__(self):
        self.dec = codecs.getincrementaldecoder("utf-8")("replace")
        self.state = "ground"
        self.buf = ""
        self.kind = ""
        self.link = None       # (uri, params, held output, shown text) while inside a plain-looking link
        self._reset_track()

    def _reset_track(self) -> None:
        self.modes = {}        # DEC mode -> the last h/l the box set
        self.kitty = 0         # kitty keyboard pushes not yet popped
        self.kitty_set = False  # flags set (CSI = u) with nothing of the box's pushed
        self.mok = set()       # modifyOtherKeys resources set (CSI > Pp ; Pv m)
        self.charsets = set()  # G0..G3 given something other than ASCII
        self.keypad = self.irm = self.region = self.shape = False

    def feed(self, data: bytes) -> bytes:
        return self._run(self.dec.decode(data)).encode("utf-8")

    def flush(self) -> bytes:
        """When the box is quiet: a link still open is shown as plain text, without its link."""
        out = self._run(self.dec.decode(b"", final=False))
        if self.link is not None:
            out += self.link[2]
            self.link = None
        return out.encode("utf-8")

    def restore(self) -> str:
        """What puts every lasting mode the box set back to a shell's state, and SGR 0."""
        out = [] if self.modes.get("2026") != "h" else ["\x1b[?2026l"]
        out += [f"\x1b[?{m}l" for m in SCREEN_MODES if self.modes.get(m) == "h"]
        for want in "lh":
            ms = sorted((m for m, v in self.modes.items() if m not in SCREEN_MODES and m != "2026"
                         and v != MODE_REST[m] and MODE_REST[m] == want), key=int)
            if ms:
                out.append(f"\x1b[?{';'.join(ms)}{want}")
        if self.kitty:
            out.append(f"\x1b[<{self.kitty}u")
        if self.kitty_set:
            out.append("\x1b[=0u")
        out += [f"\x1b[>{r}m" for r in sorted(self.mok)]
        if self.keypad:
            out.append("\x1b>")
        if self.irm:
            out.append("\x1b[4l")
        if self.region:
            out.append("\x1b7\x1b[r\x1b8")     # the full screen again, the cursor where it was
        if self.shape:
            out.append("\x1b[0 q")
        out += [f"\x1b{g}B" for g in sorted(self.charsets)]
        out.append("\x1b[0m")
        self._reset_track()
        return "".join(out)

    def finish(self) -> bytes:
        """The end of the output (any end): what is held, then the restore."""
        out = self.flush()
        self.state, self.buf = "ground", ""
        return out + self.restore().encode("utf-8")

    def _note(self, s: str) -> None:
        """Keep track of a lasting mode in a sequence that is passed."""
        if s in ("\x1b=", "\x1b>"):
            self.keypad = s == "\x1b="
            return
        if len(s) == 3 and s[1] in CHARSET_INTRO:
            (self.charsets.discard if s[2] == "B" else self.charsets.add)(s[1])
            return
        m = SEQ_RX.fullmatch(s)
        if not m:
            return
        priv, body, inter, final = m.groups()
        if priv == "?" and final in "hl" and not inter:
            for p in body.split(";"):
                if p in MODE_REST:
                    self.modes[p] = final
        elif not priv and not inter and final in "hl":
            self.irm = final == "h"            # the only plain mode kept is 4 (insert)
        elif not priv and not inter and final == "r":
            self.region = bool(body.strip(";"))
        elif not priv and inter == " " and final == "q":
            self.shape = body not in ("", "0")
        elif priv == ">" and not inter and final == "u":
            self.kitty += 1
        elif priv == "<" and not inter and final == "u":
            n = body.split(";")[0]
            self.kitty = max(0, self.kitty - (int(n) if n.isdigit() else 1))
        elif priv == "=" and not inter and final == "u" and not self.kitty:
            self.kitty_set = True
        elif priv == ">" and not inter and final == "m":
            parts = body.split(";")
            if not body:
                self.mok.clear()
            elif len(parts) > 1 and parts[1] not in ("", "0"):
                self.mok.add(parts[0])
            else:
                self.mok.discard(parts[0])

    def _emit(self, out: list, s: str, shown: str = "") -> None:
        if s.startswith("\x1b"):
            self._note(s)
        if self.link is None:
            out.append(s)
            return
        uri, params, held, text = self.link
        held += s
        text += shown
        if len(held) > MAX_LINK_HOLD or len(text) > MAX_LINK_TEXT:
            out.append(held)
            self.link = None
        else:
            self.link = (uri, params, held, text)

    def _osc_end(self, out: list) -> None:
        body, self.buf = self.buf, ""
        if not body.startswith("8;"):
            return
        params, sep, uri = body[2:].partition(";")
        if not sep:
            return
        if not uri:                       # the link's end
            if self.link is not None:
                u, p, held, text = self.link
                self.link = None
                if text.strip() == u:
                    out.append(f"\x1b]8;{p};{u}\x1b\\{held}\x1b]8;;\x1b\\")
                else:
                    out.append(held)
            return
        if self.link is not None:         # a second start: the first link's text goes plain
            out.append(self.link[2])
            self.link = None
        if LINK_RX.fullmatch(uri) and LINK_PARAMS_RX.fullmatch(params):
            self.link = (uri, params, "", "")

    def _run(self, s: str) -> str:
        out = []
        i, n = 0, len(s)
        while i < n:
            st = self.state
            if st == "ground":
                m = PLAIN.match(s, i)
                if m:
                    self._emit(out, m.group(0), m.group(0))
                    i = m.end()
                    continue
                c = s[i]
                i += 1
                if c in KEEP_C0:
                    self._emit(out, c, "")
                elif c == "\x1b":
                    self.state = "esc"
                elif c in C1_INTRO:
                    self._intro(C1_INTRO[c])
                continue
            c = s[i]
            i += 1
            if st == "esc":
                if c in "\x18\x1a":
                    self.state = "ground"
                elif c == "\x1b":
                    pass
                elif c in "[" or c in STR_INTRO:
                    self._intro(c)
                elif c in ESC_KEEP:
                    self._emit(out, "\x1b" + c)
                    self.state = "ground"
                elif c in CHARSET_INTRO:
                    self.buf, self.state = c, "charset"
                elif "\x20" <= c <= "\x2f":
                    self.state = "esc-skip"    # another intermediate: skip to its final
                elif c < "\x20":
                    self.state = "ground"
                    i -= 1                     # a control ends it and is itself looked at
                else:
                    self.state = "ground"
            elif st == "esc-skip":
                if not ("\x20" <= c <= "\x2f"):
                    self.state = "ground"
                    if c < "\x20" or c == "\x1b":
                        i -= 1
            elif st == "charset":
                self.state = "ground"
                if c in CHARSET_KEEP:
                    self._emit(out, "\x1b" + self.buf + c)
                elif c < "\x20" or c == "\x1b":
                    i -= 1
                self.buf = ""
            elif st in ("csi", "csi-skip"):
                if "\x40" <= c <= "\x7e":
                    if st == "csi":
                        p, inter = self.buf, ""
                        while p and "\x20" <= p[-1] <= "\x2f":
                            inter, p = p[-1] + inter, p[:-1]
                        ok = csi_ok(p, inter, c) if not any("\x20" <= x <= "\x2f" for x in p) else None
                        if ok:
                            self._emit(out, ok)
                    self.state, self.buf = "ground", ""
                elif "\x20" <= c <= "\x3f":
                    if st == "csi":
                        self.buf += c
                        if len(self.buf) > MAX_CSI:
                            self.state, self.buf = "csi-skip", ""
                else:                          # a control or ESC inside: the sequence is dropped
                    self.state, self.buf = "ground", ""
                    if c != "\x18" and c != "\x1a":
                        i -= 1
            elif st == "str":
                if c == "\x07" and self.kind == "osc":
                    self._str_end(out)
                elif c == "\x1b":
                    self.state = "str-esc"
                elif c == "\x9c":
                    self._str_end(out)
                elif c in "\x18\x1a":
                    self.state, self.buf = "ground", ""
                elif self.kind == "osc" and self.buf is not None:
                    self.buf += c
                    if len(self.buf) > MAX_OSC:
                        self.buf = None        # too long: skipped to its end
            elif st == "str-esc":
                if c == "\\":
                    self._str_end(out)
                else:                          # ESC starts something new; the string is dropped
                    self.state, self.buf = "esc", ""
                    i -= 1
        return "".join(out)

    def _intro(self, c: str) -> None:
        if c == "[":
            self.state, self.buf = "csi", ""
        else:
            self.state, self.kind, self.buf = "str", STR_INTRO[c], ""

    def _str_end(self, out: list) -> None:
        if self.kind == "osc" and self.buf is not None:
            self._osc_end(out)
        self.state, self.buf = "ground", ""


# ---------------------------------------------------------------------------- the pump

def _write(fd: int, data: bytes) -> None:
    while data:
        try:
            k = os.write(fd, data)
        except InterruptedError:
            continue
        data = data[k:]


def _read(fd: int) -> bytes:
    try:
        return os.read(fd, 65536)
    except InterruptedError:
        return None
    except OSError as e:
        if e.errno in (errno.EIO, errno.EBADF):   # the box side closed
            return b""
        raise


def pump(mode: str, src_in: int = 0, src_out: int = 1) -> int:
    """Filter until either side ends; whatever ends it, the person's terminal gets the restore."""
    f = Filter()
    term = src_out if mode == "pipe" else src_in
    try:
        return _pump(f, mode, src_in, src_out)
    finally:
        try:
            _write(term, f.finish())
        except OSError:
            pass


def _pump(f: Filter, mode: str, src_in: int, src_out: int) -> int:
    if mode == "pipe":
        while True:
            r, _, _ = select.select([src_in], [], [], 0.05)
            if not r:
                _write(src_out, f.flush())
                continue
            got = _read(src_in)
            if got is None:
                continue
            if not got:
                return 0
            _write(src_out, f.feed(got))
    # pty: fd 0 the person's terminal (keys in, filtered output out), fd 1 the box side
    person, box = src_in, src_out
    while True:
        r, _, _ = select.select([person, box], [], [], 0.05)
        if not r:
            _write(person, f.flush())
            continue
        if box in r:
            got = _read(box)
            if got == b"":
                return 0
            if got:
                _write(person, f.feed(got))
        if person in r:
            got = _read(person)
            if got == b"":
                return 0
            if got:
                _write(box, got)


def main(argv) -> int:
    if argv not in (["pty"], ["pipe"]):
        print("usage: tty-filter pty|pipe", file=sys.stderr)
        return 2

    def stop(*_):
        for s in (signal.SIGTERM, signal.SIGHUP):
            signal.signal(s, signal.SIG_IGN)
        raise SystemExit(0)    # pump's finally sends the restore
    for s in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(s, stop)
    try:
        return pump(argv[0])
    except (BrokenPipeError, KeyboardInterrupt):
        return 0
    except OSError as e:
        print(f"tty-filter: {e.strerror}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
