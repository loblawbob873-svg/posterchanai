"""Concord community folders (Armada's model): the shipped layout code under node.

"add ability to put communities in groups like Armada does it". Runs tests/client/concord_rail_runtime.mjs:
Armada's rules (a one-community folder dissolves, duplicates go, c1:/c2: keys are one community), an
Armada-made layout drawn over this device's rooms (a room it never mentions still shows), new folder
from two communities, add / remove / rename / ungroup, and an edit re-applied to a copy another device
changed keeping both folders."""
import shutil
import subprocess
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent


@pytest.mark.skipif(not shutil.which("node"), reason="node not installed")
def test_folder_layout_rules():
    r = subprocess.run(["node", str(HERE / "concord_rail_runtime.mjs")], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0 and r.stdout.strip().endswith("ok"), r.stdout + r.stderr
