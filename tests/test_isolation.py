"""The fast suite runs in a home of its own (tests/conftest.py), in one process or many (-n auto):
never the real ~/.claude or ~/.kaptyorka of whoever runs it."""
import os
import pwd
from pathlib import Path


def test_not_the_real_home():
    real = Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()
    assert Path.home().resolve() != real
    assert Path(os.environ["KAPTYORKA_HOME"]).resolve() != real / ".kaptyorka"
    assert not str(Path(os.environ["KAPTYORKA_HOME"]).resolve()).startswith(str(real / ".kaptyorka"))
