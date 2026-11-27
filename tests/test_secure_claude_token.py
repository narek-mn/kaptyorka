"""The installer's `claude setup-token` step (kap_claude_token.py), against a stand-in claude that
behaves as the real one did on a pseudo-terminal when started (Claude Code 2.1.332: a screen with
a sign-in link and a paste field). The real sign-in and what claude prints after the code are not
tested here: no real login is ever made by a test."""
import sys

import pytest

from kaptyorka.secure import kap_claude_token as ct

TOKEN = "sk-ant-oat01-" + "Ab1_-" * 19
CODE = "AbCdEf123456-xyz_code#state"

FAKE = r'''
import os, sys, time, tty
tty.setraw(0)
cols = os.get_terminal_size(0).columns
mode = sys.argv[1]
w = sys.stdout.write
w("\x1b[?25l\x1b[?2004h\x1b[38;5;174mWelcome\x1b[9Gto\x1b[12GClaude\x1b[19GCode\x1b[39m\r\r\n")
if mode == "nolink":
    time.sleep(1); sys.exit(0)
w("\x1b]11;?\x07\x1b[c\x1b[2GBrowser didn't open? Use the URL below\r\r\n")
url = "https://claude.ai/oauth/authorize?code=true&client_id=x&state=" + "S" * 60
w("\x1b]8;id=1;" + url + "\x07\x1b[38;5;246m" + url + "\x1b[39m\x1b]8;;\x07\r\r\n")
w("\x1b[2GPaste code here if prompted\r\r\n")
sys.stdout.flush()
got = b""
while not got.endswith(b"\r"):
    got += os.read(0, 4096)
if b"AbCdEf123456-xyz_code#state" not in got or b"\x1b[200~" not in got:
    w("\x1b[2GInvalid code\r\n"); sys.exit(1)
if mode == "notoken":
    w("\x1b[2GDone, but no token here\r\n"); sys.exit(0)
tok = "sk-ant-oat01-" + "Ab1_-" * 19
if cols < 300 or mode == "wrap100":
    tok = tok[:100] + "\r\r\n" + tok[100:]          # a narrow screen would break the word
elif mode == "wrap70":
    tok = tok[:70] + "\r\r\n" + tok[70:]
elif mode == "split":
    tok = " ".join(tok[i:i + 12] for i in range(0, len(tok), 12))
elif mode == "box":
    tok = "\u2502 " + tok + " \u2502"
w("\x1b[2GYour token:\x1b[0m\r\r\n\x1b[2G\x1b[1m" + tok + "\x1b[22m\r\r\n")
sys.exit(0)
'''


@pytest.fixture
def fake(tmp_path):
    f = tmp_path / "fake_claude.py"
    f.write_text(FAKE)
    return lambda mode: [sys.executable, str(f), mode]


class Said:
    def __init__(self):
        self.lines = []

    def __call__(self, *a):
        self.lines += [str(x) for x in a]

    def text(self):
        return "\n".join(self.lines)


def test_the_link_is_shown_the_code_typed_in_and_the_token_returned_but_never_shown(fake):
    said = Said()
    asked = []
    tok = ct.drive(fake("ok"), lambda: asked.append(1) or CODE, said, url_wait=10, token_wait=10)
    assert tok == TOKEN and asked == [1]
    assert "https://claude.ai/oauth/authorize?code=true&client_id=x&state=" + "S" * 60 in said.text()
    assert TOKEN not in said.text() and CODE not in said.text() and "sk-ant" not in said.text()


def test_no_link_means_nothing_is_asked_and_nothing_returned(fake):
    said = Said()
    assert ct.drive(fake("nolink"), lambda: pytest.fail("asked"), said, url_wait=4) == ""
    assert "did not show a sign-in link" in said.text()


def test_a_claude_that_prints_no_token_gives_nothing_and_no_screen_text(fake):
    said = Said()
    assert ct.drive(fake("notoken"), lambda: CODE, said, url_wait=10, token_wait=6) == ""
    assert said.lines[-1] == "  claude did not give a token." and "no token here" not in said.text()


@pytest.mark.parametrize("mode,cut", [("split", "it came in pieces"), ("wrap70", "it was too short"),
                                      ("wrap100", "it was broken over two lines")])
def test_a_token_in_pieces_is_never_shown_and_never_saved_cut(fake, mode, cut):
    """Round 6, finding 4: a token split by spaces was shown whole in the error line, and a token
    wrapped onto a second line was saved cut. Now: nothing of the screen after the code, a
    warning, and nothing returned (the person pastes one instead)."""
    said = Said()
    assert ct.drive(fake(mode), lambda: CODE, said, url_wait=10, token_wait=6) == ""
    assert f"did not come through whole ({cut})" in said.text() and "none was saved" in said.text()
    shown = said.text().replace(" ", "")
    for piece in ("Ab1_-Ab1", "sk-ant", "oat01"):
        assert piece not in shown


def test_a_token_between_a_drawn_box_s_sides_is_taken_whole(fake):
    assert ct.drive(fake("box"), lambda: CODE, Said(), url_wait=10, token_wait=10) == TOKEN


def test_an_empty_or_odd_code_is_refused_before_anything_is_typed(fake):
    for bad in ("", "has space and \x1b[31m escape"):
        said = Said()
        assert ct.drive(fake("ok"), lambda: bad, said, url_wait=10) == ""
        assert "code" in said.text()


def test_why_cuts_out_long_values_and_escapes():
    raw = b"\x1b[31mfailed: " + TOKEN.encode() + b" and https://claude.ai/oauth/authorize?x=" + b"a" * 50 + b"\r\n"
    out = ct.why(raw)
    assert "sk-ant" not in out and "oauth" not in out and "failed:" in out and "\x1b" not in out
    split = b"failed: " + " ".join(TOKEN[i:i + 12] for i in range(0, len(TOKEN), 12)).encode()
    assert "Ab1_-" not in ct.why(split) and "failed:" in ct.why(split)


def test_the_screen_parsers():
    raw = ("\x1b]8;id=1;https://claude.ai/oauth/authorize?abc123def\x07\x1b[38;5;246mhttps://claude.ai/oauth/authorize?abc123def"
           "\x1b[39m\x1b]8;;\x07\r\n").encode()
    assert ct.find_url(raw) == "https://claude.ai/oauth/authorize?abc123def"
    assert ct.find_url(b"see https://evil.example/oauth/authorize?x=1234567890") == ""
    assert ct.find_token(("\x1b[1m" + TOKEN + "\x1b[22m\r\n").encode()) == TOKEN
    assert ct.find_token(b"sk-ant-oat01-short") == ""
    assert ct.find_token(TOKEN.encode()) == ""                       # the output may go on: not yet
    assert ct.find_token(TOKEN.encode() + b"\r\n") == TOKEN
    assert ct.find_token(TOKEN.encode() + b"\r\n\r\nStore it safely.\r\n") == TOKEN
    assert ct.find_token(TOKEN.encode() + b"\r\n  Done\r\n") == TOKEN    # an ordinary word next
    assert ct.find_token(TOKEN.encode() + b"\r\nQA\r\n") == ""              # reads as its tail
    assert ct.find_token(b"x" + TOKEN.encode() + b"\n") == ""                # inside a longer word


def test_the_command_runs_the_boxs_claude_behind_wallcheck_with_a_throwaway_home(monkeypatch):
    monkeypatch.setattr(ct.kr, "props_args", lambda p, b: ["-p", "ProtectSystem=strict"])
    monkeypatch.setattr(ct.kr, "host_addrs", lambda: ["192.0.2.5"])
    monkeypatch.setattr(ct.kr, "shared_canaries", lambda: ["/etc/c1"])
    cmd = ct.command("alice", "alice", "kapbox-alice-tokensetup-ab12", "/home/box/.kap-setup-token")
    assert "--pty" in cmd and "--unit=kapbox-alice-tokensetup-ab12" in cmd
    assert "Environment=HOME=/home/box/.kap-setup-token" in cmd and "ProtectSystem=strict" in cmd
    i = cmd.index("--")
    assert cmd[i + 1] == ct.kc.WALLCHECK and cmd[-3:] == ["--", "/opt/kap-tools/bin/claude", "setup-token"]
    assert "-chat-" not in " ".join(cmd)      # not counted as a chat (kr.chats)


def run_from_box(monkeypatch, tmp_path, drive_result, paste):
    monkeypatch.setattr(ct.kc, "box_home", lambda b: str(tmp_path))
    monkeypatch.setattr(ct.kr, "ensure_box_running", lambda b: None)
    monkeypatch.setattr(ct.kr, "mkdir", lambda p, mode, uid=0, gid=0: __import__("os").makedirs(p, exist_ok=True))
    monkeypatch.setattr(ct.kr, "systemctl", lambda *a, **k: None)
    monkeypatch.setattr(ct, "command", lambda *a: ["true"])
    monkeypatch.setattr(ct, "drive", lambda cmd, ask, say, **k: drive_result)
    said = Said()
    got = ct.from_box("alice", said, lambda: CODE, lambda: paste)
    return got, said


def test_from_box_returns_the_boxs_own_token_without_asking_for_a_pasted_one(monkeypatch, tmp_path):
    got, said = run_from_box(monkeypatch, tmp_path, TOKEN, pytest.fail)
    assert got == TOKEN and TOKEN not in said.text() and not (tmp_path / ".kap-setup-token").exists()


def test_from_box_falls_back_to_a_pasted_token_and_checks_it(monkeypatch, tmp_path):
    got, said = run_from_box(monkeypatch, tmp_path, "", TOKEN)
    assert got == TOKEN and "That did not work" in said.text()
    got, said = run_from_box(monkeypatch, tmp_path, "", "not a token!")
    assert got == "" and "does not look like a token" in said.text() and "token-rotate" in said.text()
    assert run_from_box(monkeypatch, tmp_path, "", "")[0] == ""


def test_the_installer_step_only_runs_at_a_terminal_without_a_token(monkeypatch, tmp_path):
    from kaptyorka.secure import kap_setup as setup
    calls = []
    monkeypatch.setattr(setup.kap_claude_token, "from_box", lambda *a: calls.append(a) or TOKEN)
    monkeypatch.setattr(setup.kap_claude_token, "save", lambda p, t: calls.append(("save", p)))
    monkeypatch.setattr(setup.kc, "SECRET", str(tmp_path))
    monkeypatch.setattr(setup.kap_tools, "installed", lambda: {"claude": "2.1.3"})
    v = {"claude": ""}
    monkeypatch.setattr(setup.sys.stdin, "isatty", lambda: False, raising=False)
    setup.claude_token_step("alice", v)
    assert calls == [] and v["claude"] == ""
    monkeypatch.setattr(setup.sys.stdin, "isatty", lambda: True, raising=False)
    setup.claude_token_step("alice", {"claude": TOKEN})
    assert calls == []
    monkeypatch.setattr(setup.kap_tools, "installed", lambda: {"codex": "1"})
    setup.claude_token_step("alice", v)
    assert calls == []
    monkeypatch.setattr(setup.kap_tools, "installed", lambda: {"claude": "2.1.3"})
    setup.claude_token_step("alice", v)
    assert v["claude"] == TOKEN and calls[-1] == ("save", "alice")
