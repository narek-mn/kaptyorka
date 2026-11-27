#!/usr/bin/python3 -I
"""hubchat-root (secure design v2, 4.9): add, remove, list, token-rotate. Nothing else. Root, reached only
through the sudo rule (/usr/local/bin/hubchat is one line: exec sudo -n ... "$@"). It never talks
to the hub; a folder becomes a team folder only by being moved here by the person's own command.

  hubchat add <folder> [--name NAME] [--separate] [--yes] [--force]
      Move a folder of yours into the team area and leave a shortcut (a symlink) where it was.
      A file that holds a key or token, or that is also elsewhere on your disk (a hard link),
      stops it unless --force.
  hubchat add <git URL> [--name NAME] [--separate]
      Clone a repository inside the box (never as root) into a new team folder; the shortcut
      goes into the current folder.
  hubchat remove <folder or name>
      Make a team folder personal again, back where its shortcut was, made safe for git.
  hubchat list
      Every team folder, its shortcut, size, box and running chats; sign-ins, allow list,
      self-check, warnings.
  hubchat token-rotate
      Give the box a new Claude token (from `claude setup-token`, pasted or on standard input),
      end the running Claude chats, and say how to end the old token.

Installed as /usr/local/libexec/kaptyorka/hubchat-root.
"""
import errno
import os
import secrets
import shutil
import stat
import subprocess
import sys
import time

if __package__:
    from . import kap_tools, kapcommon as kc, kaproot as kr
else:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import kap_tools
    import kapcommon as kc
    import kaproot as kr

Refuse = kc.Refuse
USAGE = """hubchat: share folders with your team. Team chats run in a separate, locked box.

  hubchat add <folder>       make a folder of yours a team folder (a shortcut stays where it was)
  hubchat add <git URL>      get a repository as a new team folder, here in the current folder
  hubchat list               your team folders, and a check that every wall is in place
  hubchat remove <folder>    make a team folder personal again

Then cd into a team folder and run claude, codex, opencode or cursor-agent as usual.

Options for add:
  --name NAME   use another name for the team folder
  --separate    give this folder a box of its own
  --force       share it even with files that look like keys or passwords
  hubchat update-tools [claude codex ...]   newer AI tools in the box now (a timer does it daily)
                             (claude=2.1.300 for one version; opencode=2 for OpenCode 2)
Rarely needed:
  hubchat token-rotate       give the box a new Claude token (if a team chat may have leaked it)"""
SHORT = {"add": "hubchat add <folder>   or   hubchat add <git URL>", "remove": "hubchat remove <folder or name>",
         "list": "hubchat list", "token-rotate": "hubchat token-rotate",
         "update-tools": "hubchat update-tools [claude] [codex] [opencode] [cursor-agent]"}


def ask(question: str) -> bool:
    if not sys.stdin.isatty():
        return False
    try:
        return input(question + " [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


def base_allow() -> str:
    return kr.read_text(f"{kc.BOXCONF}/egress-base") or ""


def pick_box(person: str, name: str, separate: bool) -> str:
    box = kc.box_id(person, name if separate else None)
    if separate:
        kr.ensure_box(person, box, base_allow(), separate=True)
    elif not os.path.isdir(f"{kc.BOXES}/{box}"):
        raise Refuse("this account has no box yet: run the installer first")
    return box


LINKED = "this file is also elsewhere on your disk (a hard link): the box would get a copy of it"
O_PEEK = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC | getattr(os, "O_NOATIME", 0)


def peek(dfd: int, name: str) -> bytes:
    """The first bytes of a regular file, opened without following a link; b"" if it is not a
    plain file any more or is too big to be a key file."""
    try:
        fd = os.open(name, O_PEEK, dir_fd=dfd)
    except PermissionError:          # O_NOATIME on a file root does not own on some file systems
        try:
            fd = os.open(name, O_PEEK & ~getattr(os, "O_NOATIME", 0), dir_fd=dfd)
        except OSError:
            return b""
    except OSError:
        return b""
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size > kc.CONTENT_MAX_SIZE:
            return b""
        return os.read(fd, kc.CONTENT_SAMPLE)
    except OSError:
        return b""
    finally:
        os.close(fd)


def scan_secrets(top_fd: int, limit: int = 200000):
    """What looks like a secret (4.9 step 2, and red team round 1 finding 2), walked without
    following links: [(path, reason, kind)], kind "name" (its name), "content" (its first
    bytes: a key block, a token shape, credential JSON, a KEY=secret line) or "link" (a plain
    file with more than one hard link, so it is also somewhere else on the disk)."""
    found, seen = [], 0
    stack = [(os.dup(top_fd), "")]
    while stack:
        fd, rel = stack.pop()
        try:
            for name in sorted(os.listdir(fd)):
                seen += 1
                if seen > limit:
                    break
                try:
                    st = os.stat(name, dir_fd=fd, follow_symlinks=False)
                except OSError:
                    continue
                if stat.S_ISDIR(st.st_mode):
                    if name in ("objects", "node_modules") and rel.endswith(".git/") or name == "node_modules":
                        continue
                    try:
                        stack.append((os.open(name, kr.O_DIR, dir_fd=fd), f"{rel}{name}/"))
                    except OSError:
                        pass
                    continue
                if not stat.S_ISREG(st.st_mode):
                    continue
                if kc.is_secret_name(name):
                    found.append((rel + name, "its name looks like a secret", "name"))
                reason = kc.secret_in_content(peek(fd, name))
                if reason:
                    found.append((rel + name, reason, "content"))
                if st.st_nlink > 1:
                    found.append((rel + name, LINKED, "link"))
        finally:
            os.close(fd)
    return found


def show_found(found, most: int = 30):
    lines = [f"  {kc.shown(p, 200)}: {why}" for p, why, _ in found[:most]]
    return lines + (["  ..."] if len(found) > most else [])


def check_source(person: str, path: str):
    """(real path, parent fd, folder fd, stat) of a folder the person may make a team folder:
    owned by them, inside their home but not the home itself, not in a synced folder, on the
    same disk as the team area, not a mount point."""
    pw = kr.person_home(person)
    real = os.path.realpath(path)
    home = pw.pw_dir
    hit = kc.team_folder(real, person)
    if hit:
        raise Refuse(f"{kc.shown(path)} is already a team folder ({hit[0]}); see: hubchat list")
    if real == home or not kc.under(real, home):
        raise Refuse(f"{kc.shown(path)} must be a folder inside your home, not the home itself")
    if kc.under(real, home + "/.ssh") or os.path.basename(real).startswith("."):
        raise Refuse("hidden folders and ~/.ssh cannot be team folders")
    parts = real[len(home) + 1:].split("/")
    synced = kc.in_synced_folder(parts)
    if synced:
        raise Refuse(f"{real} is inside a synced folder ({synced}); team chats would reach the sync tool")
    pfd = kr.open_dir(os.path.dirname(real))
    try:
        fd = os.open(os.path.basename(real), kr.O_DIR, dir_fd=pfd)
    except OSError as e:
        os.close(pfd)
        raise Refuse(f"{kc.shown(path)}: {os.strerror(e.errno)} (it must be a folder, not a symlink)")
    try:
        st, pst = os.fstat(fd), os.fstat(pfd)
        if st.st_uid != pw.pw_uid or pst.st_uid != pw.pw_uid:
            raise Refuse(f"{real} and the folder holding it must be yours")
        if st.st_dev != pst.st_dev:
            raise Refuse(f"{real} is a mount point (another disk). Copy it into your home, for example "
                         f"cp -a {real} ~/, then hubchat add the copy")
        if st.st_dev != os.stat(kc.TEAM).st_dev:
            raise Refuse(f"{real} is on another disk than the team area. Copy it into your home, for example "
                         f"cp -a {real} ~/, then hubchat add the copy")
        # a sync tool's marker anywhere from the folder up to the home
        p = real
        while kc.under(p, home):
            for m in kc.SYNC_MARKERS:
                if os.path.lexists(os.path.join(p, m)):
                    raise Refuse(f"{real} is inside a synced folder ({p}/{m})")
            if p == home:
                break
            p = os.path.dirname(p)
    except BaseException:
        os.close(fd)
        os.close(pfd)
        raise
    return real, pfd, fd, st


COVERED = ("  Note: tool settings in it, at any depth (.claude, .codex, .cursor, .opencode, .vscode, .mcp.json,",
           "  .envrc, and git's .git/config, .git/hooks and others) look empty to you, so your own tools and",
           "  your own git never load what team chats wrote. To change them, ask a team chat. git typed here",
           "  runs in the box: never add this folder to git's safe.directory.")


def add_folder(person: str, path: str, name: str, separate: bool, yes: bool, force: bool = False) -> None:
    folders = kr.load_folders(person)
    real, pfd, fd, st = check_source(person, path)
    try:
        name = kc.check_name(name or os.path.basename(real))
        if name in folders:
            raise Refuse(f"a team folder named {name} exists; pick another with --name")
        found = scan_secrets(fd)
        strong = [f for f in found if f[2] != "name"]
        if found:
            kr.say("These files look like secrets, or are also elsewhere on your disk. If you share the folder,",
                   "team chats can read all of them:", *show_found(found))
        # A secret seen in a file's content, or a hard link, is refused unless --force: no
        # question at the terminal, so a habit of typing "y" does not share a key.
        if strong and not force:
            raise Refuse("nothing was changed. Move those files out of the folder and run the same command again, "
                         "or add --force to share them anyway")
        if found and not strong and not (yes or force) and not ask("Share them anyway?"):
            raise Refuse("nothing was changed. Move those files out of the folder and run the same command again, "
                         "or add --yes to share them anyway")
        box = pick_box(person, name, separate)
        pw = kr.person_home(person)
        buid, sgid = kr.uid_of(kc.box_user(box)), kr.gid_of(kc.share_group(box))
        dst = kr.open_dir(kc.box_projects(box))
        base = os.path.basename(real)
        try:
            if os.path.lexists(f"{kc.box_projects(box)}/{name}"):
                raise Refuse(f"{name} already exists in the team area")
            try:
                os.rename(base, name, src_dir_fd=pfd, dst_dir_fd=dst)      # one rename, same disk
            except OSError as e:
                if e.errno == errno.EXDEV:
                    raise Refuse(f"{real} cannot be moved in one step (another disk or layer); "
                                 "copy it into a new folder in your home first")
                raise
            moved = os.stat(name, dir_fd=dst, follow_symlinks=False)
            if not stat.S_ISDIR(moved.st_mode) or moved.st_ino != st.st_ino:
                os.rename(name, base, src_dir_fd=dst, dst_dir_fd=pfd)
                raise Refuse("the folder changed while it was being moved; nothing was changed")
            try:
                os.fchown(fd, buid, sgid)
                os.fchmod(fd, 0o2770)
                kr.set_default_acl(fd, sgid)
                rep = kr.take_tree(fd, buid, sgid, team=True)
                kr.write_file(kr.since_stamp(box, name), "", 0o600)   # after the hand-over: what changes later counts
                kr.add_view_mount(person, name, f"{kc.box_projects(box)}/{name}")
                view = kc.view_path(person, name)
                os.symlink(view, base, dir_fd=pfd)
                os.chown(base, pw.pw_uid, pw.pw_gid, dir_fd=pfd, follow_symlinks=False)
            except BaseException:
                kr.drop_view_mount(person, name)
                try:
                    os.unlink(base, dir_fd=pfd)
                except OSError:
                    pass
                os.rename(name, base, src_dir_fd=dst, dst_dir_fd=pfd)
                kr.take_tree(fd, pw.pw_uid, pw.pw_gid, team=False)
                os.fchown(fd, pw.pw_uid, pw.pw_gid)
                raise
        finally:
            os.close(dst)
        folders[name] = {"name": name, "box": box, "shortcut": real, "source": "", "separate": separate}
        kr.save_folders(person, folders)
    finally:
        os.close(fd)
        os.close(pfd)
    notes = []
    if rep.copied:
        notes.append(f"{len(rep.copied)} hard-linked file(s) were replaced by a copy (your other copy is unchanged)")
    if rep.removed:
        notes.append(f"removed {len(rep.removed)} device file(s), pipe(s) or socket(s)")
    if rep.stripped:
        notes.append(f"removed set-user-id bits from {len(rep.stripped)} file(s)")
    kr.say(f"{name} is now a team folder{' in its own box' if separate else ''}.",
           f"  your shortcut: {real} -> {view}",
           f"  Next: cd {real} and run claude, codex, opencode or cursor-agent as usual.",
           "  They start in the team box.", *[f"  {n}" for n in notes], *COVERED)
    warn_all(person)


def add_git(person: str, url: str, name: str, separate: bool) -> None:
    url, host, port, default = kc.parse_git_url(url)
    name = kc.check_name(name or default)
    folders = kr.load_folders(person)
    if name in folders:
        raise Refuse(f"a team folder named {name} exists; pick another with --name")
    pw = kr.person_home(person)
    here = os.path.realpath(os.getcwd())
    if not kc.under(here, pw.pw_dir):
        raise Refuse("the new folder's shortcut is made in the current folder, so cd into a folder in "
                     "your home first (for example cd ~)")
    pfd = kr.open_dir(here)
    try:
        if os.fstat(pfd).st_uid != pw.pw_uid:
            raise Refuse("the current folder must be yours: the shortcut goes there")
        if os.path.lexists(os.path.join(here, name)):
            raise Refuse(f"{os.path.join(here, name)} exists; pick another name with --name")
        box = pick_box(person, name, separate)
        buid, sgid = kr.uid_of(kc.box_user(box)), kr.gid_of(kc.share_group(box))
        allow_path = f"{kc.BOXES}/{box}/egress-allow"
        text, added = kc.allow_add(kr.read_text(allow_path), host, port)
        if added:
            kr.write_file(allow_path, text)
        top = f"{kc.box_projects(box)}/{name}"
        kr.mkdir(top, 0o2770, buid, sgid)
        tfd = os.open(top, kr.O_DIR)
        kr.set_default_acl(tfd, sgid)
        os.close(tfd)
        try:
            problems = kr.outside_check(person, sorted({box, person}))
            if problems:
                raise Refuse("self-check: " + "; ".join(problems))
            kr.ensure_box_running(box)
            kr.start_helper(person, box)
            view = kc.view_path(person, name)
            kr.say(f"Cloning {url} inside the box ...")
            r = subprocess.run(
                ["systemd-run", "--quiet", "--collect", "--expand-environment=no", "--pipe", "--wait",
                 f"--unit=kapbox-{box}-clone-{secrets.token_hex(4)}", f"--working-directory={view}",
                 *kr.props_args(person, box), "--", kc.WALLCHECK, "--box", box, "--person", person,
                 "--addrs", ",".join(kr.host_addrs()), "--", kc.BOX_EXEC, "git",
                 "-c", "protocol.allow=never", "-c", "protocol.https.allow=always", "-c", "protocol.ssh.allow=always",
                 "clone", "--no-recurse-submodules", "--", url, "."],
                stdin=subprocess.DEVNULL, env=kr.CLEAN_ENV, timeout=3600)
            if r.returncode != 0:
                raise Refuse(f"git clone failed inside the box (exit {r.returncode}); nothing was kept. Check the URL. "
                             "For a private repository use its git@host:path address and add the box's SSH key "
                             "(hubchat list shows it) to your git account")
            kr.write_file(kr.since_stamp(box, name), "", 0o600)
            kr.add_view_mount(person, name, top)
            os.symlink(view, name, dir_fd=pfd)
            os.chown(name, pw.pw_uid, pw.pw_gid, dir_fd=pfd, follow_symlinks=False)
        except BaseException:
            kr.drop_view_mount(person, name)
            shutil.rmtree(top, ignore_errors=True)
            if added:
                kr.write_file(allow_path, kc.allow_remove(kr.read_text(allow_path), host, port))
            raise
        folders[name] = {"name": name, "box": box, "shortcut": os.path.join(here, name), "source": url,
                         "separate": separate}
        kr.save_folders(person, folders)
    finally:
        os.close(pfd)
    kr.say(f"{name} is now a team folder, cloned inside the box{' (its own box)' if separate else ''}.",
           f"  your shortcut: {os.path.join(here, name)} -> {view}",
           f"  the box may now reach {host}{':22' if port == 22 else ''}.",
           f"  Next: cd {os.path.join(here, name)} and run claude, codex, opencode or cursor-agent as usual.",
           *COVERED)
    warn_all(person)


def find_entry(person: str, target: str, folders) -> dict:
    if target in folders:
        return folders[target]
    p = os.path.join(os.getcwd(), target)
    if os.path.islink(p):
        hit = kc.team_folder(os.readlink(p), person)
        if hit and not hit[1] and hit[0] in folders:
            return folders[hit[0]]
    hit = kc.team_folder(os.path.realpath(p), person)
    if hit and hit[0] in folders:
        return folders[hit[0]]
    raise Refuse(f"{kc.shown(target)} is not one of your team folders (run hubchat list)")


def return_place(person: str, entry: dict):
    """(parent fd, name) where the folder goes back: its shortcut's place if the shortcut is
    still ours, else a free name in the home."""
    pw = kr.person_home(person)
    short = entry.get("shortcut") or ""
    view = kc.view_path(person, entry["name"])
    if short and kc.under(short, pw.pw_dir) and short != pw.pw_dir:
        try:
            pfd = kr.open_dir(os.path.dirname(short))
            base = os.path.basename(short)
            if os.fstat(pfd).st_uid == pw.pw_uid:
                st = os.stat(base, dir_fd=pfd, follow_symlinks=False) if _lexists(pfd, base) else None
                if st is None:
                    return pfd, base
                if stat.S_ISLNK(st.st_mode) and os.readlink(base, dir_fd=pfd) == view:
                    os.unlink(base, dir_fd=pfd)
                    return pfd, base
            os.close(pfd)
        except (Refuse, OSError):
            pass
    pfd = kr.open_dir(pw.pw_dir)
    for n in [entry["name"]] + [f"{entry['name']}.from-team-{i}" for i in range(1, 100)]:
        if not _lexists(pfd, n):
            return pfd, n
    os.close(pfd)
    raise Refuse("no free name in your home to put the folder back")


def _lexists(dfd: int, name: str) -> bool:
    try:
        os.stat(name, dir_fd=dfd, follow_symlinks=False)
        return True
    except FileNotFoundError:
        return False


def remove(person: str, target: str, uninstall: bool = False) -> None:
    folders = kr.load_folders(person)
    entry = find_entry(person, target, folders)
    name, box = entry["name"], entry["box"]
    pw = kr.person_home(person)
    buid = kr.uid_of(kc.box_user(box))
    real = f"{kc.box_projects(box)}/{name}"
    view = kc.view_path(person, name)
    # 1. no box process keeps a door into it: chats there stop; the folder is closed to the box;
    #    any box process still inside it is killed
    for unit, _, wd in kr.chats(box):
        if kc.under(wd, view):
            kr.systemctl("stop", unit, check=False)
    since = kr.read_since(box, name)
    fd = kr.open_dir(real)
    try:
        os.fchown(fd, 0, 0)
        os.fchmod(fd, 0o700)
        for _ in range(3):
            if kr.kill_box_procs_in(buid, [view, real]) == 0:
                break
            time.sleep(0.2)
        kr.drop_view_mount(person, name)
        # 2. made safe for git, then the person's again, then back at the shortcut's place
        notes = kr.make_safe_for_git(fd)
        traps = kr.trap_files(fd, since, git=False)   # git's own were just made safe
        kr.take_tree(fd, pw.pw_uid, pw.pw_gid, team=False)
        kr.drop_acls(fd)
        src = kr.open_dir(kc.box_projects(box))
        try:
            pfd, base = return_place(person, entry)
            try:
                os.rename(name, base, src_dir_fd=src, dst_dir_fd=pfd)
                os.fchown(fd, pw.pw_uid, pw.pw_gid)
                os.fchmod(fd, 0o750)
                back = os.path.join(os.readlink(f"/proc/self/fd/{pfd}"), base)
            finally:
                os.close(pfd)
        finally:
            os.close(src)
    finally:
        os.close(fd)
    del folders[name]
    kr.save_folders(person, folders)
    try:
        os.unlink(kr.since_stamp(box, name))
    except OSError:
        pass
    if entry.get("source"):
        _, host, port, _ = kc.parse_git_url(entry["source"])
        if not any(f.get("source") and kc.parse_git_url(f["source"])[1:3] == (host, port)
                   for f in folders.values() if f["box"] == box):
            path = f"{kc.BOXES}/{box}/egress-allow"
            kr.write_file(path, kc.allow_remove(kr.read_text(path), host, port))
    if entry.get("separate") and not any(f["box"] == box for f in folders.values()):
        kr.drop_box(person, box)
        notes.append(f"its own box {kc.box_user(box)} was removed; revoke that box's tool sign-ins at each vendor")
    kr.say(f"{name} is personal again: {back}",
           "  Team chats cannot reach it any more. Nothing the box wrote in its git settings runs as you:",
           "  only plain settings are left in .git/config, and git hooks were moved aside.",
           *[f"  {n}" for n in notes],
           *([] if uninstall else
             ["  If a team chat may have leaked the box's Claude token, give it a new one: hubchat token-rotate"]))
    if traps:
        kr.say("  Team chats may have changed these files, which can run code when a program of yours opens "
               "them (your own AI tool, your editor, a build). Read them before you trust the folder:",
               *[f"    {kc.shown(t, 200)}" for t in traps if not t.startswith("?")],
               *([f"    {kr.trap_cut(traps)}"] if kr.trap_cut(traps) else []))


def human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def warn_all(person: str) -> None:
    folders = kr.load_folders(person)
    shorts = [f["shortcut"] for f in folders.values() if f.get("shortcut")]
    for w in kc.safe_directory_warnings(kr.git_safe_lines(person), shorts):
        kr.say(w)
    pw = kr.person_home(person)
    for box in kr.boxes_of(person):
        try:
            mine = open(f"{kc.box_home(box)}/.ssh/id_ed25519.pub").read().split()[1]
        except (OSError, IndexError):
            continue
        for pub in ("id_ed25519.pub", "id_rsa.pub", "id_ecdsa.pub"):
            try:
                fd = os.open(f"{pw.pw_dir}/.ssh/{pub}", os.O_RDONLY | os.O_NOFOLLOW)
                with os.fdopen(fd) as f:
                    if mine in f.read(65536):
                        kr.say(f"WARNING: the box's SSH key is your own key {pub}. Make the box its own key.")
            except OSError:
                pass


def list_all(person: str) -> None:
    folders = kr.load_folders(person)
    boxes = kr.boxes_of(person)
    kr.say(f"Team folders of {person} on this computer ({len(folders)}):")
    running = {b: kr.chats(b) for b in boxes}
    for name in sorted(folders):
        f = folders[name]
        real = f"{kc.box_projects(f['box'])}/{name}"
        view = kc.view_path(person, name)
        inside = [f"{tool} ({unit.split('-')[-1].split('.')[0]})" for unit, tool, wd in running.get(f["box"], [])
                  if kc.under(wd, view)]
        kr.say(f"  {name}", f"    shortcut: {f['shortcut'] or '-'}", f"    view:     {view}",
               f"    size:     {human(kr.tree_size(real))}",
               f"    box:      {kc.box_user(f['box'])}{' (separate)' if f.get('separate') else ''}",
               *( [f"    from:     {f['source']}"] if f.get("source") else []),
               f"    chats:    {', '.join(inside) if inside else 'none running'}")
        try:
            fd = kr.open_dir(real)
            try:
                traps = kr.trap_files(fd, kr.read_since(f["box"], name))
            finally:
                os.close(fd)
            if traps:
                kr.say("    WARNING: files that can run code when your own tools, editor, git or a build open them,",
                       "    changed while this was a team folder (your view covers the tool and git ones): "
                       + kr.trap_notice(traps, 20))
            secrets_found = [f for f in scan_secrets_path(real) if f[2] != "link"]
            if secrets_found:
                kr.say("    WARNING: these look like secrets and team chats can read them:",
                       *["  " + ln for ln in show_found(secrets_found, 10)])
        except (Refuse, OSError):
            pass
    kr.say(f"  {kc.MEMORY} (the shared team memory)    view: {kc.view_path(person, kc.MEMORY)}")
    have = kap_tools.installed()
    kr.say("AI tools in the box: " + (", ".join(f"{t} {v}" for t, v in sorted(have.items())) or "none")
           + "  (hubchat update-tools for newer ones)")
    for box in boxes:
        kr.say(f"Box {kc.box_user(box)}:")
        tok = f"{kc.SECRET}/{person}/claude-oauth-token"
        if os.path.exists(tok):
            end = time.strftime("%Y-%m-%d", time.localtime(os.stat(tok).st_mtime + 365 * 86400))
            kr.say(f"    Claude Code sign-in: a setup-token, ends about {end}")
        else:
            kr.say("    Claude Code sign-in: none")
        home = kc.box_home(box)
        for tool, p in (("Codex", "state/codex/auth.json"), ("Cursor", ".cursor/auth.json"),
                        ("OpenCode", "state/opencode/data/opencode/auth.json")):
            kr.say(f"    {tool} sign-in: {'yes' if os.path.exists(f'{home}/{p}') else 'none'}")
        try:
            key = open(f"{home}/.ssh/id_ed25519.pub").read().split()
            kr.say("    git SSH key (add it to your git account as an extra key, for private repositories):",
                   f"      {' '.join(key[:3])}")
        except (OSError, IndexError):
            pass
        allow = sorted(kc.parse_allow(kr.read_text(f"{kc.BOXES}/{box}/egress-allow")).items())
        kr.say("    may reach: " + ", ".join(h + ("" if ports == {443} else ":" + "/".join(map(str, sorted(ports))))
                                         for h, ports in allow))
    problems = kr.outside_check(person, boxes)
    kr.say("Self-check: " + ("all walls in place" if not problems else "PROBLEMS: team chats will not start"),
           *[f"  - {p}" for p in problems],
           *(["Next: restart this computer and run hubchat list again. If a problem stays, send these lines "
              "to your team admin."] if problems else []))
    warn_all(person)


def read_new_token() -> str:
    if sys.stdin.isatty():
        import getpass
        print("Run  claude setup-token  in another terminal, in a personal folder, and copy the token it prints.")
        return getpass.getpass("Paste it here (it is not shown), then press Enter: ").strip()
    return sys.stdin.readline(512).strip()


def token_rotate(person: str) -> None:
    """A new Claude token for the person's boxes (red team round 1, finding 1: treat the box's
    token as one a tricked chat may have sent away). Read from the terminal or standard input,
    never from the command line, where every user's ps would show it."""
    tok = read_new_token()
    if not tok:
        raise Refuse("no token given; nothing was changed. Run  claude setup-token  in another terminal, "
                     "then hubchat token-rotate again and paste the token it printed")
    if not kc.TOKEN_RX.fullmatch(tok):
        raise Refuse("that does not look like a token from `claude setup-token`; nothing was changed")
    path = f"{kc.SECRET}/{person}/claude-oauth-token"
    if kr.read_text(path).strip() == tok:
        raise Refuse("that is the token the box has now; make a new one with `claude setup-token`")
    kr.mkdir(kc.SECRET, 0o700)
    kr.mkdir(f"{kc.SECRET}/{person}", 0o700)
    kr.write_file(path, tok + "\n", 0o600)
    ended = 0
    for box in kr.boxes_of(person):
        for unit, tool, _ in kr.chats(box):
            if tool == "claude":
                kr.systemctl("stop", unit, check=False)
                ended += 1
    kr.say("The box has a new Claude token; the next Claude chat uses it.",
           f"  {ended} running Claude chat(s) were ended (they held the old token)." if ended else
           "  No Claude chat was running.", *kc.REVOKE_CLAUDE_TOKEN)


def scan_secrets_path(path: str):
    fd = kr.open_dir(path)
    try:
        return scan_secrets(fd, limit=50000)
    finally:
        os.close(fd)


def update_tools(pos) -> int:
    """Newer AI tools in the box, each from its vendor's installer in its locked unit (kap_tools);
    only tool names and versions get through (kap_tools.parse_specs)."""
    specs = kap_tools.parse_specs(",".join(pos)) if pos else {t: "" for t in kap_tools.installed()}
    if not specs:
        raise Refuse("no AI tool is in the box yet: name the ones you want, such as  hubchat update-tools claude codex")
    done, failed = kap_tools.update(specs, kr.say)
    for t, e in failed.items():
        print(kc.term(f"hubchat: {t} was not changed: {e}"), file=sys.stderr)
    return 1 if failed else 0


def main(argv) -> int:
    try:
        person = kr.caller_person()
        for ln in kr.old_login_hint(person):
            print(kc.term(ln), file=sys.stderr)
        if not argv or argv[0] in ("help", "-h", "--help"):
            kr.say(*USAGE.splitlines())
            return 0
        if kc.unfinished_install():
            raise Refuse(kc.unfinished_install())
        cmd, rest = argv[0], argv[1:]
        opts = {"--name": "", "--separate": False, "--yes": False, "--force": False}
        pos = []
        while rest:
            a = rest.pop(0)
            if a == "--name" and rest:
                opts["--name"] = rest.pop(0)
            elif a in ("--separate", "--yes", "--force"):
                opts[a] = True
            elif a.startswith("-"):
                raise Refuse(f"unknown option {kc.shown(a)}. Usage: {SHORT.get(cmd, 'hubchat help')}")
            else:
                pos.append(a)
        if cmd == "add" and len(pos) == 1:
            if kc.looks_like_url(pos[0]):
                add_git(person, pos[0], opts["--name"], opts["--separate"])
            else:
                add_folder(person, pos[0], opts["--name"], opts["--separate"], opts["--yes"], opts["--force"])
        elif cmd == "remove" and len(pos) == 1 and not opts["--name"]:
            remove(person, pos[0])
        elif cmd == "list" and not pos:
            list_all(person)
        elif cmd == "token-rotate" and not pos and not any(opts.values()):
            token_rotate(person)
        elif cmd == "update-tools" and not any(opts.values()):
            return update_tools(pos)
        elif cmd in SHORT:
            raise Refuse(f"usage: {SHORT[cmd]}")
        else:
            raise Refuse(f"there is no command {kc.shown(cmd)}. The commands are add, list and remove "
                         "(hubchat help shows them all)")
        return 0
    except BrokenPipeError:   # `hubchat list | head`: the reader left, nothing to say
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())   # no second error at exit
        return 0
    except Refuse as e:
        print(kc.term(f"hubchat: {e}"), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
