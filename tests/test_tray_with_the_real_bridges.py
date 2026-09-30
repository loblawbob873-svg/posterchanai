"""The tray against the bridges desktop/preload.js really exposes -- frozen, the way contextBridge
hands them to the page. See tests/client/tray_real_bridges_runtime.cjs for why a plain-object fake
could never see the bug that removed the whole Quick Settings button (sound, Wi-Fi, power)."""
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(not shutil.which("node"), reason="node required")
def test_the_tray_works_on_the_real_frozen_bridges():
    r = subprocess.run(["node", "tests/client/tray_real_bridges_runtime.cjs"], cwd=ROOT,
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0 and "OK tray with the real bridges" in r.stdout, (r.stdout[-2500:], r.stderr[-2500:])
