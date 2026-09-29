"""A partly-locked Monero balance says how much is confirming and when it unlocks — see
tests/client/monero_partial_lock_runtime.cjs (runs the shipped meWalletHtml)."""
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.skipif(not shutil.which("node"), reason="node required")
def test_a_partly_locked_balance_is_explained():
    r = subprocess.run(["node", "tests/client/monero_partial_lock_runtime.cjs"], cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0 and "OK partial lock" in r.stdout, (r.stdout[-3000:], r.stderr[-3000:])
