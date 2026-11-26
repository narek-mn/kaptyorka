"""The secure box's container tests (tests/secure/sliceN.sh), each in its own throw-away docker
container with systemd: the relay with its real units (slice 1), the box walls, launcher and
installer (slice 2), a box chat on a real hub (slice 3), the four tools in the box (slice 5).
Run before a release with: pytest -m slow tests/test_secure_container_slow.py (about 20 minutes;
one at a time, never beside the soak, the eval or the rest of the slow suite; `pytest -m slow
tests` skips it). One script:
add `-k slice2`. A failure lists the script's FAIL rows; the whole table is in
tests/secure/sliceN-results.tsv."""
import os
import shutil
import subprocess
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent / "secure"
SCRIPTS = {"slice1": "kaptest-s1", "slice2": "kaptest-s2", "slice3": "kaptest-s3", "slice5": "kaptest-s5"}
LIMIT = 3600   # each script bounds its own steps; this only stops a stuck docker client


def docker_works() -> bool:
    if shutil.which("docker") is None or shutil.which("gcc") is None:
        return False
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=30).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


@pytest.mark.slow
@pytest.mark.skipif(os.geteuid() == 0, reason="the container tests never run as root on the host")
@pytest.mark.parametrize("name", sorted(SCRIPTS))
def test_secure_container(name, request):
    # about 20 minutes: not part of `pytest -m slow tests`, only when this file is named
    if not any(Path(a.split("::")[0]).resolve() == Path(__file__).resolve() for a in request.config.args):
        pytest.skip("runs only when named: pytest -m slow tests/test_secure_container_slow.py")
    if not docker_works():
        pytest.skip("needs a working docker (and gcc for the stub claude)")
    results = HERE / f"{name}-results.tsv"
    results.unlink(missing_ok=True)
    env = {k: v for k, v in os.environ.items() if k not in ("KEEP", "PHASES", "C", "REPO", "TOOLS")}
    try:
        r = subprocess.run(["bash", str(HERE / f"{name}.sh")], env=env, capture_output=True, text=True, errors="replace",
                           timeout=LIMIT)
    finally:
        subprocess.run(["timeout", "-k", "5", "90", "docker", "rm", "-f", SCRIPTS[name]], capture_output=True)
    tail = "\n".join((r.stdout + r.stderr).splitlines()[-15:])
    assert r.returncode == 0, f"{name}.sh exited {r.returncode}:\n{tail}"
    assert results.exists(), f"{name}.sh wrote no results:\n{tail}"
    rows = [ln.split("\t") for ln in results.read_text(errors="replace").splitlines() if ln.strip()]
    fails = [f"{row[0]}: {row[4][:300] if len(row) > 4 else ''}" for row in rows if len(row) > 1 and row[1] == "FAIL"]
    passes = sum(1 for row in rows if len(row) > 1 and row[1] == "PASS")
    assert passes, f"{name}.sh recorded no PASS row:\n{tail}"
    assert not fails, f"{name}.sh: {len(fails)} FAIL rows of {len(rows)}:\n" + "\n".join(fails)
