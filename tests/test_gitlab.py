"""The GitLab project list: the client against a fake GitLab (pagination, a 401, a redirect,
the token file's permissions), the admin's page API, and the `kaptyorka gitlab` commands.
The token must never show in any answer, log line or command output."""
import json
import logging
import os
import sys
from types import SimpleNamespace

import pytest

from conftest import start_hub
from helpers import FakeGitLab, gitlab_project, person
from kaptyorka import gitlab, http, paths
from kaptyorka.gitlab import GitLabError, GitLabSource
from kaptyorka.manager import ManagerStore
from kaptyorka.store import Store

TOKEN = "glpat-test-token"


@pytest.fixture
def fake():
    g = FakeGitLab([gitlab_project(f"proj-{i:03d}", f"project {i}") for i in range(150)], TOKEN)
    yield g
    g.close()


def put_token(text=TOKEN, mode=0o600):
    p = paths.home() / "gitlab-token"
    p.write_text(text + "\n")
    os.chmod(p, mode)
    return p


def test_fetch_follows_pages_and_sends_the_token(fake):
    got = gitlab.fetch(fake.url, "team", TOKEN)
    assert len(got) == 150 and got[0] == {"name": "proj-000", "description": "project 0",
                                          "repo": "ssh://git@gitlab.example:2222/team/proj-000.git",
                                          "path": "team/proj-000"}
    assert [p.split("?")[0] for p, _ in fake.seen] == ["/api/v4/groups/team/projects"] * 2
    assert "include_subgroups=true" in fake.seen[0][0] and "per_page=100" in fake.seen[0][0]
    assert "page=2" in fake.seen[1][0] and all(t == TOKEN for _, t in fake.seen)


def test_fetch_quotes_a_subgroup_path(fake):
    gitlab.fetch(fake.url, "team/sub", TOKEN)
    assert fake.seen[0][0].startswith("/api/v4/groups/team%2Fsub/projects?")


def test_fetch_errors_are_plain_and_hold_no_token(fake):
    with pytest.raises(GitLabError) as e:
        gitlab.fetch(fake.url, "team", "glpat-wrong")
    assert "refused the token (401)" in str(e.value) and "glpat" not in str(e.value)
    fake.status = 404
    with pytest.raises(GitLabError, match="no group team"):
        gitlab.fetch(fake.url, "team", TOKEN)
    port = fake.server.server_address[1]
    fake.close()
    with pytest.raises(GitLabError) as e:
        gitlab.fetch(f"http://127.0.0.1:{port}", "team", TOKEN)
    assert str(e.value) == "cannot reach GitLab at 127.0.0.1"


def test_a_redirect_is_refused_so_the_token_stays(fake):
    other = FakeGitLab([], TOKEN)
    try:
        fake.redirect = other.url + "/elsewhere"
        with pytest.raises(GitLabError, match="redirect"):
            gitlab.fetch(fake.url, "team", TOKEN)
        assert other.seen == []
    finally:
        other.close()


def test_token_file_checks(home):
    with pytest.raises(GitLabError, match="no GitLab token"):
        gitlab.read_token()
    p = put_token(mode=0o644)
    with pytest.raises(GitLabError) as e:
        gitlab.read_token()
    assert gitlab.shown(p) == "~/.kaptyorka/gitlab-token"
    assert "can be opened by other users" in str(e.value) and f"chmod 600 {gitlab.shown(p)}" in str(e.value)
    assert TOKEN not in str(e.value)
    os.chmod(p, 0o640)
    with pytest.raises(GitLabError, match="other users"):
        gitlab.read_token()
    os.chmod(p, 0o600)
    assert gitlab.read_token() == TOKEN
    put_token("   ")
    with pytest.raises(GitLabError, match="is empty"):
        gitlab.read_token()


def test_settings_are_checked():
    for url in ("gitlab.example", "ftp://gitlab.example", "https://u:p@gitlab.example", "https://"):
        with pytest.raises(GitLabError):
            gitlab.check_url(url)
    assert gitlab.check_url("https://gitlab.example/") == "https://gitlab.example"
    with pytest.raises(GitLabError):
        gitlab.check_group("a b")
    assert gitlab.check_group("/team/sub/") == "team/sub"


def test_source_caches_the_list(tmp_path, home, fake):
    store = Store(tmp_path / "hub.db")
    now = [0.0]
    src = GitLabSource(store, clock=lambda: now[0])
    assert src.projects() == {"on": False, "url": None, "group": None, "projects": [], "error": None}
    src.set(fake.url, "team", "alice")
    put_token()
    assert len(src.projects()["projects"]) == 150 and len(fake.seen) == 2
    now[0] = gitlab.CACHE_S - 1
    src.projects()
    assert len(fake.seen) == 2  # from the cache
    src.projects(fresh=True)
    assert len(fake.seen) == 4
    now[0] += gitlab.CACHE_S
    put_token(mode=0o644)
    out = src.projects()
    assert out["projects"] == [] and "other users" in out["error"]
    src.off("alice")
    assert src.projects()["on"] is False
    store.close()


@pytest.fixture
def app(tmp_path, home):
    store = Store(tmp_path / "hub.db")
    ManagerStore(store).set_enabled(False, "test")
    stop = start_hub(store)
    ids, tokens = {}, {}
    for name, role in (("alice", "admin"), ("bob", "member")):
        ids[name], tokens[name] = person(store, name, role)
    yield SimpleNamespace(url=f"http://127.0.0.1:{stop.port}", store=store, ids=ids, tokens=tokens)
    stop()
    store.close()


def call(app, who, method, path, body=None):
    return http.request(method, app.url + path, app.tokens[who], body)


def test_page_api_lists_gitlab_projects_for_the_admin_only(app, fake, caplog):
    caplog.set_level(logging.DEBUG)
    answers = []
    assert call(app, "alice", "GET", "/api/gitlab/projects")[1]["on"] is False
    status, st = call(app, "alice", "POST", "/api/gitlab", {"url": fake.url + "/", "group": "team"})
    answers.append(st)
    assert status == 200 and st["on"] and st["url"] == fake.url and st["count"] is None
    assert st["token"].startswith("no GitLab token") and st["error"].startswith("no GitLab token")
    put_token()
    status, st = call(app, "alice", "GET", "/api/gitlab")
    answers.append(st)
    assert (status, st["token"], st["count"], st["error"]) == (200, "ok", 150, None)
    status, got = call(app, "alice", "GET", "/api/gitlab/projects")
    answers.append(got)
    assert status == 200 and got["error"] is None and len(got["projects"]) == 150
    assert got["projects"][1]["name"] == "proj-001" and got["projects"][1]["description"] == "project 1"
    # Add to hub is the plain project route with the GitLab description and repo
    p = got["projects"][1]
    assert call(app, "alice", "POST", "/api/projects", {"name": p["name"], "description": p["description"],
                                                         "repo": p["repo"]})[0] == 204
    assert [(x["name"], x["description"], x["repo"]) for x in call(app, "alice", "GET", "/api/projects")[1]] == \
        [("proj-001", "project 1", "ssh://git@gitlab.example:2222/team/proj-001.git")]
    # members get nothing of it
    for method, path, body in (("GET", "/api/gitlab", None), ("GET", "/api/gitlab/projects", None),
                               ("POST", "/api/gitlab", {"url": fake.url, "group": "x"}),
                               ("DELETE", "/api/gitlab", None)):
        assert call(app, "bob", method, path, body)[0] == 403
    # a wrong token: one plain line, and the token is nowhere
    put_token("glpat-other")
    got = call(app, "alice", "GET", "/api/gitlab/projects?fresh=1")[1]
    answers.append(got)
    assert got["projects"] == [] and "refused the token (401)" in got["error"]
    assert call(app, "alice", "POST", "/api/gitlab", {"url": "nope", "group": "team"}) == \
        (400, "the GitLab address must look like https://gitlab.example")
    answers.append(call(app, "alice", "GET", "/api/feed")[1])
    assert call(app, "alice", "DELETE", "/api/gitlab")[0] == 204
    assert call(app, "alice", "GET", "/api/gitlab/projects")[1]["on"] is False
    log = [r["sentence"] for r in call(app, "alice", "GET", "/api/feed")[1]["log"]]
    assert "alice turned the GitLab project list off" in log
    assert f"alice set the GitLab project list to the group team on {fake.url}" in log
    text = json.dumps(answers) + caplog.text + json.dumps(log)
    assert TOKEN not in text and "glpat-other" not in text


def test_project_description_is_limited(app):
    assert call(app, "alice", "POST", "/api/projects", {"name": "proj-a", "description": "x" * 501}) == \
        (400, "description is too long (max 500 characters)")


def test_cli_gitlab_commands(app, fake, capsys, monkeypatch):
    from test_cli import entry_with_exit

    paths.save_config({"hub": app.url, "token": app.tokens["alice"], "machine": "box-1"})
    assert entry_with_exit(["gitlab", "status"]) == 0
    assert capsys.readouterr().out.startswith("GitLab project list: off")
    assert entry_with_exit(["gitlab", "set", fake.url, "team"]) == 1  # no token file yet
    out = capsys.readouterr().out
    assert f"GitLab:     {fake.url}" in out and "group:      team" in out and "no GitLab token" in out
    put_token()
    assert entry_with_exit(["gitlab", "status"]) == 0
    out = capsys.readouterr().out
    assert ": ok\n" in out and out.rstrip().endswith("projects:   150")
    put_token(mode=0o644)
    assert entry_with_exit(["gitlab", "status"]) == 1
    out = capsys.readouterr()
    assert "chmod 600" in out.out and TOKEN not in out.out + out.err
    assert entry_with_exit(["gitlab", "set", "nope", "team"]) == 1
    assert capsys.readouterr().err.strip() == "the GitLab address must look like https://gitlab.example"
    assert entry_with_exit(["gitlab", "off"]) == 0
    assert capsys.readouterr().out.strip() == "GitLab project list off; the projects on the hub stay"
    assert entry_with_exit(["gitlab", "status"]) == 0
    capsys.readouterr()
