#!/usr/bin/python3 -I
"""kap_release: signed releases of the personal-mode installer (build plan 16, sections 1 and 2).

One small manifest per release says the version, the installer's file name and its SHA-256; it
is signed with `ssh-keygen -Y sign` (ed25519, namespace kaptyorka-release), so the passphrase is
typed once. A laptop trusts a manifest only when `ssh-keygen -Y verify` accepts its signature
against the release key pinned at install (/etc/kaptyorka/release-key), and the version comes
from inside the signed manifest, never from a file name. Standard library and ssh-keygen only.

The release key (RELEASE_KEY below) is a placeholder until the first signed release. To set it,
once, on the owner's computer, from a checkout:

  ssh-keygen -t ed25519 -C kaptyorka-release -f ~/.ssh/kaptyorka-release   # asks a passphrase
  python3 -I src/kaptyorka/secure/kap_release.py set-key ~/.ssh/kaptyorka-release.pub
  # it sets RELEASE_KEY here and the KEY= line of the README's install steps: commit both. Back
  # up the private key once, offline, under its passphrase. There is no key rotation: a lost
  # key means every laptop installs again by hand.

Release side (the release script, after CI built the tag's installer), from a checkout AT THE TAG:

  kap_release.py prepare VERSION --ci CI-INSTALLER --key PRIVATE-KEY --out DIR
      builds the installer here, refuses unless it is byte for byte CI's, writes DIR/release.manifest
      and lets ssh-keygen ask the passphrase at the terminal (never -P, an environment variable or
      a file), then checks the signature against RELEASE_KEY
  kap_release.py verify DIR [--key-file PUB]       the three files of DIR, as a laptop checks them
  kap_release.py publish DIR SSH-DESTINATION       copies them to the hub over ssh (the hub has no
      upload route): into ~/.kaptyorka/releases/VERSION/, then switches releases/current to it
"""
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile

RELEASE_KEY = "REPLACE-WITH-THE-RELEASE-PUBLIC-KEY"   # set-key writes "ssh-ed25519 AAAA..." here
PLACEHOLDER = "REPLACE-WITH-THE-RELEASE-PUBLIC-KEY"
NAMESPACE = "kaptyorka-release"
PRINCIPAL = "kaptyorka-release"
INSTALLER = "kaptyorka-secure-setup.pyz"
MANIFEST = "release.manifest"
SIGNATURE = "release.manifest.sig"
FILES = (INSTALLER, MANIFEST, SIGNATURE)
HEADER = "kaptyorka release manifest 1"
KEY_RX = re.compile(r"ssh-ed25519 AAAAC3NzaC1lZDI1NTE5[A-Za-z0-9+/]{48}")
VERSION_RX = re.compile(r"(0|[1-9][0-9]{0,5})(\.(0|[1-9][0-9]{0,5})){1,3}")
SHA_RX = re.compile(r"[0-9a-f]{64}")
MAX_MANIFEST = 1024
MAX_SIGNATURE = 4096
MAX_INSTALLER = 32 << 20
KEYGEN = "/usr/bin/ssh-keygen"
NOBODY = 65534        # the user root's ssh-keygen runs as (nobody, nogroup on Debian and Ubuntu)
CLEAN_ENV = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8"}


def download_value(code: str) -> str:
    """What the README's install lines send instead of the invite code to download the release
    (red team round 6, finding 1): those lines fetch with `curl -k`, before anything checks the
    hub, so whoever answers sees it. This names the invite to the hub but cannot redeem it. The
    shell gives the same: printf 'kaptyorka-release:%s' CODE | sha256sum."""
    return hashlib.sha256(f"kaptyorka-release:{code}".encode()).hexdigest()


class Bad(Exception):
    """A file that must not be used; the text says why in one sentence."""


def code_key() -> str:
    """The release key built into this code: '' while it is the placeholder."""
    return RELEASE_KEY if KEY_RX.fullmatch(RELEASE_KEY) else ""


def parse_key(text: str) -> str:
    """The 'ssh-ed25519 AAAA...' part of a public key or allowed-signers line; '' when there is none."""
    m = re.search(r"ssh-ed25519 AAAAC3NzaC1lZDI1NTE5[A-Za-z0-9+/]{48}(?![A-Za-z0-9+/=])", text or "")
    return m.group(0) if m else ""


def signers_line(key: str) -> str:
    """The allowed-signers line ssh-keygen -Y verify takes: this key, for this namespace only."""
    if not KEY_RX.fullmatch(key):
        raise Bad("that is not an ed25519 public key")
    return f'{PRINCIPAL} namespaces="{NAMESPACE}" {key}\n'


def version_tuple(v: str) -> tuple:
    if not VERSION_RX.fullmatch(v or ""):
        raise Bad(f"{v[:40]!r} is not a release version (digits and dots)")
    return tuple(int(x) for x in v.split("."))


def newer(a: str, b: str) -> bool:
    """True when version a is strictly newer than version b. A b that is not a release version
    (an unknown install) counts as the oldest."""
    try:
        old = version_tuple(b)
    except Bad:
        return VERSION_RX.fullmatch(a or "") is not None
    new = version_tuple(a)
    n = max(len(new), len(old))
    return new + (0,) * (n - len(new)) > old + (0,) * (n - len(old))


def manifest_text(version: str, sha256: str, name: str = INSTALLER) -> str:
    version_tuple(version)
    if not SHA_RX.fullmatch(sha256) or name != INSTALLER:
        raise Bad("a manifest needs the installer's name and its SHA-256")
    return f"{HEADER}\nversion={version}\nfile={name}\nsha256={sha256}\n"


def parse_manifest(data: bytes) -> dict:
    """{"version", "file", "sha256"} of a manifest in exactly the form manifest_text writes."""
    if len(data) > MAX_MANIFEST:
        raise Bad("the manifest is too long")
    try:
        text = data.decode("ascii")
    except UnicodeDecodeError:
        raise Bad("the manifest is not plain text")
    lines = text.split("\n")
    if len(lines) != 5 or lines[0] != HEADER or lines[4] != "":
        raise Bad("the manifest is not a kaptyorka release manifest")
    out = {}
    for ln, key in zip(lines[1:4], ("version", "file", "sha256")):
        k, eq, v = ln.partition("=")
        if k != key or not eq:
            raise Bad("the manifest is not a kaptyorka release manifest")
        out[key] = v
    version_tuple(out["version"])
    if out["file"] != INSTALLER or not SHA_RX.fullmatch(out["sha256"]):
        raise Bad("the manifest names another file or no SHA-256")
    return out


def verify(manifest: bytes, signature: bytes, key: str, keygen: str = KEYGEN) -> dict:
    """The manifest's values, only when its signature is good for this key and namespace;
    else Bad. Nothing of the manifest is used before ssh-keygen accepted it."""
    if not KEY_RX.fullmatch(key or ""):
        raise Bad("no release key is set on this computer")
    if not manifest or not signature:
        raise Bad("the release is not signed")
    if len(manifest) > MAX_MANIFEST or len(signature) > MAX_SIGNATURE:
        raise Bad("the release's manifest or signature is too long")
    d = tempfile.mkdtemp(prefix="kaptyorka-verify-")
    # as root (the updater), ssh-keygen parses the hub's bytes as nobody: a bug in its parser is
    # then not root's (red team round 6, I-1). The two files are public: readable, root's to write.
    drop = {"user": NOBODY, "group": NOBODY, "extra_groups": []} if os.geteuid() == 0 else {}
    try:
        os.chmod(d, 0o711 if drop else 0o700)
        for name, data in (("signers", signers_line(key).encode()), ("manifest.sig", signature)):
            with open(os.open(os.path.join(d, name), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as f:
                f.write(data)
                os.fchmod(f.fileno(), 0o644 if drop else 0o600)
        try:
            r = subprocess.run([keygen, "-Y", "verify", "-f", os.path.join(d, "signers"), "-I", PRINCIPAL,
                                "-n", NAMESPACE, "-s", os.path.join(d, "manifest.sig")], input=manifest,
                               capture_output=True, timeout=30, env=CLEAN_ENV, cwd="/", **drop)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise Bad(f"cannot run ssh-keygen to check the signature ({type(e).__name__})")
    finally:
        shutil.rmtree(d, ignore_errors=True)
    if r.returncode != 0:
        raise Bad("the release's signature is not good for the release key")
    return parse_manifest(manifest)


def check_installer(data: bytes, m: dict) -> None:
    if len(data) > MAX_INSTALLER:
        raise Bad("the installer is too big")
    if hashlib.sha256(data).hexdigest() != m["sha256"]:
        raise Bad("the installer is not the one the signed manifest names (its SHA-256 differs)")


# ---------------------------------------------------------------------------- the release side

def _installer_module():
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import kap_installer
    return kap_installer


def set_key(pub_path: str) -> int:
    key = parse_key(open(pub_path).read())
    if not key:
        print(f"{pub_path}: no ed25519 public key in it", file=sys.stderr)
        return 2
    me = os.path.abspath(__file__)
    text = open(me).read()
    new, n = re.subn(r'^RELEASE_KEY = "[^"\n]*"', f'RELEASE_KEY = "{key}"', text, count=1, flags=re.M)
    if n != 1:
        print("cannot find the RELEASE_KEY line", file=sys.stderr)
        return 2
    with open(me, "w") as f:
        f.write(new)
    print(f"RELEASE_KEY is now {key}")
    readme = os.path.join(os.path.dirname(me), "..", "..", "..", "README.md")
    if os.path.isfile(readme):   # a checkout: the README's install steps carry the same key
        text = open(readme).read()
        new, n = re.subn(r"^KEY='[^'\n]*'", f"KEY='{key}'", text, flags=re.M)
        with open(readme, "w") as f:
            f.write(new)
        print(f"README.md: {n} KEY= line(s) set.")
    print("Commit both files. The release page carries the key from this file (publish.yml).")
    return 0


def prepare(version: str, ci: str, key_path: str, out: str) -> int:
    ki = _installer_module()
    if not code_key():
        print("RELEASE_KEY is still the placeholder: set it first (kap_release.py set-key)", file=sys.stderr)
        return 2
    if ki.version() != version:
        print(f"this checkout is version {ki.version()!r}, not {version}: run it from a checkout at the tag",
              file=sys.stderr)
        return 2
    version_tuple(version)
    pub = parse_key(open(key_path + ".pub").read()) if os.path.exists(key_path + ".pub") else ""
    if pub != code_key():
        print(f"{key_path}.pub is not the release key built into this code (RELEASE_KEY)", file=sys.stderr)
        return 2
    mine = ki.build()
    with open(ci, "rb") as f:
        theirs = f.read()
    if mine != theirs:
        print(f"STOP: the installer built here ({hashlib.sha256(mine).hexdigest()[:16]}) is not CI's "
              f"({hashlib.sha256(theirs).hexdigest()[:16]}): nothing was signed", file=sys.stderr)
        return 1
    os.makedirs(out, exist_ok=True)
    for n in FILES:
        if os.path.lexists(os.path.join(out, n)):
            os.unlink(os.path.join(out, n))
    with open(os.path.join(out, INSTALLER), "wb") as f:
        f.write(mine)
    man = os.path.join(out, MANIFEST)
    with open(man, "w") as f:
        f.write(manifest_text(version, hashlib.sha256(mine).hexdigest()))
    print(f"The installer is CI's byte for byte. Signing {version}: ssh-keygen asks the key's passphrase.")
    # the terminal is ssh-keygen's own: it asks the passphrase there; nothing else carries it
    r = subprocess.run([shutil.which("ssh-keygen") or KEYGEN, "-Y", "sign", "-f", key_path, "-n", NAMESPACE, man])
    if r.returncode != 0:
        print("STOP: ssh-keygen did not sign", file=sys.stderr)
        return 1
    return verify_dir(out, code_key())


def verify_dir(d: str, key: str) -> int:
    try:
        files = {}
        for n in FILES:
            with open(os.path.join(d, n), "rb") as f:
                files[n] = f.read()
        m = verify(files[MANIFEST], files[SIGNATURE], key, shutil.which("ssh-keygen") or KEYGEN)
        check_installer(files[INSTALLER], m)
    except (OSError, Bad) as e:
        print(f"refused: {e}", file=sys.stderr)
        return 1
    print(f"good: {m['version']} {m['file']} sha256 {m['sha256']}")
    return 0


def publish(d: str, dest: str, folder: str = ".kaptyorka/releases") -> int:
    """The three files to the hub's static folder over ssh, then releases/current switched to
    them in one rename. Checked first, as a laptop checks them."""
    if verify_dir(d, code_key()) != 0:
        return 1
    m = parse_manifest(open(os.path.join(d, MANIFEST), "rb").read())
    if not re.fullmatch(r"[A-Za-z0-9_.-]+@[A-Za-z0-9_.:-]+|[A-Za-z0-9_.-]+", dest) or \
            not re.fullmatch(r"[A-Za-z0-9_./-]+", folder) or ".." in folder:
        print("bad ssh destination or folder", file=sys.stderr)
        return 2
    v = m["version"]
    steps = [["ssh", dest, f"mkdir -p {folder}/{v}"],
             ["scp", "-q", *[os.path.join(d, n) for n in FILES], f"{dest}:{folder}/{v}/"],
             ["ssh", dest, f"cd {folder} && ln -sfn {v} current.new && mv -T current.new current && ls current/"]]
    for cmd in steps:
        if subprocess.run(cmd, timeout=300).returncode != 0:
            print(f"STOP: {cmd[0]} failed", file=sys.stderr)
            return 1
    print(f"The hub now offers {v}.")
    return 0


def main(argv) -> int:
    opts, pos = {}, []
    it = iter(argv)
    for a in it:
        if a.startswith("--"):
            opts[a] = next(it, "")
        else:
            pos.append(a)
    try:
        if pos[:1] == ["set-key"] and len(pos) == 2:
            return set_key(pos[1])
        if pos[:1] == ["prepare"] and len(pos) == 2 and all(opts.get(k) for k in ("--ci", "--key", "--out")):
            return prepare(pos[1], opts["--ci"], opts["--key"], opts["--out"])
        if pos[:1] == ["verify"] and len(pos) == 2:
            key = parse_key(open(opts["--key-file"]).read()) if opts.get("--key-file") else code_key()
            return verify_dir(pos[1], key)
        if pos[:1] == ["publish"] and len(pos) == 3:
            return publish(pos[1], pos[2], opts.get("--folder") or ".kaptyorka/releases")
    except (OSError, Bad) as e:
        print(f"kap_release: {e}", file=sys.stderr)
        return 2
    print(__doc__.split("Release side", 1)[1].split("\n", 2)[2].rstrip(), file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
