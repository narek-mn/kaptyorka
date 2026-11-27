"""The Claude token for the box, made by the installer (secure design, "the installer runs
`claude setup-token` itself"): the BOX's own claude (/opt/kap-tools/bin/claude) is started in the
box's walls on a terminal that this program owns; the person never sees the box's raw output.

What `claude setup-token` does (read on 2.1.332, see compat.py Dep claude.setup_token): on a
terminal it draws a screen with a sign-in link (https://claude.ai/oauth/authorize?...) and a
"Paste code here if prompted" field, then waits; with its output on a pipe it prints nothing at
all. So it is run on a pseudo-terminal 300 columns wide (no line breaks inside a long value),
this program finds the link and shows it, the person signs in with her own browser and pastes
the code it shows, this program types the code in, and reads the token claude prints afterwards
(sk-ant-oat..., one word). UNVERIFIED without a real sign-in: what claude does after the code
(prints the token as one word; exits) and whether the paste is taken as typed. If anything
differs, nothing is saved and the person pastes a token made by hand (fallback below).

The token is never printed, logged or put in an error message; claude's HOME and config folder
are a throw-away folder, so nothing it writes outlives the run. Standard library only."""
import fcntl
import os
import pty
import re
import secrets
import select
import shutil
import signal
import struct
import subprocess
import sys
import termios
import time

if __package__:
    from . import kapcommon as kc, kaproot as kr
else:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import kapcommon as kc
    import kaproot as kr

COLS = 300
# escape sequences: OSC (title, hyperlink) up to BEL or ST, CSI, and the short ones
ANSI = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b\[[0-?]*[ -/]*[@-~]|\x1b[()][A-Z0-9]|\x1b[=>78Mc]")
URL_RX = re.compile(r"https://(?:claude\.ai|platform\.claude\.com|console\.anthropic\.com)/oauth/[A-Za-z0-9._~:/?#@!$&'()*+,;=%-]{4,1500}")
TOKEN_RX = re.compile(r"(?<![A-Za-z0-9_-])sk-ant-oat[0-9]{2}-[A-Za-z0-9_-]{20,400}")
TOKEN_MIN = 90        # a whole token is longer (sk-ant-oat01- and about 95 more); a shorter one was cut
FRAME = " \t│┃║|"     # what may stand around a value on its line: spaces and a drawn box's sides
WORD_RX = re.compile(r"[A-Z]?[a-z]+[.!:]?")   # an ordinary word on the next line, not a token's tail
LONG_RX = re.compile(r"[A-Za-z0-9_%./:=-]{20,}")


def plain(raw: bytes) -> str:
    """What the screen shows, as text: escape sequences gone, carriage returns as line ends."""
    return ANSI.sub("", raw.decode("utf-8", "replace")).replace("\r", "\n")


def find_url(raw: bytes) -> str:
    m = URL_RX.search(plain(raw))
    return m.group(0) if m else ""


def _token(raw: bytes):
    """(token, '') for a whole token on the screen; ('', why) for one that is not whole; ('', '')
    for none (red team round 6, finding 4: a token wrapped onto a second line was saved cut).
    Whole: it ends at a space or a line end (not where the output read so far stops), is at
    least TOKEN_MIN long, and the next line does not go on with what reads as its tail."""
    text = plain(raw)
    for m in TOKEN_RX.finditer(text):
        rest = text[m.end():]
        line, nl, after = rest.partition("\n")
        if not rest or rest[0] not in FRAME + "\n":
            return "", "it ran into other text or was not finished"
        if len(m.group(0)) < TOKEN_MIN:
            return "", "it was too short"
        if nl and not line.strip(FRAME):
            nxt = next((ln.strip(FRAME) for ln in after.split("\n") if ln.strip(FRAME)), "")
            if re.fullmatch(r"[A-Za-z0-9_-]+", nxt) and not WORD_RX.fullmatch(nxt):
                return "", "it was broken over two lines"
        return (m.group(0), "") if kc.TOKEN_RX.fullmatch(m.group(0)) else ("", "it did not look right")
    return ("", "it came in pieces") if "sk-ant-" in text else ("", "")


def find_token(raw: bytes) -> str:
    return _token(raw)[0]


def why(raw: bytes) -> str:
    """A few words of what the screen said last, with every long run (a link, a code, a token)
    and anything from a token's start on cut out: safe to show. Used only before the code is
    typed; after it, nothing of the screen is shown (round 6, finding 4)."""
    text = plain(raw)
    text = text[:text.find("sk-ant")] if "sk-ant" in text else text
    tail = " ".join(LONG_RX.sub("...", text).split())[-160:]
    return kc.term(tail)


def command(person: str, box: str, unit: str, cfg: str) -> list:
    """systemd-run with the box's walls and wallcheck in front of the box's claude, as a chat is
    started (kap_enter.build), but not named like a chat, with its own home and config folder."""
    cmd = ["systemd-run", "--quiet", "--collect", "--expand-environment=no", f"--unit={unit}",
           f"--working-directory={kc.view_root(person)}", "--pty"]
    cmd += kr.props_args(person, box)
    for env in (f"HOME={cfg}", f"CLAUDE_CONFIG_DIR={cfg}/config", f"XDG_CONFIG_HOME={cfg}/xdg",
                f"XDG_CACHE_HOME={cfg}/cache", "TERM=xterm-256color", "DISABLE_AUTOUPDATER=1", "BROWSER=true"):
        cmd += ["-p", f"Environment={env}"]
    return cmd + ["--", kc.WALLCHECK, "--box", box, "--person", person, "--addrs", ",".join(kr.host_addrs()),
                  "--canaries", ",".join(kr.shared_canaries()), "--", f"{kc.TOOLS_DIR}/bin/claude", "setup-token"]


def drive(cmd, ask_code, say, url_wait: float = 60, token_wait: float = 120) -> str:
    """Run cmd (claude setup-token) on a wide pseudo-terminal; show the sign-in link through say,
    ask_code() for the code, type it in, return the token claude prints, or '' (after saying
    why in plain words). Nothing of the screen is shown but the link."""
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 50, COLS, 0, 0))
    try:
        proc = subprocess.Popen(cmd, stdin=slave, stdout=slave, stderr=slave, start_new_session=True,
                                env=kr.CLEAN_ENV, close_fds=True)
    except OSError as e:
        os.close(master)
        os.close(slave)
        say(f"  could not start claude in the box ({e.strerror}).")
        return ""
    os.close(slave)
    buf = b""

    def read_until(found, seconds):
        nonlocal buf
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            r, _, _ = select.select([master], [], [], 0.5)
            if r:
                try:
                    d = os.read(master, 65536)
                except OSError:      # the other side closed: the program ended
                    d = b""
                if not d:
                    return found(buf)
                buf += d[:65536]
                buf = buf[-200000:]
            got = found(buf)
            if got:
                return got
            if proc.poll() is not None and not r:
                return found(buf)
        return found(buf)

    try:
        url = read_until(find_url, url_wait)
        if not url:
            say("  claude in the box did not show a sign-in link"
                + (f" (it said: {why(buf)})" if why(buf) else "") + ".")
            return ""
        say("  Open this link in your browser, sign in, and copy the code the page shows:", "", "    " + kc.term(url), "")
        code = (ask_code() or "").strip()
        if not code or not re.fullmatch(r"[A-Za-z0-9._~#%+/=-]{8,1000}", code):
            say("  No usable code was entered." if not code else "  That does not look like the code the page shows.")
            return ""
        buf = b""
        os.write(master, b"\x1b[200~" + code.encode() + b"\x1b[201~")
        time.sleep(0.4)
        os.write(master, b"\r")
        code = ""
        tok = read_until(find_token, token_wait)
        if tok:
            return tok
        cut = _token(buf)[1]   # never any screen text after the code: the token may be on it in pieces
        say(f"  warning: claude's token did not come through whole ({cut}), so none was saved." if cut
            else "  claude did not give a token.")
        return ""
    finally:
        buf = b""
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass
        os.close(master)


def from_box(person: str, say, ask_code, ask_paste) -> str:
    """The token for this person's box, or '' when she skipped. The box's claude makes it (drive);
    when that does not work, ask_paste() takes one she made by hand with `claude setup-token`."""
    box = person
    unit = f"kapbox-{box}-tokensetup-{secrets.token_hex(3)}"
    home = kc.box_home(box)
    cfg = f"{home}/.kap-setup-token"
    say("Making the box's Claude sign-in with the box's own claude (a token that lasts about a year) ...")
    tok = ""
    try:
        kr.ensure_box_running(box)
        shutil.rmtree(cfg, ignore_errors=True)
        pw = os.stat(home)
        kr.mkdir(cfg, 0o700, pw.st_uid, pw.st_gid)
        tok = drive(command(person, box, unit, cfg), ask_code, say)
    except (kc.Refuse, OSError, subprocess.SubprocessError) as e:
        say(f"  could not run claude in the box: {kc.term(e)[:200]}")
    finally:
        kr.systemctl("stop", f"{unit}.service", check=False)
        shutil.rmtree(cfg, ignore_errors=True)
    if tok:
        return tok
    say("That did not work. You can make a token yourself instead: run  claude setup-token  as yourself in"
        " another terminal.")
    got = (ask_paste() or "").strip()
    if got and not kc.TOKEN_RX.fullmatch(got):
        say("  That does not look like a token; none was saved. Later:  hubchat token-rotate")
        return ""
    return got


def save(person: str, tok: str) -> None:
    kr.mkdir(kc.SECRET, 0o700)
    kr.mkdir(f"{kc.SECRET}/{person}", 0o700)
    kr.write_file(f"{kc.SECRET}/{person}/claude-oauth-token", tok + "\n", 0o600)
