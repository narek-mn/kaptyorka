#!/usr/bin/python3 -I
"""kaptyorka's own updates on a personal laptop (build plan 16, "laptop updater"). Root, run by
kaptyorka-update.timer every hour; installed as /usr/local/libexec/kaptyorka/kap_update.py.

  kap_update.py run [--now]   check the hub (at most every CHECK_S; --now: at once), then
                              install a waiting update when no team chat runs
  kap_update.py status    the lines hubchat list shows

A check asks the hub (pinned TLS, the device token: the same as the relay's) for the signed
manifest of its release. The manifest's signature must be good for the release key pinned at
install (/etc/kaptyorka/release-key; never a key from the hub), and its version strictly newer
than the installed one. Only then is the installer downloaded, and it must have the manifest's
SHA-256. No byte of it runs before that. The verified files wait in a root-only folder.

The install waits until no team chat runs, and a chat cannot start while it runs (the update
lock, which enter holds for each chat). It keeps the installed installer, runs the new one's
repair (every file and unit written again, then the self-check), and when that fails runs the
kept one's repair: the laptop is back on the version it had. A version that failed is never
tried again. The hub decides nothing: it can only offer a signed release or none.
"""
import fcntl
import hashlib
import hmac
import http.client
import json
import os
import ssl
import subprocess
import sys
import threading
import time

if __package__:
    from . import kap_release as rel, kapcommon as kc
else:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import kap_release as rel
    import kapcommon as kc

KEY_FILE = kc.ETC + "/release-key"          # the allowed-signers line of the pinned release key
STATE_FILE = kc.ETC + "/update.json"        # what hubchat list shows; no secret
WORK = kc.LIBEXEC + "/update"               # root-only: the verified waiting files, the kept installer
MAC = sys.platform == "darwin"   # macOS (TEST VERSION): launchd's hourly job instead of the timer
LOCK = kc.RUN + "/update.lock"
RUN_LOCK = kc.RUN + "/update-run.lock"   # one updater at a time
CHECK_S = 6 * 3600       # the hub is asked at most this often (each ask tells it the laptop is on)
REPAIR_S = 1800          # one repair (the new installer's or the kept one's) at most
FETCH_S = 120            # one small file from the hub, all in (a hub that drips bytes is cut off)
INSTALLER_S = 600        # the installer, all in
STAGED = {n: f"{WORK}/staged-{n}" for n in rel.FILES}
PREVIOUS = f"{WORK}/previous.pyz"
ENV = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8"}


def _say(*lines) -> None:
    for ln in lines:
        print(kc.term(ln), flush=True)


# ---------------------------------------------------------------------------- the pinned key

def pinned_key() -> str:
    try:
        fd = os.open(KEY_FILE, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return ""
    with os.fdopen(fd) as f:
        st = os.fstat(f.fileno())
        if st.st_uid not in (0, os.geteuid()) or st.st_mode & 0o022:   # root's own (the owner, in tests)
            return ""
        return rel.parse_key(f.read(4096))


def pin_key(write_file) -> str:
    """At install and repair: the release key in this code goes to /etc/kaptyorka/release-key
    when none is pinned yet. A pinned key is never replaced (there is no key rotation). Returns a
    line for the person, or ''."""
    have, mine = pinned_key(), rel.code_key()
    if have:
        return "" if have == mine or not mine else \
            "warning: this installer carries another release key than the one pinned here; the pinned one stays"
    if not mine:
        return "This installer has no release key built in, so kaptyorka will not update itself."
    write_file(KEY_FILE, rel.signers_line(mine), 0o644)
    return ""


# ---------------------------------------------------------------------------- state

def load_state() -> dict:
    try:
        with open(STATE_FILE) as f:
            got = json.load(f)
        return got if isinstance(got, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(st: dict) -> None:
    tmp = f"{STATE_FILE}.tmp"
    with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o644), "w") as f:
        json.dump(st, f, indent=1, sort_keys=True)
    os.chmod(tmp, 0o644)
    os.replace(tmp, STATE_FILE)


def installed_version() -> str:
    try:
        with open(f"{kc.ETC}/version") as f:
            return f.read(64).strip()
    except OSError:
        return ""


def status_lines() -> list:
    """For hubchat list: the installed version, the last update check and a waiting update."""
    st = load_state()
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(st["checked"])) if isinstance(st.get("checked"), (int, float)) \
        else "never"
    out = [f"kaptyorka {installed_version() or '?'}; last update check: {when}"
           + (f" ({kc.term(str(st['result']))[:200]})" if st.get("result") else "")]
    if not pinned_key():
        out.append("  Automatic updates: off (no release key on this computer).")
    if st.get("waiting"):
        out.append(f"  Update {kc.term(str(st['waiting']))[:32]} is waiting: it installs by itself once no team chat runs.")
    return out


def waiting() -> str:
    """The waiting update's version, '' when none (enter says it at each chat start)."""
    v = load_state().get("waiting")
    return v if isinstance(v, str) and rel.VERSION_RX.fullmatch(v) else ""


# ---------------------------------------------------------------------------- the hub

class Pinned(http.client.HTTPSConnection):
    """https to the hub, accepted only by the certificate fingerprint the install pinned (the
    relay's and the installer's own way), checked before the token goes out."""

    def __init__(self, host: str, port: int, pin: str, timeout: float):
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE   # the fingerprint decides
        super().__init__(host, port, timeout=timeout, context=ctx)
        self._pin = pin

    def connect(self):
        super().connect()
        der = self.sock.getpeercert(binary_form=True)
        if not der or not hmac.compare_digest(hashlib.sha256(der).hexdigest(), self._pin):
            self.sock.close()
            raise OSError("the hub's certificate is not the pinned one")


def hub_values() -> dict:
    conf = dict(ln.split("=", 1) for ln in _read(f"{kc.ETC}/relay.conf").splitlines() if "=" in ln)
    tok = _read(f"{kc.SECRET}/device-token").strip()
    import ipaddress
    try:
        ip = str(ipaddress.ip_address(conf.get("hub_ip", "")))
    except ValueError:
        raise rel.Bad("this computer's hub settings are damaged")
    port, fp = conf.get("hub_port", ""), conf.get("hub_sha256", "")
    if not (port.isdigit() and 0 < int(port) < 65536 and len(fp) == 64 and all(c in "0123456789abcdef" for c in fp)
            and kc.TOKEN_RX.fullmatch(tok)):
        raise rel.Bad("this computer's hub settings are damaged")
    return {"ip": ip, "port": int(port), "fp": fp, "token": tok}


def _read(path: str, limit: int = 1 << 16) -> str:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return ""
    with os.fdopen(fd, "rb") as f:
        return f.read(limit).decode("utf-8", "replace")


class Unreachable(rel.Bad):
    """The hub did not answer as it should: nothing was refused, the next check tries again."""


def fetch(hub: dict, name: str, limit: int, timeout: float = 60, deadline: float = FETCH_S):
    """One release file from the hub's static folder: its bytes, or None when the hub has none.
    timeout bounds each read, deadline the whole fetch (red team round 6, finding 3: a hub that
    sends a byte now and then, or endless "100 Continue" lines, never trips a read's timeout)."""
    conn = Pinned(hub["ip"], hub["port"], hub["fp"], min(timeout, deadline))
    late = threading.Event()

    def cut():
        late.set()
        sock = conn.sock
        if sock is not None:
            try:
                sock.shutdown(2)   # wakes the read that waits
            except OSError:
                pass
    timer = threading.Timer(deadline, cut)
    timer.daemon = True
    timer.start()
    try:
        conn.request("GET", f"/box/v1/release/{name}", headers={"Authorization": f"Bearer {hub['token']}",
                                                                 "X-Kaptyorka-Mode": "personal"})
        r = conn.getresponse()
        data = r.read(limit + 1)
    except (OSError, ssl.SSLError, http.client.HTTPException, ValueError) as e:
        if late.is_set():
            raise Unreachable(f"the hub took longer than {int(deadline)} seconds for {name}")
        raise Unreachable(f"cannot reach the hub ({kc.term(str(e))[:80] or type(e).__name__})")
    finally:
        timer.cancel()
        conn.close()
    if late.is_set():
        raise Unreachable(f"the hub took longer than {int(deadline)} seconds for {name}")
    if r.status == 404:
        return None
    if r.status != 200:
        raise Unreachable(f"the hub answered {r.status}")
    if len(data) > limit:
        raise rel.Bad(f"the hub's {name} is too big")
    return data


# ---------------------------------------------------------------------------- check and install

def _write(path: str, data: bytes, mode: int = 0o600) -> None:
    tmp = path + ".tmp"
    with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600), "wb") as f:
        f.write(data)
        os.fchmod(f.fileno(), mode)
    os.replace(tmp, path)


def _workdir() -> None:
    try:
        os.mkdir(WORK, 0o700)
    except FileExistsError:
        pass
    st = os.lstat(WORK)
    if not os.path.isdir(WORK) or os.path.islink(WORK) or st.st_uid not in (0, os.geteuid()):
        raise rel.Bad(f"{WORK} is not root's own folder")
    os.chmod(WORK, 0o700)


def drop_staged() -> None:
    for p in STAGED.values():
        if os.path.lexists(p):
            os.unlink(p)


def verified(files: dict, key: str) -> dict:
    """The manifest's values when all three files pass: signature, strictly newer, the hash."""
    m = rel.verify(files[rel.MANIFEST], files[rel.SIGNATURE], key)
    cur = installed_version()
    if not rel.newer(m["version"], cur):
        raise rel.Bad("up to date" if m["version"] == cur else
                      f"the hub offers {m['version']}, older than {cur}: refused")
    rel.check_installer(files[rel.INSTALLER], m)
    return m


def check(st: dict, key: str) -> None:
    """Ask the hub; a verified newer release waits in WORK afterwards. The time of the check is
    saved before the hub is asked, so a run that is killed while the hub stalls it still counts
    (red team round 6, finding 3: the next run would ask again within the hour)."""
    st["checked"] = int(time.time())
    save_state(st)
    hub = hub_values()
    man = fetch(hub, rel.MANIFEST, rel.MAX_MANIFEST)
    if man is None:
        st["result"] = "the hub offers no signed release"
        return
    sig = fetch(hub, rel.SIGNATURE, rel.MAX_SIGNATURE) or b""
    m = rel.verify(man, sig, key)   # before anything else of the release is fetched or read
    cur = installed_version()
    if not rel.newer(m["version"], cur):
        st["result"] = "up to date" if m["version"] == cur else f"refused: the hub offers {m['version']}, older than {cur}"
        return
    if m["version"] in st.get("failed", []):
        st["result"] = f"{m['version']} failed its self-check here before; waiting for a newer release"
        return
    data = fetch(hub, rel.INSTALLER, rel.MAX_INSTALLER, timeout=300, deadline=INSTALLER_S) or b""
    verified({rel.MANIFEST: man, rel.SIGNATURE: sig, rel.INSTALLER: data}, key)
    _workdir()
    for n, b in ((rel.INSTALLER, data), (rel.SIGNATURE, sig), (rel.MANIFEST, man)):
        _write(STAGED[n], b)
    st["waiting"] = m["version"]
    st["result"] = f"{m['version']} downloaded and verified"


CGROUP = "" if MAC else "/sys/fs/cgroup"   # Linux only: on macOS chats come from kap_mac.chats()
PROC = "" if MAC else "/proc"


def _units(*patterns) -> list:
    r = subprocess.run(["systemctl", "list-units", "--type=service", "--state=active,activating,deactivating",
                        "--plain", "--no-legend", "--no-pager", *patterns],
                       capture_output=True, text=True, timeout=30, env=ENV)
    return [ln.split()[0] for ln in r.stdout.splitlines() if ln.strip()]


def _is_helper_chat(pid: str, main: str) -> bool:
    """A helper chat: a session leader of its own (kap_helper starts each with a new session)
    that is the box's claude, by its program or by its command line."""
    try:
        with open(f"{PROC}/{pid}/stat") as f:
            stat = f.read(4096)
        sid = stat.rsplit(")", 1)[1].split()[3]
        if sid != pid or pid == main:
            return False
        exe = os.readlink(f"{PROC}/{pid}/exe")
        with open(f"{PROC}/{pid}/cmdline", "rb") as f:
            args = f.read(1 << 16).split(b"\0")
    except (OSError, IndexError):
        return False
    return exe.startswith(f"{kc.TOOLS_DIR}/claude/") or b"--strict-mcp-config" in args


def helper_chats(box: str = "*") -> int:
    """The helper chats (`claude -p`, started by kap_helper inside kapbox-<box>-helper.service when
    the hub hands on a `work` request) that run now (red team round 6, finding 5): they are not
    chat units, so chats_running would miss them."""
    n = 0
    for unit in _units(f"kapbox-{box}-helper.service"):
        r = subprocess.run(["systemctl", "show", "-p", "ControlGroup", "-p", "MainPID", unit],
                           capture_output=True, text=True, timeout=30, env=ENV)
        props = dict(ln.split("=", 1) for ln in r.stdout.splitlines() if "=" in ln)
        cg = props.get("ControlGroup", "")
        if not cg.startswith("/") or ".." in cg:
            continue
        for top, _, files in os.walk(CGROUP + cg):
            if "cgroup.procs" in files:
                try:
                    with open(os.path.join(top, "cgroup.procs")) as f:
                        pids = f.read(1 << 20).split()
                except OSError:
                    continue
                n += sum(1 for p in pids if p.isdigit() and _is_helper_chat(p, props.get("MainPID", "")))
    return n


def chats_running() -> int:
    """Team chats, clones and helper chats running now: an update never installs under one."""
    if MAC:   # enter's chat records (kap_mac.CHATS), each still alive
        if __package__:
            from . import kap_mac
        else:
            import kap_mac
        return len(kap_mac.chats())
    return len(_units("kapbox-*-chat-*", "kapbox-*-clone-*")) + helper_chats()


def lock_shared():
    """The update lock, shared, for something that must not run while an update installs (a
    team chat's whole life, a hubchat command). Its descriptor; None when the lock file cannot
    be made (the updater still sees a chat by its unit); BlockingIOError while an update installs."""
    try:
        os.makedirs(os.path.dirname(LOCK), 0o755, exist_ok=True)
        fd = os.open(LOCK, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    except OSError:
        return None
    try:
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        raise BlockingIOError("an update is installing")
    return fd


def lock_exclusive(path: str = ""):
    """The update lock, or None while a team chat holds it (enter keeps it shared per chat)."""
    path = path or LOCK
    os.makedirs(os.path.dirname(path), mode=0o755, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    return fd


def repair_with(pyz: str) -> subprocess.CompletedProcess:
    try:
        return subprocess.run([python(), "-I", pyz, "repair"], capture_output=True, text=True,
                              timeout=REPAIR_S, env=ENV, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired as e:
        return subprocess.CompletedProcess(e.cmd, 124, "", "the repair took too long")


def python() -> str:
    """The interpreter root's programs run with: on macOS the one the installer recorded."""
    if MAC:
        p = _read(kc.ETC + "/python", 4096).strip()
        if p.startswith("/") and os.path.isfile(p):
            return p
    return "/usr/bin/python3"


def install(st: dict, key: str) -> None:
    """The waiting update, when no team chat runs; the kept installer's repair when it fails."""
    files = {}
    for n, p in STAGED.items():
        with open(p, "rb") as f:
            files[n] = f.read(rel.MAX_INSTALLER + 1)
    try:
        m = verified(files, key)   # again: nothing is run that was not checked just now
    except rel.Bad as e:
        drop_staged()
        st.pop("waiting", None)
        st["result"] = f"the waiting update was dropped: {e}"
        return
    lock = lock_exclusive()
    if lock is None or chats_running():
        st["result"] = f"{m['version']} is waiting until no team chat runs"
        if lock is not None:
            os.close(lock)
        return
    try:
        old = installed_version()
        with open(kc.INSTALLER, "rb") as f:
            _write(PREVIOUS, f.read(), 0o700)
        r = repair_with(STAGED[rel.INSTALLER])
        if r.returncode == 0 and installed_version() == m["version"]:
            st["result"] = f"installed {m['version']} (was {old}); self-check passed"
            st["installed"] = int(time.time())
        else:
            why = " ".join((r.stdout + r.stderr).strip().splitlines()[-3:])[-300:]
            back = repair_with(PREVIOUS)
            st.setdefault("failed", []).append(m["version"])
            st["failed"] = st["failed"][-20:]
            st["result"] = (f"{m['version']} failed ({why}); rolled back to {installed_version() or old}"
                            + ("" if back.returncode == 0 else ": the rollback's self-check failed too, run  "
                               f"sudo {kc.INSTALLER} repair"))
        drop_staged()
        st.pop("waiting", None)
    finally:
        os.close(lock)


def run(now: bool = False) -> int:
    if kc.unfinished_install():
        _say("kap_update: the install did not finish; nothing is updated until  repair  finished it")
        return 0
    if _read(kc.MODE_FILE).strip() != "personal":
        return 0
    me = lock_exclusive(RUN_LOCK)
    if me is None:
        _say("kap_update: another update run is busy")
        return 0
    st = load_state()
    key = pinned_key()
    try:
        if not key:
            st["result"] = "no release key on this computer: no automatic updates"
        else:
            if not all(os.path.exists(p) for p in STAGED.values()):
                st.pop("waiting", None)
                if now or time.time() - float(st.get("checked") or 0) >= CHECK_S:
                    try:
                        check(st, key)
                    except Unreachable as e:
                        st["result"] = str(e)
                    except rel.Bad as e:
                        st["result"] = f"refused: {e}"
            if all(os.path.exists(p) for p in STAGED.values()):
                install(st, key)
    finally:
        save_state(st)
        os.close(me)
    _say(f"kap_update: {st.get('result', '')}")
    return 0


def main(argv) -> int:
    if os.geteuid() != 0:
        print("kap_update: root only", file=sys.stderr)
        return 2
    if argv in (["run"], ["run", "--now"]):   # --now: ask the hub whenever the last check was
        return run(now=argv[1:] == ["--now"])
    if argv == ["status"]:
        _say(*status_lines())
        return 0
    print("usage: kap_update.py run [--now] | status", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
