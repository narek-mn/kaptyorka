"""kaproot: what the root programs of a personal laptop do to the system (secure design v2,
sections 4.1, 4.2, 4.6, 4.9, 4.10, 9.6): make and remove a box, hand a folder tree to the box
and back without following links, make a returned folder safe for git, the outside half of the
self-check, the person's view mounts, and the box units.

Python standard library only; installed as /usr/local/libexec/kaptyorka/kaproot.py. Every name
it gets has passed kapcommon's patterns. It never follows a symlink inside a team folder and
never runs a program on box-written data (git included).
"""
import ctypes
import errno
import grp
import ipaddress
import json
import os
import pwd
import secrets
import shutil
import signal
import socket
import stat
import struct
import subprocess
import sys
from typing import Dict, List, Optional, Tuple

if __package__:
    from . import kapcommon as kc
else:
    import kapcommon as kc

Refuse = kc.Refuse
SYSTEMD = "/etc/systemd/system"
CLEAN_ENV = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8"}


# ---------------------------------------------------------------------------- small tools

def run(cmd: List[str], check: bool = True, timeout: float = 60, **kw) -> subprocess.CompletedProcess:
    kw.setdefault("env", CLEAN_ENV)
    kw.setdefault("stdin", subprocess.DEVNULL)
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, **kw)
    if check and r.returncode != 0:
        raise Refuse(f"{cmd[0]} {' '.join(cmd[1:3])}: {(r.stderr or r.stdout).strip()[:300]}")
    return r


def write_file(path: str, data, mode: int = 0o644, uid: int = 0, gid: int = 0) -> None:
    """Atomic write: a new file next to it, then rename. Never follows a link at path."""
    d, base = os.path.split(path)
    tmp = os.path.join(d, f".{base}.{secrets.token_hex(4)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        os.write(fd, data.encode() if isinstance(data, str) else data)
        if os.geteuid() == 0:
            os.fchown(fd, uid, gid)
        os.fchmod(fd, mode)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)


def read_text(path: str, limit: int = 1 << 20) -> str:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return ""
    with os.fdopen(fd, "rb") as f:
        return f.read(limit).decode("utf-8", "replace")


def mkdir(path: str, mode: int, uid: int = 0, gid: int = 0) -> None:
    """A folder with exactly this owner and mode. An existing symlink or file there is refused."""
    try:
        os.mkdir(path, 0o700)
    except FileExistsError:
        pass
    st = os.lstat(path)
    if not stat.S_ISDIR(st.st_mode):
        raise Refuse(f"{path} exists and is not a folder")
    os.chown(path, uid, gid, follow_symlinks=False)
    os.chmod(path, mode)


def lock_dirs(parent: str, names) -> None:
    """Root-owned empty folders (0755) in a folder the box can write: the mount points of the
    box's read-only folders there. Made through descriptors, never following a link the box
    put on the way; an entry the box made that is not a folder is refused."""
    pfd = open_dir(parent)
    try:
        for n in names:
            try:
                os.mkdir(n, 0o755, dir_fd=pfd)
            except FileExistsError:
                pass
            try:
                fd = os.open(n, O_DIR, dir_fd=pfd)
            except OSError as e:
                raise Refuse(f"{parent}/{n}: {os.strerror(e.errno)} (not a folder, or a link)")
            try:
                os.fchown(fd, 0, 0)
                os.fchmod(fd, 0o755)
            finally:
                os.close(fd)
    finally:
        os.close(pfd)


def uid_of(user: str) -> int:
    return pwd.getpwnam(user).pw_uid


def gid_of(group: str) -> int:
    return grp.getgrnam(group).gr_gid


def systemctl(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return run(["systemctl", *args], check=check)


def is_active(unit: str) -> bool:
    return systemctl("is-active", "--quiet", unit, check=False).returncode == 0


# ---------------------------------------------------------------------------- who is calling

def caller_person() -> str:
    """The person who called through sudo: sudo's SUDO_UID (sudo refuses a spoofed SUDO_USER and
    -E: MEASURED, 07 section 3). The name must be a known person of this laptop."""
    if os.geteuid() != 0:
        raise Refuse("run through the launcher or hubchat (sudo)")
    uid = os.environ.get("SUDO_UID", "")
    if not uid.isdigit():
        raise Refuse("run through the launcher or hubchat (sudo)")
    try:
        name = pwd.getpwuid(int(uid)).pw_name
    except KeyError:
        raise Refuse("unknown caller")
    kc.check_person(name)
    if os.environ.get("SUDO_USER") != name or not os.path.isdir(f"{kc.PEOPLE}/{name}"):
        raise Refuse("this account has no team box on this computer")
    return name


def person_home(person: str) -> pwd.struct_passwd:
    pw = pwd.getpwnam(kc.check_person(person))
    if not pw.pw_dir.startswith("/home/") or os.path.realpath(pw.pw_dir) != pw.pw_dir:
        raise Refuse(f"{person}'s home is not really under /home")
    return pw


def load_folders(person: str) -> Dict[str, dict]:
    return kc.parse_folders(read_text(f"{kc.PEOPLE}/{kc.check_person(person)}/folders.json"))


def save_folders(person: str, folders: Dict[str, dict]) -> None:
    write_file(f"{kc.PEOPLE}/{kc.check_person(person)}/folders.json", kc.dump_folders(folders), 0o644)


def runtime_max() -> str:
    return kc.parse_limits(read_text(kc.LIMITS_FILE))


def props_args(person: str, box: str) -> List[str]:
    out: List[str] = []
    for line in kc.box_props(person, box, runtime_max()):
        out += ["-p", line]
    return out


def host_addrs() -> List[str]:
    """This laptop's own addresses (not loopback), for wallcheck's W5."""
    seen, prev = [], None
    try:
        for line in open("/proc/net/fib_trie"):
            t = line.strip()
            if t.startswith("|--"):
                prev = t[3:].strip()
            elif "/32 host LOCAL" in t and prev and not prev.startswith("127.") and prev not in seen:
                seen.append(prev)
    except OSError:
        pass
    try:
        for line in open("/proc/net/if_inet6"):
            h = line.split()[0]
            a = ":".join(h[i:i + 4] for i in range(0, 32, 4))
            ip = ipaddress.ip_address(a)
            if not ip.is_loopback and not ip.is_link_local and str(ip) not in seen:
                seen.append(str(ip))
    except OSError:
        pass
    return seen[:16]


# ---------------------------------------------------------------------------- folder trees without links

O_DIR = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


def open_dir(path: str) -> int:
    """An fd of an absolute path, opened one component at a time without following any symlink,
    so a link swapped in anywhere on the way is refused instead of followed."""
    if not path.startswith("/"):
        raise Refuse("not an absolute path")
    fd = os.open("/", O_DIR)
    try:
        for part in [p for p in path.split("/") if p]:
            if part in (".", ".."):
                raise Refuse("path with . or ..")
            nfd = os.open(part, O_DIR, dir_fd=fd)
            os.close(fd)
            fd = nfd
    except OSError as e:
        os.close(fd)
        raise Refuse(f"{path}: {os.strerror(e.errno)} (a symlink on the way is not followed)")
    except Refuse:
        os.close(fd)
        raise
    return fd


# posix ACL as the kernel stores it (system.posix_acl_default): version 2, then (tag, perm, id)
ACL_USER_OBJ, ACL_GROUP_OBJ, ACL_GROUP, ACL_MASK, ACL_OTHER = 0x01, 0x04, 0x08, 0x10, 0x20
NO_ID = 0xFFFFFFFF


def default_acl(gid: int) -> bytes:
    """Default ACL g:<share group>:rwX, so files made later by the person or the box stay
    editable by both."""
    ents = [(ACL_USER_OBJ, 7, NO_ID), (ACL_GROUP_OBJ, 7, NO_ID), (ACL_GROUP, 7, gid),
            (ACL_MASK, 7, NO_ID), (ACL_OTHER, 0, NO_ID)]
    return struct.pack("<I", 2) + b"".join(struct.pack("<HHI", *e) for e in ents)


def set_default_acl(fd: int, gid: int) -> None:
    try:
        os.setxattr(fd, "system.posix_acl_default", default_acl(gid))
    except OSError as e:
        if e.errno not in (errno.EOPNOTSUPP, errno.ENOTSUP):
            raise


def drop_acls(fd: int) -> None:
    for a in ("system.posix_acl_default", "system.posix_acl_access"):
        try:
            os.removexattr(fd, a)
        except OSError as e:
            if e.errno not in (errno.ENODATA, errno.EOPNOTSUPP, errno.ENOTSUP, errno.EINVAL):
                raise


def _copy_out(dfd: int, name: str, fd: int) -> int:
    """Replace a file that has more than one hard link by a private copy, so changing its owner
    does not change the person's other copy (4.9 step 4). Returns the new file's fd."""
    tmp = f".kapcopy-{secrets.token_hex(6)}"
    nfd = os.open(tmp, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=dfd)
    try:
        os.lseek(fd, 0, 0)
        while True:
            b = os.read(fd, 1 << 20)
            if not b:
                break
            os.write(nfd, b)
        os.rename(tmp, name, src_dir_fd=dfd, dst_dir_fd=dfd)
    except BaseException:
        os.close(nfd)
        try:
            os.unlink(tmp, dir_fd=dfd)
        except OSError:
            pass
        raise
    return nfd


class TreeReport:
    def __init__(self):
        self.copied: List[str] = []      # hard-linked files replaced by a copy
        self.removed: List[str] = []     # device files, pipes, sockets
        self.stripped: List[str] = []    # set-user-id / set-group-id bits removed
        self.count = 0


def take_tree(dfd: int, uid: int, gid: int, team: bool, rep: Optional[TreeReport] = None,
              rel: str = "", depth: int = 0) -> TreeReport:
    """Give every entry below dfd to uid:gid, never following a link. team=True: group gets
    the owner's rights, others none, folders setgid with the share group's default ACL.
    team=False (back to the person): set-id bits and ACLs removed, modes otherwise kept."""
    rep = rep or TreeReport()
    if depth > 256:
        raise Refuse("folder tree too deep")
    for name in os.listdir(dfd):
        path = f"{rel}{name}"
        st = os.stat(name, dir_fd=dfd, follow_symlinks=False)
        rep.count += 1
        if stat.S_ISLNK(st.st_mode):
            os.chown(name, uid, gid, dir_fd=dfd, follow_symlinks=False)
            continue
        if stat.S_ISDIR(st.st_mode):
            fd = os.open(name, O_DIR, dir_fd=dfd)
            try:
                if os.fstat(fd).st_ino != st.st_ino:
                    raise Refuse(f"{path} changed while being moved")
                os.fchown(fd, uid, gid)
                m = st.st_mode & 0o7777
                if m & 0o4000:
                    rep.stripped.append(path)
                if team:
                    os.fchmod(fd, 0o2000 | (m & 0o700) | ((m & 0o700) >> 3))
                    set_default_acl(fd, gid)
                else:
                    drop_acls(fd)
                    os.fchmod(fd, m & 0o777)
                take_tree(fd, uid, gid, team, rep, path + "/", depth + 1)
            finally:
                os.close(fd)
            continue
        if not stat.S_ISREG(st.st_mode):  # device, pipe, socket: nothing a team folder needs
            os.unlink(name, dir_fd=dfd)
            rep.removed.append(path)
            continue
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=dfd)
        try:
            fst = os.fstat(fd)
            if not stat.S_ISREG(fst.st_mode):
                raise Refuse(f"{path} changed while being moved")
            if fst.st_nlink > 1:
                nfd = _copy_out(dfd, name, fd)
                os.close(fd)
                fd = nfd
                rep.copied.append(path)
            os.fchown(fd, uid, gid)
            m = fst.st_mode & 0o7777
            if m & 0o6000:
                rep.stripped.append(path)
            m &= 0o777
            if team:
                m = (m & 0o700) | ((m & 0o700) >> 3)
            else:
                drop_acls(fd)
            os.fchmod(fd, m)
        finally:
            os.close(fd)
    return rep


def lstat_rel(top_fd: int, rel: str) -> Optional[os.stat_result]:
    """lstat of a path below top_fd, with every folder on the way opened without following."""
    parts = rel.split("/")
    fd = top_fd
    opened = []
    try:
        for p in parts[:-1]:
            fd = os.open(p, O_DIR, dir_fd=fd)
            opened.append(fd)
        return os.stat(parts[-1], dir_fd=fd, follow_symlinks=False)
    except OSError:
        return None
    finally:
        for f in opened:
            os.close(f)


def trap_files(top_fd: int, since: float = 0.0, build: bool = True, git: bool = True) -> List[str]:
    """Files that can run code when a program of the person's opens the folder (7.2), at any
    depth, changed after `since` (their ctime: no user can set it back, and it moves whoever
    owns the file, so a box write into a folder the person made counts too; red team round 3,
    finding 1). Everything inside a tool's folder (.opencode, .claude, .codex, .cursor, .vscode,
    ...), the tool files (opencode.json, .mcp.json, .envrc, ...), with git: every git hook and
    repository config, with build: Makefiles, package.json and version files. Walked without
    following a link, at most TRAP_DEPTH deep and TRAP_BUDGET entries; at most TRAP_MAX names,
    sorted, the last one '...' when there were more."""
    out: List[str] = []
    seen = 0
    stack = [(os.dup(top_fd), "", False, 0)]
    try:
        while stack:
            fd, rel, tool, depth = stack.pop()
            try:
                names = sorted(os.listdir(fd))
            except OSError:
                os.close(fd)
                continue
            try:
                for name in names:
                    seen += 1
                    if seen > kc.TRAP_BUDGET:
                        break
                    try:
                        st = os.stat(name, dir_fd=fd, follow_symlinks=False)
                    except OSError:
                        continue
                    path = rel + name
                    in_git = rel == ".git/" or rel.endswith("/.git/")
                    if stat.S_ISDIR(st.st_mode):
                        if depth >= kc.TRAP_DEPTH or (in_git and name == "objects") or \
                                (name == "node_modules" and not tool):
                            continue
                        child_tool = tool or name in kc.TOOL_DIRS or (in_git and name == "hooks" and git)
                        try:
                            stack.append((os.open(name, O_DIR, dir_fd=fd), path + "/", child_tool, depth + 1))
                        except OSError:
                            pass
                        continue
                    if not stat.S_ISREG(st.st_mode) or max(st.st_ctime, st.st_mtime) <= since:
                        continue
                    hit = (tool and not name.endswith(".sample")) or name in kc.TOOL_FILES \
                        or (git and in_git and name in ("config", "config.worktree")) \
                        or (build and name in kc.BUILD_FILES)
                    if hit:
                        out.append(path)
            finally:
                os.close(fd)
    finally:
        for fd, *_ in stack:
            os.close(fd)
    out.sort()
    return out[:kc.TRAP_MAX] + (["..."] if len(out) > kc.TRAP_MAX or seen > kc.TRAP_BUDGET else [])


def trap_notice(traps: List[str], most: int = 12) -> str:
    """The trap files as one line for the person's terminal (every name through kc.shown)."""
    more = [t for t in traps if t != "..."]
    shown = ", ".join(kc.shown(t, 200) for t in more[:most])
    left = len(more) - most + (1 if traps and traps[-1] == "..." else 0)
    return shown + (f" and {left} more{'+' if traps[-1:] == ['...'] else ''}" if left > 0 else "")


def since_stamp(box: str, name: str) -> str:
    """Root's mark of when a folder became a team folder: trap files changed after it count."""
    return f"{kc.box_root(box)}/.since-{kc.check_name(name)}"


def read_since(box: str, name: str) -> float:
    try:
        return os.lstat(since_stamp(box, name)).st_mtime
    except OSError:
        return 0.0


# ---------------------------------------------------------------------------- the person's view: tool files covered

MS_RDONLY, MS_NOSUID, MS_NODEV, MS_NOEXEC, MS_REMOUNT, MS_BIND, MS_PRIVATE = 1, 2, 4, 8, 32, 4096, 1 << 18
MNT_DETACH = 2


def _libc():
    libc = ctypes.CDLL(None, use_errno=True)
    libc.mount.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p, ctypes.c_ulong, ctypes.c_char_p]
    libc.umount2.argtypes = [ctypes.c_char_p, ctypes.c_int]
    return libc


def _mount(src, target: str, fstype, flags: int, data=None) -> None:
    enc = (lambda s: s.encode() if isinstance(s, str) else s)
    if _libc().mount(enc(src), enc(target), enc(fstype), flags, enc(data)) != 0:
        e = ctypes.get_errno()
        raise OSError(e, f"mount {target}: {os.strerror(e)}")


def _umount(target: str) -> None:
    if _libc().umount2(target.encode(), MNT_DETACH) != 0:
        e = ctypes.get_errno()
        raise OSError(e, f"umount {target}: {os.strerror(e)}")


def _unoctal(s: str) -> str:
    """mountinfo writes space, tab, newline and backslash as \\ooo."""
    out, i = [], 0
    while i < len(s):
        if s[i] == "\\" and i + 4 <= len(s) and s[i + 1:i + 4].isdigit():
            out.append(chr(int(s[i + 1:i + 4], 8)))
            i += 4
        else:
            out.append(s[i])
            i += 1
    return "".join(out)


def mounts(text: Optional[str] = None) -> List[dict]:
    """This process's mounts: id, parent, mount point, its propagation tags, file system type."""
    if text is None:
        with open("/proc/self/mountinfo") as f:
            text = f.read()
    out = []
    for line in text.splitlines():
        f = line.split()
        if len(f) < 7 or "-" not in f[6:]:
            continue
        dash = f.index("-", 6)
        out.append({"id": int(f[0]), "parent": int(f[1]), "point": _unoctal(f[4]),
                    "tags": f[6:dash], "fstype": f[dash + 1] if dash + 1 < len(f) else "",
                    "source": _unoctal(f[dash + 2]) if dash + 2 < len(f) else ""})
    return out


def hides_wanted(top_fd: int) -> Dict[str, str]:
    """What must be covered in the view of this folder: relative path -> "dir" or "file". Only
    real folders and plain files (a link is not followed in the view: nosymfollow)."""
    want: Dict[str, str] = {}
    for n in kc.TOOL_DIRS + kc.TOOL_FILES + (kc.HIDE_GIT,):
        st = lstat_rel(top_fd, n)
        if st is None:
            continue
        if stat.S_ISDIR(st.st_mode) and n not in kc.TOOL_FILES:
            want[n] = "dir"
        elif stat.S_ISREG(st.st_mode) and n in kc.TOOL_FILES:
            want[n] = "file"
    return want


def hide_view(person: str, name: str, box: str, mi: Optional[List[dict]] = None) -> List[str]:
    """Cover the tool folders and files at the top of the person's view of one team folder (and
    its .git/hooks), so her own tools started there load nothing the box wrote; uncover what
    is no longer there. The view is made private first: a mount below it then stays in the
    view and never shows in the box's own copy of the folder (measured: on a shared view, a
    mount at the view's .opencode also appeared over the box's). Returns what it changed."""
    where = kc.view_path(person, name)
    mi = mounts() if mi is None else mi
    view = [m for m in mi if m["point"] == where]
    if not view:
        return []
    vm = view[-1]
    done: List[str] = []
    if any(t.startswith("shared:") for t in vm["tags"]):
        _mount(None, where, None, MS_PRIVATE)
        done.append("private")
    fd = open_dir(f"{kc.box_projects(box)}/{name}")
    try:
        want = hides_wanted(fd)
    finally:
        os.close(fd)
    have = {m["point"][len(where) + 1:]: m for m in mi if m["parent"] == vm["id"]
            and m["point"].startswith(where + "/")}
    for rel, m in sorted(have.items()):
        kind = "dir" if m["fstype"] == "tmpfs" else "file"
        if want.get(rel) != kind:
            try:
                _umount(f"{where}/{rel}")
                done.append(f"-{rel}")
            except OSError:
                pass
    pw = person_home(person)
    for rel, kind in sorted(want.items()):
        if rel in have and ("dir" if have[rel]["fstype"] == "tmpfs" else "file") == kind:
            continue
        # the place opened without following a link, then mounted on through /proc/self/fd:
        # a link the box swaps in between is never followed
        try:
            vfd = open_dir(where + "/" + os.path.dirname(rel)) if "/" in rel else open_dir(where)
        except Refuse:
            continue
        try:
            tfd = os.open(os.path.basename(rel), os.O_PATH | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=vfd)
        except OSError:
            os.close(vfd)
            continue
        try:
            st = os.fstat(tfd)
            target = f"/proc/self/fd/{tfd}"
            if kind == "dir" and stat.S_ISDIR(st.st_mode):
                _mount(kc.HIDE_TMPFS, target, "tmpfs", MS_NOSUID | MS_NODEV | MS_NOEXEC,
                       f"mode=0700,uid={pw.pw_uid},gid={pw.pw_gid},size=128m,nr_inodes=40000")
            elif kind == "file" and stat.S_ISREG(st.st_mode):
                src = kc.HIDE_JSON if rel.endswith((".json", ".jsonc")) else kc.HIDE_FILE
                _mount(src, target, None, MS_BIND)
            else:
                continue
            done.append(f"+{rel}")
        except OSError:
            pass
        finally:
            os.close(tfd)
            os.close(vfd)
    return done


def hidden_problem(person: str, name: str, box: str, mi: List[dict]) -> Optional[str]:
    """The view is shared, or a tool folder or file at its top is not covered."""
    where = kc.view_path(person, name)
    view = [m for m in mi if m["point"] == where]
    if not view:
        return None
    if any(t.startswith("shared:") for t in view[-1]["tags"]):
        return f"the view {where} is shared (its covers would show in the box)"
    try:
        fd = open_dir(f"{kc.box_projects(box)}/{name}")
    except Refuse:
        return None
    try:
        want = hides_wanted(fd)
    finally:
        os.close(fd)
    have = {m["point"][len(where) + 1:] for m in mi if m["parent"] == view[-1]["id"]}
    missing = sorted(set(want) - have)
    if missing:
        return f"in the view {where}, {', '.join(kc.shown(m) for m in missing[:6])} not covered"
    return None


# ---------------------------------------------------------------------------- the box's sign-ins, for the relay

def _read_box_file(home_fd: int, rel: str, limit: int = 256 * 1024) -> str:
    """A file below the box home, every folder on the way and the file opened without following
    a link; '' if it is not a plain file of at most `limit` bytes."""
    parts = rel.split("/")
    fd, opened = home_fd, []
    try:
        for p in parts[:-1]:
            fd = os.open(p, O_DIR, dir_fd=fd)
            opened.append(fd)
        ffd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=fd)
    except OSError:
        return ""
    finally:
        for f in opened:
            os.close(f)
    try:
        st = os.fstat(ffd)
        if not stat.S_ISREG(st.st_mode) or st.st_size > limit:
            return ""
        return os.read(ffd, limit).decode("utf-8", "replace")
    finally:
        os.close(ffd)


def box_secret_values(person: str, box: str) -> List[str]:
    """The current secret values of one box: its tools' sign-in files, its SSH key, the Claude
    token root hands it."""
    out: List[str] = []
    try:
        hfd = open_dir(kc.box_home(box))
    except Refuse:
        hfd = None
    if hfd is not None:
        try:
            for rel in kc.SIGNIN_FILES:
                out += kc.signin_values(_read_box_file(hfd, rel))
        finally:
            os.close(hfd)
    tok = read_text(f"{kc.SECRET}/{kc.check_person(person)}/claude-oauth-token").strip()
    if tok:
        out.append(tok)
    return list(dict.fromkeys(out))


def write_box_secrets(person: str, box: str) -> int:
    """/run/kaptyorka/box-secrets/<box> for the relay: the box's current secret values first,
    then the ones it had before (a copy a chat took before a refresh is still that account's),
    at most SECRETS_KEEP. root:kaprelay-secrets 0640 in a 0750 folder; only the relay is in that
    group. Returns how many values it holds."""
    try:
        gid = gid_of(kc.RELAY_GROUP)
    except KeyError:
        gid = 0
    os.makedirs(os.path.dirname(kc.BOX_SECRETS), 0o755, exist_ok=True)
    mkdir(kc.BOX_SECRETS, 0o750, 0, gid)
    path = f"{kc.BOX_SECRETS}/{kc.check_box(box)}"
    try:
        old = json.loads(read_text(path) or "{}").get("values", [])
    except (ValueError, AttributeError):
        old = []
    new = box_secret_values(person, box)
    values = list(dict.fromkeys(new + [v for v in old if isinstance(v, str)]))[:kc.SECRETS_KEEP]
    data = json.dumps({"values": values}) + "\n"
    if read_text(path) != data:
        write_file(path, data, 0o640 if gid else 0o600, 0, gid)
    return len(values)


def open_rel_dir(top_fd: int, rel: str) -> int:
    fd = os.dup(top_fd)
    for p in rel.split("/"):
        nfd = os.open(p, O_DIR, dir_fd=fd)
        os.close(fd)
        fd = nfd
    return fd


# ---------------------------------------------------------------------------- git made safe (4.9 remove)

def _is_gitdir(dfd: int) -> bool:
    try:
        h = os.stat("HEAD", dir_fd=dfd, follow_symlinks=False)
        c = os.stat("config", dir_fd=dfd, follow_symlinks=False)
        o = os.stat("objects", dir_fd=dfd, follow_symlinks=False)
    except OSError:
        return False
    return stat.S_ISREG(h.st_mode) and stat.S_ISREG(c.st_mode) and stat.S_ISDIR(o.st_mode)


def _free_name(dfd: int, base: str) -> str:
    for n in [base] + [f"{base}.{i}" for i in range(2, 100)]:
        try:
            os.stat(n, dir_fd=dfd, follow_symlinks=False)
        except FileNotFoundError:
            return n
    raise Refuse("too many old copies")


def _sanitize_gitdir(gfd: int, rel: str, notes: List[str]) -> None:
    try:
        st = os.stat("config", dir_fd=gfd, follow_symlinks=False)
    except FileNotFoundError:
        st = None
    if st is not None:
        if stat.S_ISREG(st.st_mode) and st.st_size <= 1 << 20:
            fd = os.open("config", os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=gfd)
            with os.fdopen(fd, "rb") as f:
                text = f.read(1 << 20).decode("utf-8", "replace")
            new, dropped = kc.safe_git_config(text)
            tmp = f".config.{secrets.token_hex(4)}"
            nfd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644, dir_fd=gfd)
            os.write(nfd, new.encode())
            os.close(nfd)
            os.rename(tmp, "config", src_dir_fd=gfd, dst_dir_fd=gfd)
            if dropped:
                notes.append(f"{rel}config: kept only plain keys; dropped {', '.join(sorted(set(dropped))[:12])}")
        else:
            os.unlink("config", dir_fd=gfd)
            notes.append(f"{rel}config: not a plain file, removed")
    for name, new in (("hooks", "hooks.from-team"), ("config.worktree", "config.worktree.from-team")):
        try:
            os.stat(name, dir_fd=gfd, follow_symlinks=False)
        except FileNotFoundError:
            continue
        target = _free_name(gfd, new)
        os.rename(name, target, src_dir_fd=gfd, dst_dir_fd=gfd)
        notes.append(f"{rel}{name} moved to {rel}{target}")


def make_safe_for_git(dfd: int, rel: str = "", notes: Optional[List[str]] = None, depth: int = 0) -> List[str]:
    """Every git repository in the tree (.git folders, bare repositories, submodule folders)
    gets a config with only plain keys, and its hooks moved aside. A .git *file* (a pointer to
    another repository) is moved aside. Without this, git trusts the folder the moment the
    person owns it again (4.9)."""
    notes = [] if notes is None else notes
    if depth > 256:
        return notes
    if _is_gitdir(dfd):
        _sanitize_gitdir(dfd, rel, notes)
    for name in sorted(os.listdir(dfd)):
        st = os.stat(name, dir_fd=dfd, follow_symlinks=False)
        if name == ".git" and not stat.S_ISDIR(st.st_mode):
            target = _free_name(dfd, ".git.from-team")
            os.rename(name, target, src_dir_fd=dfd, dst_dir_fd=dfd)
            notes.append(f"{rel}.git (a link to another repository) moved to {rel}{target}")
            continue
        if stat.S_ISDIR(st.st_mode) and name != "objects":
            fd = os.open(name, O_DIR, dir_fd=dfd)
            try:
                make_safe_for_git(fd, f"{rel}{name}/", notes, depth + 1)
            finally:
                os.close(fd)
    return notes


# ---------------------------------------------------------------------------- the outside half of the self-check (4.6)

def canary_ok(path: str) -> bool:
    """path is root's canary: a plain root-owned file (never a link) holding the mark."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError:
        return False
    try:
        st = os.fstat(fd)
        return st.st_uid == 0 and stat.S_ISREG(st.st_mode) and kc.CANARY_MARK in os.read(fd, 64)
    finally:
        os.close(fd)


def _make_canary(path: str) -> None:
    """A new canary at path; O_EXCL with O_NOFOLLOW, so a file or link already there is never used."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o644)
    try:
        os.write(fd, kc.CANARY_MARK + b"\n")
        os.fchmod(fd, 0o644)
    finally:
        os.close(fd)


def _canary_file_ok(path: str) -> Optional[str]:
    """A fixed canary, in a folder only root can write: there when it should be, made again when
    it was cleaned away."""
    d = os.path.dirname(path)
    try:
        st = os.lstat(d)
    except OSError as e:
        return f"canary folder {d}: {os.strerror(e.errno)}"
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != 0 or st.st_mode & 0o022:
        return f"canary folder {d} is not a root-owned folder only root can write"
    if canary_ok(path):
        return None
    try:
        if os.path.lexists(path):
            os.unlink(path)   # only root writes here, so this is a damaged one of ours
        _make_canary(path)
        return None
    except OSError as e:
        return f"canary file {path} cannot be made: {os.strerror(e.errno)}"


def shared_canaries(bad: Optional[List[str]] = None) -> List[str]:
    """The canaries in /tmp and /dev/shm, which every user can write. Each has a name nobody can
    guess, made with O_EXCL by root: a file or link someone made first is never taken for ours,
    and once made, the sticky bit stops others from removing or replacing it. The names live in
    CANARY_STATE (in /run/kaptyorka, which only root can write). A missing one (a reboot, a /tmp
    cleaner) is made anew."""
    try:
        old = [p for p in read_text(kc.CANARY_STATE).split() if kc.CANARY_SHARED_RX.fullmatch(p)]
    except OSError:
        old = []
    out = []
    for d in kc.CANARY_SHARED_DIRS:
        have = [p for p in old if os.path.dirname(p) == d and canary_ok(p)]
        if have:
            out.append(have[0])
            continue
        try:
            st = os.lstat(d)
        except OSError as e:
            if bad is not None:
                bad.append(f"canary folder {d}: {os.strerror(e.errno)}")
            continue
        if not stat.S_ISDIR(st.st_mode) or st.st_uid != 0 or not st.st_mode & stat.S_ISVTX:
            if bad is not None:
                bad.append(f"{d} is not a root-owned folder with the sticky bit")
            continue
        for _ in range(8):
            p = f"{d}/kaptyorka-wall-canary-{secrets.token_hex(8)}"
            try:
                _make_canary(p)
                out.append(p)
                break
            except FileExistsError:
                continue
            except OSError as e:
                if bad is not None:
                    bad.append(f"canary file in {d} cannot be made: {os.strerror(e.errno)}")
                break
        else:
            if bad is not None:
                bad.append(f"no free canary name in {d}")
    if sorted(out) != sorted(old):
        try:
            os.makedirs(os.path.dirname(kc.CANARY_STATE), 0o755, exist_ok=True)
            write_file(kc.CANARY_STATE, "".join(p + "\n" for p in out), 0o644)
        except OSError as e:
            if bad is not None:
                bad.append(f"{kc.CANARY_STATE} cannot be written: {os.strerror(e.errno)}")
    return out


def _recv(fam: int, addr, timeout: float = 1.0) -> bytes:
    s = socket.socket(fam, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(addr)
        return s.recv(64)
    except OSError:
        return b""
    finally:
        s.close()


def _tree_problem(top: str) -> Optional[str]:
    """top and everything below it root-owned and not writable by group or others; the folders
    above it too (else they could be swapped)."""
    p = top
    while True:
        try:
            st = os.lstat(p)
        except FileNotFoundError:
            return None if p == top else f"{p} is missing"
        if not stat.S_ISLNK(st.st_mode) and (st.st_uid != 0 or st.st_mode & 0o022):
            return f"{p} is not root-owned or is writable by others"
        if p == "/":
            break
        p = os.path.dirname(p)
    if not os.path.isdir(top) or os.path.islink(top):
        return None
    for dirpath, dirs, files in os.walk(top):
        for n in dirs + files:
            st = os.lstat(os.path.join(dirpath, n))
            if not stat.S_ISLNK(st.st_mode) and (st.st_uid != 0 or st.st_mode & 0o022):
                return f"{os.path.join(dirpath, n)} is not root-owned or is writable by others"
    return None


def mount_flags() -> Dict[str, set]:
    """mount point -> per-mount options, from this process's mountinfo (the host's view)."""
    out: Dict[str, set] = {}
    for line in open("/proc/self/mountinfo"):
        f = line.split()
        out[f[4].replace("\\040", " ")] = set(f[5].split(","))
    return out


def outside_check(person: str, boxes: List[str]) -> List[str]:
    """The outside half of the self-check, as root, before a box unit starts. Every canary must
    be alive (a dead canary would let the inside check pass for the wrong reason), the box
    accounts and the root files as installed, the namespace with only lo, every view mount with
    its four flags, the mode personal. Returns the problems; empty means all is well."""
    bad: List[str] = []
    for f in kc.CANARY_FILES:
        p = _canary_file_ok(f)
        if p:
            bad.append(p)
    shared_canaries(bad)
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.shmget(kc.CANARY_SHM_KEY, 0, 0) < 0:
        bad.append("canary SysV segment is not reachable (kaptyorka-canary.service)")
    if kc.CANARY_MARK not in _recv(socket.AF_UNIX, kc.CANARY_ABSTRACT):
        bad.append("abstract canary socket is not reachable (kaptyorka-canary.service)")
    if kc.CANARY_MARK not in _recv(socket.AF_INET, ("127.0.0.1", kc.CANARY_TCP_LO)):
        bad.append("loopback canary is not reachable (kaptyorka-canary.service)")
    found = False
    for d in os.listdir("/proc"):
        if d.isdigit():
            try:
                with open(f"/proc/{d}/cmdline", "rb") as f:
                    if kc.CANARY_PROC.encode() in f.read(4096):
                        found = True
                        break
            except OSError:
                continue
    if not found:
        bad.append("canary process is not running (kaptyorka-canary.service)")
    try:
        pw = person_home(person)
        m = os.stat(pw.pw_dir).st_mode & 0o777
        if m not in (0o700, 0o750):
            bad.append(f"{person}'s home has mode {m:o}; it must be 700 or 750 (chmod 700 {pw.pw_dir})")
    except (Refuse, KeyError, OSError) as e:
        bad.append(str(e))
    for box in boxes:
        u, g = kc.box_user(box), kc.share_group(box)
        try:
            bpw = pwd.getpwnam(u)
            have = set(os.getgrouplist(u, bpw.pw_gid))
            want = {gid_of(u), gid_of(g)}
            if have != want:
                names = sorted(grp.getgrgid(x).gr_name for x in have)
                bad.append(f"{u} is in groups {', '.join(names)}; only {u} and {g} are allowed")
            if grp.getgrnam(u).gr_mem:
                bad.append(f"group {u} has members {grp.getgrnam(u).gr_mem}; it must have none")
        except KeyError as e:
            bad.append(f"box account missing: {e}")
        r = run(["ip", "-n", f"kapbox-{box}", "-o", "link", "show"], check=False)
        links = [ln for ln in r.stdout.splitlines() if ln.strip()]
        if r.returncode != 0 or len(links) != 1 or ": lo:" not in links[0]:
            bad.append(f"network namespace kapbox-{box} does not hold exactly lo")
    for top in (kc.LIBEXEC, kc.ETC, kc.OPT, kc.TOOLS_DIR, f"/etc/sudoers.d/kaptyorka-{person}",
                "/etc/profile.d/kaptyorka.sh", "/usr/local/bin/hubchat"):
        p = _tree_problem(top)
        if p:
            bad.append(p)
    try:
        members = grp.getgrnam(kc.RELAY_GROUP).gr_mem
        if members:
            bad.append(f"group {kc.RELAY_GROUP} has members {members}; it must have none")
    except KeyError:
        bad.append(f"group {kc.RELAY_GROUP} is missing (run the installer again)")
    if not is_active("kaptyorka-guard.service"):
        bad.append("kaptyorka-guard.service is not running (it covers tool files in your views)")
    flags = mount_flags()
    try:
        folders = load_folders(person)
        names = sorted(folders) + [kc.MEMORY]
    except (Refuse, ValueError, KeyError) as e:
        folders, names = {}, [kc.MEMORY]
        bad.append(f"folders.json: {e}")
    mi = mounts()
    for name in sorted(folders):
        p = hidden_problem(person, name, folders[name]["box"], mi)
        if p:
            bad.append(p)
    for name in names:
        where = kc.view_path(person, name)
        have = flags.get(where)
        if have is None:
            bad.append(f"the view {where} is not mounted")
            continue
        for fl in ("nosymfollow", "noexec", "nosuid", "nodev"):
            if fl not in have:
                bad.append(f"the view {where} lacks {fl}")
    if read_text(kc.MODE_FILE).strip() != "personal":
        bad.append(f"{kc.MODE_FILE} does not say personal")
    return bad


def git_safe_lines(person: str) -> List[Tuple[str, str]]:
    """safe.directory entries the person's own git sees, read by git running as the person
    (their own config, their own rights)."""
    pw = person_home(person)
    try:
        r = subprocess.run(["setpriv", f"--reuid={pw.pw_uid}", f"--regid={pw.pw_gid}", "--clear-groups",
                            "--", "git", "config", "--show-origin", "--get-all", "safe.directory"],
                           cwd="/", env={"HOME": pw.pw_dir, "PATH": "/usr/bin:/bin"}, stdin=subprocess.DEVNULL,
                           capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return []
    out = []
    for line in r.stdout.splitlines():
        origin, _, value = line.partition("\t")
        out.append((origin.replace("file:", "", 1), value))
    return out


# ---------------------------------------------------------------------------- box units

def ensure_box_running(box: str) -> None:
    units = [f"kapbox-base@{box}.service", f"kaptyorka-egress@{box}.socket"]
    if os.path.exists(f"{SYSTEMD}/kaptyorka-relay-box@.socket"):
        units.append(f"kaptyorka-relay-box@{box}.socket")
    systemctl("start", *units)
    # The forwarder's unit denies private ranges; this computer's own addresses (a public IPv4,
    # global IPv6) change, so they are added here at every start (slice 2, E-2b: a bad forwarder
    # reached a person's server on 0.0.0.0 through the computer's own public address).
    addrs = host_addrs()
    if addrs:
        systemctl("set-property", "--runtime", f"kaptyorka-egress@{box}.service",
                  "IPAddressDeny=" + " ".join(addrs), check=False)


def start_helper(person: str, box: str) -> None:
    """The box helper (5.1): the in-box bridge from 127.0.0.1:3128 to the forwarder's socket,
    and today's bridge in box mode (helper.py run: reports the box's chats through the relay's
    door, delivers into their mailboxes, close, stuck check, helper chats). Same walls, same
    self-check. It gets the Claude token as an open file, like a chat, for the helper chats it
    starts; it ends itself ten minutes after the last chat.
    The helper has the same hard end as a chat (RuntimeMaxSec), but a chat started later must not
    lose its way out when the helper reaches it: systemd holds the listening socket of 127.0.0.1:3128
    in the box's namespace (a socket unit) and starts the helper again at once when its time ends
    (Restart=on-abnormal: a time-out, never a clean idle end or a refused self-check). New
    connections wait in the socket's queue meanwhile, none is refused."""
    unit = f"kapbox-{box}-helper.service"
    sock = f"kapbox-{box}-helper.socket"
    state = systemctl("is-active", unit, check=False).stdout.strip()
    if state in ("active", "activating", "reloading"):   # activating: also its restart after the time limit
        return
    systemctl("stop", sock, unit, check=False)
    systemctl("reset-failed", sock, unit, check=False)
    token = f"{kc.SECRET}/{person}/claude-oauth-token"
    cred = ["-p", f"OpenFile={token}:claude-token:read-only"] if os.path.isfile(token) else []
    run(["systemd-run", "--quiet", "--collect", "--expand-environment=no", f"--unit={unit}",
         "--socket-property=ListenStream=127.0.0.1:3128",
         f"--socket-property=NetworkNamespacePath=/run/netns/kapbox-{box}",
         "--socket-property=FileDescriptorName=proxy",
         f"--working-directory={kc.view_root(person)}", *props_args(person, box), *cred,
         "-p", "Restart=on-abnormal", "--",
         kc.WALLCHECK, "--box", box, "--person", person, "--addrs", ",".join(host_addrs()),
         "--canaries", ",".join(shared_canaries()), "--",
         "/usr/bin/python3", "-I", kc.BOXCODE + "/helper.py", "run", "--idle", "600"], check=False)
    systemctl("start", unit, check=False)   # systemd-run with a socket starts only the socket


def chats(box: str) -> List[Tuple[str, str, str]]:
    """(unit, tool, working folder) of every running chat of this box."""
    r = systemctl("list-units", "--type=service", "--state=active,activating", "--plain", "--no-legend",
                  "--no-pager", f"kapbox-{box}-chat-*", check=False)
    units = [ln.split()[0] for ln in r.stdout.splitlines() if ln.strip()]
    out = []
    if not units:
        return out
    r = systemctl("show", "-p", "Id", "-p", "WorkingDirectory", *units, check=False)
    cur: Dict[str, str] = {}
    for line in r.stdout.splitlines() + [""]:
        if not line.strip():
            if cur.get("Id"):
                u = cur["Id"]
                rest = u[len(f"kapbox-{box}-chat-"):-len(".service")]
                tool = rest.rsplit("-", 1)[0]
                out.append((u, tool, cur.get("WorkingDirectory", "")))
            cur = {}
            continue
        k, _, v = line.partition("=")
        cur[k] = v
    return out


def kill_box_procs_in(uid: int, prefixes: List[str]) -> int:
    """Kill every process of the box user whose folder or open files are inside one of the
    prefixes (both its own and the host's name for the place). So nothing of the box keeps a
    door into a folder that goes back to the person."""
    killed = 0
    for d in os.listdir("/proc"):
        if not d.isdigit():
            continue
        try:
            if os.stat(f"/proc/{d}").st_uid != uid:
                continue
            links = [os.readlink(f"/proc/{d}/cwd")]
            for fd in os.listdir(f"/proc/{d}/fd"):
                try:
                    links.append(os.readlink(f"/proc/{d}/fd/{fd}"))
                except OSError:
                    pass
        except OSError:
            continue
        if any(kc.under(ln, p) for ln in links for p in prefixes):
            try:
                os.kill(int(d), signal.SIGKILL)
                killed += 1
            except OSError:
                pass
    return killed


def add_view_mount(person: str, name: str, what: str) -> None:
    where = kc.view_path(person, name)
    unit = kc.view_mount_unit(person, name)
    if where not in mount_flags():          # never re-own a folder that is already the view
        mkdir(where, 0o755)
    write_file(f"{SYSTEMD}/{unit}", kc.mount_unit_text(person, name, what), 0o644)
    systemctl("daemon-reload")
    systemctl("enable", "--now", unit)
    if name != kc.MEMORY:
        hide_view(person, name, box_of_projects(what))
        systemctl("kill", "--signal=HUP", "kaptyorka-guard.service", check=False)   # watch it now


def box_of_projects(what: str) -> str:
    """The box of a path /var/lib/kap-team/kap-<box>/projects/<name>."""
    rest = what[len(kc.TEAM) + 1:] if what.startswith(kc.TEAM + "/") else ""
    top = rest.split("/", 1)[0]
    if not top.startswith("kap-"):
        raise Refuse("not a team folder of a box")
    return kc.check_box(top[4:])


def drop_view_mount(person: str, name: str) -> None:
    where = kc.view_path(person, name)
    unit = kc.view_mount_unit(person, name)
    run(["umount", "-l", where], check=False)      # a shell of the person may still be in there
    systemctl("disable", "--now", unit, check=False)
    try:
        os.unlink(f"{SYSTEMD}/{unit}")
    except FileNotFoundError:
        pass
    systemctl("daemon-reload", check=False)
    systemctl("reset-failed", unit, check=False)
    try:
        os.rmdir(where)
    except OSError:
        pass


# ---------------------------------------------------------------------------- make and remove a box (4.1, 4.2)

def _group(name: str) -> None:
    try:
        grp.getgrnam(name)
    except KeyError:
        run(["groupadd", "--system", name])


def box_owner(box: str) -> str:
    return read_text(f"{kc.BOXES}/{box}/owner").strip()


def ensure_box(person: str, box: str, allow_list: str, separate: bool = False) -> None:
    """Make (or repair) one box of a person: account, groups, home skeleton, memory, the files
    in /etc/kaptyorka/boxes/<box>, the units. Idempotent."""
    pw = person_home(person)
    u, g = kc.box_user(box), kc.share_group(box)
    try:
        pwd.getpwnam(u)
        if box_owner(box).split("\n")[0] != person:
            raise Refuse(f"the account {u} already exists and is not a kaptyorka box of {person}")
    except KeyError:
        _group(g)
        run(["useradd", "--system", "--user-group", "--no-create-home", "--home-dir", kc.box_home(box),
             "--shell", "/bin/bash", u])
        run(["passwd", "-l", u])
    _group(g)
    run(["usermod", "-a", "-G", g, u])
    run(["usermod", "-a", "-G", g, person])
    buid, bgid, sgid = uid_of(u), gid_of(u), gid_of(g)
    # /etc/kaptyorka/boxes/<box>
    bdir = f"{kc.BOXES}/{box}"
    mkdir(kc.BOXES, 0o755)
    mkdir(bdir, 0o755)
    write_file(f"{bdir}/owner", person + ("\nseparate\n" if separate else "\n"))
    write_file(f"{bdir}/passwd", f"root:x:0:0:root:/root:/usr/sbin/nologin\n"
                                 f"{u}:x:{buid}:{bgid}::{kc.box_home(box)}:/bin/bash\n"
                                 "nobody:x:65534:65534:nobody:/nonexistent:/usr/sbin/nologin\n")
    write_file(f"{bdir}/group", f"root:x:0:\n{u}:x:{bgid}:\n{g}:x:{sgid}:{u}\nnogroup:x:65534:\n")
    settings = json.loads(read_text(f"{kc.BOXCONF}/claude/settings.json") or "{}")
    settings["autoMemoryDirectory"] = f"{kc.VIEW}/{person}/{kc.MEMORY}"
    write_file(f"{bdir}/claude-settings.json", json.dumps(settings, indent=1, sort_keys=True) + "\n")
    if not os.path.exists(f"{bdir}/egress-allow"):
        write_file(f"{bdir}/egress-allow", allow_list)
    # storage (4.2)
    mkdir(kc.TEAM, 0o755)
    root, home = kc.box_root(box), kc.box_home(box)
    mkdir(root, 0o710, 0, bgid)
    mkdir(home, 0o750, 0, bgid)
    mkdir(home + "/state", 0o750, 0, bgid)
    for d in kc.STATE_DIRS:
        mkdir(f"{home}/{d}", 0o700, buid, bgid)
    for d in ("state/opencode/cfg", "state/opencode/cfg/opencode"):
        mkdir(f"{home}/{d}", 0o700, buid, bgid)
    lock_dirs(f"{home}/{kc.OPENCODE_CFG}", kc.OPENCODE_LOCKED_DIRS)
    for _, dst in kc.LOCKED_FILES:       # mount points for the read-only file binds
        p = f"{home}/{dst}"
        if not os.path.lexists(p):
            write_file(p, "", 0o644)
    mkdir(home + "/.ssh", 0o750, 0, bgid)
    write_file(f"{home}/.ssh/config",
               "# root-owned (kaptyorka): git over ssh goes through the forwarder\n"
               "Host *\n"
               f"  ProxyCommand /usr/bin/python3 -I {kc.BOXCODE}/proxybridge.py --stdio %h %p\n"
               "  UserKnownHostsFile ~/state/ssh/known_hosts\n"
               "  IdentityFile ~/.ssh/id_ed25519\n  IdentitiesOnly yes\n"
               "  StrictHostKeyChecking accept-new\n", 0o644)
    key = f"{home}/.ssh/id_ed25519"
    if not os.path.exists(key) and shutil.which("ssh-keygen"):
        run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", f"{u}@team-box", "-f", key])
        os.chown(key, buid, bgid)
        os.chmod(key, 0o600)
        os.chmod(key + ".pub", 0o644)
    write_file(f"{home}/.gitconfig", f"[user]\n\tname = {person} (team box)\n\temail = {u}@team-box.invalid\n"
                                     "[init]\n\tdefaultBranch = main\n", 0o644)
    mkdir(kc.box_memory(box), 0o2770, buid, sgid)
    fd = os.open(kc.box_memory(box), O_DIR)
    try:
        set_default_acl(fd, sgid)
    finally:
        os.close(fd)
    mkdir(kc.box_projects(box), 0o750, 0, bgid)
    mkdir(f"{kc.box_projects(box)}/{kc.MEMORY}", 0o755)   # mount point inside the box
    # the person's view
    mkdir(kc.VIEW, 0o755)
    mkdir(kc.view_root(person), 0o750, 0, gid_of(kc.share_group(person)))
    if not separate:
        add_view_mount(person, kc.MEMORY, kc.box_memory(box))
    units = [f"kapbox-base@{box}.service", f"kaptyorka-egress@{box}.socket"]
    if os.path.exists(f"{SYSTEMD}/kaptyorka-relay-box@.socket"):
        units.append(f"kaptyorka-relay-box@{box}.socket")
        # systemd will not start a new door while the relay runs ("already active, refusing"),
        # and a running relay only has the doors it started with: stop it; the doors start it again
        if not is_active(f"kaptyorka-relay-box@{box}.socket"):
            systemctl("stop", "kaptyorka-relay.service", check=False)
    systemctl("enable", "--now", *units)


def stop_box_units(box: str) -> None:
    for pat in (f"kapbox-{box}-chat-*", f"kapbox-{box}-clone-*", f"kapbox-{box}-helper.socket",
                f"kapbox-{box}-helper.service"):
        systemctl("stop", pat, check=False)
    systemctl("disable", "--now", f"kaptyorka-egress@{box}.socket", f"kaptyorka-relay-box@{box}.socket",
              check=False)
    systemctl("stop", f"kaptyorka-egress@{box}.service", check=False)
    systemctl("disable", "--now", f"kapbox-base@{box}.service", check=False)
    systemctl("stop", kc.slice_name(box), check=False)


def drop_box(person: str, box: str, delete_home: bool = True) -> None:
    """Remove a box: its units, account, groups and files. Its team folders must already be
    gone (hubchat remove)."""
    stop_box_units(box)
    if box == person:
        drop_view_mount(person, kc.MEMORY)
    u, g = kc.box_user(box), kc.share_group(box)
    run(["gpasswd", "-d", person, g], check=False)
    run(["userdel", u], check=False)
    run(["groupdel", g], check=False)
    try:
        grp.getgrnam(u)
        run(["groupdel", u], check=False)
    except KeyError:
        pass
    shutil.rmtree(f"{kc.BOXES}/{box}", ignore_errors=True)
    if delete_home:
        shutil.rmtree(kc.box_root(box), ignore_errors=True)
    for p in (f"{kc.RUNBOX}/{box}",):
        shutil.rmtree(p, ignore_errors=True)
    try:
        os.unlink(f"{kc.BOX_SECRETS}/{box}")
    except OSError:
        pass


def boxes_of(person: str) -> List[str]:
    out = []
    try:
        names = sorted(os.listdir(kc.BOXES))
    except FileNotFoundError:
        return out
    for b in names:
        if kc.BOX_RX.fullmatch(b) and box_owner(b).split("\n")[0] == person:
            out.append(b)
    return out


def tree_size(path: str) -> int:
    total = 0
    for dirpath, dirs, files in os.walk(path):
        for n in files:
            try:
                total += os.lstat(os.path.join(dirpath, n)).st_size
            except OSError:
                pass
    return total


def say(*lines: str) -> None:
    """Lines for the person's terminal, each made safe (kc.term): a name the box chose that
    reaches a message anyway cannot carry an escape sequence or a line break."""
    for ln in lines:
        print(kc.term(ln), flush=True)


def die(msg: str, code: int = 2) -> None:
    print(kc.term(f"{os.path.basename(sys.argv[0])}: {msg}"), file=sys.stderr, flush=True)
    sys.exit(code)
