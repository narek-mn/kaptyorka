"""The box of the secure design, slice 2 (src/kaptyorka/secure): the team-folder decision, input
checks of the root programs, the tool flags enter lets through, git URLs, the forwarder's allow
list and checks, the git config hubchat remove keeps, the tree hand-over without following
links, and wallcheck's doors. Everything that needs root, systemd or a second user runs in a
throw-away container (secure-design/testbox/slice2.sh), never here."""
import ipaddress
import json
import os
import shutil
import socket
import stat
import struct
import subprocess
import threading

import pytest

from kaptyorka.secure import kapcommon as kc
from kaptyorka.secure import kaproot as kr
from kaptyorka.secure import kap_box_exec as bx
from kaptyorka.secure import kap_egress as eg
from kaptyorka.secure import kap_enter as en
from kaptyorka.secure import kap_hubchat as hc
from kaptyorka.secure import kap_wallcheck as wc
from kaptyorka.secure import kap_setup as setup
from kaptyorka.secure import kap_addrwatch as aw

SECURE = os.path.dirname(kc.__file__)


# ---------------------------------------------------------------------------- the team-folder decision

@pytest.mark.parametrize("real,want", [
    ("/srv/team/alice/proj-a", ("proj-a", "")),
    ("/srv/team/alice/proj-a/src/x", ("proj-a", "src/x")),
    ("/srv/team/alice/_memory", ("_memory", "")),
    ("/srv/team/alice", None),
    ("/srv/team/alice/", None),
    ("/srv/team/bob/proj-a", None),
    ("/srv/team/alicex/proj-a", None),
    ("/home/alice/team/proj-a", None),
    ("/srv/team/alice/../bob/x", None),
    ("/srv/team/alice/proj-a/../../bob", None),
    ("/srv/team/alice/.hidden", None),
    ("/srv/team/alice/-flag", None),
    ("srv/team/alice/proj-a", None),
    ("/srv/team/alice/proj-a/a//b", None),
    ("/srv/team/alice/pro\0j", None),
])
def test_team_folder_by_real_path_only(real, want):
    assert kc.team_folder(real, "alice") == want


def test_team_folder_refuses_a_bad_person():
    with pytest.raises(kc.Refuse):
        kc.team_folder("/srv/team/x/y", "../x")


@pytest.mark.parametrize("name", ["proj-a", "P1", "a.b_c-d", "x" * 64])
def test_folder_names_ok(name):
    assert kc.check_name(name) == name


@pytest.mark.parametrize("name", ["", ".git", "-rf", "a/b", "..", "_memory", "a b", "x" * 65, "a\nb", "é", "x.lock", None])
def test_folder_names_refused(name):
    with pytest.raises(kc.Refuse):
        kc.check_name(name)


@pytest.mark.parametrize("name", ["root;", "Alice", "a" * 32, "1abc", "a b", "", "../x"])
def test_person_names_refused(name):
    with pytest.raises(kc.Refuse):
        kc.check_person(name)


def test_box_ids():
    assert kc.box_id("alice") == "alice"
    assert kc.box_id("alice", "proj-b") == "alice-proj-b"
    assert kc.box_user("alice-proj-b") == "kap-alice-proj-b"
    assert len(kc.box_user(kc.box_id("a" * 13, "b" * 14))) <= 32
    with pytest.raises(kc.Refuse):
        kc.box_id("alice", "a-very-long-project-name-x")
    with pytest.raises(kc.Refuse):
        kc.box_id("alice", "x y")


def test_unit_names_match_systemd_escape():
    for p in ("/srv/team/alice/proj-a", "/srv/team/alice/_memory", "/srv/team/alice/a.b_c", "/srv/team/a/.x"):
        want = subprocess.run(["systemd-escape", "--path", p], capture_output=True, text=True).stdout.strip() \
            if shutil.which("systemd-escape") else None
        if want:
            assert kc.unit_escape_path(p) == want
    assert kc.view_mount_unit("alice", "proj-a") == "srv-team-alice-proj\\x2da.mount"
    assert kc.slice_name("alice-proj-b") == "kapbox-alice\\x2dproj\\x2db.slice"


def test_mount_unit_has_the_four_flags():
    t = kc.mount_unit_text("alice", "proj-a", "/var/lib/kap-team/kap-alice/projects/proj-a")
    assert "Options=bind,nosymfollow,noexec,nosuid,nodev,private\n" in t
    assert "Where=/srv/team/alice/proj-a\n" in t
    with pytest.raises(kc.Refuse):
        kc.mount_unit_text("alice", "proj-a", "/var/lib/x y")


# ---------------------------------------------------------------------------- folders.json

def test_folders_round_trip_and_strict():
    f = {"proj-a": {"name": "proj-a", "box": "alice", "shortcut": "/home/alice/team/proj-a", "source": "",
                    "separate": False}}
    assert kc.parse_folders(kc.dump_folders(f)) == f
    assert kc.parse_folders("") == {}
    for bad in ({"name": "../x", "box": "alice"}, {"name": "a", "box": "Bad Box"},
                {"name": "a", "box": "alice", "shortcut": "relative"},
                {"name": "a", "box": "alice", "shortcut": "/home/alice/../root"},
                {"name": "a", "box": "alice", "source": "ext::sh -c id"}):
        with pytest.raises((kc.Refuse, KeyError)):
            kc.parse_folders(json.dumps({"folders": [bad]}))


# ---------------------------------------------------------------------------- the walls as one list

def test_box_props_carry_every_wall_and_the_slice0_changes():
    props = kc.box_props("alice", "alice", "60s")
    text = "\n".join(props)
    for line in ("TemporaryFileSystem=/:ro", "TemporaryFileSystem=/usr/local:ro", "BindReadOnlyPaths=" + kc.LIBEXEC,
                 "BindReadOnlyPaths=/sys/devices/system/cpu", "PrivateIPC=yes", "PrivateDevices=yes", "ProtectProc=invisible",
                 "TemporaryFileSystem=/sys/fs/cgroup:ro", "TemporaryFileSystem=/dev/shm:mode=1777",
                 "NetworkNamespacePath=/run/netns/kapbox-alice", "NoNewPrivileges=yes", "CapabilityBoundingSet=",
                 "RestrictNamespaces=yes", "SystemCallFilter=@pkey", "KillMode=control-group", "RuntimeMaxSec=60s",
                 "MemorySwapMax=0", "User=kap-alice", "SupplementaryGroups=kapshare-alice", "UMask=0007",
                 "ReadOnlyPaths=/var/lib/kap-team/kap-alice/home", "ProcSubset=pid"):
        assert line in props, line
    assert "ProtectControlGroups" not in text          # D1
    # measured on systemd 255: next to these, systemd drops TemporaryFileSystem=/ without a word
    assert "ProtectSystem" not in text and "DynamicUser" not in text and "RootDirectory" not in text
    tl = next(p for p in props if p.startswith("TemporaryFileSystem=/sys/block"))
    assert {x.split(":")[0] for x in tl.split("=", 1)[1].split()} == {"/sys/" + d for d in kc.SYS_HIDDEN}
    assert "PrivateUsers" not in text
    assert "/home" not in text.replace("/var/lib/kap-team/kap-alice/home", "")
    assert "BindPaths=/var/lib/kap-team/kap-alice/projects:/srv/team/alice" in props
    assert "BindPaths=/run/kaptyorka/relay/alice.sock /run/kapbox/alice/pub/egress.sock:/run/egress.sock" in props
    assert any(p.startswith("BindPaths=/run/kapbox/alice/tmp:/tmp ") for p in props)  # D4
    for src, dst in kc.LOCKED_FILES:
        assert f"/etc/kaptyorka/box/{src}:/var/lib/kap-team/kap-alice/home/{dst}" in text
    with pytest.raises(kc.Refuse):
        kc.box_props("alice", "alice", "60s; rm")


def test_separate_box_props_use_its_own_everything():
    text = "\n".join(kc.box_props("alice", "alice-proj-b"))
    assert "User=kap-alice-proj-b" in text and "kapshare-alice-proj-b" in text
    assert "/var/lib/kap-team/kap-alice-proj-b/projects:/srv/team/alice" in text
    assert "kap-alice/" not in text


def test_limits_file():
    assert kc.parse_limits("") == "7d"
    assert kc.parse_limits("runtime_max=90s\n") == "90s"
    with pytest.raises(kc.Refuse):
        kc.parse_limits("runtime_max=1; touch /x")


# ---------------------------------------------------------------------------- tool arguments

@pytest.mark.parametrize("tool,args", [
    ("claude", []), ("claude", ["fix the bug"]), ("claude", ["-c"]), ("claude", ["--resume", "abc-123"]),
    ("claude", ["-r"]), ("claude", ["-p", "--model", "opus", "say hi"]), ("claude", ["--model=sonnet", "x"]),
    ("claude", ["--", "--settings is just text here"]), ("claude", ["echo $HOME and %h"]),
    ("codex", ["resume", "--last"]), ("codex", ["exec", "fix it"]), ("codex", ["-m", "gpt-5"]),
    ("opencode", ["-c"]), ("opencode", ["run", "hello"]), ("opencode", ["-s", "ses_1"]),
    ("cursor-agent", ["-p", "hi"]), ("cursor-agent", ["--resume"]), ("cursor-agent", ["--resume", "id1"]),
    ("git", ["status"]), ("git", ["-c", "core.fsmonitor=x", "status"]),
])
def test_allowed_tool_args_pass_unchanged(tool, args):
    assert kc.filter_args(tool, args) == args


@pytest.mark.parametrize("tool,args", [
    ("claude", ["--settings", "/tmp/x.json"]), ("claude", ["--plugin-dir", "/home/alice/p"]),
    ("claude", ["--add-dir", "/home/alice"]), ("claude", ["--mcp-config", "x"]), ("claude", ["--dangerously-skip-permissions"]),
    ("claude", ["--model", "--settings"]), ("claude", ["--model"]), ("claude", ["mcp", "add", "x"]),
    ("claude", ["setup-token"]), ("claude", ["--permission-mode", "bypassPermissions"]),
    ("claude", ["--resume=../x"]), ("claude", ["--append-system-prompt", "x"]), ("claude", ["--settings=x"]),
    ("codex", ["-c", "sandbox=danger"]), ("codex", ["login"]), ("codex", ["--cd", "/home/alice"]),
    ("codex", ["app-server"]), ("codex", ["cloud"]), ("codex", ["--dangerously-bypass-approvals-and-sandbox"]),
    ("opencode", ["serve"]), ("opencode", ["auth", "login"]), ("opencode", ["--port", "4096"]), ("opencode", ["web"]),
    ("cursor-agent", ["login"]), ("cursor-agent", ["--api-key", "x"]), ("cursor-agent", ["mcp"]),
    ("bash", ["-c", "id"]), ("sh", []),
])
def test_refused_tool_args(tool, args):
    with pytest.raises(kc.Refuse):
        kc.filter_args(tool, args)


def test_subcommand_words_later_are_prompt_text():
    assert kc.filter_args("claude", ["please", "login", "to", "mcp"]) == ["please", "login", "to", "mcp"]


# ---------------------------------------------------------------------------- git URLs

@pytest.mark.parametrize("url,host,port,name", [
    ("https://git.example.com/team/proj-a.git", "git.example.com", 443, "proj-a"),
    ("https://git.example.com:443/team/proj-a", "git.example.com", 443, "proj-a"),
    ("ssh://git@github.com/org/repo.git", "github.com", 22, "repo"),
    ("git@gitlab.example.org:group/sub/repo.git", "gitlab.example.org", 22, "repo"),
])
def test_git_urls_ok(url, host, port, name):
    assert kc.parse_git_url(url) == (url, host, port, name)


@pytest.mark.parametrize("url", [
    "ext::sh -c touch% /tmp/pwned", "file:///home/alice/.ssh", "/home/alice/repo", "../repo",
    "--upload-pack=touch /tmp/x", "https://git.example.com/a/../../b", "https://user:pw@git.example.com/a",
    "https://git.example.com:8443/a", "http://git.example.com/a", "https://192.168.1.5/a",
    "https://localhost/a", "ssh://root@host.example/a", "git@host.example:-oProxyCommand=x",
    "https://git.example.com/a b", "https://git.example.com/a?x=1", "https://GIT.example.com/a",
    "git@host.example:.hidden/x", "https://git.example.com/", "fd::17", "https://git.example.com/a\n/b",
    "ssh://git@host.example:2222/a",
])
def test_hostile_git_urls_refused(url):
    with pytest.raises(kc.Refuse):
        kc.parse_git_url(url)


def test_url_or_folder():
    assert kc.looks_like_url("https://x.example/a") and kc.looks_like_url("git@x.example:a")
    assert not kc.looks_like_url("proj-a") and not kc.looks_like_url("./team/proj-a")


# ---------------------------------------------------------------------------- the allow list

def test_allow_list_parse_add_remove():
    text = "api.anthropic.com\n# comment\ngit.example.com:22  # git\nbad host\n127.0.0.1\nx.example:8080\n"
    assert kc.parse_allow(text) == {"api.anthropic.com": {443}, "git.example.com": {22}}
    t2, changed = kc.allow_add(text, "git.example.com", 443)
    assert changed and kc.parse_allow(t2)["git.example.com"] == {22, 443}
    assert kc.allow_add(t2, "git.example.com", 443) == (t2, False)
    t3 = kc.allow_remove(t2, "git.example.com", 22)
    assert kc.parse_allow(t3)["git.example.com"] == {443}
    with pytest.raises(kc.Refuse):
        kc.allow_add("", "evil host", 443)


def test_forwarder_rules():
    allow = eg.parse_allow("allowed.test\ngit.test:22\n")
    assert eg.decide("allowed.test", 443, allow) == ""
    assert eg.decide("allowed.test", 80, allow)
    assert eg.decide("git.test", 443, allow)
    assert eg.decide("other.test", 443, allow)
    import ipaddress as ip
    for a in ("127.0.0.1", "10.1.2.3", "172.17.0.1", "192.168.1.1", "100.64.0.1", "169.254.1.1", "::1",
              "fd00::1", "fe80::1", "224.0.0.1", "::ffff:127.0.0.1", "0.0.0.0"):
        assert eg.refused_address(ip.ip_address(a)), a
    assert eg.refused_address(ip.ip_address("198.51.100.7")) == ""
    assert eg.parse_connect(b"CONNECT allowed.test:443 HTTP/1.1\r\n\r\n") == ("allowed.test", 443)
    for bad in (b"GET / HTTP/1.1\r\n", b"CONNECT 1.2.3.4:443 HTTP/1.1\r\n", b"CONNECT [::1]:443 HTTP/1.1\r\n",
                b"CONNECT allowed.test HTTP/1.1\r\n", b"CONNECT a.test:443 FOO\r\n"):
        with pytest.raises(ValueError):
            eg.parse_connect(bad)


def test_forwarder_refuses_over_a_real_socket(tmp_path):
    allow = tmp_path / "allow"
    allow.write_text("localhost.test\n")
    fwd = eg.Forwarder(str(allow))
    for req, want in ((b"CONNECT notlisted.test:443 HTTP/1.1\r\n\r\n", b"not on the allow list"),
                      (b"CONNECT localhost.test:80 HTTP/1.1\r\n\r\n", b"not on the allow list"),
                      (b"CONNECT 127.0.0.1:443 HTTP/1.1\r\n\r\n", b"names only"),
                      (b"POST / HTTP/1.1\r\n\r\n", b"only CONNECT")):
        a, b = socket.socketpair()
        t = threading.Thread(target=fwd.handle, args=(b,))
        t.start()
        a.sendall(req)
        got = a.recv(400)
        t.join(5)
        a.close()
        assert got.startswith(b"HTTP/1.1 403") and want in got, got


# ---------------------------------------------------------------------------- git made safe

PLANTED = """[core]
\trepositoryformatversion = 0
\tfilemode = true
\tbare = false
\tfsmonitor = "echo pwned"
\thooksPath = /tmp/evil
\tsshCommand = sh -c id
\tpager = less; id
[remote "origin"]
\turl = https://git.example.com/team/proj-a.git
\tfetch = +refs/heads/*:refs/remotes/origin/*
\tuploadpack = sh -c id
[remote "evil"]
\turl = ext::sh -c id
[branch "main"]
\tremote = origin
\tmerge = refs/heads/main
[alias]
\tst = !sh -c id
[include]
\tpath = /tmp/x
[filter "lfs"]
\tclean = sh -c id
[credential]
\thelper = !sh -c id
[core.weird]
\tx = 1
[extensions]
\tobjectformat = sha256
\tworktreeConfig = true
"""


def test_safe_git_config_keeps_only_plain_keys():
    new, dropped = kc.safe_git_config(PLANTED)
    assert "fsmonitor" not in new and "hooksPath" not in new.lower() and "sshcommand" not in new.lower()
    assert "sh -c" not in new and "ext::" not in new and "include" not in new and "alias" not in new
    assert "\turl = https://git.example.com/team/proj-a.git" in new
    assert '[branch "main"]' in new and "\tmerge = refs/heads/main" in new
    assert "\tobjectformat = sha256" in new and "worktreeconfig" not in new.lower()
    assert any("fsmonitor" in d for d in dropped)
    again, _ = kc.safe_git_config(new)
    assert again == new


def test_safe_git_config_drops_tricks():
    for trick in ('[core]\n\tfsmonitor = x\\\n', '[core]\n\tbare = "true" ; x\n', '[remote "a\\"b"]\n\turl = https://x.example/a\n',
                  '[core]\n\trepositoryformatversion = 0 # c\n\tfsmonitor\n'):
        new, _ = kc.safe_git_config(trick)
        assert "fsmonitor" not in new and '\\"' not in new


def make_repo(top):
    g = top / ".git"
    (g / "hooks").mkdir(parents=True)
    (g / "objects").mkdir()
    (g / "HEAD").write_text("ref: refs/heads/main\n")
    (g / "config").write_text(PLANTED)
    (g / "hooks" / "pre-commit").write_text("#!/bin/sh\necho pwned\n")
    (g / "config.worktree").write_text("[core]\n\tfsmonitor = x\n")
    return g


def test_make_safe_for_git_every_repo_in_the_tree(tmp_path):
    make_repo(tmp_path)
    sub = tmp_path / "vendor" / "lib"
    sub.mkdir(parents=True)
    make_repo(sub)
    bare = tmp_path / "bare.git"
    (bare / "objects").mkdir(parents=True)
    (bare / "HEAD").write_text("ref: refs/heads/main\n")
    (bare / "config").write_text("[core]\n\tbare = true\n\tfsmonitor = x\n")
    (tmp_path / "wt").mkdir()
    (tmp_path / "wt" / ".git").write_text("gitdir: /home/alice/other/.git\n")
    fd = os.open(str(tmp_path), os.O_RDONLY | os.O_DIRECTORY)
    try:
        notes = kr.make_safe_for_git(fd)
    finally:
        os.close(fd)
    for cfg in (tmp_path / ".git/config", sub / ".git/config", bare / "config"):
        assert "fsmonitor" not in cfg.read_text()
    assert not (tmp_path / ".git/hooks").exists() and (tmp_path / ".git/hooks.from-team/pre-commit").exists()
    assert (tmp_path / ".git/config.worktree.from-team").exists()
    assert not (tmp_path / "wt/.git").exists() and (tmp_path / "wt/.git.from-team").exists()
    assert any("fsmonitor" in n for n in notes)


def test_git_refuses_the_planted_config_after_sanitising(tmp_path):
    if not shutil.which("git"):
        pytest.skip("no git")
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    canary = tmp_path / "ran"
    (tmp_path / ".git" / "config").write_text(PLANTED.replace("echo pwned", f"touch {canary}; false")
                                              .replace("objectformat = sha256", "objectformat = sha1"))
    fd = os.open(str(tmp_path), os.O_RDONLY | os.O_DIRECTORY)
    kr.make_safe_for_git(fd)
    os.close(fd)
    subprocess.run(["git", "-C", str(tmp_path), "status"], capture_output=True)
    assert not canary.exists()


# ---------------------------------------------------------------------------- handing a tree over

def test_take_tree_never_follows_and_copies_hard_links(tmp_path):
    outside = tmp_path / "outside-key"
    outside.write_text("KEY")
    top = tmp_path / "proj"
    (top / "sub").mkdir(parents=True)
    (top / "a.txt").write_text("a")
    os.chmod(top / "a.txt", 0o4755)
    os.link(outside, top / "sub" / "linked-key")
    os.symlink(str(outside), top / "link-out")
    os.mkfifo(top / "pipe")
    me, g = os.getuid(), os.getgid()
    fd = os.open(str(top), os.O_RDONLY | os.O_DIRECTORY)
    try:
        rep = kr.take_tree(fd, me, g, team=True)
    finally:
        os.close(fd)
    assert rep.copied == ["sub/linked-key"] and rep.removed == ["pipe"] and rep.stripped == ["a.txt"]
    assert os.stat(outside).st_nlink == 1 and (top / "sub" / "linked-key").read_text() == "KEY"
    assert os.lstat(top / "link-out").st_mode & stat.S_IFLNK
    assert stat.S_IMODE(os.stat(top / "a.txt").st_mode) == 0o770
    assert stat.S_IMODE(os.stat(top / "sub").st_mode) & 0o2000
    assert not (top / "pipe").exists()
    fd = os.open(str(top), os.O_RDONLY | os.O_DIRECTORY)
    try:
        kr.take_tree(fd, me, g, team=False)
    finally:
        os.close(fd)
    assert not stat.S_IMODE(os.stat(top / "sub").st_mode) & 0o2000


def test_default_acl_is_what_setfacl_writes(tmp_path):
    if not shutil.which("getfacl"):
        pytest.skip("no getfacl")
    fd = os.open(str(tmp_path), os.O_RDONLY | os.O_DIRECTORY)
    try:
        try:
            kr.set_default_acl(fd, os.getgid())
        except PermissionError:
            pytest.skip("no ACLs here")
    finally:
        os.close(fd)
    out = subprocess.run(["getfacl", "-n", "-d", str(tmp_path)], capture_output=True, text=True).stdout
    if "default:" not in out:
        pytest.skip("filesystem without ACLs")
    assert f"default:group:{os.getgid()}:rwx" in out and "default:other::---" in out
    assert struct.unpack("<I", kr.default_acl(5)[:4]) == (2,)


def test_open_dir_refuses_a_symlink_on_the_way(tmp_path):
    (tmp_path / "real").mkdir()
    os.symlink(str(tmp_path / "real"), tmp_path / "link")
    os.close(kr.open_dir(str(tmp_path / "real")))
    with pytest.raises(kc.Refuse):
        kr.open_dir(str(tmp_path / "link"))
    with pytest.raises(kc.Refuse):
        kr.open_dir("relative/path")


def _trap(top, **kw):
    fd = os.open(str(top), os.O_RDONLY | os.O_DIRECTORY)
    try:
        return kr.trap_files(fd, **kw)
    finally:
        os.close(fd)


def _put(top, rel, text="x"):
    p = top / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


def test_trap_files_find_code_files_at_any_depth_whoever_made_the_folder(tmp_path):
    """Red team round 3, finding 1: the box wrote .opencode/plugin/pwn.js inside a folder the
    person had made, and the old notice (only the direct entries of each tool folder, by owner)
    named nothing. Now every file in a tool folder, at any depth, the tool files anywhere, git's
    hooks and configs, and (build=True) Makefiles and package.json, are named."""
    for rel in (".opencode/plugin/pwn.js", ".cursor/rules/r.mdc", ".codex/prompts/p.md", ".vscode/sub/tasks.json",
                ".claude/hooks/h.sh", "sub/.opencode/agent/a.md", "sub/deep/opencode.json", ".envrc",
                ".git/hooks/pre-commit", ".git/hooks/pre-push.sample", ".git/config", "lib/.git/config",
                "Makefile", "web/package.json", "README.md", "src/main.py", ".git/objects/ab/cd",
                "node_modules/x/.vscode/settings.json", ".opencode/node_modules/p/index.js"):
        _put(tmp_path, rel)
    os.symlink("/etc", tmp_path / ".idea")
    got = _trap(tmp_path)
    assert got == sorted([".claude/hooks/h.sh", ".codex/prompts/p.md", ".cursor/rules/r.mdc", ".envrc",
                          ".git/config", ".git/hooks/pre-commit", ".opencode/node_modules/p/index.js",
                          ".opencode/plugin/pwn.js", ".vscode/sub/tasks.json", "Makefile", "lib/.git/config",
                          "sub/.opencode/agent/a.md", "sub/deep/opencode.json", "web/package.json"])
    assert "Makefile" not in _trap(tmp_path, build=False) and ".git/config" not in _trap(tmp_path, git=False)


def test_trap_files_count_only_what_changed_after_the_hand_over(tmp_path):
    _put(tmp_path, ".opencode/plugin/old.js")
    st = os.stat(tmp_path / ".opencode/plugin/old.js")
    since = max(st.st_ctime, st.st_mtime) + 0.001
    assert _trap(tmp_path, since=since) == []
    import time as _t
    _t.sleep(0.02)
    _put(tmp_path, ".opencode/plugin/pwn.js")                 # the box writes later
    assert _trap(tmp_path, since=since) == [".opencode/plugin/pwn.js"]
    os.chmod(tmp_path / ".opencode/plugin/old.js", 0o644)      # any change moves the ctime
    assert ".opencode/plugin/old.js" in _trap(tmp_path, since=since)


def test_trap_files_are_bounded_and_say_so(tmp_path, monkeypatch):
    for i in range(30):
        _put(tmp_path, f".vscode/f{i:02}.json")
    monkeypatch.setattr(kc, "TRAP_MAX", 10)
    got = _trap(tmp_path)
    assert len(got) == 11 and got[-1] == "..."
    line = kr.trap_notice(got, most=3)
    assert line.startswith("'.vscode/f00.json', '.vscode/f01.json', '.vscode/f02.json' and ") and line.endswith("more+")
    deep = tmp_path / "d"
    p = deep
    for i in range(kc.TRAP_DEPTH + 3):
        p = p / "a"
    _put(p, ".envrc")
    assert not any(x.endswith(".envrc") for x in _trap(tmp_path))


def test_trap_notice_shows_names_safely():
    line = kr.trap_notice([".opencode/plugin/\x1b]0;x\x07.js"])
    assert "\x1b" not in line and "\x07" not in line and line.startswith("'.opencode/plugin/")


def test_hides_wanted_only_real_tool_folders_and_files_at_the_top(tmp_path):
    for rel in (".opencode/plugin/p.js", "opencode.json", ".mcp.json", ".git/hooks/pre-commit", "sub/.claude/x"):
        _put(tmp_path, rel)
    (tmp_path / ".envrc").mkdir()                    # a folder with a file's name: not a file cover
    os.symlink("/etc", tmp_path / ".claude")         # a link: not followed in the view (nosymfollow)
    fd = os.open(str(tmp_path), os.O_RDONLY | os.O_DIRECTORY)
    try:
        want = kr.hides_wanted(fd)
    finally:
        os.close(fd)
    assert want == {".opencode": "dir", "opencode.json": "file", ".mcp.json": "file", ".git/hooks": "dir"}


MI = """22 1 8:1 / / rw shared:1 - ext4 /dev/sda1 rw
50 22 8:1 /var/lib/kap-team/kap-alice/projects/proj-a {view} rw,nosuid,nodev,noexec,nosymfollow {tag} - ext4 /dev/sda1 rw
{extra}"""


def _hide_env(tmp_path, monkeypatch, extra="", tag="shared:7"):
    monkeypatch.setattr(kc, "VIEW", str(tmp_path / "srv"))
    monkeypatch.setattr(kc, "TEAM", str(tmp_path / "team"))
    back = tmp_path / "team" / "kap-alice" / "projects" / "proj-a"
    back.mkdir(parents=True)
    view = tmp_path / "srv" / "alice" / "proj-a"
    view.parent.mkdir(parents=True)
    os.symlink(back, view)       # stands in for the bind: the same folder
    calls = []
    monkeypatch.setattr(kr, "_mount", lambda src, target, fstype, flags, data=None:
                        calls.append(("mount", src, os.path.basename(os.readlink(target)) if target.startswith("/proc/")
                                      else target, fstype, flags, data)))
    monkeypatch.setattr(kr, "_umount", lambda target: calls.append(("umount", target)))
    monkeypatch.setattr(kr, "person_home", lambda p: type("pw", (), {"pw_uid": 1001, "pw_gid": 1001})())
    monkeypatch.setattr(kr, "open_dir", lambda p: os.open(p, os.O_RDONLY | os.O_DIRECTORY))
    mi = kr.mounts(MI.format(view=str(view), tag=tag, extra=extra))
    return back, str(view), mi, calls


def test_hide_view_makes_the_view_private_then_covers_each_tool_folder_and_file(tmp_path, monkeypatch):
    back, view, mi, calls = _hide_env(tmp_path, monkeypatch)
    _put(back, ".opencode/plugin/pwn.js")
    _put(back, "opencode.json", '{"plugin": ["./evil.js"]}')
    _put(back, ".envrc")
    done = kr.hide_view("alice", "proj-a", "alice", mi)
    assert calls[0] == ("mount", None, view, None, kr.MS_PRIVATE, None)
    mounted = {c[2]: c for c in calls[1:]}
    assert mounted[".opencode"][1:4] == (kc.HIDE_TMPFS, ".opencode", "tmpfs")
    assert "uid=1001,gid=1001" in mounted[".opencode"][5] and "mode=0700" in mounted[".opencode"][5]
    assert mounted[".opencode"][4] & (kr.MS_NOEXEC | kr.MS_NOSUID | kr.MS_NODEV) == kr.MS_NOEXEC | kr.MS_NOSUID | kr.MS_NODEV
    assert mounted["opencode.json"][1] == kc.HIDE_JSON and mounted[".envrc"][1] == kc.HIDE_FILE
    assert done == ["private", "+.envrc", "+.opencode", "+opencode.json"]


def test_hide_view_uncovers_what_the_box_renamed_and_keeps_what_is_covered(tmp_path, monkeypatch):
    extra = ("60 50 0:40 / {v}/.opencode rw - tmpfs kaptyorka-hide rw\n"
             "61 50 0:41 / {v}/.opencode.old rw - tmpfs kaptyorka-hide rw\n")
    back, view, _, calls = _hide_env(tmp_path, monkeypatch, tag="")
    mi = kr.mounts(MI.format(view=view, tag="", extra=extra.format(v=view)))
    _put(back, ".opencode/plugin/x.js")
    _put(back, ".opencode.old/plugin/pwn.js")
    done = kr.hide_view("alice", "proj-a", "alice", mi)
    assert calls == [("umount", view + "/.opencode.old")] and done == ["-.opencode.old"]
    assert kr.hidden_problem("alice", "proj-a", "alice", mi) is None
    _put(back, ".claude/settings.json")
    assert "'.claude' not covered" in kr.hidden_problem("alice", "proj-a", "alice", mi)


def test_hidden_problem_names_a_shared_view(tmp_path, monkeypatch):
    back, view, mi, _ = _hide_env(tmp_path, monkeypatch)
    assert "is shared" in kr.hidden_problem("alice", "proj-a", "alice", mi)


def test_mountinfo_names_with_spaces():
    m = kr.mounts("5 1 0:1 / /srv/team/a/p/x\\040y rw - tmpfs t rw\n")
    assert m[0]["point"] == "/srv/team/a/p/x y"


def test_take_tree_hands_every_folder_to_the_box_even_tool_folders(tmp_path, monkeypatch):
    """(round 3, d) hubchat add leaves no folder of the person's inside a team folder: the case
    the attacker used, a .opencode/plugin the person made before, goes to the box too."""
    for rel in (".opencode/plugin/ok.js", ".cursor/rules/r.mdc", "a/b/c/d.txt"):
        _put(tmp_path, rel)
    owned = {}
    real_fchown = os.fchown
    monkeypatch.setattr(os, "fchown", lambda fd, u, g: owned.__setitem__(os.readlink(f"/proc/self/fd/{fd}"), u))
    fd = os.open(str(tmp_path), os.O_RDONLY | os.O_DIRECTORY)
    try:
        kr.take_tree(fd, 4242, os.getgid(), team=True)
    finally:
        os.close(fd)
    monkeypatch.setattr(os, "fchown", real_fchown)
    every = {str(p) for p in tmp_path.rglob("*")}
    assert every == set(owned) and set(owned.values()) == {4242}


def test_signin_values_take_the_secrets_of_each_tool_s_file():
    codex = json.dumps({"OPENAI_API_KEY": None, "tokens": {"id_token": "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJ4In0.sig12345abc",
                                                            "access_token": "AccEss9Token8Value7XyZ", "refresh_token": "rt_9QwErTyUiOp1234"},
                        "last_refresh": "2026-11-26T10:00:00.123456Z"})
    assert kc.signin_values(codex) == ["rt_9QwErTyUiOp1234", "AccEss9Token8Value7XyZ",
                                       "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJ4In0.sig12345abc"] or \
        set(kc.signin_values(codex)) == {"rt_9QwErTyUiOp1234", "AccEss9Token8Value7XyZ",
                                         "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJ4In0.sig12345abc"}
    oc = json.dumps({"opencode": {"type": "api", "key": "CANARY-BOX-OPENCODE-AUTH-0c0c"},
                     "anthropic": {"type": "oauth", "refresh": "ref-Ab12Cd34Ef56Gh78", "expires": 1764000000000}})
    assert set(kc.signin_values(oc)) == {"CANARY-BOX-OPENCODE-AUTH-0c0c", "ref-Ab12Cd34Ef56Gh78"}
    key = "-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXktdjEAAAAA\nBBBBccccDDDD\n-----END OPENSSH PRIVATE KEY-----\n"
    assert kc.signin_values(key) == ["b3BlbnNzaC1rZXktdjEAAAAABBBBccccDDDD"]
    for plain in ("https://api.openai.com/v1/xyz123ABC", "alice@example.com", "/home/x/abcdefABCDEF123",
                  "aaaaaaaaaaaaaaaaaaaa", "short1A"):
        assert not kc.secret_like(plain)


def test_write_box_secrets_keeps_new_values_first_and_old_ones(tmp_path, monkeypatch):
    monkeypatch.setattr(kc, "TEAM", str(tmp_path / "team"))
    monkeypatch.setattr(kc, "BOX_SECRETS", str(tmp_path / "run" / "box-secrets"))
    monkeypatch.setattr(kc, "SECRET", str(tmp_path / "secret"))
    monkeypatch.setattr(kr, "gid_of", lambda g: os.getgid())
    monkeypatch.setattr(kr, "mkdir", lambda p, mode, uid=0, gid=0: os.makedirs(p, exist_ok=True))
    (tmp_path / "run").mkdir()
    home = tmp_path / "team" / "kap-alice" / "home"
    _put(home, "state/codex/auth.json", json.dumps({"tokens": {"refresh_token": "rt_FIRST9value8XyZ"}}))
    os.symlink("/etc/shadow", home / ".cursor")      # a link the box made: never followed
    _put(tmp_path / "secret" / "alice", "claude-oauth-token", "sk-ant-oat01-ABCdef123456789\n")
    monkeypatch.setattr(kr, "open_dir", lambda p: os.open(p, os.O_RDONLY | os.O_DIRECTORY))
    assert kr.write_box_secrets("alice", "alice") == 2
    _put(home, "state/codex/auth.json", json.dumps({"tokens": {"refresh_token": "rt_SECOND9value8XyZ"}}))
    assert kr.write_box_secrets("alice", "alice") == 3
    f = tmp_path / "run" / "box-secrets" / "alice"
    assert json.loads(f.read_text())["values"] == ["rt_SECOND9value8XyZ", "sk-ant-oat01-ABCdef123456789",
                                                   "rt_FIRST9value8XyZ"]
    assert stat.S_IMODE(f.stat().st_mode) == 0o640


# ---------------------------------------------------------------------------- hubchat add checks

def test_secret_names():
    for n in (".env", ".env.local", "id_ed25519", "id_rsa", "server.pem", "tls.key", ".npmrc", ".netrc",
              "credentials.json", "auth.json", ".git-credentials", "terraform.tfstate"):
        assert kc.is_secret_name(n), n
    for n in ("id_ed25519.pub", "README.md", "keys.py", "environment.yml", "monkey.txt"):
        assert not kc.is_secret_name(n), n


def test_scan_secrets_walks_without_links(tmp_path):
    (tmp_path / "a" / ".git" / "objects").mkdir(parents=True)
    (tmp_path / "a" / ".env").write_text("X=1")
    (tmp_path / "a" / ".git" / "objects" / "id_rsa").write_text("not scanned")
    os.symlink(os.path.expanduser("~"), tmp_path / "home-link")
    fd = os.open(str(tmp_path), os.O_RDONLY | os.O_DIRECTORY)
    try:
        assert [(p, k) for p, _, k in hc.scan_secrets(fd)] == [("a/.env", "name")]
    finally:
        os.close(fd)


FAKE = {   # made-up values in each shape; none is a real secret
    "a private key": b"-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaA==\n",
    "rsa": b"x\n-----BEGIN RSA PRIVATE KEY-----\n",
    "pgp": b"-----BEGIN PGP PRIVATE KEY BLOCK-----\n",
    "anthropic": b"token: sk-ant-oat01-" + b"a" * 40,
    "github": b"ghp_" + b"A" * 36, "github_pat": b"github_pat_" + b"B" * 40,
    "gitlab": b"glpat-" + b"c" * 20, "aws": b"key AKIAABCDEFGHIJKLMNOP end", "slack": b"xoxb-1234567890-abc",
    "google": b"AIza" + b"d" * 35, "hf": b"hf_" + b"e" * 34, "npm": b"//registry/:_authToken=npm_" + b"f" * 36,
    "json": b'{"claudeAiOauth": {"accessToken": "CANARY-CLAUDE-TOKEN-c0de"}}',
    "json2": b'{"client_secret": "abcdef123456"}',
    "env": b"# settings\nexport GITHUB_TOKEN=abcd1234efgh\n", "env2": b"DB_PASSWORD='hunter2hunter2'\n",
}


@pytest.mark.parametrize("name", sorted(FAKE))
def test_secret_in_content_finds_each_shape(name):
    assert kc.secret_in_content(FAKE[name]), name


@pytest.mark.parametrize("text", [
    b"# README\nNo secrets here. Use $GITHUB_TOKEN from your shell.\n",
    b"max_tokens = 100000000\ntoken_count = 12345678\n",       # code, not an environment file
    b"API_KEY=\nTOKEN=${TOKEN}\nPASSWORD=$PASS\n",            # empty or a variable
    b'{"password": ""}', b"ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAI user@host\n",   # a public key
    b"\0\x01-----BEGIN OPENSSH PRIVATE KEY-----",             # binary-looking: not looked into
])
def test_secret_in_content_leaves_plain_files(text):
    assert kc.secret_in_content(text) is None


def test_scan_secrets_reads_contents_and_names_hard_links(tmp_path):
    (tmp_path / "keys").mkdir()
    (tmp_path / "keys" / "hardlink-to-key").write_bytes(FAKE["a private key"])
    os.link(tmp_path / "keys" / "hardlink-to-key", tmp_path / "elsewhere")          # nlink 2
    (tmp_path / "notes.txt").write_bytes(FAKE["json"])
    (tmp_path / "big.log").write_bytes(b"x" * (kc.CONTENT_MAX_SIZE + 1) + FAKE["anthropic"])   # over the cap
    (tmp_path / "late.txt").write_bytes(b"x" * kc.CONTENT_SAMPLE + FAKE["anthropic"])          # past the sample
    (tmp_path / "plain.md").write_text("hello")
    os.symlink(tmp_path / "notes.txt", tmp_path / "link-to-notes")                  # never followed
    os.mkfifo(tmp_path / "pipe")                                                    # never opened
    fd = os.open(str(tmp_path), os.O_RDONLY | os.O_DIRECTORY)
    try:
        found = hc.scan_secrets(fd)
    finally:
        os.close(fd)
    got = {(p, k) for p, _, k in found}
    assert got == {("keys/hardlink-to-key", "content"), ("keys/hardlink-to-key", "link"),
                   ("elsewhere", "content"), ("elsewhere", "link"), ("notes.txt", "content")}
    assert any(why == hc.LINKED for _, why, _ in found)
    assert any("private key" in why for p, why, _ in found if p == "keys/hardlink-to-key")
    assert all(ln.startswith("  ") and ": " in ln for ln in hc.show_found(found))


def test_synced_folders():
    assert kc.in_synced_folder(["Dropbox", "work"]) == "Dropbox"
    assert kc.in_synced_folder(["Nextcloud", "x"]) and kc.in_synced_folder(["OneDrive - Org", "x"])
    assert kc.in_synced_folder(["work", "proj-a"]) is None


def test_safe_directory_warnings():
    shorts = ["/home/alice/team/proj-a"]
    w = lambda v: kc.safe_directory_warnings([("/home/alice/.gitconfig", v)], shorts)
    assert w("*") and w("/srv/team/alice/proj-a") and w("/srv/team/*") and w("/home/alice/team/proj-a")
    assert w("/var/lib/kap-team/kap-alice/projects/proj-a") and w("/home/alice/*") and w("/*")
    assert not w("/home/alice/personal") and not w("/opt/x/*")


# ---------------------------------------------------------------------------- the programs' own pieces

def test_box_exec_plan_per_tool():
    home = "/var/lib/kap-team/kap-alice/home"
    prog, argv, env = bx.plan("claude", ["-c"], {"PATH": "/usr/bin"}, home, "alice", "tok")
    assert prog == "/opt/kap-tools/bin/claude" and argv[-1] == "-c" and "--strict-mcp-config" in argv
    assert "/etc/kaptyorka/boxes/alice/claude-settings.json" in argv
    assert env["CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR"] == "3" and "tok" not in env.values()
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env and "CLAUDE_CODE_SUBPROCESS_ENV_SCRUB" not in env
    assert env["CLAUDE_CONFIG_DIR"] == home + "/state/claude"
    _, _, env = bx.plan("claude", [], {}, home, "alice", "")
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env and "CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR" not in env
    assert bx.plan("codex", [], {}, home, "alice", "x")[2]["CODEX_HOME"] == home + "/state/codex"
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in bx.plan("codex", [], {}, home, "alice", "x")[2]
    assert bx.plan("opencode", [], {}, home, "alice", "")[2]["XDG_CONFIG_HOME"] == home + "/state/opencode/cfg"
    assert bx.plan("cursor-agent", [], {}, home, "alice", "")[2]["AGENT_CLI_CREDENTIAL_STORE"] == "file"
    assert bx.plan("git", ["status"], {}, home, "alice", "")[1] == ["/usr/bin/git", "status"]
    with pytest.raises(SystemExit):
        bx.plan("bash", [], {}, home, "alice", "")


def test_box_exec_reads_and_closes_the_token_fd():
    r, w = os.pipe()
    os.write(w, b"sk-ant-oat01-test\n")
    os.close(w)
    saved = os.dup(3) if _fd_open(3) else None
    try:
        os.dup2(r, 3)
        env = {"LISTEN_FDS": "1", "LISTEN_PID": str(os.getpid()), "LISTEN_FDNAMES": "claude-token"}
        assert bx.take_token(env) == "sk-ant-oat01-test"
        assert not _fd_open(3) and "LISTEN_FDS" not in env
    finally:
        os.close(r)
        if saved is not None:
            os.dup2(saved, 3)
            os.close(saved)


def test_box_exec_hands_the_token_only_to_an_unreadable_claude(tmp_path):
    prog = tmp_path / "claude"
    prog.write_bytes(b"\x7fELF")
    prog.chmod(0o111)              # what 0711 root-owned is for the box user: execute only
    if os.geteuid() != 0:          # root reads everything; the box user is never root
        assert bx.token_readable_problem(str(prog)) == ""
    prog.chmod(0o755)
    assert "can be read" in bx.token_readable_problem(str(prog))
    os.symlink(prog, tmp_path / "link")
    assert "can be read" in bx.token_readable_problem(str(tmp_path / "link"))


def test_box_exec_hands_the_token_on_a_socket_read_once():
    fd = 60
    bx.hand_token("sk-ant-oat01-test", fd)
    try:
        assert stat.S_ISSOCK(os.fstat(fd).st_mode) and os.get_inheritable(fd)
        assert os.read(fd, 100) == b"sk-ant-oat01-test\n"
        assert os.read(fd, 100) == b""          # nothing left for a command that inherits it
    finally:
        os.close(fd)


def _fd_open(fd):
    try:
        os.fstat(fd)
        return True
    except OSError:
        return False


def test_enter_builds_pty_or_pipe(monkeypatch):
    monkeypatch.setattr(kr, "runtime_max", lambda: "60s")
    monkeypatch.setattr(kr, "host_addrs", lambda: ["203.0.113.5"])
    monkeypatch.setattr(kr, "shared_canaries", lambda: ["/tmp/kaptyorka-wall-canary-0123456789abcdef"])
    monkeypatch.setattr(en.os.path, "isfile", lambda p: p.endswith("claude-oauth-token"))
    c = en.build("alice", "alice", "claude", ["-c"], "/srv/team/alice/proj-a", True, "xterm-256color")
    assert "--pty" in c and "--expand-environment=no" in c and "--pipe" not in c
    assert "OpenFile=/etc/kaptyorka/secret/alice/claude-oauth-token:claude-token:read-only" in c
    assert "Environment=TERM=xterm-256color" in c
    i = c.index("--")
    assert c[i + 1:i + 3] == [kc.WALLCHECK, "--box"] and c[-3:] == [kc.BOX_EXEC, "claude", "-c"]
    assert c[i + 6:i + 8] == ["--addrs", "203.0.113.5"]
    assert c[i + 8:i + 10] == ["--canaries", "/tmp/kaptyorka-wall-canary-0123456789abcdef"]
    c = en.build("alice", "alice", "git", ["status"], "/srv/team/alice/proj-a", False, "x;y")
    assert "--pipe" in c and "--wait" in c and not any(x.startswith("OpenFile") for x in c)
    assert not any("TERM=" in x for x in c)


def test_wallcheck_usage_is_strict():
    with pytest.raises(SystemExit):
        wc.parse(["--box", "alice", "--", "/bin/true"])
    with pytest.raises(SystemExit):
        wc.parse(["--box", "../x", "--person", "alice", "--", "/bin/true"])
    opts, cmd = wc.parse(["--box", "alice", "--person", "alice", "--addrs", "1.2.3.4,", "--", "/bin/true", "x"])
    assert opts["addrs"] == ["1.2.3.4"] and cmd == ["/bin/true", "x"]
    c = "/tmp/kaptyorka-wall-canary-0123456789abcdef,/dev/shm/kaptyorka-wall-canary-fedcba9876543210"
    opts, _ = wc.parse(["--box", "alice", "--person", "alice", "--canaries", c, "--", "/bin/true"])
    assert opts["canaries"] == c.split(",")
    for bad in ("/etc/shadow", "/tmp/kaptyorka-wall-canary", "/tmp/../etc/kaptyorka-wall-canary-0123456789abcdef"):
        with pytest.raises(SystemExit):
            wc.parse(["--box", "alice", "--person", "alice", "--canaries", bad, "--", "/bin/true"])


def test_wallcheck_view_is_an_exact_list(tmp_path):
    (tmp_path / "usr").mkdir()
    spec = [(str(tmp_path), {"usr"}, {"lib64"})]
    assert wc.view_problem(spec) == ""
    (tmp_path / "lib64").mkdir()
    assert wc.view_problem(spec) == ""
    (tmp_path / "kaptest-extra").mkdir()           # an extra folder: the empty root was dropped
    assert "shows kaptest-extra" in wc.view_problem(spec)
    assert "lacks etc" in wc.view_problem([(str(tmp_path), {"usr", "etc"}, {"lib64", "kaptest-extra"})])
    assert "cannot be listed" in wc.view_problem([(str(tmp_path / "none"), set(), set())])
    v = dict((d, need | may) for d, need, may in wc.view("alice", "alice"))
    assert "home" not in v["/"] and "mnt" not in v["/"] and v["/srv/team"] == {"alice"}
    assert v["/run/kaptyorka/relay"] == {"alice.sock"} and v["/usr/local"] == {"libexec"}
    assert wc.SYS_HIDDEN == kc.SYS_HIDDEN == eg.SYS_HIDDEN


def test_canary_files_are_never_a_link_or_a_file_made_first(tmp_path):
    good = tmp_path / "c"
    kr._make_canary(str(good))
    assert kc.CANARY_MARK in good.read_bytes() and oct(good.stat().st_mode & 0o777) == "0o644"
    with pytest.raises(FileExistsError):           # someone made the name first: never taken
        kr._make_canary(str(good))
    link = tmp_path / "l"
    link.symlink_to(good)
    with pytest.raises(FileExistsError):
        kr._make_canary(str(link))
    assert not kr.canary_ok(str(link))             # a link to a canary is not one
    assert not kr.canary_ok(str(good))             # not root's (tests run as a user)
    assert not kr.canary_ok(str(tmp_path / "none"))
    fifo = tmp_path / "f"
    os.mkfifo(fifo)
    assert not kr.canary_ok(str(fifo))             # opened without blocking, refused
    for p in ("/tmp/kaptyorka-wall-canary-0123456789abcdef", "/dev/shm/kaptyorka-wall-canary-0123456789abcdef"):
        assert kc.CANARY_SHARED_RX.fullmatch(p)
    assert not kc.CANARY_SHARED_RX.fullmatch("/tmp/kaptyorka-wall-canary")
    for p in kc.CANARY_FILES:                      # fixed names only where only root can write
        assert os.path.dirname(p) in ("/", "/home", "/run/kaptyorka")


def test_forwarder_viewcheck(tmp_path):
    (tmp_path / "etc").mkdir()
    assert eg.view_problem([(str(tmp_path), {"etc"})]) == ""
    (tmp_path / "home").mkdir()
    assert eg.view_problem([(str(tmp_path), {"etc"})]) == f"{tmp_path} shows home"
    assert eg.main(["egress.py", "--viewcheck"]) == 1
    assert eg.main(["egress.py", "alice", "relative/path"]) == 2


def test_wallcheck_relay_door(tmp_path):
    from kaptyorka.relay import relay
    path = str(tmp_path / "relay.sock")
    s = socket.socket(socket.AF_UNIX)
    s.bind(path)
    s.listen(4)
    answers = [b'{"ok": false, "error": "wallcheck: unknown call \'wallcheck\'"}\n',
               b'{"ok": false, "error": "refused: you are not this box\'s user"}\n',
               json.dumps(relay.BUSY).encode() + b"\n"]   # the same text as the relay's

    def serve():
        for a in answers:
            c, _ = s.accept()
            c.recv(200)
            c.sendall(a)
            c.close()
    t = threading.Thread(target=serve)
    t.start()
    assert wc.relay_answers(path) == ""
    assert "refused" in wc.relay_answers(path)
    assert wc.relay_answers(path) == ""   # the relay's own "busy": its door answers
    t.join(5)
    s.close()
    assert wc.relay_answers(str(tmp_path / "none.sock"))


def test_wallcheck_needs_every_managed_tool_file(tmp_path):
    """W17 lists every file the package installs into /etc/kaptyorka/box, plus the two system
    places they are bound to (/etc/opencode, /etc/codex); W2 allows nothing else in those."""
    files = os.path.join(os.path.dirname(kc.__file__), "files", "box")
    shipped = set()
    for dirpath, _, names in os.walk(files):
        for n in names:
            shipped.add(os.path.relpath(os.path.join(dirpath, n), files))
    want = {f"{wc.BOXCONF}/{r}" for r in shipped}
    want |= {"/etc/opencode/" + os.path.relpath(r, "opencode/etc") for r in shipped if r.startswith("opencode/etc/")}
    want.add("/etc/codex/requirements.toml")
    assert wc.BOXCONF == kc.BOXCONF and set(wc.MANAGED) == want
    v = {d: need | may for d, need, may in wc.view("alice", "alice")}
    assert v["/etc/opencode"] == {"opencode.json", "plugins"} and v["/etc/opencode/plugins"] == {"kaptyorka.js"}
    assert v["/etc/codex"] == {"requirements.toml"}
    good, empty, d = tmp_path / "good", tmp_path / "empty", tmp_path / "dir"
    good.write_text("{}")
    empty.write_text("")
    d.mkdir()
    assert wc.managed_problem([str(good)]) == ""
    assert "is empty" in wc.managed_problem([str(good), str(empty)])
    assert "is not a file" in wc.managed_problem([str(d)])
    assert "ENOENT" in wc.managed_problem([str(tmp_path / "none")])


def test_wallcheck_constants_match_kapcommon():
    assert wc.CANARY_FILES == kc.CANARY_FILES and wc.CANARY_SHM_KEY == kc.CANARY_SHM_KEY
    assert wc.CANARY_SHARED_RX.pattern == kc.CANARY_SHARED_RX.pattern
    assert wc.CANARY_ABSTRACT == kc.CANARY_ABSTRACT and wc.CANARY_PROC.decode() == kc.CANARY_PROC
    assert (wc.CANARY_TCP_LO, wc.CANARY_TCP_ANY) == (kc.CANARY_TCP_LO, kc.CANARY_TCP_ANY)
    assert set(wc.LOCKED) == {dst for _, dst in kc.LOCKED_FILES}


def test_launcher_runs_the_personal_tool_outside_team_folders(tmp_path):
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    (bin_ / "claude").write_text("#!/bin/sh\necho PERSONAL \"$@\"\n")
    (bin_ / "sudo").write_text("#!/bin/sh\necho SUDO \"$@\"\n")
    for f in bin_.iterdir():
        f.chmod(0o755)
    launch = os.path.join(SECURE, "files", "launch")
    env = {"PATH": f"{bin_}:/usr/bin:/bin"}
    r = subprocess.run(["sh", launch, "claude", "a b"], cwd=str(tmp_path), env=env, capture_output=True, text=True)
    assert r.stdout.strip() == "PERSONAL a b"
    r = subprocess.run(["sh", launch, "bash"], cwd=str(tmp_path), env=env, capture_output=True, text=True)
    assert r.returncode == 2


def _launch_copy(tmp_path, team):
    """The real launcher with its team area at `team` (the real one is /srv/team, root's)."""
    text = open(os.path.join(SECURE, "files", "launch")).read()
    assert text.count('team="/srv/team/$me/"') == 1
    p = tmp_path / "launch"
    p.write_text(text.replace('team="/srv/team/$me/"', f'team="{team}/$me/"'))
    return str(p)


def test_launcher_refuses_a_team_folder_named_in_the_arguments(tmp_path):
    """Round 3 (a): `opencode ~/team/x` or `codex -C ~/team/x` from outside would load that
    folder's plugins and settings as the person."""
    me = subprocess.run(["id", "-un"], capture_output=True, text=True).stdout.strip()
    team = tmp_path / "srv"
    (team / me / "proj-a" / "sub").mkdir(parents=True)
    home = tmp_path / "home"
    home.mkdir()
    os.symlink(team / me / "proj-a", home / "proj-a")      # the shortcut hubchat leaves
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    for t in ("opencode", "codex", "sudo"):
        (bin_ / t).write_text(f"#!/bin/sh\necho {t.upper()} \"$@\"\n")
        (bin_ / t).chmod(0o755)
    launch = _launch_copy(tmp_path, team)
    env = {"PATH": f"/usr/local/libexec/kaptyorka/shims:{bin_}:/usr/bin:/bin"}
    run = lambda *a, cwd=home: subprocess.run(["sh", launch, *a], cwd=str(cwd), env=env, capture_output=True, text=True)
    for args in (("opencode", "proj-a"), ("opencode", str(team / me / "proj-a" / "sub")), ("codex", "-C", "proj-a"),
                 ("codex", "--cd=proj-a"), ("opencode", "run", "hi", "proj-a/sub")):
        r = run(*args)
        assert r.returncode == 2 and "team folder; cd into it" in r.stderr and "OPENCODE" not in r.stdout, args
    r = run("opencode", "run", "fix proj-a please")          # a prompt is not a path
    assert r.stdout.strip() == "OPENCODE run fix proj-a please"
    r = run("opencode", cwd=home / "proj-a")                 # inside: the box, through sudo enter
    assert r.stdout.startswith("SUDO -n /usr/local/libexec/kaptyorka/enter opencode")


def test_the_shell_hook_puts_the_shims_first_once_and_hands_functions_to_bash_children(tmp_path):
    hook = open(os.path.join(SECURE, "files", "shell-hook.sh")).read().replace(
        "/etc/kaptyorka/launcher.sh", os.path.join(SECURE, "files", "launcher.sh"))
    p = tmp_path / "hook.sh"
    p.write_text(hook)
    r = subprocess.run(["bash", "-c", f". {p}; . {p}; echo $PATH; bash -c 'type -t opencode; [ \"$(type -t git)\" = function ] && echo fgit || echo nogit'"],
                       capture_output=True, text=True, env={"PATH": "/usr/bin:/bin"})
    path, *types = r.stdout.split()
    assert path == "/usr/local/libexec/kaptyorka/shims:/usr/bin:/bin" and types == ["function", "nogit"]
    r = subprocess.run(["sh", "-c", f". {p}; echo $PATH"], capture_output=True, text=True, env={"PATH": "/bin"})
    assert r.stdout.strip() == "/usr/local/libexec/kaptyorka/shims:/bin"


def test_setup_strips_only_its_own_hook(tmp_path, monkeypatch):
    f = tmp_path / "bash.bashrc"
    hook = open(os.path.join(SECURE, "files", "shell-hook.sh")).read()
    f.write_text("before\n" + hook + "after\n")
    setup.strip_hook(str(f))
    assert f.read_text() == "before\nafter\n"


def test_shell_hook_defines_functions_in_bash_only_outside_posix_mode(tmp_path):
    hook = open(os.path.join(SECURE, "files", "shell-hook.sh")).read().replace(
        "/etc/kaptyorka/launcher.sh", os.path.join(SECURE, "files", "launcher.sh"))
    p = tmp_path / "hook.sh"
    p.write_text(hook)
    r = subprocess.run(["bash", "-c", f". {p}; type -t claude; type -t cursor-agent"], capture_output=True, text=True)
    assert r.stdout.split() == ["function", "function"]
    r = subprocess.run(["bash", "--posix", "-c", f". {p}; type -t claude || echo none"], capture_output=True, text=True)
    assert "function" not in r.stdout and r.returncode == 0
    if shutil.which("dash"):
        r = subprocess.run(["dash", "-c", f". {p}; echo ok"], capture_output=True, text=True)
        assert r.stdout.strip() == "ok"


def test_programs_import_only_the_standard_library_and_each_other():
    import ast
    import sys as _sys
    ours = {"kapcommon", "kaproot", "kap_hubchat", "proxybridge", "fence", "kap_fence", "toolwire",
            "kap_toolwire"}  # kap_proxybridge.py, kap_fence.py, kap_toolwire.py: installed so
    for name in os.listdir(SECURE):
        if not name.endswith(".py"):
            continue
        tree = ast.parse(open(os.path.join(SECURE, name)).read())
        for node in ast.walk(tree):
            mods = []
            if isinstance(node, ast.Import):
                mods = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                mods = [node.module.split(".")[0]]
            for m in mods:
                assert m in _sys.stdlib_module_names or m in ours, (name, m)


# ---------------------------------------------------------------------------- addrwatch, token-rotate

def test_addrwatch_adds_only_missing_addresses_and_is_restart_safe(monkeypatch):
    shown = {"kaptyorka-egress@alice.service": "0.0.0.0/8 10.0.0.0/8 203.0.113.5/32 ::1/128",
             "kapbox-alice.slice": ""}
    calls = []

    def fake(*args, check=True):
        calls.append(args)
        if args[0] == "show":
            return subprocess.CompletedProcess(args, 0, shown[args[-1]] + "\n", "")
        if args[0] == "set-property":
            shown[args[2]] = (shown[args[2]] + " " + args[3].split("=", 1)[1]).strip()
        return subprocess.CompletedProcess(args, 0, "", "")
    monkeypatch.setattr(aw.kr, "systemctl", fake)
    monkeypatch.setattr(aw, "units", lambda: sorted(shown))
    monkeypatch.setattr(aw.kr, "host_addrs", lambda: ["203.0.113.5", "203.0.113.77", "2001:db8::5"])
    cache = {}
    assert aw.sync(cache) == 2
    sets = [c for c in calls if c[0] == "set-property"]
    assert ("set-property", "--runtime", "kaptyorka-egress@alice.service",
            "IPAddressDeny=203.0.113.77 2001:db8::5") in sets
    assert ("set-property", "--runtime", "kapbox-alice.slice",
            "IPAddressDeny=203.0.113.5 203.0.113.77 2001:db8::5") in sets
    assert aw.missing(["10.77.0.9", "203.0.113.5"], aw.denied("kaptyorka-egress@alice.service")) == []
    calls.clear()
    assert aw.sync(cache) == 0 and not calls                    # nothing new: not even a question
    assert aw.sync({}) == 0 and not [c for c in calls if c[0] == "set-property"]   # a restart adds nothing twice
    assert aw.missing(["2001:db8:0::5"], {ipaddress.ip_network("2001:db8::5")}) == []


def test_addrwatch_listens_for_address_changes():
    s = aw.listener()
    try:
        assert s.family == socket.AF_NETLINK
    finally:
        s.close()


def test_token_rotate_writes_the_new_token_and_ends_claude_chats(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(kc, "SECRET", str(tmp_path))
    monkeypatch.setattr(hc.kr, "mkdir", lambda p, mode, uid=0, gid=0: os.makedirs(p, exist_ok=True))
    monkeypatch.setattr(hc.kr, "write_file", lambda p, d, mode=0o644, uid=0, gid=0: open(p, "w").write(d))
    monkeypatch.setattr(hc.kr, "boxes_of", lambda person: ["alice"])
    monkeypatch.setattr(hc.kr, "chats", lambda box: [("kapbox-alice-chat-claude-1.service", "claude", "/x"),
                                                     ("kapbox-alice-chat-codex-2.service", "codex", "/x")])
    stopped = []
    monkeypatch.setattr(hc.kr, "systemctl", lambda *a, check=True: stopped.append(a))
    new = "sk-ant-oat01-" + "n" * 30
    monkeypatch.setattr(hc, "read_new_token", lambda: new)
    hc.token_rotate("alice")
    assert open(tmp_path / "alice" / "claude-oauth-token").read() == new + "\n"
    assert stopped == [("stop", "kapbox-alice-chat-claude-1.service")]
    out = capsys.readouterr().out
    assert "1 running Claude chat" in out and "claude.ai" in out and "Anthropic support" in out
    with pytest.raises(kc.Refuse, match="the token the box has now"):
        hc.token_rotate("alice")
    monkeypatch.setattr(hc, "read_new_token", lambda: "not a token; rm -rf")
    with pytest.raises(kc.Refuse, match="does not look like a token"):
        hc.token_rotate("alice")


# red team round 2, finding 1: a name the box chose never reaches the person's terminal raw
EVIL_NAMES = ["id_rsa\x1b]0;PWNED-TITLE\x07\x1b[31mFAKE\x1b[0m.pem",
              "a.pem\n$ hubchat list   # all clean",
              "x\x1b]52;c;cm0gLXJmIH4=\x07.key", "b‮gnp.pem", "c\x9b2J.pem", "d\r\x08\x08.pem"]


def _terminal_safe(out: str) -> bool:
    return all(c.isprintable() or c == "\n" for c in out)


@pytest.mark.parametrize("name", EVIL_NAMES)
def test_show_found_and_say_print_no_terminal_control(name, capsys):
    lines = hc.show_found([(name, "its name looks like a secret", "name")])
    assert len(lines) == 1 and all(c.isprintable() for c in lines[0])
    kr.say("found:", *lines, "trap: " + name)     # the trap listings print raw names through say
    out = capsys.readouterr().out
    assert _terminal_safe(out) and out.count("\n") == 3


def test_hubchat_and_enter_errors_are_one_safe_line(monkeypatch, capsys):
    monkeypatch.setattr(hc.kr, "caller_person", lambda: "alice")

    def refuse(*a, **k):
        raise kc.Refuse("a\x1b]0;x\x07b\nforged line")
    monkeypatch.setattr(hc, "list_all", refuse)
    assert hc.main(["list"]) == 2
    err = capsys.readouterr().err
    assert _terminal_safe(err) and err.count("\n") == 1
    monkeypatch.setattr(en.kr, "caller_person", lambda: "alice")
    monkeypatch.setattr(en.kc, "filter_args", refuse)
    assert en.main(["claude"]) == 2
    err = capsys.readouterr().err
    assert _terminal_safe(err) and err.count("\n") == 1


def test_wallcheck_fail_prints_one_safe_line_and_the_fixed_next_step(capsys):
    with pytest.raises(SystemExit):
        wc.fail("W1", "/x\x1b[2J\nwallcheck ok: 15 walls")
    err = capsys.readouterr().err
    first, second = err.splitlines()   # the box-made name stays inside the first line
    assert _terminal_safe(err) and err.count("\n") == 2 and "wall W1 is missing" in first
    assert second.startswith("Next: restart this computer")


# ---------------------------------------------------------------------------- kaptyorka-guard (round 3)

def test_guard_sees_a_tool_folder_made_at_once(tmp_path):
    from kaptyorka.secure import kap_guard as gd
    ino = gd.Inotify()
    try:
        ino.add(str(tmp_path), gd.DIR_EVENTS, "folder", "alice/proj-a")
        ino.add(str(tmp_path / "missing"), gd.DIR_EVENTS, "folder", "x")      # not there: no watch, no error
        assert ino.read() == ([], False)
        (tmp_path / ".opencode").mkdir()
        os.rename(tmp_path / ".opencode", tmp_path / ".x")
        hits, lost = ino.read()
        assert set(hits) == {("folder", "alice/proj-a")} and not lost
    finally:
        os.close(ino.fd)


def test_guard_unit_stays_in_the_computer_s_mount_namespace():
    text = open(os.path.join(SECURE, "files", "units", "kaptyorka-guard.service")).read()
    lines = [l for l in text.splitlines() if l and not l.startswith("#")]
    for bad in ("ProtectSystem=", "PrivateTmp=", "ProtectHome=", "PrivateDevices=", "TemporaryFileSystem=",
                "RootDirectory=", "PrivateMounts=", "ReadOnlyPaths=", "BindPaths="):
        assert not any(l.startswith(bad) for l in lines), bad
    assert "ExecStart=/usr/bin/python3 -I /usr/local/libexec/kaptyorka/guard" in lines
    assert ("kap_guard.py", "guard", 0o755) in setup.PROGRAMS and "kaptyorka-guard.service" in setup.OUR_UNITS
