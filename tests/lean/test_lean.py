"""The Lean 4 track (D11, D41) builds: `lake build` in lean/ succeeds and prints no `sorry`."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

LEAN = Path(__file__).resolve().parents[2] / "lean"

pytestmark = pytest.mark.lean


@pytest.mark.skipif(shutil.which("lake") is None, reason="needs the Lean 4 toolchain (elan)")
def test_lean_project_builds():
    r = subprocess.run(["lake", "build"], cwd=LEAN, capture_output=True, text=True, timeout=1800)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "sorry" not in r.stdout + r.stderr


def test_lean_sources_have_no_sorry():
    for f in (LEAN / "MLS").glob("*.lean"):
        assert "sorry" not in f.read_text(), f
