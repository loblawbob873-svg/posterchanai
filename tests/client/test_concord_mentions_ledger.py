"""'i got tagged twice in a concord room today but never got notification' -- the mention ledger that
the bell and Notifications read. Runs the shipped concord.js under node."""
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.skipif(not shutil.which("node"), reason="node is required")
def test_concord_mentions_wait_in_the_bell_until_read():
    run = subprocess.run(["node", str(ROOT / "tests/client/concord_mentions_ledger_runtime.mjs")],
                         capture_output=True, text=True, timeout=60, cwd=ROOT)
    assert run.returncode == 0, run.stdout + run.stderr
    assert "OK concord mentions wait in the bell until read" in run.stdout
