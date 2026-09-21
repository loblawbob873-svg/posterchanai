"""Concord must not re-sign unchanged state — see concord_no_duplicate_publish_runtime.mjs."""
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def test_concord_signs_each_thing_once():
    if not shutil.which("node"):
        pytest.skip("node not installed")
    run = subprocess.run(["node", str(ROOT / "tests/client/concord_no_duplicate_publish_runtime.mjs")],
                         capture_output=True, text=True, timeout=120)
    assert run.returncode == 0, run.stdout + run.stderr
    assert "no duplicate publishes passed" in run.stdout
