"""Concord looks up a member's profile where the room lives when this relay has none (see the .cjs)."""
import shutil
import subprocess
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent


@pytest.mark.skipif(not shutil.which("node"), reason="node required")
def test_a_member_without_a_profile_here_is_looked_up_once_runtime():
    r = subprocess.run(["node", str(HERE / "concord_member_profiles_runtime.cjs")], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr
