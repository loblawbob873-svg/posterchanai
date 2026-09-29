"""The shared DM cache is uploaded only when it grew, not from every window, and the copy it replaces
is let go of. tests/client/dmcache_push_runtime.mjs runs the shipped pushShared."""
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(not shutil.which("node"), reason="node required")
def test_dm_cache_is_not_reuploaded_for_nothing():
    r = subprocess.run(["node", "tests/client/dmcache_push_runtime.mjs"], cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0 and "OK dmcache push" in r.stdout, (r.stdout[-2000:], r.stderr[-2000:])
