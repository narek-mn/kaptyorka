"""The terminal filter (secure design v2, 4.8; src/kaptyorka/secure/kap_ttyfilter.py): what a team
chat prints reaches the person's terminal without the codes that act through it (clipboard,
titles, strings, reports with chosen text), and with everything the tools draw with. Its unit
and how enter puts it between the box and the terminal are checked here too; tests/secure
measures it with the real tools."""
import os
import random
import re
import subprocess
import sys
import threading

import pytest

from kaptyorka.secure import kap_ttyfilter as tf
from kaptyorka.secure import kapcommon as kc

E = "\x1b"


def run(*chunks: str) -> str:
    f = tf.Filter()
    out = b"".join(f.feed(c.encode("utf-8") if isinstance(c, str) else c) for c in chunks) + f.flush()
    return out.decode("utf-8")


@pytest.mark.parametrize("s", [
    "hello wörld ✳ 日本\r\n\ttab\x08\x07",
    f"{E}[31mred{E}[0m {E}[1;38;5;208mX{E}[38;2;1;2;3mY{E}[48:2::1:2:3mZ{E}[m",
    f"{E}[H{E}[2J{E}[10;20H{E}[K{E}[3A{E}[2B{E}[C{E}[D{E}[5G{E}[1;24r{E}[S{E}[T{E}[2X{E}[3@{E}[2P{E}[L{E}[M",
    f"{E}[?1049h{E}[?25l{E}[?2004h{E}[?1000;1006h{E}[?2026h{E}[?2026l{E}[?1049l{E}[?25h{E}[?1004h",
    f"{E}7{E}8{E}={E}>{E}M{E}(0lqk{E}(B{E}[s{E}[u{E}[6 q{E}[!p",
    f"{E}[6n{E}[c{E}[>c{E}[?2026$p{E}[?u{E}[>1u{E}[<u{E}[=1;1u{E}[>4;2m",
])
def test_what_the_tools_draw_with_passes_unchanged(s):
    assert run(s) == s


@pytest.mark.parametrize("bad", [
    f"{E}]52;c;ZWNobyBwd25lZAo={E}\\",            # clipboard write, ST
    f"{E}]52;c;?\x07",                             # clipboard read, BEL
    "\x9d52;c;ZXZpbA==\x9c",                       # the same with 8-bit (Unicode C1) controls
    f"{E}]0;fake prompt $ \x07", f"{E}]2;title{E}\\", f"{E}]1;icon\x07",
    f"{E}]1337;File=name=eDtzaA==;inline=1:ZWNobw=={E}\\",   # iTerm2 file transfer
    f"{E}]7;file:///etc\x07", f"{E}]9;notify\x07", f"{E}]777;notify;a;b\x07", f"{E}]133;A\x07",
    f"{E}]4;1;rgb:00/00/00\x07", f"{E}]10;?\x07", f"{E}]11;?{E}\\", f"{E}]104\x07",
    f"{E}P$q\"p{E}\\", f"{E}P+q544e{E}\\", f"{E}Ptmux;{E}{E}]52;c;eA=={E}{E}\\{E}\\",   # DCS, tmux passthrough
    f"{E}_Gf=100,a=T;iVBORw0KGgo={E}\\",            # kitty graphics (APC)
    f"{E}^private{E}\\", f"{E}Xsos{E}\\",
    f"{E}[21t", f"{E}[14t", f"{E}[8;100;100t", f"{E}[3;0;0t", f"{E}[2t",     # title report, size report, resize, move, iconify
    f"{E}[1;1;10;10*y", f"{E}[>q", f"{E}[=c", f"{E}[5i", f"{E}[?5n", f"{E}[0\"q", f"{E}[12l",
    f"{E}Z", f"{E}c", f"{E}#8", "\x05", "\x0e", "\x0f", "\x00", "\x7f", "\x9b21t", "\x90q\x9c", "\x85", "\x8d",
])
def test_codes_that_act_through_the_terminal_are_dropped(bad):
    out = run("a", bad, "b")
    assert out == "ab"


def test_a_sequence_split_anywhere_is_still_caught():
    seq = f"x{E}]52;c;ZWNobyBwd25lZAo={E}\\y{E}[31mz"
    for cut in range(len(seq)):
        assert run(seq[:cut], seq[cut:]) == f"xy{E}[31mz"
    text = "ü✳日".encode()
    for cut in range(len(text)):
        assert run(text[:cut], text[cut:]) == "ü✳日"


def test_8_bit_controls_become_their_checked_7_bit_form():
    assert run("\x9b31mx\x9b0m") == f"{E}[31mx{E}[0m"
    assert run(b"a\x9b31m\xc2\x9b32m") == f"a\ufffd31m{E}[32m"   # a raw byte is not UTF-8: it shows as text


def test_concealed_text_shows_but_colour_8_stays():
    assert run(f"{E}[8mhidden{E}[0m") == "hidden" + f"{E}[0m"
    assert run(f"{E}[1;8;31m") == f"{E}[1;31m"
    assert run(f"{E}[38;5;8;48;5;8m") == f"{E}[38;5;8;48;5;8m"


def test_only_the_tools_dec_modes_pass():
    assert run(f"{E}[?1049;5;1000h") == f"{E}[?1049;1000h"
    assert run(f"{E}[?5h{E}[?3h{E}[?2031h") == ""


def test_a_plain_link_stays_a_link_and_a_disguised_one_becomes_text():
    url = "https://example.org/docs"
    assert run(f"{E}]8;;{url}{E}\\{url}{E}]8;;{E}\\") == f"{E}]8;;{url}{E}\\{url}{E}]8;;{E}\\"
    assert run(f"see {E}]8;id=a1;{url}\x07{url}{E}]8;;\x07.") == f"see {E}]8;id=a1;{url}{E}\\{url}{E}]8;;{E}\\."
    # shown text that is not the address: only the text remains
    assert run(f"{E}]8;;https://evil.example/x{E}\\https://example.org{E}]8;;{E}\\") == "https://example.org"
    assert run(f"{E}]8;;file:///etc/passwd{E}\\docs{E}]8;;{E}\\") == "docs"
    assert run(f"{E}]8;;javascript:alert(1){E}\\x{E}]8;;{E}\\") == "x"
    # styled text inside a plain link keeps its style
    assert run(f"{E}]8;;{url}{E}\\{E}[4m{url}{E}[24m{E}]8;;{E}\\") == f"{E}]8;;{url}{E}\\{E}[4m{url}{E}[24m{E}]8;;{E}\\"


def test_a_link_that_never_closes_shows_its_text_when_the_box_is_quiet():
    f = tf.Filter()
    assert f.feed(f"{E}]8;;https://example.org{E}\\https://ex".encode()) == b""
    assert f.flush() == b"https://ex"
    assert f.feed(f"ample.org{E}]8;;{E}\\".encode()) == b"ample.org"


def test_a_long_string_is_skipped_to_its_end_without_growing():
    f = tf.Filter()
    big = (f"{E}]52;c;" + "A" * 300000 + f"{E}\\after").encode()
    out = b"".join(f.feed(big[i:i + 4096]) for i in range(0, len(big), 4096))
    assert out == b"after" and (f.buf is None or len(f.buf) <= tf.MAX_OSC + 1)


def test_random_bytes_never_leave_a_string_code_or_an_unchecked_csi():
    rng = random.Random(4)
    alphabet = [b"\x1b", b"[", b"]", b"P", b"_", b"^", b"X", b"\\", b"\x07", b";", b"?", b">", b"52", b"8", b"0",
                b"m", b"h", b"t", b"c", b"a", b"\xc2\x9b", b"\xc2\x9d", b"\xc2\x9c", b"\x9b", b"\x18", b"$", b"p", b" "]
    for _ in range(400):
        data = b"".join(rng.choice(alphabet) for _ in range(rng.randint(1, 80)))
        f = tf.Filter()
        out = (f.feed(data) + f.flush()).decode()
        assert not re.search(r"\x1b[P_^X]", out) and not re.search("[\x80-\x9f]", out)
        for m in re.finditer(r"\x1b\](\d*);", out):
            assert m.group(1) == "8"
        assert run(out) == out    # what comes out passes again unchanged: nothing half-checked


def test_the_pipe_pump_filters_and_ends_at_the_end_of_input():
    r1, w1 = os.pipe()
    r2, w2 = os.pipe()
    t = threading.Thread(target=tf.pump, args=("pipe", r1, w2), daemon=True)
    t.start()
    os.write(w1, f"ok{E}]52;c;eA=={E}\\{E}[32mgreen".encode())
    os.close(w1)
    t.join(5)
    os.close(w2)
    assert os.read(r2, 1000) == f"ok{E}[32mgreen{E}[0m".encode()    # the end: the restore (SGR 0 here)


def _read_until(fd, want: bytes, limit: float = 5) -> bytes:
    import select
    import time
    got, end = b"", time.time() + limit
    while want not in got and time.time() < end:
        if select.select([fd], [], [], 0.2)[0]:
            got += os.read(fd, 1000)
    return got


def test_the_pty_pump_passes_keys_in_unchanged_and_filters_what_comes_back():
    import pty
    import tty
    pm, ps = pty.openpty()     # the person's side
    bm, bs = pty.openpty()     # the box's side (as enter makes it)
    tty.setraw(ps)
    tty.setraw(bs)
    t = threading.Thread(target=tf.pump, args=("pty", ps, bm), daemon=True)
    t.start()
    os.write(pm, b"keys \x1b[A\x03")
    assert _read_until(bs, b"\x03") == b"keys \x1b[A\x03"
    os.write(bs, f"out{E}]0;title\x07{E}[1mbold".encode())
    seen = _read_until(pm, b"bold")
    assert seen == f"out{E}[1mbold".encode()
    os.close(bs)
    t.join(5)
    assert not t.is_alive()


def test_the_filters_unit_has_no_network_no_files_no_capabilities_and_a_hard_end():
    props = kc.tty_filter_props("7d")
    for want in ("DynamicUser=yes", "PrivateNetwork=yes", "RestrictAddressFamilies=AF_UNIX", "ProtectHome=yes",
                 "ProtectSystem=strict", "NoNewPrivileges=yes", "CapabilityBoundingSet=", "MemoryDenyWriteExecute=yes",
                 "SystemCallErrorNumber=EPERM", "KillMode=control-group", "RuntimeMaxSec=7d", "TasksMax=4"):
        assert want in props, want
    deny = [p for p in props if p.startswith("SystemCallFilter=~")]
    assert deny and all(w in ("@" + deny[0].split("@", 1)[1]).split() for w in ("@network-io", "@process", "@privileged"))
    with pytest.raises(kc.Refuse):
        kc.tty_filter_props("7d;x")


def test_enter_runs_the_filter_from_root_owned_libexec_in_its_own_unit(monkeypatch):
    from kaptyorka.secure import kap_enter as en
    monkeypatch.setattr(en.kr, "runtime_max", lambda: "150s")
    cmd = en.filter_cmd("pty")
    assert cmd[:5] == ["systemd-run", "--quiet", "--collect", "--pipe", "--wait"]
    assert cmd[-4:] == ["/usr/bin/python3", "-I", "/usr/local/libexec/kaptyorka/tty-filter", "pty"]
    assert "RuntimeMaxSec=150s" in cmd and en.unit_of(cmd).startswith("kaptyorka-ttyfilter-")
    from kaptyorka.secure import kap_setup as setup
    assert ("kap_ttyfilter.py", "tty-filter", 0o755) in setup.PROGRAMS


def test_the_filter_program_runs_as_its_own_script():
    r = subprocess.run([sys.executable, "-I", tf.__file__, "pipe"], input=f"a{E}]52;c;x\x07b".encode(),
                       capture_output=True, timeout=20)
    assert r.returncode == 0 and r.stdout == f"ab{E}[0m".encode()
    assert subprocess.run([sys.executable, "-I", tf.__file__, "x"], capture_output=True, timeout=20).returncode == 2


def test_bracketed_paste_is_never_switched_off():
    assert run(f"{E}[?2004h") == f"{E}[?2004h"
    assert run(f"a{E}[?2004lb") == "ab"
    assert run(f"{E}[?1049;2004l") == f"{E}[?1049l"
    assert run("\x9b?2004l", f"{E}[?20", "04l") == ""


def finish(*chunks: str) -> str:
    f = tf.Filter()
    out = b"".join(f.feed(c.encode()) for c in chunks)
    return (out + f.finish()).decode()


@pytest.mark.parametrize("on, back", [
    (f"{E}[?1000h{E}[?1002h{E}[?1003h{E}[?1006h{E}[?1015h{E}[?1004h", f"{E}[?1000;1002;1003;1004;1006;1015l"),
    (f"{E}[?1049h", f"{E}[?1049l"), (f"{E}[?47h", f"{E}[?47l"), (f"{E}[?1047h", f"{E}[?1047l"),
    (f"{E}(0", f"{E}(B"), (f"{E})0", f"{E})B"),
    (f"{E}[>1u", f"{E}[<1u"), (f"{E}[>1u{E}[>5u", f"{E}[<2u"), (f"{E}[=5u", f"{E}[=0u"),
    (f"{E}[>4;2m", f"{E}[>4m"), (f"{E}[?1h{E}=", f"{E}[?1l{E}>"), (f"{E}[?25l", f"{E}[?25h"), (f"{E}[?7l", f"{E}[?7h"),
    (f"{E}[4h", f"{E}[4l"), (f"{E}[5;20r", f"{E}7{E}[r{E}8"), (f"{E}[5 q", f"{E}[0 q"), (f"{E}[?2026h", f"{E}[?2026l"),
    ("\x9b?1003h", f"{E}[?1003l"),
])
def test_every_lasting_mode_the_box_leaves_on_is_put_back_at_the_end(on, back):
    assert finish("x", on, "y") == f"x{on}y{back}{E}[0m".replace("\x9b", f"{E}[")


def test_a_mode_the_box_put_back_itself_is_not_sent_again():
    assert finish(f"{E}[?1049h{E}[?1003h{E}(0{E}[>1u{E}[?25l", f"{E}[?1049l{E}[?1003l{E}(B{E}[<u{E}[?25h").endswith(
        f"{E}[?25h{E}[0m")
    assert finish(f"{E}[>1u{E}[>1u{E}[<5u") == f"{E}[>1u{E}[>1u{E}[<5u{E}[0m"
    assert finish(f"{E}[5;9r{E}[r") == f"{E}[5;9r{E}[r{E}[0m"


def test_the_restore_leaves_the_alternate_screen_and_ends_synchronized_output_first():
    out = finish(f"{E}[?2026h{E}[?1003h{E}[?1049h{E}(0")
    rest = out[len(f"{E}[?2026h{E}[?1003h{E}[?1049h{E}(0"):]
    assert rest == f"{E}[?2026l{E}[?1049l{E}[?1003l{E}(B{E}[0m"


def test_the_restore_comes_after_whatever_was_held():
    f = tf.Filter()
    assert f.feed(f"{E}[?1003h{E}]8;;https://example.org{E}\\https://ex".encode()) == f"{E}[?1003h".encode()
    assert f.finish() == f"https://ex{E}[?1003l{E}[0m".encode()
    assert f.finish() == f"{E}[0m".encode()     # nothing is restored twice


def test_the_pump_restores_at_every_end():
    r1, w1 = os.pipe()
    r2, w2 = os.pipe()
    t = threading.Thread(target=tf.pump, args=("pipe", r1, w2), daemon=True)
    t.start()
    os.write(w1, f"a{E}[?1003h{E}(0".encode())
    assert _read_until(r2, b"(0") == f"a{E}[?1003h{E}(0".encode()
    os.close(w1)
    t.join(5)
    assert _read_until(r2, b"[0m") == f"{E}[?1003l{E}(B{E}[0m".encode()
    for fd in (r1, r2, w2):
        os.close(fd)
    pm, ps = __import__("pty").openpty()
    bm, bs = __import__("pty").openpty()
    __import__("tty").setraw(ps)
    t = threading.Thread(target=tf.pump, args=("pty", ps, bm), daemon=True)
    t.start()
    os.write(bs, f"{E}[?1049h{E}[>1u".encode())
    assert _read_until(pm, b"1u") == f"{E}[?1049h{E}[>1u".encode()
    os.close(bs)
    t.join(5)
    assert _read_until(pm, b"[0m") == f"{E}[?1049l{E}[<1u{E}[0m".encode()


def test_the_filter_restores_when_its_unit_stops_it():
    import signal
    import time
    p = subprocess.Popen([sys.executable, "-I", tf.__file__, "pipe"], stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    p.stdin.write(f"go{E}[?1002h{E}[?2004l".encode())
    p.stdin.flush()
    got = b""
    end = time.time() + 10
    while b"1002h" not in got and time.time() < end:
        got += os.read(p.stdout.fileno(), 100)
    p.send_signal(signal.SIGTERM)
    got += p.stdout.read()
    assert p.wait(10) == 0
    assert got == f"go{E}[?1002h{E}[?1002l{E}[0m".encode()


def test_enter_resets_the_terminal_after_the_filter_ended(monkeypatch):
    from kaptyorka.secure import kap_enter as en
    r, w = os.pipe()
    en.reset_terminal(w, clean=True)
    en.reset_terminal(w, clean=False)
    os.close(w)
    out = os.read(r, 10000).decode()
    clean, full = out[:len(en.RESET)], out[len(en.RESET):]
    assert clean == en.RESET and full.endswith(en.RESET) and full.startswith(f"{E}[?1049l")
    for want in ("?1000l", "?1002l", "?1003l", "?1006l", "?1015l", "?2026l", "<u", ">4m", "?1l", "?25h", "0m"):
        assert f"{E}[{want}" in clean, want
    assert f"{E}(B" in clean and f"{E}>" in clean and "2004l" not in out and "1049" not in clean
    assert run(out) == out     # the filter itself would pass all of it
