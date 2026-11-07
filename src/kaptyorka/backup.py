import re
import secrets
import shutil
import sqlite3
import subprocess
import tarfile
import tempfile
import time
from pathlib import Path
from typing import Callable, Tuple

from kaptyorka import service

_SAFE = {"filter": "data"} if hasattr(tarfile, "data_filter") else {}
DEST = re.compile(r"[^@\s:-][^@\s:]*@[^@\s:]+:\S+")


def _check_dest(dest: str) -> None:
    if dest.startswith("-") or not DEST.fullmatch(dest):
        raise ValueError(f"backup destination must look like user@host:path, not {dest!r}")


def _passfile(home: Path) -> Path:
    p = home / "backup-pass"
    if not p.exists():
        service._write_private(p, secrets.token_urlsafe(32) + "\n")
    return p


def _fail(r, cmd: str) -> RuntimeError:
    err = (getattr(r, "stderr", "") or "").strip().splitlines()
    return RuntimeError(err[0] if err else f"{cmd} failed")


def make(home: Path, run: Callable = subprocess.run, now: Callable = time.time) -> Tuple[Path, bool]:
    """Write a tar of hub.db and files/ into a temp folder; encrypt it when gpg is installed."""
    stamp = time.strftime("%Y%m%d-%H%M", time.localtime(now()))
    out = Path(tempfile.mkdtemp(prefix="kaptyorka-backup-"))
    try:
        return _make(home, run, out, stamp)
    except BaseException:
        shutil.rmtree(out, ignore_errors=True)  # never leave a plaintext tar behind
        raise


def _make(home: Path, run: Callable, out: Path, stamp: str) -> Tuple[Path, bool]:
    tar = out / f"kaptyorka-{stamp}.tar"
    stage = out / "stage"
    stage.mkdir()
    src = home / "hub.db"
    if src.exists():
        a = sqlite3.connect(src)
        b = sqlite3.connect(stage / "hub.db")
        try:
            a.backup(b)
        finally:
            b.close()
            a.close()
    with tarfile.open(tar, "w") as t:
        if (stage / "hub.db").exists():
            t.add(stage / "hub.db", arcname="hub.db")
        if (home / "files").is_dir():
            t.add(home / "files", arcname="files")
    shutil.rmtree(stage)
    if not shutil.which("gpg"):
        return tar, False
    pw = _passfile(home)
    gpg = Path(str(tar) + ".gpg")
    r = run(["gpg", "--batch", "--yes", "--symmetric", "--cipher-algo", "AES256",
             "--passphrase-file", str(pw), "-o", str(gpg), str(tar)],
            capture_output=True, text=True)
    if r.returncode != 0:
        raise _fail(r, "gpg")
    tar.unlink()
    return gpg, True


def send(path: Path, dest: str, run: Callable = subprocess.run) -> None:
    _check_dest(dest)
    r = run(["scp", "-o", "BatchMode=yes", "--", str(path), dest], capture_output=True, text=True)
    if r.returncode != 0:
        raise _fail(r, "scp")


def run_backup(home: Path, dest: str, run: Callable = subprocess.run, out: Callable = print) -> int:
    path = None
    try:
        _check_dest(dest)
        path, enc = make(home, run)
        send(path, dest, run)
    except (ValueError, RuntimeError, OSError, sqlite3.Error) as e:
        out(f"backup: {e}")
        return 1
    finally:
        if path is not None:
            shutil.rmtree(path.parent, ignore_errors=True)
    if enc:
        out(f"backup: {path.name} sent to {dest} (encrypted with gpg; "
            f"passphrase in {home / 'backup-pass'}, keep a copy)")
    else:
        out(f"backup: {path.name} sent to {dest} UNENCRYPTED (gpg not installed)")
    return 0


def _rm(p: Path) -> None:
    if p.is_dir() and not p.is_symlink():
        shutil.rmtree(p)
    elif p.exists() or p.is_symlink():
        p.unlink()


def _swap_in(home: Path, new: Path) -> None:
    """Move the current data aside as *.before-restore, put the new data in place, and only
    then delete the older *.before-restore copies. On failure, put everything back."""
    moved = []  # (path, aside, prev): current data now at aside, older aside at prev (or None)
    try:
        for name in ("hub.db", "hub.db-wal", "hub.db-shm", "files"):
            path, aside = home / name, home / f"{name}.before-restore"
            if not path.exists():
                continue
            prev = None
            if aside.exists() or aside.is_symlink():
                prev = home / f"{name}.before-restore.old"
                _rm(prev)
                aside.rename(prev)
            moved.append((path, aside, prev))
            path.rename(aside)
        (new / "hub.db").rename(home / "hub.db")
        if (new / "files").is_dir():
            (new / "files").rename(home / "files")
    except BaseException:
        for path, aside, prev in reversed(moved):
            try:
                _rm(path)
                if aside.exists():
                    aside.rename(path)
                if prev is not None and prev.exists():
                    prev.rename(aside)
            except OSError:
                pass
        raise
    for _, _, prev in moved:
        if prev is not None:
            _rm(prev)


def restore(home: Path, file: Path, run: Callable = subprocess.run, out: Callable = print) -> int:
    if service.is_active("kaptyorka-hub"):
        out("stop the hub first: kaptyorka stop")
        return 1
    file = Path(file)
    if not file.is_file():
        out(f"restore: {file} not found")
        return 1
    work = Path(tempfile.mkdtemp(prefix="kaptyorka-restore-", dir=home))
    try:
        tar = file
        if file.name.endswith(".gpg"):
            pw = home / "backup-pass"
            if not pw.exists():
                out(f"restore: {pw} is missing; copy the passphrase file there first")
                return 1
            tar = work / "backup.tar"
            r = run(["gpg", "--batch", "--yes", "--quiet", "--passphrase-file", str(pw),
                     "-o", str(tar), "-d", str(file)], capture_output=True, text=True)
            if r.returncode != 0:
                out(f"restore: {_fail(r, 'gpg')}")
                return 1
        new = work / "new"
        new.mkdir()
        try:
            with tarfile.open(tar) as t:
                members = t.getmembers()
                for m in members:
                    parts = Path(m.name).parts
                    if (m.name.startswith("/") or ".." in parts or not parts
                            or parts[0] not in ("hub.db", "files") or not (m.isfile() or m.isdir())):
                        out(f"restore: unexpected entry {m.name!r} in {file.name}")
                        return 1
                if not any(m.name == "hub.db" and m.isfile() for m in members):
                    out(f"restore: {file.name} holds no hub.db")
                    return 1
                for m in members:
                    t.extract(m, new, **_SAFE)
        except (tarfile.TarError, OSError) as e:
            out(f"restore: cannot read {file.name}: {e}")
            return 1
        try:
            _swap_in(home, new)
        except OSError as e:
            out(f"restore: failed, old data put back: {e}")
            return 1
    finally:
        shutil.rmtree(work, ignore_errors=True)
    out(f"restored {file}; start the hub again with kaptyorka start")
    return 0
