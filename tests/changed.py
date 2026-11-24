"""Run only the fast tests worth running for what changed against origin/main (commits, staged,
unstaged and new files), in parallel. For a worker before a commit; the whole suite still runs
before a release. Usage: python tests/changed.py [--dry] [extra pytest args]

src/kaptyorka/X.py (or adapters/X.py) -> tests/test_X*.py and every test that imports it;
web/index.html -> tests/test_page*.py; a changed test file -> itself; tests/conftest.py,
tests/helpers.py or pyproject.toml -> the whole suite; README.md -> tests/test_readme.py."""
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TESTS = ROOT / "tests"
EVERYTHING = {"tests/conftest.py", "tests/helpers.py", "tests/chrome.py", "pyproject.toml"}


def git(*args):
    r = subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=True)
    return r.stdout.split() if r.returncode == 0 else []


def changed() -> set:
    base = git("merge-base", "origin/main", "HEAD") or git("merge-base", "main", "HEAD")
    files = set(git("diff", "--name-only", base[0])) if base else set()
    files |= set(git("diff", "--name-only", "HEAD")) | set(git("ls-files", "--others", "--exclude-standard"))
    return {f for f in files if (ROOT / f).exists()}


def test_files() -> list:
    return sorted(p for p in TESTS.glob("test_*.py") if not p.stem.endswith("_slow"))


def importers(module: str) -> set:
    """Test files that import kaptyorka.<module> (dotted, such as adapters.claude)."""
    parent, _, leaf = module.rpartition(".")
    pkg = "kaptyorka" + ("." + parent if parent else "")
    pat = re.compile(rf"(\bkaptyorka\.{re.escape(module)}\b|from {re.escape(pkg)} import [^\n]*\b{re.escape(leaf)}\b)")
    return {p for p in test_files() if pat.search(p.read_text(errors="replace"))}


def choose(files: set) -> list:
    if files & EVERYTHING:
        return test_files()
    out = set()
    for f in files:
        p = Path(f)
        if f.startswith("tests/") and p.name.startswith("test_") and p.suffix == ".py":
            if not p.stem.endswith("_slow"):
                out.add(ROOT / f)
        elif f.startswith("src/kaptyorka/") and p.suffix == ".py":
            module = ".".join(p.relative_to("src/kaptyorka").with_suffix("").parts)
            leaf = p.stem
            out |= {t for t in test_files() if t.stem == f"test_{leaf}" or t.stem.startswith(f"test_{leaf}_")}
            out |= importers(module)
        elif f.startswith("src/kaptyorka/web/"):
            out |= set(TESTS.glob("test_page*.py")) - set(TESTS.glob("*_slow.py"))
        elif f == "README.md":
            out |= {TESTS / "test_readme.py"}
    return sorted(out)


def main(argv):
    dry = "--dry" in argv
    extra = [a for a in argv if a != "--dry"]
    files = changed()
    picked = choose(files)
    print(f"changed: {len(files)} file(s); tests chosen: {len(picked)}")
    for t in picked:
        print("  " + str(t.relative_to(ROOT)))
    if not picked or dry:
        return 0
    cmd = [sys.executable, "-m", "pytest", *map(str, picked), "-q", "-n", "auto", "--dist", "worksteal", *extra]
    return subprocess.call(cmd, cwd=ROOT)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
