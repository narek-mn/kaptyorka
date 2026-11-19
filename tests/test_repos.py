"""A session's project from its git remote: a copy of a registered repo in a folder with
another name counts as that project; any other folder stays private."""
import json
import os
import shutil
import subprocess

import pytest

from kaptyorka import identity, offhub, paths, repos
from kaptyorka import bridge as bridge_mod
from kaptyorka.adapters.base import Session
from kaptyorka.bridge import Bridge
from test_bridge import FakeAdapter
from test_privacy import team  # noqa: F401  (a fixture)
from test_team_only import Answers, beat, quiet, register  # noqa: F401  (quiet is a fixture)

pytestmark = pytest.mark.team_only
REAL_RUN = subprocess.run  # the bridge tests replace subprocess.run
HTTP = "http://gitserver:8000/group/proj-a.git"
SSH = "ssh://git@gitserver:2222/group/proj-a.git"


def git(*args, cwd):
    REAL_RUN(["git", *args], cwd=cwd, check=True, capture_output=True,
                   env={**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"})


def repo(path, url=None):
    path.mkdir(parents=True)
    git("init", "-q", "-b", "main", cwd=path)
    if url:
        git("remote", "add", "origin", url, cwd=path)
    return path


def test_normalize():
    for url in (HTTP, SSH, "https://GitServer/group/proj-a/", "git@gitserver:group/proj-a.git",
                "ssh://gitserver/group/proj-a", "http://user:pw@gitserver:8000/group/proj-a.git/"):
        assert repos.normalize(url) == "gitserver/group/proj-a", url
    assert repos.normalize("/srv/git/proj-a.git") == "" and repos.normalize("") == ""
    assert repos.fingerprint(HTTP) == repos.fingerprint(SSH) != repos.fingerprint(
        "http://gitserver/group/proj-b.git")


needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")


@needs_git
def test_a_worktree_with_the_remote_counts_as_the_project(tmp_path):
    main = repo(tmp_path / "proj-a", HTTP)
    subprocess.run(["git", "-c", "user.name=a", "-c", "user.email=a@x", "commit", "-q",
                    "--allow-empty", "-m", "one"], cwd=main, check=True, capture_output=True)
    git("worktree", "add", "-q", str(tmp_path / "proj-a-fix-login"), cwd=main)
    wt = tmp_path / "proj-a-fix-login"
    assert (wt / ".git").is_file()  # a linked worktree: its config is the main repo's
    known = {repos.fingerprint(SSH): "proj-a"}  # registered with the ssh URL: still a match
    assert repos.resolve("proj-a-fix-login", str(wt / "src"), {"proj-a"}, known) == "proj-a"
    assert repos.resolve("proj-a-fix-login", str(wt), {"proj-a"}, {}) == "proj-a-fix-login"
    # the folder name wins when it is a team project itself
    assert repos.resolve("proj-b", str(wt), {"proj-a", "proj-b"}, known) == "proj-b"
    # a project no longer registered: not a match
    assert repos.resolve("proj-a-fix-login", str(wt), {"proj-b"}, known) == "proj-a-fix-login"


@needs_git
def test_another_remote_stays_private_and_the_cache_follows_the_config(tmp_path):
    other = repo(tmp_path / "diary", "git@elsewhere:me/diary.git")
    known = {repos.fingerprint(HTTP): "proj-a"}
    assert repos.resolve("diary", str(other), {"proj-a"}, known) == "diary"
    assert repos.resolve("plain", str(tmp_path), {"proj-a"}, known) == "plain"  # no repo
    git("remote", "add", "up", HTTP, cwd=other)
    cfg = other / ".git" / "config"
    os.utime(cfg, (1, os.stat(cfg).st_mtime + 5))  # a change within the same second
    assert repos.resolve("diary", str(other), {"proj-a"}, known) == "proj-a"
    both = {**known, repos.fingerprint("git@elsewhere:me/diary.git"): "proj-b"}
    assert repos.match(str(other), both) is None  # two projects: neither


def test_a_submodule_reads_its_own_config(tmp_path):
    top = tmp_path / "super"
    mod = top / ".git" / "modules" / "lib"
    mod.mkdir(parents=True)
    (top / ".git" / "config").write_text('[remote "origin"]\n\turl = git@elsewhere:me/super\n')
    (mod / "config").write_text('[core]\n\tbare = false\n[remote "origin"]\n'
                                f'\turl = "{HTTP}"\n\tfetch = +refs/heads/*:refs/remotes/origin/*\n')
    (top / "lib").mkdir()
    (top / "lib" / ".git").write_text("gitdir: ../.git/modules/lib\n")
    assert repos.remotes(str(top / "lib")) == [HTTP]
    assert repos.resolve("lib", str(top / "lib"), {"proj-a"},
                         {repos.fingerprint(HTTP): "proj-a"}) == "proj-a"
    assert repos.resolve("super", str(top), {"proj-a"},
                         {repos.fingerprint(HTTP): "proj-a"}) == "super"


def test_the_hub_sends_fingerprints_only_and_skips_projects_without_a_repo(team):  # noqa: F811
    team.store.project_register("proj-a", repo=HTTP, by="admin")
    register(team, "proj-b")  # no repo: matched by its name only
    answer = beat(team, "alice", {"sessions": []})
    assert answer["repos"] == {repos.fingerprint(HTTP): "proj-a"}
    assert "gitserver" not in json.dumps(answer)


@needs_git
def test_the_bridge_reports_the_copy_and_sends_nothing_of_other_folders(
        tmp_path, home, quiet, monkeypatch):  # noqa: F811
    wt = repo(tmp_path / "w" / "proj-a-fix", SSH)
    diary = repo(tmp_path / "w" / "diary", "git@elsewhere:me/diary.git")
    hub = Answers(monkeypatch, {"team": ["proj-a"], "keep": [],
                                "repos": {repos.fingerprint(HTTP): "proj-a"}})
    adapter = FakeAdapter(sessions=[Session("codex:w", "codex", "proj-a-fix", cwd=str(wt)),
                                    Session("codex:d", "codex", "diary", cwd=str(diary))])
    Bridge({"hub": "http://h", "token": "t", "machine": "box-1"}, [adapter],
           bridge_mod.log).heartbeat()
    body = hub.bodies[-1]
    assert [(s["key"], s["project"]) for s in body["sessions"]] == [("codex:w", "proj-a")]
    sent = json.dumps(hub.bodies)
    assert "diary" not in sent and "elsewhere" not in sent and "gitserver" not in sent
    # the shim and hooks decide the same from the saved list
    assert offhub.load_repos() == {repos.fingerprint(HTTP): "proj-a"}
    assert identity.project_of(str(wt)) == "proj-a"
    assert identity.project_of(str(diary)) == "diary"
    assert offhub.personal(identity.project_of(str(diary)), str(diary)) == paths.NOT_TEAM


def test_the_hub_keeps_the_copys_project_for_its_brief():
    from kaptyorka.memory import MemoryStore

    folders = {"proj-a": "/w/proj-a"}
    assert MemoryStore.session_project("/w/proj-a-fix", "proj-a", folders) == "proj-a"
    assert MemoryStore.session_project("/w/proj-a--2", "proj-a--2", folders) == "proj-a"
    assert MemoryStore.session_project("/w/proj-a", "x", folders) == "proj-a"
