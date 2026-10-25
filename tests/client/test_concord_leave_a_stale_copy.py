"""Leaving a stale copy of a community (same invite address, older link) never leaves the real one.
Runs the shipped concord.js under node: concord_leave_a_stale_copy_runtime.mjs."""
import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.skipif(not shutil.which("node"), reason="node required")
def test_leaving_a_stale_copy_keeps_the_real_community():
    script = Path(__file__).with_name("concord_leave_a_stale_copy_runtime.mjs")
    r = subprocess.run(["node", str(script)], capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr
