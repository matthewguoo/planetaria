"""deploy/live/autodeploy.sh serves both engines on the box through
PLANETARIA_* env; --show prints the resolved config with no side effects."""

import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "deploy" / "live" / "autodeploy.sh"


def _bash() -> str | None:
    for candidate in (shutil.which("bash"), r"C:\Program Files\Git\bin\bash.exe"):
        if candidate and Path(candidate).exists():
            out = subprocess.run([candidate, "--version"], capture_output=True, text=True)
            if "GNU bash" in out.stdout:
                return candidate
    return None


@pytest.mark.skipif(_bash() is None, reason="GNU bash not available")
@pytest.mark.parametrize("env, expected", [
    ({}, "service=planetaria-live port=8001 mode=live_manual"),
    ({"PLANETARIA_SERVICE": "planetaria-paper", "PLANETARIA_PORT": "8000", "PLANETARIA_MODE": "paper"},
     "service=planetaria-paper port=8000 mode=paper"),
])
def test_show_resolves_engine(env, expected):
    import os

    out = subprocess.run([_bash(), str(SCRIPT), "--show"], capture_output=True, text=True,
                         env={**os.environ, "HOME": "/h", **env})
    assert out.returncode == 0, out.stderr
    assert expected in out.stdout
    assert "repo=" in out.stdout and "log=" in out.stdout
