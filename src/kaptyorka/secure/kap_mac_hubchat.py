#!/usr/bin/python3 -I
"""hubchat-root on macOS (secure design v2, 4.9 and 10; TEST VERSION): add, remove, list,
token-rotate, update-tools, as on Linux. Root, reached only through the sudo rule
(/usr/local/bin/hubchat is one line: exec sudo -n ... "$@"). It never talks to the hub.

A team folder lives at /Users/Shared/kaptyorka/<person>/<name>, owned by the box, group the share
group (the person and the box), with an inherited ACL so files either side makes stay editable by
both. The person keeps a shortcut (a symlink) where the folder was. macOS has no mount that could
give her a view without symlinks and programs, so the box's files there are as they are: hubchat
list and enter name the ones that can run code when her own tools open them.

Installed as /usr/local/libexec/kaptyorka/hubchat-root on macOS.
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
    from . import kap_hubchat as hc, kap_mac as km, kap_tools, kap_update, kapcommon as kc, kaproot as kr
else:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import kap_hubchat as hc
    import kap_mac as km
    import kap_tools
    import kap_update
    import kapcommon as kc
    import kaproot as kr

Refuse = kc.Refuse
USAGE = hc.USAGE.replace("  --separate    give this folder a box of its own\n", "") + \
    "\n\n" + km.TEST_NOTICE + "\nOn macOS: no --separate boxes, and one OpenCode 1 window per box at a time."
SHORT = hc.SHORT
NOT_COVERED = ("  Note (macOS): your own programs see the team folder exactly as team chats left it. Tool settings",
               "  in it (.claude, .codex, .cursor, .vscode, .mcp.json, .envrc, .git/config, .git/hooks ...) can",
               "  run code when your own tools, editor or git open it: hubchat list names the ones that changed.",
               "  git typed here runs in the box: never add this folder to git's safe.directory.")


def apply_acl(path: str, group: str) -> None:
    """The share group's inherited ACL on every folder and file below path (find never follows a
    link; path's top is root's or the person's 0700 while this runs, so the box cannot swap a
    thing inside)."""
    ace = km.acl_entry(group)
    flat = ace.replace(",file_inherit,directory_inherit", "")
    km.run(["/usr/bin/find", path, "-type", "d", "-exec", "/bin/chmod", "+a", ace, "{}", "+"], timeout=600)
    km.run(["/usr/bin/find", path, "-type", "f", "-exec", "/bin/chmod", "+a", flat, "{}", "+"], timeout=600)


def drop_acl(path: str) -> None:
    km.run(["/bin/chmod", "-R", "-N", path], check=False, timeout=600)


def check_source(person: str, path: str):
    """(real path, parent fd, folder fd, stat) of a folder the person may make a team folder: hers,
    inside her home but not the home itself, not synced (iCloud Desktop & Documents, a cloud
    storage folder), on the same disk as the team area, not a mount point."""
    pw = km.person_home(person)
    home = pw.pw_dir
    try:
        rfd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as e:
        raise Refuse(f"{kc.shown(path)}: {os.strerror(e.errno)} (it must be a folder, not a symlink)")
    try:
        real = km.fd_path(rfd)
    finally:
        os.close(rfd)
    if km.team_folder(real, person):
        raise Refuse(f"{kc.shown(path)} is already a team folder; see: hubchat list")
    if real == home or not kc.under(real, home):
        raise Refuse(f"{kc.shown(path)} must be a folder inside your home, not the home itself")
    if kc.under(real, home + "/.ssh") or os.path.basename(real).startswith(".") or kc.under(real, home + "/Library"):
        raise Refuse("hidden folders, ~/.ssh and ~/Library cannot be team folders")
    if os.path.isdir(home + "/Library/Mobile Documents/com~apple~CloudDocs") and \
            (kc.under(real, home + "/Desktop") or kc.under(real, home + "/Documents")):
        raise Refuse(f"{real} is in Desktop or Documents, which iCloud may sync; team chats would reach iCloud. "
                     "Move it elsewhere in your home first (for example ~/workspace)")
    synced = kc.in_synced_folder(real[len(home) + 1:].split("/"))
    if synced:
        raise Refuse(f"{real} is inside a synced folder ({synced}); team chats would reach the sync tool")
    return (real,) + hc_check_fds(real, pw)


def hc_check_fds(real: str, pw):
    pfd = kr.open_dir(os.path.dirname(real))
    try:
        fd = os.open(os.path.basename(real), kr.O_DIR, dir_fd=pfd)
    except OSError as e:
        os.close(pfd)
        raise Refuse(f"{real}: {os.strerror(e.errno)} (it must be a folder, not a symlink)")
    try:
        st, pst = os.fstat(fd), os.fstat(pfd)
        if st.st_uid != pw.pw_uid or pst.st_uid != pw.pw_uid:
            raise Refuse(f"{real} and the folder holding it must be yours")
        if st.st_dev != pst.st_dev or st.st_dev != os.stat(km.TEAMAREA).st_dev:
            raise Refuse(f"{real} is on another disk than the team area. Copy it into your home first, then hubchat "
                         "add the copy")
        p = real
        while kc.under(p, pw.pw_dir) and p != pw.pw_dir:
            for m in kc.SYNC_MARKERS:
                if os.path.lexists(os.path.join(p, m)):
                    raise Refuse(f"{real} is inside a synced folder ({p}/{m})")
            p = os.path.dirname(p)
    except BaseException:
        os.close(fd)
        os.close(pfd)
        raise
    return pfd, fd, st


def add_folder(person: str, path: str, name: str, yes: bool, force: bool = False) -> None:
    folders = kr.load_folders(person)
    real, pfd, fd, st = check_source(person, path)
    box = person
    try:
        name = kc.check_name(name or os.path.basename(real))
        if name in folders:
            raise Refuse(f"a team folder named {name} exists; pick another with --name")
        found = hc.scan_secrets(fd)
        strong = [f for f in found if f[2] != "name"]
        if found:
            kr.say("These files look like secrets, or are also elsewhere on your disk. If you share the folder,",
                   "team chats can read all of them:", *hc.show_found(found))
        if strong and not force:
            raise Refuse("nothing was changed. Move those files out of the folder and run the same command again, "
                         "or add --force to share them anyway")
        if found and not strong and not (yes or force) and not hc.ask("Share them anyway?"):
            raise Refuse("nothing was changed. Move those files out of the folder and run the same command again, "
                         "or add --yes to share them anyway")
        pw = km.person_home(person)
        buid, _, sgid = km.ids(box)
        dst = kr.open_dir(km.team_root(person))
        base, stage = os.path.basename(real), f".adding-{secrets.token_hex(4)}"
        old_mode = st.st_mode & 0o7777
        try:
            if os.path.lexists(km.team_path(person, name)):
                raise Refuse(f"{name} already exists in the team area")
            os.fchmod(fd, 0o700)          # closed to everyone but the person while it moves
            try:
                os.rename(base, stage, src_dir_fd=pfd, dst_dir_fd=dst)
            except OSError as e:
                os.fchmod(fd, old_mode)
                if e.errno == errno.EXDEV:
                    raise Refuse(f"{real} cannot be moved in one step (another disk); copy it into a new folder "
                                 "in your home first")
                raise
            moved = os.stat(stage, dir_fd=dst, follow_symlinks=False)
            if not stat.S_ISDIR(moved.st_mode) or moved.st_ino != st.st_ino:
                os.rename(stage, base, src_dir_fd=dst, dst_dir_fd=pfd)
                os.fchmod(fd, old_mode)
                raise Refuse("the folder changed while it was being moved; nothing was changed")
            try:
                rep = kr.take_tree(fd, buid, sgid, team=True)
                apply_acl(f"{km.team_root(person)}/{stage}", kc.share_group(box))
                os.fchown(fd, buid, sgid)
                os.fchmod(fd, 0o2770)
                os.rename(stage, name, src_dir_fd=dst, dst_dir_fd=dst)
                kr.write_file(kr.since_stamp(box, name), "", 0o600)
                os.symlink(km.team_path(person, name), base, dir_fd=pfd)
                os.chown(base, pw.pw_uid, pw.pw_gid, dir_fd=pfd, follow_symlinks=False)
            except BaseException:
                try:
                    os.unlink(base, dir_fd=pfd)
                except OSError:
                    pass
                os.fchown(fd, pw.pw_uid, pw.pw_gid)
                os.fchmod(fd, 0o700)
                for n in (name, stage):
                    try:
                        if os.stat(n, dir_fd=dst, follow_symlinks=False).st_ino == st.st_ino:
                            os.rename(n, base, src_dir_fd=dst, dst_dir_fd=pfd)
                            break
                    except OSError:
                        continue
                kr.take_tree(fd, pw.pw_uid, pw.pw_gid, team=False)
                drop_acl(real)
                os.fchmod(fd, old_mode)
                raise
        finally:
            os.close(dst)
        folders[name] = {"name": name, "box": box, "shortcut": real, "source": "", "separate": False}
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
    kr.say(f"{name} is now a team folder.", f"  your shortcut: {real} -> {km.team_path(person, name)}",
           f"  Next: cd {real} and run claude, codex, opencode or cursor-agent as usual. They start in the team box.",
           *[f"  {n}" for n in notes], *NOT_COVERED)


def add_git(person: str, url: str, name: str) -> None:
    url, host, port, default = kc.parse_git_url(url)
    name = kc.check_name(name or default)
    folders = kr.load_folders(person)
    if name in folders:
        raise Refuse(f"a team folder named {name} exists; pick another with --name")
    pw = km.person_home(person)
    hfd = os.open(".", os.O_RDONLY | os.O_DIRECTORY)
    try:
        here = km.fd_path(hfd)
    finally:
        os.close(hfd)
    if not kc.under(here, pw.pw_dir):
        raise Refuse("the new folder's shortcut is made in the current folder, so cd into a folder in your home "
                     "first (for example cd ~)")
    box = person
    pfd = kr.open_dir(here)
    try:
        if os.fstat(pfd).st_uid != pw.pw_uid:
            raise Refuse("the current folder must be yours: the shortcut goes there")
        if os.path.lexists(os.path.join(here, name)):
            raise Refuse(f"{os.path.join(here, name)} exists; pick another name with --name")
        buid, bgid, sgid = km.ids(box)
        allow_path = f"{kc.BOXES}/{box}/egress-allow"
        text, added = kc.allow_add(kr.read_text(allow_path), host, port)
        if added:
            kr.write_file(allow_path, text)
        top = km.team_path(person, name)
        kr.mkdir(top, 0o2770, buid, sgid)
        apply_acl(top, kc.share_group(box))
        try:
            km.repair_before_start(person, box)
            problems = km.outside_check(person, box)
            if problems:
                raise Refuse("self-check: " + "; ".join(problems))
            kr.say(f"Cloning {url} inside the box ...")
            p = km.popen_box(person, box, [km.python(), "-I", km.BOX_EXEC, "git", "-c", "protocol.allow=never",
                                           "-c", "protocol.https.allow=always", "-c", "protocol.ssh.allow=always",
                                           "clone", "--no-recurse-submodules", "--", url, "."],
                             top, stdin=subprocess.DEVNULL, start_new_session=True)
            km.record_chat(p.pid, person, box, "git", top)
            try:
                rc = p.wait(3600)
            finally:
                km.drop_chat(p.pid)
            if rc != 0:
                raise Refuse(f"git clone failed inside the box (exit {rc}); nothing was kept. Check the URL. For a "
                             "private repository use its git@host:path address and add the box's SSH key (hubchat "
                             "list shows it) to your git account")
            kr.write_file(kr.since_stamp(box, name), "", 0o600)
            os.symlink(top, name, dir_fd=pfd)
            os.chown(name, pw.pw_uid, pw.pw_gid, dir_fd=pfd, follow_symlinks=False)
        except BaseException:
            shutil.rmtree(top, ignore_errors=True)
            if added:
                kr.write_file(allow_path, kc.allow_remove(kr.read_text(allow_path), host, port))
            raise
        folders[name] = {"name": name, "box": box, "shortcut": os.path.join(here, name), "source": url,
                         "separate": False}
        kr.save_folders(person, folders)
    finally:
        os.close(pfd)
    kr.say(f"{name} is now a team folder, cloned inside the box.",
           f"  your shortcut: {os.path.join(here, name)} -> {top}",
           f"  the box may now reach {host}{':22' if port == 22 else ''}.",
           f"  Next: cd {os.path.join(here, name)} and run claude, codex, opencode or cursor-agent as usual.", *NOT_COVERED)


def find_entry(person: str, target: str, folders) -> dict:
    if target in folders:
        return folders[target]
    p = os.path.join(os.getcwd(), target)
    for cand in ([os.readlink(p)] if os.path.islink(p) else []) + [os.path.realpath(p)]:
        hit = km.team_folder(cand, person)
        if hit and not hit[1] and hit[0] in folders:
            return folders[hit[0]]
    raise Refuse(f"{kc.shown(target)} is not one of your team folders (run hubchat list)")


def return_place(person: str, entry: dict):
    pw = km.person_home(person)
    short = entry.get("shortcut") or ""
    where = km.team_path(person, entry["name"])
    if short and kc.under(short, pw.pw_dir) and short != pw.pw_dir:
        try:
            pfd = kr.open_dir(os.path.dirname(short))
            base = os.path.basename(short)
            if os.fstat(pfd).st_uid == pw.pw_uid:
                st = os.stat(base, dir_fd=pfd, follow_symlinks=False) if hc._lexists(pfd, base) else None
                if st is None:
                    return pfd, base
                if stat.S_ISLNK(st.st_mode) and os.readlink(base, dir_fd=pfd) == where:
                    os.unlink(base, dir_fd=pfd)
                    return pfd, base
            os.close(pfd)
        except (Refuse, OSError):
            pass
    pfd = kr.open_dir(pw.pw_dir)
    for n in [entry["name"]] + [f"{entry['name']}.from-team-{i}" for i in range(1, 100)]:
        if not hc._lexists(pfd, n):
            return pfd, n
    os.close(pfd)
    raise Refuse("no free name in your home to put the folder back")


def remove(person: str, target: str, uninstall: bool = False) -> None:
    folders = kr.load_folders(person)
    entry = find_entry(person, target, folders)
    name, box = entry["name"], entry["box"]
    pw = km.person_home(person)
    where = km.team_path(person, name)
    for rec in km.chats(box):
        if kc.under(rec.get("cwd", ""), where):
            km.stop_chat(rec)
    since = kr.read_since(box, name)
    fd = kr.open_dir(where)
    try:
        os.fchown(fd, 0, 0)
        os.fchmod(fd, 0o700)
        for _ in range(3):
            if km.kill_box_procs_in(box, where) == 0:
                break
            time.sleep(0.2)
        notes = kr.make_safe_for_git(fd)
        traps = kr.trap_files(fd, since, git=False)
        kr.take_tree(fd, pw.pw_uid, pw.pw_gid, team=False)
        drop_acl(where)
        src = kr.open_dir(km.team_root(person))
        try:
            pfd, base = return_place(person, entry)
            try:
                os.rename(name, base, src_dir_fd=src, dst_dir_fd=pfd)
                os.fchown(fd, pw.pw_uid, pw.pw_gid)
                os.fchmod(fd, 0o755)
                back = os.path.join(km.fd_path(pfd), base)
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
        if not any(f.get("source") and kc.parse_git_url(f["source"])[1:3] == (host, port) for f in folders.values()):
            path = f"{kc.BOXES}/{box}/egress-allow"
            kr.write_file(path, kc.allow_remove(kr.read_text(path), host, port))
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


def list_all(person: str) -> None:
    folders = kr.load_folders(person)
    box = person
    running = km.chats(box)
    kr.say(km.TEST_NOTICE, "", f"Team folders of {person} on this computer ({len(folders)}):")
    for name in sorted(folders):
        f = folders[name]
        where = km.team_path(person, name)
        inside = [f"{r.get('tool')} (pid {r.get('pid')})" for r in running if kc.under(r.get("cwd", ""), where)]
        kr.say(f"  {name}", f"    shortcut: {f['shortcut'] or '-'}", f"    place:    {where}",
               f"    size:     {hc.human(kr.tree_size(where))}", f"    box:      {kc.box_user(box)}",
               *([f"    from:     {f['source']}"] if f.get("source") else []),
               f"    chats:    {', '.join(inside) if inside else 'none running'}")
        try:
            fd = kr.open_dir(where)
            try:
                traps = kr.trap_files(fd, kr.read_since(box, name))
            finally:
                os.close(fd)
            if traps:
                kr.say("    WARNING: files that can run code when your own tools, editor, git or a build open them",
                       "    changed while this was a team folder (on macOS nothing hides them from you): "
                       + kr.trap_notice(traps, 20))
            found = [x for x in hc.scan_secrets_path(where) if x[2] != "link"]
            if found:
                kr.say("    WARNING: these look like secrets and team chats can read them:",
                       *["  " + ln for ln in hc.show_found(found, 10)])
        except (Refuse, OSError):
            pass
    kr.say(f"  {kc.MEMORY} (the shared team memory)    place: {km.team_path(person, kc.MEMORY)}")
    kr.say(*kap_update.status_lines())
    have = kap_tools.installed()
    kr.say("AI tools in the box: " + (", ".join(f"{t} {v}" for t, v in sorted(have.items())) or "none")
           + "  (hubchat update-tools for newer ones)")
    kr.say(f"Box {kc.box_user(box)}:")
    tok = f"{kc.SECRET}/{person}/claude-oauth-token"
    if os.path.exists(tok):
        end = time.strftime("%Y-%m-%d", time.localtime(os.stat(tok).st_mtime + 365 * 86400))
        kr.say(f"    Claude Code sign-in: a setup-token, ends about {end}")
    else:
        kr.say("    Claude Code sign-in: none")
    home = kc.box_home(box)
    for tool, p in (("Codex", "state/codex/auth.json"), ("Cursor", ".cursor/auth.json")):
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
    if kr.read_text(km.PROFILE_EXTRA).strip():
        kr.say(f"WARNING: {km.PROFILE_EXTRA} adds rules to the box's profile (root wrote them). They may open a wall.")
    km.repair_before_start(person, box)
    problems = km.outside_check(person, box)
    kr.say("Self-check: " + ("all walls in place (outside; the inside half runs at every chat start)"
                             if not problems else "PROBLEMS: team chats will not start"),
           *[f"  - {p}" for p in problems],
           *([f"Next: run  sudo {kc.INSTALLER} repair  and hubchat list again. If a problem stays, send these "
              "lines to your team admin."] if problems else []))


def token_rotate(person: str) -> None:
    tok = hc.read_new_token()
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
    for rec in km.chats(person):
        if rec.get("tool") == "claude":
            km.stop_chat(rec)
            ended += 1
    kr.say("The box has a new Claude token; the next Claude chat uses it.",
           f"  {ended} running Claude chat(s) were ended (they held the old token)." if ended else
           "  No Claude chat was running.", *kc.REVOKE_CLAUDE_TOKEN)


def main(argv) -> int:
    try:
        person = kr.caller_person()
        if not argv or argv[0] in ("help", "-h", "--help"):
            kr.say(*USAGE.splitlines())
            return 0
        if kc.unfinished_install():
            raise Refuse(kc.unfinished_install())
        cmd, rest = argv[0], argv[1:]
        opts = {"--name": "", "--yes": False, "--force": False}
        pos = []
        while rest:
            a = rest.pop(0)
            if a == "--name" and rest:
                opts["--name"] = rest.pop(0)
            elif a in ("--yes", "--force"):
                opts[a] = True
            elif a == "--separate":
                raise Refuse("separate boxes are not in the macOS test version yet: every team folder shares your box")
            elif a.startswith("-"):
                raise Refuse(f"unknown option {kc.shown(a)}. Usage: {SHORT.get(cmd, 'hubchat help')}")
            else:
                pos.append(a)
        if cmd == "add" and len(pos) == 1:
            if kc.looks_like_url(pos[0]):
                add_git(person, pos[0], opts["--name"])
            else:
                add_folder(person, pos[0], opts["--name"], opts["--yes"], opts["--force"])
        elif cmd == "remove" and len(pos) == 1 and not opts["--name"]:
            remove(person, pos[0])
        elif cmd == "list" and not pos:
            list_all(person)
        elif cmd == "token-rotate" and not pos and not any(opts.values()):
            token_rotate(person)
        elif cmd == "update-tools" and not any(opts.values()):
            return hc.update_tools(pos)
        elif cmd in SHORT:
            raise Refuse(f"usage: {SHORT[cmd]}")
        else:
            raise Refuse(f"there is no command {kc.shown(cmd)}. The commands are add, list and remove "
                         "(hubchat help shows them all)")
        return 0
    except BrokenPipeError:
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return 0
    except Refuse as e:
        print(kc.term(f"hubchat: {e}"), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
