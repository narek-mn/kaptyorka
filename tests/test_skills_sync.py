import os

from kaptyorka import paths, skills_sync as ss


def quiet(msg):
    pass


def skill(home, name):
    return home / ".claude" / "skills" / ("team-" + name) / "SKILL.md"


def test_render_frontmatter_from_skill_description():
    out = ss.render("review", "---\nname: x\ndescription: \"Check a diff\"\n---\nBody line\n")
    assert out == f'---\nname: "team-review"\ndescription: "Check a diff"\n---\n{ss.MARKER}\n\nBody line\n'


def test_render_description_from_first_line():
    out = ss.render("go", "\n\nFirst line\nsecond\n")
    assert 'description: "First line"\n---\n' in out
    long = ss.render("go", "x" * 500)
    assert long.split("\n")[2] == 'description: "' + "x" * 200 + '"'


def test_sync_writes_and_removes_marked_folders(tmp_path, home):
    lines = ss.sync(tmp_path, "v1", [{"name": "a", "text": "A"}, {"name": "b", "text": "B"}], quiet)
    assert len(lines) == 2 and skill(tmp_path, "a").exists() and skill(tmp_path, "b").exists()
    assert ss.applied_version(paths.home()) == "v1"
    lines = ss.sync(tmp_path, "v2", [{"name": "a", "text": "A2"}], quiet)
    assert len(lines) == 2 and not skill(tmp_path, "b").parent.exists()
    assert "A2" in skill(tmp_path, "a").read_text()


def test_sync_leaves_foreign_folder_alone(tmp_path, home):
    f = skill(tmp_path, "foo")
    f.parent.mkdir(parents=True)
    f.write_bytes(b"mine")
    logged = []
    ss.sync(tmp_path, "v1", [], logged.append)
    assert f.read_bytes() == b"mine" and any("not ours" in x for x in logged)
    ss.sync(tmp_path, "v2", [{"name": "foo", "text": "T"}], quiet)
    assert f.read_bytes() == b"mine"


def test_sync_is_idempotent_per_version(tmp_path, home):
    items = [{"name": "a", "text": "A"}]
    ss.sync(tmp_path, "v1", items, quiet)
    os.utime(skill(tmp_path, "a"), (1, 1))
    assert ss.sync(tmp_path, "v1", items, quiet) == []
    assert skill(tmp_path, "a").stat().st_mtime == 1


def test_remove_all(tmp_path, home):
    ss.sync(tmp_path, "v1", [{"name": "a", "text": "A"}], quiet)
    other = skill(tmp_path, "keep")
    other.parent.mkdir(parents=True)
    other.write_text("mine")
    assert len(ss.remove_all(tmp_path, quiet)) == 1
    assert not skill(tmp_path, "a").parent.exists() and other.exists()
    assert not (paths.home() / "skills.json").exists()


def test_render_quotes_description():
    assert 'description: "Review changes"\n' in ss.render("r", "# Review changes\nx")
    assert 'description: "Check: a diff"\n' in ss.render("r", "Check: a diff")
    assert 'name: "team-r"\n' in ss.render("r", "x")


def test_unsafe_names_skipped(tmp_path, home):
    logged = []
    bad = ["..", "a/b", "/abs", "", None, 5, "x" * 300, ".hidden"]
    ss.sync(tmp_path, "v1", [{"name": n, "text": "T"} for n in bad], logged.append)
    assert len(logged) == len(bad)
    assert not (tmp_path / ".claude/skills").exists() or not list((tmp_path / ".claude/skills").iterdir())
    assert not (tmp_path / "abs").exists() and not (tmp_path / "a").exists()


def test_symlinked_team_folder_untouched(tmp_path, home):
    ss.sync(tmp_path, "v1", [{"name": "a", "text": "A"}], quiet)
    real = tmp_path / "elsewhere"
    real.mkdir()
    (real / "SKILL.md").write_text(ss.MARKER + "orig")
    link = tmp_path / ".claude/skills/team-x"
    link.symlink_to(real)
    ss.sync(tmp_path, "v2", [{"name": "x", "text": "NEW"}], quiet)
    assert (real / "SKILL.md").read_text() == ss.MARKER + "orig" and link.is_symlink()
    ss.sync(tmp_path, "v3", [], quiet)
    assert link.is_symlink() and (real / "SKILL.md").exists()
    ss.remove_all(tmp_path, quiet)
    assert link.is_symlink() and (real / "SKILL.md").exists()


def test_symlinked_skill_md_untouched(tmp_path, home):
    real = tmp_path / "real.md"
    real.write_text(ss.MARKER + "orig")
    f = skill(tmp_path, "x")
    f.parent.mkdir(parents=True)
    f.symlink_to(real)
    ss.sync(tmp_path, "v1", [{"name": "x", "text": "NEW"}], quiet)
    ss.sync(tmp_path, "v2", [], quiet)
    ss.remove_all(tmp_path, quiet)
    assert f.is_symlink() and real.read_text() == ss.MARKER + "orig"


def test_symlinked_skills_root_gets_files(tmp_path, home):
    target = tmp_path / "dotfiles-skills"
    target.mkdir()
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude/skills").symlink_to(target)
    ss.sync(tmp_path, "v1", [{"name": "a", "text": "A"}], quiet)
    assert (target / "team-a/SKILL.md").exists()
    ss.remove_all(tmp_path, quiet)
    assert not (target / "team-a").exists()
