"""The helpers the slow tests share for real Claude Code sessions (helpers.py)."""
import os

import pytest

from helpers import KEEP_FAILURES, READ_ONLY_TOOLS, allowed_tools, remove_transcripts, transcript_dir


def test_allowed_tools_always_has_the_read_only_ones():
    flag = allowed_tools("Write")
    assert flag.startswith("--allowedTools=") and flag.endswith(",Write")
    for t in ("Bash(pwd)", "Bash(ls *)", "Bash(test *)", "Bash(echo *)", "Bash(cat *)"):
        assert t in READ_ONLY_TOOLS and t in flag


def test_transcripts_of_a_scratch_folder_and_only_those_go(tmp_path):
    scratch = tmp_path / "w" / ".kaptyorka-slow-a_b.c"
    want = transcript_dir(scratch, tmp_path)
    assert want.name == str(scratch).replace("/", "-").replace(".", "-").replace("_", "-")
    keep = want.parent / (want.name + "x")  # another scratch folder that starts the same
    inner = want.parent / (want.name + "-proj-a")  # a session in a folder inside it
    for d in (want, keep, inner):
        d.mkdir(parents=True)
    remove_transcripts(scratch, tmp_path)
    assert not want.exists() and not inner.exists() and keep.exists()


def _session(tmp_path):
    scratch = tmp_path / "w" / "scratch"
    d = transcript_dir(scratch, tmp_path)
    d.mkdir(parents=True)
    (d / "s1.jsonl").write_text('{"type":"user"}\n')
    return scratch, d


def test_a_failing_slow_test_keeps_its_transcripts(tmp_path, monkeypatch, capsys):
    keep = tmp_path / "keep"
    monkeypatch.setenv("KAPTYORKA_TEST_KEEP", str(keep))
    scratch, d = _session(tmp_path)
    with pytest.raises(AssertionError):
        try:
            raise AssertionError("no shell status within 120 s")
        finally:
            remove_transcripts(scratch, tmp_path)
    assert not d.exists()
    [kept] = list(keep.iterdir())
    assert kept.name.startswith("test_a_failing_slow_test_keeps_its_transcripts-")
    assert (kept / d.name / "s1.jsonl").read_text() == '{"type":"user"}\n'
    assert f"kept in {kept}" in capsys.readouterr().out


def test_a_passing_slow_test_keeps_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("KAPTYORKA_TEST_KEEP", str(tmp_path / "keep"))
    scratch, d = _session(tmp_path)
    remove_transcripts(scratch, tmp_path)
    assert not d.exists() and not (tmp_path / "keep").exists()


def test_only_the_newest_failures_are_kept(tmp_path, monkeypatch):
    keep = tmp_path / "keep"
    monkeypatch.setenv("KAPTYORKA_TEST_KEEP", str(keep))
    for i in range(KEEP_FAILURES + 2):
        old = keep / f"t-{i:02d}"
        old.mkdir(parents=True)
        os.utime(old, (1000 + i, 1000 + i))
    scratch, _ = _session(tmp_path)
    try:
        raise RuntimeError("x")
    except RuntimeError:
        remove_transcripts(scratch, tmp_path)
    names = sorted(p.name for p in keep.iterdir())
    assert len(names) == KEEP_FAILURES and "t-00" not in names and "t-02" not in names
