import shutil
import sqlite3
import subprocess
import tarfile
from pathlib import Path

import pytest

from kaptyorka import backup, remove, service


def fake_run(calls, fail=()):
    def run(cmd, **kw):
        calls.append(list(cmd))
        if cmd[0] == "gpg" and cmd[0] not in fail and "-o" in cmd:
            shutil.copy(cmd[-1], cmd[cmd.index("-o") + 1])
        return subprocess.CompletedProcess(cmd, 1 if cmd[0] in fail else 0, "", "boom\nmore")
    return run


@pytest.fixture
def hub(home):
    h = home / ".kaptyorka"
    h.mkdir(exist_ok=True)
    c = sqlite3.connect(h / "hub.db")
    c.execute("create table t(x)")
    c.execute("insert into t values ('hello')")
    c.commit()
    c.close()
    (h / "files").mkdir()
    (h / "files" / "a.txt").write_text("alpha")
    (h / "files" / "sub").mkdir()
    (h / "files" / "sub" / "b.txt").write_text("beta")
    return h


@pytest.fixture(autouse=True)
def hub_stopped(monkeypatch):
    monkeypatch.setattr(service, "is_active", lambda *a, **k: None)


def test_make_without_gpg(hub, monkeypatch):
    monkeypatch.setattr(backup.shutil, "which", lambda n: None)
    path, enc = backup.make(hub, fake_run([]), now=lambda: 0)
    assert enc is False and path.suffix == ".tar" and path.name.startswith("kaptyorka-")
    with tarfile.open(path) as t:
        names = t.getnames()
    assert "hub.db" in names and "files/a.txt" in names and "files/sub/b.txt" in names
    assert not any(n.startswith("/") for n in names)
    assert not (hub / "backup-pass").exists()


def test_make_with_gpg(hub, monkeypatch):
    monkeypatch.setattr(backup.shutil, "which", lambda n: "/usr/bin/gpg")
    calls = []
    path, enc = backup.make(hub, fake_run(calls))
    assert enc is True and path.name.endswith(".tar.gpg")
    cmd = calls[0]
    assert cmd[:7] == ["gpg", "--batch", "--yes", "--symmetric", "--cipher-algo", "AES256", "--passphrase-file"]
    assert cmd[7] == str(hub / "backup-pass")
    pw = hub / "backup-pass"
    assert pw.stat().st_mode & 0o777 == 0o600 and len(pw.read_text().strip()) >= 32


def test_passphrase_kept_and_never_printed(hub, monkeypatch):
    monkeypatch.setattr(backup.shutil, "which", lambda n: "/usr/bin/gpg")
    lines = []
    assert backup.run_backup(hub, "alice@box-1:/srv/b", fake_run([]), out=lines.append) == 0
    secret = (hub / "backup-pass").read_text().strip()
    assert not any(secret in l for l in lines)
    first = (hub / "backup-pass").read_text()
    backup.make(hub, fake_run([]))
    assert (hub / "backup-pass").read_text() == first


def test_run_backup_messages(hub, monkeypatch):
    calls, lines = [], []
    monkeypatch.setattr(backup.shutil, "which", lambda n: None)
    assert backup.run_backup(hub, "alice@box-1:/srv/b", fake_run(calls), out=lines.append) == 0
    assert calls[0][:5] == ["scp", "-o", "BatchMode=yes", "--", calls[0][4]]
    assert calls[0][-1] == "alice@box-1:/srv/b"
    assert lines[0].endswith("sent to alice@box-1:/srv/b UNENCRYPTED (gpg not installed)")
    assert lines[0].startswith("backup: kaptyorka-")
    monkeypatch.setattr(backup.shutil, "which", lambda n: "/usr/bin/gpg")
    lines.clear()
    backup.run_backup(hub, "alice@box-1:/srv/b", fake_run([]), out=lines.append)
    assert lines[0].endswith(f"(encrypted with gpg; passphrase in {hub / 'backup-pass'}, keep a copy)")


def test_run_backup_scp_failure(hub, monkeypatch):
    monkeypatch.setattr(backup.shutil, "which", lambda n: None)
    lines = []
    assert backup.run_backup(hub, "alice@box-1:/srv/b", fake_run([], fail=("scp",)), out=lines.append) == 1
    assert lines == ["backup: boom"]


@pytest.mark.parametrize("dest", ["-oProxyCommand=x", "alice@box-1", "a b@h:p", "alice@box-1:/a b",
                                  "alice@:p", "@h:p", ""])
def test_bad_dest(hub, dest):
    calls = []
    lines = []
    assert backup.run_backup(hub, dest, fake_run(calls), out=lines.append) == 1
    assert calls == []
    with pytest.raises(ValueError):
        backup.send(hub / "x", dest, fake_run(calls))


def test_restore_refuses_while_hub_runs(hub, monkeypatch, tmp_path):
    monkeypatch.setattr(service, "is_active", lambda *a, **k: True)
    lines = []
    assert backup.restore(hub, tmp_path / "x.tar", fake_run([]), out=lines.append) == 1
    assert lines == ["stop the hub first: kaptyorka stop"]


def test_restore_round_trip(hub, monkeypatch, tmp_path):
    monkeypatch.setattr(backup.shutil, "which", lambda n: None)
    path, _ = backup.make(hub, fake_run([]))
    c = sqlite3.connect(hub / "hub.db")
    c.execute("insert into t values ('later')")
    c.commit()
    c.close()
    (hub / "files" / "a.txt").write_text("changed")
    (hub / "hub.db-wal").write_text("w")
    (hub / "hub.db-shm").write_text("s")
    lines = []
    assert backup.restore(hub, path, fake_run([]), out=lines.append) == 0
    assert lines == [f"restored {path}; start the hub again with kaptyorka start"]
    rows = sqlite3.connect(hub / "hub.db").execute("select x from t").fetchall()
    assert rows == [("hello",)]
    assert (hub / "files" / "a.txt").read_text() == "alpha"
    assert (hub / "files" / "sub" / "b.txt").read_text() == "beta"
    assert (hub / "files.before-restore" / "a.txt").read_text() == "changed"
    old = sqlite3.connect(hub / "hub.db.before-restore").execute("select count(*) from t").fetchone()
    assert old == (2,)
    assert (hub / "hub.db-wal.before-restore").exists() and (hub / "hub.db-shm.before-restore").exists()
    assert not (hub / "hub.db-wal").exists()
    # a second restore replaces the older copies
    assert backup.restore(hub, path, fake_run([]), out=lambda l: None) == 0
    assert (hub / "files.before-restore" / "a.txt").read_text() == "alpha"


def test_restore_gpg_uses_passphrase_file(hub, monkeypatch):
    monkeypatch.setattr(backup.shutil, "which", lambda n: "/usr/bin/gpg")
    path, _ = backup.make(hub, fake_run([]))
    calls = []
    assert backup.restore(hub, path, fake_run(calls), out=lambda l: None) == 0
    cmd = calls[0]
    assert cmd[:6] == ["gpg", "--batch", "--yes", "--quiet", "--passphrase-file", str(hub / "backup-pass")]
    assert cmd[6] == "-o" and cmd[8:] == ["-d", str(path)]


def test_restore_gpg_without_passphrase_file(hub, monkeypatch, tmp_path):
    f = tmp_path / "x.tar.gpg"
    f.write_bytes(b"x")
    lines = []
    assert backup.restore(hub, f, fake_run([]), out=lines.append) == 1
    assert "backup-pass" in lines[0] and "missing" in lines[0]
    assert (hub / "hub.db").exists()


def test_restore_rejects_bad_tar(hub, tmp_path):
    bad = tmp_path / "bad.tar"
    with tarfile.open(bad, "w") as t:
        f = tmp_path / "evil"
        f.write_text("x")
        t.add(f, arcname="../evil")
    lines = []
    assert backup.restore(hub, bad, fake_run([]), out=lines.append) == 1
    nodb = tmp_path / "nodb.tar"
    with tarfile.open(nodb, "w") as t:
        t.add(tmp_path / "evil", arcname="files/evil")
    assert backup.restore(hub, nodb, fake_run([]), out=lines.append) == 1
    assert sqlite3.connect(hub / "hub.db").execute("select count(*) from t").fetchone() == (1,)
    assert not (hub / "hub.db.before-restore").exists()


def test_timer_text_linux():
    t = service.timer_text("kaptyorka-backup", ["/k", "backup", "a@h:p"], "linux")
    assert "OnCalendar=*-*-* 03:00:00" in t and "Persistent=true" in t
    s = service.oneshot_text("kaptyorka-backup", ["/k", "backup", "a@h:p"])
    assert "Type=oneshot" in s and "ExecStart=/k backup a@h:p" in s


def test_timer_text_darwin():
    t = service.timer_text("kaptyorka-backup", ["/k", "backup", "a@h:p"], "darwin")
    assert "io.kaptyorka.kaptyorka-backup" in t and "StartCalendarInterval" in t
    assert "<key>Hour</key><integer>3</integer>" in t and "KeepAlive" not in t


def test_install_timer_linux(home):
    calls = []
    lines = service.install_timer("kaptyorka-backup", ["/k", "backup", "a@h:p"], run=fake_run(calls),
                                  platform="linux")
    d = home / ".config/systemd/user"
    assert (d / "kaptyorka-backup.service").exists() and (d / "kaptyorka-backup.timer").exists()
    assert ["systemctl", "--user", "enable", "--now", "kaptyorka-backup.timer"] in calls
    assert len(lines) == 3 and "03:00" in lines[-1]
    out = service.remove("kaptyorka-backup", run=fake_run(calls), platform="linux")
    assert not (d / "kaptyorka-backup.service").exists() and not (d / "kaptyorka-backup.timer").exists()
    assert ["systemctl", "--user", "disable", "--now", "kaptyorka-backup.timer"] in calls
    assert out == ["kaptyorka-backup: service removed"]


def test_install_timer_darwin(home):
    calls = []
    service.install_timer("kaptyorka-backup", ["/k", "backup", "a@h:p"], run=fake_run(calls), platform="darwin")
    plist = home / "Library/LaunchAgents/io.kaptyorka.kaptyorka-backup.plist"
    assert plist.exists() and plist.stat().st_mode & 0o777 == 0o600
    assert any(c[:2] == ["launchctl", "bootstrap"] for c in calls)
    assert service.remove("kaptyorka-backup", run=fake_run(calls), platform="darwin")
    assert not plist.exists()


def test_remove_lists_backup_timer(home, monkeypatch):
    seen = []
    monkeypatch.setattr(service, "remove", lambda n, **k: seen.append(n) or [])
    remove.main(adapters=[], run=fake_run([]), platform="linux", isatty=False)
    assert "kaptyorka-backup" in seen


def test_make_failure_leaves_no_temp_folder(hub, monkeypatch, tmp_path):
    monkeypatch.setattr(backup.shutil, "which", lambda n: "/usr/bin/gpg")
    monkeypatch.setattr(backup.tempfile, "tempdir", str(tmp_path / "tmp"))
    (tmp_path / "tmp").mkdir()
    with pytest.raises(RuntimeError):
        backup.make(hub, fake_run([], fail=("gpg",)))
    assert list((tmp_path / "tmp").iterdir()) == []


def test_timer_carries_path_and_home(home, monkeypatch):
    monkeypatch.setenv("PATH", "/opt/homebrew/bin:/usr/bin")
    calls = []
    service.install_timer("kaptyorka-backup", ["/k", "backup", "a@h:p"], run=fake_run(calls), platform="linux")
    svc = (home / ".config/systemd/user/kaptyorka-backup.service").read_text()
    assert 'Environment="PATH=/opt/homebrew/bin:/usr/bin"' in svc
    assert f'Environment="KAPTYORKA_HOME={home / ".kaptyorka"}"' in svc
    service.install_timer("kaptyorka-backup", ["/k", "backup", "a@h:p"], run=fake_run(calls), platform="darwin")
    plist = (home / "Library/LaunchAgents/io.kaptyorka.kaptyorka-backup.plist").read_text()
    assert "<key>PATH</key><string>/opt/homebrew/bin:/usr/bin</string>" in plist
    assert "<key>KAPTYORKA_HOME</key>" in plist


def test_restore_failure_puts_old_data_back(hub, monkeypatch):
    monkeypatch.setattr(backup.shutil, "which", lambda n: None)
    path, _ = backup.make(hub, fake_run([]))
    (hub / "files.before-restore").mkdir()
    (hub / "files.before-restore" / "older").write_text("o")
    real = backup.Path.rename

    def flaky(self, target):
        if self.name == "hub.db" and self.parent.name == "new":
            raise OSError("disk full")
        return real(self, target)

    monkeypatch.setattr(backup.Path, "rename", flaky)
    lines = []
    assert backup.restore(hub, path, fake_run([]), out=lines.append) == 1
    monkeypatch.undo()
    assert (hub / "hub.db").exists() and (hub / "files" / "a.txt").exists()
    assert (hub / "files.before-restore" / "older").exists()
    assert "put back" in lines[0]


def test_restore_failed_move_aside_keeps_current_data(hub, monkeypatch):
    monkeypatch.setattr(backup.shutil, "which", lambda n: None)
    path, _ = backup.make(hub, fake_run([]))
    (hub / "hub.db.before-restore").write_text("older")
    (hub / "files" / "a.txt").write_text("current")
    c = sqlite3.connect(hub / "hub.db")
    c.execute("insert into t values ('current')")
    c.commit()
    c.close()
    real = backup.Path.rename

    def flaky(self, target):
        if self.name == "hub.db" and Path(target).name == "hub.db.before-restore":
            raise OSError("denied")
        return real(self, target)

    monkeypatch.setattr(backup.Path, "rename", flaky)
    lines = []
    assert backup.restore(hub, path, fake_run([]), out=lines.append) == 1
    monkeypatch.undo()
    assert "put back" in lines[0]
    rows = sqlite3.connect(hub / "hub.db").execute("select x from t").fetchall()
    assert rows == [("hello",), ("current",)]
    assert (hub / "files" / "a.txt").read_text() == "current"
    assert (hub / "hub.db.before-restore").read_text() == "older"
    assert not (hub / "hub.db.before-restore.old").exists()
