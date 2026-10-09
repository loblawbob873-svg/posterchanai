"""A queued Concord message still finds its room after the room gains its community id (see the .cjs)."""
import shutil
import subprocess
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent


@pytest.mark.skipif(not shutil.which("node"), reason="node required")
def test_retry_finds_the_room_after_its_identity_changes_runtime():
    r = subprocess.run(["node", str(HERE / "concord_delivery_room_identity_runtime.cjs")], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr
