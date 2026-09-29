"""A live stream whose session was superseded on the same key reads as ended, and its owner retires it.
Runs the shipped app.js helpers and streams.js sweep — see stream_stale_live_runtime.cjs."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.skipif(not shutil.which("node"), reason="node required")
def test_a_superseded_live_stream_is_over():
    r = subprocess.run(["node", "tests/client/stream_stale_live_runtime.cjs"], cwd=ROOT,
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr[-2000:]
    out = json.loads(r.stdout)
    bad = {k: v["detail"] for k, v in out.items() if not v["ok"]}
    assert not bad, bad
    assert len(out) == 3
