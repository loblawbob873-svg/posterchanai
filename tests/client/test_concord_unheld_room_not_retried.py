"""Desktop "not responding": a Concord room whose plane key this membership does not hold was rebuilt on
every 4 s live tick for nine hours (18,502 of 20,000 log lines), in a renderer that grew to 4-7 GB. Runs
the shipped concord.js under node."""
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.skipif(not shutil.which("node"), reason="node is required")
def test_an_unheld_room_is_tried_once_per_membership_state():
    run = subprocess.run(["node", str(ROOT / "tests/client/concord_unheld_room_not_retried_runtime.mjs")],
                         capture_output=True, text=True, timeout=60)
    assert run.returncode == 0, run.stdout + run.stderr
    assert "OK unheld Concord rooms" in run.stdout
