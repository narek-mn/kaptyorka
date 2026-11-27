"""The personal-mode installer as one file (secure design v2, 9.2): a Python zip application
holding kap_setup and every secure/ and relay/ file it installs, standard library only.

Built the same way, byte for byte, by the release workflow (the GitHub release asset, with its
.sha256 next to it) and by the installer itself (the copy it keeps in /usr/local/libexec/kaptyorka
for rejoin and repair): fixed names in sorted order, stored (no compression, so no zlib version changes a byte), one
fixed time and mode. The same package files give the same file and the same hash.

  python3 -I kap_installer.py build OUT.pyz      # prints the SHA-256
  python3 -I kap_installer.py sha256              # the hash of this package's installer
"""
import hashlib
import io
import os
import re
import sys
import zipfile

NAME = "kaptyorka-secure-setup.pyz"
RELEASES = "https://github.com/narek-mn/kaptyorka/releases"
SHEBANG = b"#!/usr/bin/python3 -I\n"
STAMP = (1980, 1, 1, 0, 0, 0)
VERSION_RX = re.compile(r"[0-9][0-9A-Za-z.+-]{0,30}")

# Runs first: unpacks the installer into a fresh root-only folder, runs kap_setup from there
# with this file's own path (it keeps a copy), and removes the folder again.
MAIN = '''"""kaptyorka-secure-setup: the personal-mode installer (secure design v2, 9.2)."""
import os, shutil, sys, tempfile, zipfile


def main():
    me = os.path.dirname(os.path.abspath(__file__))
    top = "/run" if os.geteuid() == 0 and os.path.isdir("/run") else None
    d = tempfile.mkdtemp(prefix="kaptyorka-setup-", dir=top)
    try:
        with zipfile.ZipFile(me) as z:
            for n in z.namelist():
                if n != "__main__.py" and (not n.startswith("kaptyorka/") or ".." in n.split("/")):
                    raise SystemExit("kaptyorka-secure-setup: this installer file is damaged")
            z.extractall(d)
        sys.path.insert(0, os.path.join(d, "kaptyorka", "secure"))
        import kap_setup
        kap_setup.INSTALLER_FILE = me
        return kap_setup.main(sys.argv[1:])
    finally:
        shutil.rmtree(d, ignore_errors=True)


sys.exit(main())
'''


def package_dir() -> str:
    """The kaptyorka package folder this file is in (a checkout's src/kaptyorka, or installed)."""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def members(pkg: str):
    """(name in the zip, path on disk), sorted: kaptyorka/__init__.py, the .py files of secure/ and
    relay/, and everything under secure/files and relay/units (as the wheel ships them)."""
    out = [("kaptyorka/__init__.py", os.path.join(pkg, "__init__.py"))]
    for part in ("secure", "relay"):
        top = os.path.join(pkg, part)
        for dirpath, dirs, files in os.walk(top):
            dirs[:] = sorted(x for x in dirs if x != "__pycache__")
            rel = os.path.relpath(dirpath, top).replace(os.sep, "/")
            for n in sorted(files):
                if n.endswith((".pyc", ".pyo")) or n.startswith(".") or n.endswith("~"):
                    continue
                if rel == "." and not n.endswith(".py"):
                    continue
                if rel != "." and not (rel.split("/")[0] in ("files", "units")):
                    continue
                inner = n if rel == "." else f"{rel}/{n}"
                out.append((f"kaptyorka/{part}/{inner}", os.path.join(dirpath, n)))
    return sorted(out)


def build(pkg: str = "") -> bytes:
    """The installer file's bytes."""
    pkg = pkg or package_dir()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:
        for name, data in [("__main__.py", MAIN.encode())] + [(n, open(p, "rb").read()) for n, p in members(pkg)]:
            info = zipfile.ZipInfo(name, date_time=STAMP)
            info.compress_type = zipfile.ZIP_STORED
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            z.writestr(info, data)
    return SHEBANG + buf.getvalue()


def sha256(pkg: str = "") -> str:
    return hashlib.sha256(build(pkg)).hexdigest()


def version(pkg: str = "") -> str:
    try:
        m = re.search(r'__version__\s*=\s*"([^"]*)"', open(os.path.join(pkg or package_dir(), "__init__.py")).read())
    except OSError:
        return ""
    return m.group(1) if m and VERSION_RX.fullmatch(m.group(1)) else ""


def release_page(ver: str) -> str:
    """The kaptyorka release page of this version (the installer and its .sha256 are on it);
    the list of releases for a version that does not look like one."""
    return f"{RELEASES}/tag/v{ver}" if VERSION_RX.fullmatch(ver or "") else RELEASES


def install_sentence(ver: str) -> str:
    """The one fixed sentence a hub's personal invite page and `kaptyorka invite --personal` show
    next to the link (design v2, 9.2; finding 5a): never a command, since a hostile hub could
    print any command to run as root, and never the release key, which must not come from the
    hub (build plan 16). The commands and the key are in the README and on the release page."""
    return (f"Install with the steps on the kaptyorka release page {release_page(ver)}: they download the"
            f" installer from this hub with your link, check its signature against the release key printed on"
            f" that page, and only then run it.")


def main(argv) -> int:
    if argv[:1] == ["build"] and len(argv) == 2:
        data = build()
        with open(argv[1], "wb") as f:
            f.write(data)
        os.chmod(argv[1], 0o755)
        print(f"{hashlib.sha256(data).hexdigest()}  {os.path.basename(argv[1])}")
        return 0
    if argv == ["sha256"]:
        print(sha256())
        return 0
    print(__doc__.strip().splitlines()[-2].strip() + "\n" + __doc__.strip().splitlines()[-1].strip(), file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
