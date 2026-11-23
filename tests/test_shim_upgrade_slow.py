"""A shim keeps working when kaptyorka is upgraded under it, the way pipx upgrades: the new
files replace the old ones in the same venv while the shim runs."""
import json
import os
import re
import shutil
import subprocess
import sys
import sysconfig
import venv
from pathlib import Path

import anyio
import pytest
from mcp import Client, MCPError, StdioServerParameters

import kaptyorka
from conftest import start_hub
from kaptyorka import offhub
from kaptyorka.manager import ManagerStore
from kaptyorka.store import Store

pytestmark = [pytest.mark.slow, pytest.mark.team_only]
TREE = Path(kaptyorka.__file__).parents[2]  # the source tree: pyproject.toml and src/


def build(tmp: Path, version: str, new_name: bool) -> Path:
    """A wheel of this tree as `version`; new_name: launch.py needs a name only the new
    paths.py has (as 0.9.28's launch.py needed paths.MANAGER_TMUX)."""
    src = tmp / f"src-{version}"
    shutil.copytree(TREE, src, ignore=shutil.ignore_patterns(
        ".venv", ".git", "tests", "docs", "__pycache__", "*.egg-info", "build", ".claude"))
    py = src / "pyproject.toml"
    py.write_text(re.sub(r'(?m)^version = ".*"', f'version = "{version}"', py.read_text()))
    (src / "src/kaptyorka/__init__.py").write_text(f'__version__ = "{version}"\n')
    if new_name:
        with open(src / "src/kaptyorka/paths.py", "a") as f:
            f.write('\nNEW_FOR_TEST = "new"\n')
        with open(src / "src/kaptyorka/launch.py", "a") as f:
            f.write("\n_NEW = paths.NEW_FOR_TEST\n")
    out = tmp / "wheels"
    subprocess.run([sys.executable, "-m", "pip", "wheel", "-q", "--no-deps", "--no-index",
                    "--no-build-isolation", "-w", str(out), str(src)], check=True)
    return out


def test_an_open_shim_survives_an_upgrade_under_it(tmp_path, monkeypatch):
    pytest.importorskip("setuptools")  # builds the wheels without the network
    wheels = build(tmp_path, "0.0.1", False)
    build(tmp_path, "0.0.2", True)
    env_dir = tmp_path / "venv"
    venv.create(env_dir, with_pip=True)
    # mcp and the rest, already installed here, so no download; never this kaptyorka
    deps = tmp_path / "deps"
    deps.mkdir()
    for entry in Path(sysconfig.get_paths()["purelib"]).iterdir():
        if not re.match(r"(__editable__\.)?kaptyorka|_?pytest|.*\.pth$", entry.name):
            (deps / entry.name).symlink_to(entry)
    site = next((env_dir / "lib").glob("python3*/site-packages"))
    (site / "deps.pth").write_text(f"{deps}\n")
    pip = [str(env_dir / "bin/python"), "-m", "pip", "install", "-q", "--no-index",
           "--find-links", str(wheels)]
    subprocess.run(pip + ["kaptyorka==0.0.1"], check=True)

    home = tmp_path / "home"
    folder = tmp_path / "diary"  # no team project: listing the tools touches no hub module
    folder.mkdir()
    (home / ".claude/sessions").mkdir(parents=True)
    (home / ".claude/sessions" / f"{os.getpid()}.json").write_text(
        json.dumps({"sessionId": "s1", "cwd": str(folder)}))  # this process is "Claude Code"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("KAPTYORKA_HOME", str(home / ".kaptyorka"))
    (home / ".kaptyorka").mkdir()
    offhub.save_team({"proj-a"}, set(), ["proj-a"])
    store = Store(tmp_path / "hub.db", legacy_token="T")
    ManagerStore(store).set_enabled(False, "test")
    stop = start_hub(store)
    (home / ".kaptyorka/config.json").write_text(json.dumps(
        {"hub": f"http://127.0.0.1:{stop.port}", "token": "T", "machine": "box-1"}))
    env = {"HOME": str(home), "KAPTYORKA_HOME": str(home / ".kaptyorka"),
           "PATH": os.environ.get("PATH", "")}

    async def go():
        params = StdioServerParameters(command=str(env_dir / "bin/kaptyorka"), args=["mcp"],
                                       env=env)
        async with Client(params) as c:
            assert "join_team" in {t.name for t in (await c.list_tools()).tools}
            await anyio.run_process(pip + ["--upgrade", "kaptyorka==0.0.2"])
            for name, args in (("join_team", {"project": "proj-a"}), ("notes_list", {})):
                try:
                    r = await c.call_tool(name, args)
                except MCPError as e:  # "Internal server error" before the fix
                    pytest.fail(f"{name} failed after the upgrade: {e}")
                said = r.content[0].text
                # the old code answers as before; it never reaches the fallback line
                assert r.is_error and said.startswith("kaptyorka") and "was updated" not in said, said
    try:
        anyio.run(go)
        out = subprocess.run([str(env_dir / "bin/kaptyorka"), "--version"],
                             capture_output=True, text=True).stdout
        assert "0.0.2" in out  # the upgrade really replaced the files
    finally:
        stop()
        store.close()
