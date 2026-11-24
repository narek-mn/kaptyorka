"""The helpers the slow tests share for real Claude Code sessions (helpers.py)."""
from helpers import READ_ONLY_TOOLS, allowed_tools, remove_transcripts, transcript_dir


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
