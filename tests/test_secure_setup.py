"""The personal-mode installer's invite (src/kaptyorka/secure/kap_setup.py, secure design v2 9.2,
9.5): the link's parts, every refusal before anything is sent, nothing written before the hub
gave a personal key, and only checked values in the relay's files. The real hub's answers are
in tests/test_hub_box.py."""
import pytest

from kaptyorka.secure import kap_setup as setup

FP = "ab" * 32


@pytest.mark.parametrize("link, host, port, code", [
    (f"https://192.0.2.10:8444/i/Abc_-12#sha256={FP}", "192.0.2.10", 8444, "Abc_-12"),
    (f"  https://Hub.Example.org/i/c0de/#sha256={FP.upper()}\n", "hub.example.org", 443, "c0de"),
    (f"https://[2001:db8::1]:8444/i/c#sha256={FP}", "[2001:db8::1]", 8444, "c"),
])
def test_invite_link_parts(link, host, port, code):
    assert setup.parse_invite(link) == {"host": host, "port": port, "code": code, "fp": FP}


def test_a_plain_http_link_is_refused_with_the_way_to_switch_tls_on():
    with pytest.raises(setup.Refuse, match="encrypted connection: ask your team admin to switch it on"):
        setup.parse_invite(f"http://192.0.2.10:8443/i/abc")
    with pytest.raises(setup.Refuse, match="http://"):
        setup.parse_invite("HTTP://192.0.2.10:8443/i/abc#sha256=" + FP)


@pytest.mark.parametrize("link", [
    "https://192.0.2.10:8444/i/abc",                       # no fingerprint: never from the network
    "https://192.0.2.10:8444/i/abc#",
    "https://192.0.2.10:8444/i/abc#sha256=" + FP[:-1],
    "https://192.0.2.10:8444/i/abc#sha256=" + FP + "00",
    "https://192.0.2.10:8444/i/abc#sha1=" + FP,
])
def test_a_link_without_its_fingerprint_is_refused(link):
    with pytest.raises(setup.Refuse, match="no fingerprint"):
        setup.parse_invite(link)


@pytest.mark.parametrize("link", [   # I-1: quotes, line breaks, JSON and unit text in each part
    'https://hub"],"enabled":false,"a":["/i/abc#sha256=' + FP,
    "https://hub\nIPAddressAllow=any/i/abc#sha256=" + FP,
    "https://hub:8444/i/ab\"c#sha256=" + FP,
    "https://hub:8444/i/abc\n#sha256=" + FP,
    "https://hub:8444/i/x\"],\"enabled\":false,\"a\":[\"#sha256=" + FP,
    "https://user@hub:8444/i/abc#sha256=" + FP,
    "https://hub:8444/i/abc?x=1#sha256=" + FP,
    "https://hub:8444/join/abc#sha256=" + FP,
    "https://-hub:8444/i/abc#sha256=" + FP,
    "https://hub..x:8444/i/abc#sha256=" + FP,
    "https://hub:0/i/abc#sha256=" + FP,
    "https://hub:70000/i/abc#sha256=" + FP,
    "https://hub:84a/i/abc#sha256=" + FP,
    "https://[192.0.2.1]:8444/i/abc#sha256=" + FP,
    "https://[zz::1]:8444/i/abc#sha256=" + FP,
    "", "kaptyorka join https://hub/i/abc#sha256=" + FP,
])
def test_a_link_with_a_bad_part_is_refused(link):
    with pytest.raises(setup.Refuse, match="Nothing was changed"):
        setup.parse_invite(link)


def test_computer_names_fit_the_relays_pattern_too():
    import re
    relay = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._ -]{0,63}")
    for ok in ("laptop-2", "a", "x.y_z", "A" * 32):
        assert setup.CNAME_RX.fullmatch(ok) and relay.fullmatch(ok)
    for bad in ("-x", ".x", "a b", "a\"", "", "A" * 33, "a\n"):
        assert not setup.CNAME_RX.fullmatch(bad)


@pytest.mark.parametrize("status, said, says", [
    (400, "invite already used", "already used (a link works once)"),
    (400, "invite expired", "not valid any more"),
    (400, "no such invite", "not valid any more"),
    (400, "this invite is not for a personal laptop: ask the admin for a personal one", "for a team machine"),
    (400, "a team machine joins as a team machine, never as a personal laptop", "for a team machine"),
    (429, "too many failed attempts", "wait an hour"),
    (500, "boom\x1b]0;evil\x07\nsecond line", "the hub refused this link (500: boom?]0;evil?)"),
])
def test_hub_refusals_are_one_plain_sentence_with_a_next_step(status, said, says):
    msg = setup.hub_refusal(status, said)
    assert says in msg and msg.endswith("Nothing was changed on this computer.")
    assert msg.isprintable() and "second line" not in msg


# ---------------------------------------------------------------------------- the install order

class Calls(list):
    def stub(self, name, ret=None):
        def f(*a, **k):
            self.append(name)
            return ret
        return f


@pytest.fixture
def steps(monkeypatch):
    """Every step that writes, recorded instead of run; the self-check stops the run after them."""
    calls = Calls()
    for name in ("install_code", "install_tools", "claude_execute_only", "install_etc", "install_units",
                 "install_hooks", "install_person"):
        monkeypatch.setattr(setup, name, calls.stub(name))
    seen = {}
    monkeypatch.setattr(setup, "install_relay_conf", lambda v: (calls.append("install_relay_conf"), seen.update(v)))
    monkeypatch.setattr(setup.kr, "write_file", calls.stub("write_file"))
    monkeypatch.setattr(setup.kr, "outside_check", lambda *a: ["stopped by the test"])
    monkeypatch.setattr(setup, "preflight", lambda person: ([], []))
    monkeypatch.setattr(setup.kc, "check_person", lambda p: p)
    monkeypatch.setattr(setup.sys.stdin, "isatty", lambda: False)
    calls.seen = seen
    return calls


def args(**kw):
    a = {"--personal": True, "--person": "alice", "--name": "laptop-2", "link": f"https://hub.lan:8444/i/abc#sha256={FP}"}
    a.update(kw)
    return {k: v for k, v in a.items() if v is not None}


def test_a_refused_redeem_writes_nothing(steps, monkeypatch):
    def refuse(inv, name):
        steps.append("redeem")
        raise setup.Refuse(setup.hub_refusal(400, "invite already used"))
    monkeypatch.setattr(setup, "redeem", refuse)
    with pytest.raises(setup.Refuse, match="already used"):
        setup.install(args())
    assert steps == ["redeem"]


def test_a_computer_that_is_not_ready_does_not_use_the_link(steps, monkeypatch, capsys):
    monkeypatch.setattr(setup, "preflight", lambda person: (["systemd 254 or newer is needed"], []))
    monkeypatch.setattr(setup, "redeem", steps.stub("redeem"))
    assert setup.install(args()) == 2
    assert steps == []
    assert "the link was not used" in capsys.readouterr().out


@pytest.mark.parametrize("kw, says", [
    ({"--name": None}, "add --name"),
    ({"--name": "-x"}, "--name"),
    ({"--name": 'a"],"x'}, "--name"),
    ({"link": "http://hub:8443/i/abc"}, "encrypted connection"),
    ({"link": "https://hub:8444/i/abc"}, "no fingerprint"),
    ({"--hub-ip": "192.0.2.1"}, "give the invite link alone"),
    ({"link": None}, "give the personal invite link"),
    ({"--runtime-max": "7 days"}, "--runtime-max"),
    ({"--personal": None}, "say --personal"),
])
def test_bad_values_are_refused_before_the_link_is_used(steps, monkeypatch, kw, says):
    monkeypatch.setattr(setup, "redeem", steps.stub("redeem"))
    with pytest.raises(setup.Refuse, match=says):
        setup.install(args(**kw))
    assert steps == []


def test_a_bad_claude_token_file_stops_before_the_link_is_used(steps, monkeypatch, tmp_path):
    monkeypatch.setattr(setup, "redeem", steps.stub("redeem"))
    f = tmp_path / "t"
    f.write_text("not a token; rm -rf /\n")
    with pytest.raises(setup.Refuse, match="Claude token"):
        setup.install(args(**{"--claude-token-file": str(f)}))
    assert steps == []


def test_after_the_redeem_the_relay_gets_the_connected_address_and_the_links_port_and_pin(steps, monkeypatch):
    monkeypatch.setattr(setup, "redeem", lambda inv, name: (steps.append(("redeem", name, inv["host"])), {
        "ip": "192.0.2.10", "port": inv["port"], "fp": inv["fp"], "token": "T" * 43})[1])
    assert setup.install(args()) == 3   # the test's self-check stop
    assert steps[0] == ("redeem", "laptop-2", "hub.lan") and "install_relay_conf" in steps
    assert {k: steps.seen[k] for k in ("ip", "port", "fp", "token", "name")} == {
        "ip": "192.0.2.10", "port": 8444, "fp": FP, "token": "T" * 43, "name": "laptop-2"}


def test_by_hand_values_still_work_for_tests(steps, monkeypatch, tmp_path):
    tok = tmp_path / "devtok"
    tok.write_text("D" * 43 + "\n")
    monkeypatch.setattr(setup, "redeem", steps.stub("redeem"))
    a = args(link=None, **{"--hub-ip": "127.0.0.1", "--hub-port": "8443", "--hub-sha256": FP,
                           "--device-token-file": str(tok)})
    assert setup.install(a) == 3
    assert "redeem" not in steps and steps.seen["ip"] == "127.0.0.1" and steps.seen["token"] == "D" * 43


def test_the_command_line_takes_the_link_as_the_second_word(monkeypatch):
    got = {}
    monkeypatch.setattr(setup.os, "geteuid", lambda: 0)
    monkeypatch.setattr(setup, "install", lambda a: got.update(a) or 0)
    link = f"https://hub:8444/i/abc#sha256={FP}"
    assert setup.main(["install", "--personal", link]) == 0 and got["link"] == link
    assert setup.main(["install", "--personal", link, "extra"]) == 2   # usage
