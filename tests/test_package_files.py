"""A built wheel holds every data file the installed code reads: the box's managed tool settings
(nested folders such as secure/files/box/opencode/etc/plugins), the units and the page. A glob
that missed one once shipped a box whose OpenCode ran without its deny rules."""
import os
import shutil
import subprocess
import sys
import zipfile

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG = os.path.join(ROOT, "src", "kaptyorka")
DATA_DIRS = ("secure/files", "relay/units", "web")


def data_files():
    out = set()
    for d in DATA_DIRS:
        for dirpath, dirs, files in os.walk(os.path.join(PKG, d)):
            dirs[:] = [x for x in dirs if x != "__pycache__"]
            for n in files:
                if not n.endswith((".pyc", ".py")):
                    out.add(os.path.relpath(os.path.join(dirpath, n), PKG).replace(os.sep, "/"))
    return out


def test_every_data_file_is_in_the_wheel(tmp_path):
    pytest.importorskip("build")
    src = tmp_path / "src"
    src.mkdir()
    for n in ("pyproject.toml", "README.md", "LICENSE"):
        shutil.copy(os.path.join(ROOT, n), src / n)
    shutil.copytree(os.path.join(ROOT, "src"), src / "src",
                    ignore=shutil.ignore_patterns("__pycache__", "*.egg-info"))
    out = tmp_path / "dist"
    r = subprocess.run([sys.executable, "-m", "build", "--wheel", "--no-isolation", "--outdir", str(out), str(src)],
                       capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stdout[-2000:] + r.stderr[-2000:]
    whl = next(out.glob("*.whl"))
    names = {n[len("kaptyorka/"):] for n in zipfile.ZipFile(whl).namelist() if n.startswith("kaptyorka/")}
    want = data_files()
    assert "secure/files/box/opencode/etc/plugins/kaptyorka.js" in want   # the nested ones are found
    assert "secure/files/box/opencode/etc/opencode.json" in want
    assert want - names == set()
