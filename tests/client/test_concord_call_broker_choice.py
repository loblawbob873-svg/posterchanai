"""Runs tests/client/concord_call_broker_runtime.mjs: which broker a Concord call starts with."""
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.skipif(not shutil.which("node"), reason="node required")
def test_calls_go_through_this_instance_when_it_hosts_them():
    r = subprocess.run(["node", str(ROOT / "tests/client/concord_call_broker_runtime.mjs")], cwd=ROOT,
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stdout[-2000:] + r.stderr[-3000:]
