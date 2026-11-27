"""Signed releases of the personal-mode installer (build plan 16): the manifest, its ssh-keygen
signature against the pinned release key, the release side's prepare step, the laptop's updater
(src/kaptyorka/secure/kap_update.py) and the hub's two download routes. Every key here is a
throw-away key made in the test without a passphrase; no real key is ever used."""
import hashlib
import http.client
import json
import os
import shutil
import ssl
import subprocess
import sys

import pytest

from kaptyorka.secure import kap_installer as ki
from kaptyorka.secure import kap_release as rel
from kaptyorka.secure import kap_update as ku

pytestmark = pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="needs ssh-keygen")
SHA = "ab" * 32


def make_key(d, name="k"):
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, name)
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "test-only", "-f", path], check=True,
                   timeout=30)
    return path, rel.parse_key(open(path + ".pub").read())


def sign(key_path, data: bytes, d, namespace=rel.NAMESPACE) -> bytes:
    m = os.path.join(d, "m")
    with open(m, "wb") as f:
        f.write(data)
    if os.path.exists(m + ".sig"):
        os.unlink(m + ".sig")
    subprocess.run(["ssh-keygen", "-q", "-Y", "sign", "-f", key_path, "-n", namespace, m], check=True,
                   capture_output=True, timeout=30)
    return open(m + ".sig", "rb").read()


@pytest.fixture(scope="module")
def keys(tmp_path_factory):
    d = str(tmp_path_factory.mktemp("keys"))
    good, good_pub = make_key(d, "good")
    other, other_pub = make_key(d, "other")
    return {"dir": d, "good": good, "pub": good_pub, "other": other, "other_pub": other_pub}


# ---------------------------------------------------------------------------- manifest and versions

def test_the_manifest_has_one_exact_form():
    text = rel.manifest_text("1.2.3", SHA)
    assert text == f"kaptyorka release manifest 1\nversion=1.2.3\nfile=kaptyorka-secure-setup.pyz\nsha256={SHA}\n"
    assert rel.parse_manifest(text.encode()) == {"version": "1.2.3", "file": rel.INSTALLER, "sha256": SHA}
    for bad in (text.replace("\n", "\r\n"), text + "extra=1\n", text.replace("file=kaptyorka-secure-setup.pyz",
                                                                             "file=../evil.pyz"),
                text.replace(SHA, SHA.upper()), text.replace("1.2.3", "1.2.3-rc1"), text.replace("1.2.3", "01.2"),
                text[:-1], "x" * 2000, text.replace("version=", "version = ")):
        with pytest.raises(rel.Bad):
            rel.parse_manifest(bad.encode())


def test_only_a_strictly_newer_version_is_newer():
    assert rel.newer("0.9.40", "0.9.39") and rel.newer("0.10.0", "0.9.99") and rel.newer("1.0.1", "1.0")
    assert not rel.newer("0.9.39", "0.9.39") and not rel.newer("1.0", "1.0.0") and not rel.newer("0.9.38", "0.9.39")
    assert rel.newer("0.9.40", "") and rel.newer("0.9.40", "0")   # an unknown install is the oldest
    with pytest.raises(rel.Bad):
        rel.newer("0.9.40; rm", "0.9.39")


def test_the_placeholder_key_is_no_key():
    assert rel.code_key() == "" or rel.KEY_RX.fullmatch(rel.code_key())
    assert rel.PLACEHOLDER not in rel.parse_key(rel.PLACEHOLDER)


# ---------------------------------------------------------------------------- the signature

def test_a_good_signature_passes_and_everything_else_is_refused(keys, tmp_path):
    man = rel.manifest_text("2.0.0", SHA).encode()
    sig = sign(keys["good"], man, str(tmp_path))
    assert rel.verify(man, sig, keys["pub"])["version"] == "2.0.0"
    refused = {
        "a changed manifest": (man.replace(b"2.0.0", b"9.0.0"), sig, keys["pub"]),
        "no signature": (man, b"", keys["pub"]),
        "a signature by another key": (man, sign(keys["other"], man, str(tmp_path)), keys["pub"]),
        "another namespace": (man, sign(keys["good"], man, str(tmp_path), "file"), keys["pub"]),
        "garbage": (man, b"-----BEGIN SSH SIGNATURE-----\nAAAA\n-----END SSH SIGNATURE-----\n", keys["pub"]),
        "no key": (man, sig, ""),
    }
    for why, (m, s, k) in refused.items():
        with pytest.raises(rel.Bad):
            rel.verify(m, s, k)
            pytest.fail(why)


def test_a_signed_text_that_is_not_a_manifest_is_refused_after_the_signature(keys, tmp_path):
    junk = b"version=99.0.0\n"
    with pytest.raises(rel.Bad, match="not a kaptyorka release manifest"):
        rel.verify(junk, sign(keys["good"], junk, str(tmp_path)), keys["pub"])


def test_the_installer_must_have_the_manifest_s_hash():
    data = b"#!/usr/bin/python3 -I\nzip"
    rel.check_installer(data, {"sha256": hashlib.sha256(data).hexdigest()})
    with pytest.raises(rel.Bad, match="SHA-256 differs"):
        rel.check_installer(data + b"x", {"sha256": hashlib.sha256(data).hexdigest()})


# ---------------------------------------------------------------------------- the release side

def tree_copy(tmp_path, key_pub_file=None, version=None):
    """The package files elsewhere (as a checkout at a tag), its key set by set-key."""
    pkg = tmp_path / "src" / "kaptyorka"
    shutil.copytree(ki.package_dir(), pkg, ignore=shutil.ignore_patterns("__pycache__"))
    if version:
        init = pkg / "__init__.py"
        init.write_text(init.read_text().replace(f'"{ki.version()}"', f'"{version}"'))
    if key_pub_file:
        r = subprocess.run([sys.executable, "-I", str(pkg / "secure" / "kap_release.py"), "set-key", key_pub_file],
                           capture_output=True, text=True, timeout=30)
        assert r.returncode == 0, r.stderr
    return pkg


def test_set_key_writes_the_constant_and_the_readme_line(keys, tmp_path):
    pkg = tree_copy(tmp_path)
    (tmp_path / "README.md").write_text("steps:\nKEY='ssh-ed25519 REPLACE-WITH-THE-RELEASE-PUBLIC-KEY'\nend\n")
    r = subprocess.run([sys.executable, "-I", str(pkg / "secure" / "kap_release.py"), "set-key", keys["good"] + ".pub"],
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    assert f'RELEASE_KEY = "{keys["pub"]}"' in (pkg / "secure" / "kap_release.py").read_text()
    assert f"KEY='{keys['pub']}'\n" in (tmp_path / "README.md").read_text()


def test_the_readme_key_is_the_code_s_key():
    """The install steps' pasted key and the key the installer pins are the same (set-key sets both)."""
    readme = open(os.path.join(os.path.dirname(__file__), "..", "README.md")).read()
    keys = [ln for ln in readme.splitlines() if ln.startswith("KEY='")]
    assert keys == [f"KEY='{rel.code_key() or 'ssh-ed25519 ' + rel.PLACEHOLDER}'"]


def test_prepare_signs_only_an_installer_byte_for_byte_ci_s(keys, tmp_path):
    pkg = tree_copy(tmp_path, keys["good"] + ".pub", "7.0.1")
    tool = str(pkg / "secure" / "kap_release.py")
    ci = tmp_path / "ci.pyz"
    subprocess.run([sys.executable, "-I", str(pkg / "secure" / "kap_installer.py"), "build", str(ci)], check=True,
                   capture_output=True, timeout=60)
    out = tmp_path / "out"
    r = subprocess.run([sys.executable, "-I", tool, "prepare", "7.0.1", "--ci", str(ci), "--key", keys["good"],
                        "--out", str(out)], capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
    assert r.returncode == 0, r.stdout + r.stderr
    assert sorted(os.listdir(out)) == sorted(rel.FILES)
    assert rel.parse_manifest((out / rel.MANIFEST).read_bytes())["version"] == "7.0.1"
    assert (out / rel.INSTALLER).read_bytes() == ci.read_bytes()
    # verify, as a laptop does: good with the key; another key refuses
    assert subprocess.run([sys.executable, "-I", tool, "verify", str(out)], capture_output=True, timeout=60).returncode == 0
    assert subprocess.run([sys.executable, "-I", tool, "verify", str(out), "--key-file", keys["other"] + ".pub"],
                          capture_output=True, timeout=60).returncode == 1
    # CI's file differs by one byte: nothing is signed
    ci.write_bytes(ci.read_bytes() + b"\0")
    out2 = tmp_path / "out2"
    r = subprocess.run([sys.executable, "-I", tool, "prepare", "7.0.1", "--ci", str(ci), "--key", keys["good"],
                        "--out", str(out2)], capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
    assert r.returncode == 1 and "not CI's" in r.stderr and not out2.exists()
    # another version than the checkout's, or another key than the code's: refused
    for args in (["7.0.2", "--key", keys["good"]], ["7.0.1", "--key", keys["other"]]):
        r = subprocess.run([sys.executable, "-I", tool, "prepare", args[0], "--ci", str(ci), *args[1:], "--out",
                            str(tmp_path / "out3")], capture_output=True, text=True, timeout=60)
        assert r.returncode == 2 and not (tmp_path / "out3").exists(), r.stderr


def test_prepare_refuses_while_the_key_is_the_placeholder(tmp_path):
    pkg = tree_copy(tmp_path)
    src = pkg / "secure" / "kap_release.py"
    src.write_text(src.read_text().replace(f'RELEASE_KEY = "{rel.RELEASE_KEY}"',
                                           f'RELEASE_KEY = "{rel.PLACEHOLDER}"'))
    r = subprocess.run([sys.executable, "-I", str(src), "prepare", ki.version(), "--ci", "x", "--key", "y", "--out",
                        str(tmp_path / "o")], capture_output=True, text=True, timeout=30)
    assert r.returncode == 2 and "placeholder" in r.stderr


def test_the_release_tool_never_passes_a_passphrase():
    src = open(rel.__file__).read()
    assert '"-P"' not in src and "'-P'" not in src and "SSH_ASKPASS" not in src and "passphrase=" not in src


# ---------------------------------------------------------------------------- the laptop's updater

@pytest.fixture
def lap(tmp_path, monkeypatch, keys):
    """kap_update's places in a temp folder, the pinned key the good test key, a fake hub."""
    etc, work = tmp_path / "etc", tmp_path / "work"
    etc.mkdir()
    monkeypatch.setattr(ku, "KEY_FILE", str(etc / "release-key"))
    monkeypatch.setattr(ku, "STATE_FILE", str(etc / "update.json"))
    monkeypatch.setattr(ku, "WORK", str(work))
    monkeypatch.setattr(ku, "STAGED", {n: f"{work}/staged-{n}" for n in rel.FILES})
    monkeypatch.setattr(ku, "PREVIOUS", f"{work}/previous.pyz")
    monkeypatch.setattr(ku, "LOCK", str(tmp_path / "update.lock"))
    monkeypatch.setattr(ku, "RUN_LOCK", str(tmp_path / "update-run.lock"))
    monkeypatch.setattr(ku.kc, "ETC", str(etc))
    installer = tmp_path / "installer"
    installer.write_bytes(b"old installer")
    monkeypatch.setattr(ku.kc, "INSTALLER", str(installer))
    (etc / "version").write_text("1.0.0\n")
    (etc / "release-key").write_text(rel.signers_line(keys["pub"]))
    os.chmod(etc / "release-key", 0o644)
    hub = {}
    monkeypatch.setattr(ku, "hub_values", lambda: {"ip": "127.0.0.1", "port": 1, "fp": "0" * 64, "token": "t"})
    monkeypatch.setattr(ku, "fetch", lambda h, name, limit, timeout=60: hub.get(name))
    monkeypatch.setattr(ku, "chats_running", lambda: 0)
    repairs = []

    def repair(pyz, ok=True):
        repairs.append(pyz)
        data = open(pyz, "rb").read()
        if data.startswith(b"installer "):
            (etc / "version").write_text(data.split()[1].decode() + "\n")
            return subprocess.CompletedProcess([], 0 if b"broken" not in data else 3, "", "Self-check: PROBLEMS")
        (etc / "version").write_text("1.0.0\n")   # the kept installer: back to the old version
        return subprocess.CompletedProcess([], 0, "Repaired", "")
    monkeypatch.setattr(ku, "repair_with", repair)

    def offer(version, data=None, key=keys["good"], manifest=None):
        data = data if data is not None else f"installer {version}".encode()
        man = manifest or rel.manifest_text(version, hashlib.sha256(data).hexdigest()).encode()
        hub.update({rel.INSTALLER: data, rel.MANIFEST: man, rel.SIGNATURE: sign(key, man, str(tmp_path))})
        return hub
    return type("Lap", (), {"etc": etc, "hub": hub, "offer": staticmethod(offer), "repairs": repairs,
                            "version": staticmethod(lambda: (etc / "version").read_text().strip()),
                            "state": staticmethod(ku.load_state)})


def run_now(monkeypatch):
    monkeypatch.setattr(ku.kc, "unfinished_install", lambda: "")
    monkeypatch.setattr(ku, "_read", lambda p, limit=1 << 16: "personal\n" if p.endswith("/mode") else "")
    return ku.run(now=True)


def test_a_signed_newer_release_installs_and_says_so(lap, monkeypatch):
    lap.offer("1.0.1")
    assert run_now(monkeypatch) == 0
    assert lap.version() == "1.0.1" and "installed 1.0.1 (was 1.0.0)" in lap.state()["result"]
    assert not lap.state().get("waiting") and not os.path.exists(ku.STAGED[rel.INSTALLER])
    assert open(ku.PREVIOUS, "rb").read() == b"old installer"   # the kept installer, for a rollback


@pytest.mark.parametrize("case", ["changed", "unsigned", "other key", "downgrade", "same", "wrong file"])
def test_every_bad_release_is_refused_and_nothing_runs(lap, monkeypatch, keys, case):
    if case == "changed":
        lap.offer("1.0.1")
        lap.hub[rel.MANIFEST] = lap.hub[rel.MANIFEST].replace(b"1.0.1", b"1.0.9")
    elif case == "unsigned":
        lap.offer("1.0.1")
        del lap.hub[rel.SIGNATURE]
    elif case == "other key":
        lap.offer("1.0.1", key=keys["other"])
    elif case == "downgrade":
        lap.offer("0.9.9")
    elif case == "same":
        lap.offer("1.0.0")
    else:   # the hub serves another installer than the signed manifest names
        lap.offer("1.0.1")
        lap.hub[rel.INSTALLER] = b"installer 1.0.1 but changed"
    run_now(monkeypatch)
    want = {"changed": "refused: the release's signature is not good", "unsigned": "refused: the release is not signed",
            "other key": "refused: the release's signature is not good", "downgrade": "refused: the hub offers 0.9.9, older than 1.0.0",
            "same": "up to date", "wrong file": "refused: the installer is not the one the signed manifest names"}[case]
    assert lap.state()["result"].startswith(want), lap.state()
    assert lap.version() == "1.0.0" and lap.repairs == [] and not lap.state().get("waiting")
    assert not os.path.exists(ku.STAGED[rel.INSTALLER])


def test_an_update_waits_while_a_team_chat_runs_then_installs(lap, monkeypatch):
    import fcntl
    lap.offer("1.0.1")
    monkeypatch.setattr(ku, "chats_running", lambda: 1)
    run_now(monkeypatch)
    assert lap.state()["waiting"] == "1.0.1" and lap.version() == "1.0.0" and lap.repairs == []
    assert ku.waiting() == "1.0.1" and any("1.0.1 is waiting" in ln for ln in ku.status_lines())
    # a chat that is starting holds the lock shared (enter): still waiting, the hub not asked again
    monkeypatch.setattr(ku, "chats_running", lambda: 0)
    lap.hub.clear()
    fd = os.open(ku.LOCK, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_SH)
    ku.run()
    assert lap.state()["waiting"] == "1.0.1" and lap.repairs == []
    os.close(fd)
    ku.run()
    assert lap.version() == "1.0.1" and not lap.state().get("waiting")


def test_a_waiting_file_changed_on_disk_is_dropped_not_run(lap, monkeypatch):
    lap.offer("1.0.1")
    monkeypatch.setattr(ku, "chats_running", lambda: 1)
    run_now(monkeypatch)
    with open(ku.STAGED[rel.INSTALLER], "ab") as f:
        f.write(b"x")
    monkeypatch.setattr(ku, "chats_running", lambda: 0)
    ku.run()
    assert lap.repairs == [] and "dropped" in lap.state()["result"] and not lap.state().get("waiting")


def test_a_failed_self_check_rolls_back_and_that_version_is_not_tried_again(lap, monkeypatch):
    lap.offer("1.0.1", data=b"installer 1.0.1 broken")
    run_now(monkeypatch)
    assert lap.repairs == [ku.STAGED[rel.INSTALLER], ku.PREVIOUS] and lap.version() == "1.0.0"
    assert "1.0.1 failed" in lap.state()["result"] and "rolled back to 1.0.0" in lap.state()["result"]
    assert lap.state()["failed"] == ["1.0.1"]
    lap.repairs.clear()
    run_now(monkeypatch)
    assert lap.repairs == [] and "failed its self-check here before" in lap.state()["result"]
    lap.offer("1.0.2")
    run_now(monkeypatch)
    assert lap.version() == "1.0.2"


def test_the_hub_is_asked_at_most_every_six_hours(lap, monkeypatch):
    asked = []
    monkeypatch.setattr(ku, "check", lambda st, key: asked.append(1) or st.update(checked=int(__import__("time").time())))
    monkeypatch.setattr(ku.kc, "unfinished_install", lambda: "")
    monkeypatch.setattr(ku, "_read", lambda p, limit=1 << 16: "personal\n" if p.endswith("/mode") else "")
    ku.run()
    ku.run()
    assert asked == [1]
    ku.run(now=True)
    assert asked == [1, 1]


def test_no_pinned_key_no_update(lap, monkeypatch):
    os.unlink(ku.KEY_FILE)
    lap.offer("1.0.1")
    run_now(monkeypatch)
    assert lap.version() == "1.0.0" and "no release key" in lap.state()["result"]
    assert any("Automatic updates: off" in ln for ln in ku.status_lines())


def test_a_key_open_to_others_is_not_a_pinned_key(lap):
    os.chmod(ku.KEY_FILE, 0o666)
    assert ku.pinned_key() == ""


def test_pin_key_writes_once_and_never_replaces(lap, monkeypatch, keys):
    written = []

    def wf(path, data, mode):
        written.append(path)
        with open(path, "w") as f:
            f.write(data)
        os.chmod(path, mode)
    os.unlink(ku.KEY_FILE)
    monkeypatch.setattr(rel, "RELEASE_KEY", rel.PLACEHOLDER)
    assert "no release key built in" in ku.pin_key(wf) and written == []
    monkeypatch.setattr(rel, "RELEASE_KEY", keys["pub"])
    assert ku.pin_key(wf) == "" and ku.pinned_key() == keys["pub"]
    monkeypatch.setattr(rel, "RELEASE_KEY", keys["other_pub"])
    assert "the pinned one stays" in ku.pin_key(wf) and ku.pinned_key() == keys["pub"] and len(written) == 1


def test_status_lines_show_the_version_and_the_last_check(lap, monkeypatch):
    lap.offer("1.0.0")
    run_now(monkeypatch)
    got = ku.status_lines()
    assert got[0].startswith("kaptyorka 1.0.0; last update check: 20") and got[0].endswith("(up to date)")


# ---------------------------------------------------------------------------- the hub's routes

def test_the_installer_ships_the_updater_and_its_units():
    names = {n for n, _ in ki.members(ki.package_dir())}
    for n in ("kaptyorka/secure/kap_update.py", "kaptyorka/secure/kap_release.py",
              "kaptyorka/secure/files/units/kaptyorka-update.service", "kaptyorka/secure/files/units/kaptyorka-update.timer"):
        assert n in names
    from kaptyorka.secure import kap_setup
    assert ("kap_update.py", "kap_update.py", 0o755) in kap_setup.PROGRAMS
    assert {"kaptyorka-update.service", "kaptyorka-update.timer"} <= set(kap_setup.OUR_UNITS)


def get(port, path, token=""):
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE
    c = http.client.HTTPSConnection("127.0.0.1", port, context=ctx, timeout=10)
    c.request("GET", path, headers={"Authorization": f"Bearer {token}"} if token else {})
    r = c.getresponse()
    data = r.read()
    c.close()
    return r.status, data


def test_the_hub_serves_the_release_only_with_a_personal_invite_or_device(tmp_path, monkeypatch):
    if shutil.which("openssl") is None:
        pytest.skip("openssl not installed")
    import test_hub_box as hb
    from kaptyorka.manager import ManagerStore
    from kaptyorka.store import Store
    monkeypatch.setenv("KAPTYORKA_HOME", str(tmp_path / "home"))
    rels = tmp_path / "home" / "releases"
    (rels / "7.0.1").mkdir(parents=True)
    for n in rel.FILES:
        (rels / "7.0.1" / n).write_bytes(f"file {n}".encode())
    (rels / "7.0.1" / "other.txt").write_text("not a release file")
    os.symlink("7.0.1", rels / "current")
    d = tmp_path / "c"
    d.mkdir()
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(d / "k.pem"),
                    "-out", str(d / "c.pem"), "-days", "2", "-subj", "/CN=hub"], check=True, capture_output=True,
                   timeout=60)
    s = Store(tmp_path / "hub.db")
    ManagerStore(s).set_enabled(False, "test")
    s.person_create("root", "admin")
    alice = s.person_create("alice")
    _, tm_code = s.team_machine_add("box-1", "root")
    tm_token, _ = s.invite_redeem(tm_code, "box-1", "box-1", "team")
    member = s.device_add(alice, "alice-desk")
    port, stop = hb.tls_hub(s, type("C", (), {"cert": str(d / "c.pem"), "key": str(d / "k.pem")}))
    try:
        personal = s.invite_create(alice, kind="personal", by="root")
        team = s.invite_create(alice, by="root")
        for n in rel.FILES:
            assert get(port, f"/i/{personal}/release/{n}") == (200, f"file {n}".encode())
        assert get(port, f"/i/{personal}/release/other.txt")[0] == 404
        assert get(port, f"/i/{personal}/release/..%2Fhub.db")[0] == 404
        assert get(port, f"/i/{team}/release/{rel.INSTALLER}")[0] == 404        # not a personal invite
        assert get(port, f"/i/{'x' * 22}/release/{rel.INSTALLER}")[0] == 404    # no such invite
        assert s.invite_valid(personal)                                        # a download uses nothing
        st, text = hb.post(port, f"/i/{personal}", {"device_name": "alice-laptop", "mode": "personal"})
        dev = json.loads(text)["token"]
        assert get(port, f"/i/{personal}/release/{rel.INSTALLER}")[0] == 404   # used: no more downloads
        # updates: a personal device's token only
        assert get(port, f"/box/v1/release/{rel.MANIFEST}", dev) == (200, f"file {rel.MANIFEST}".encode())
        assert get(port, f"/box/v1/release/{rel.MANIFEST}")[0] == 401
        assert get(port, f"/box/v1/release/{rel.MANIFEST}", member)[0] == 403
        assert get(port, f"/box/v1/release/{rel.MANIFEST}", tm_token)[0] == 403
        assert get(port, "/box/v1/release/other.txt", dev)[0] == 404
        # no upload route
        for method in ("PUT", "POST"):
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE
            c = http.client.HTTPSConnection("127.0.0.1", port, context=ctx, timeout=10)
            c.request(method, f"/box/v1/release/{rel.INSTALLER}", b"evil", {"Authorization": f"Bearer {dev}"})
            assert c.getresponse().status in (403, 404, 405)
            c.close()
        assert (rels / "7.0.1" / rel.INSTALLER).read_bytes() == f"file {rel.INSTALLER}".encode()
        os.unlink(rels / "current")
        assert get(port, f"/box/v1/release/{rel.MANIFEST}", dev)[0] == 404       # no release: nothing offered
    finally:
        stop()
        s.close()
