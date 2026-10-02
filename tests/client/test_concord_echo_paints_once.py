"""'communities is flashing now on desktop': every read of a room re-acknowledged an already-sent
message and repainted the whole list. Runs the shipped acknowledgeDeliveryEcho/paintDelivery."""
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.skipif(not shutil.which("node"), reason="node is required")
def test_a_sent_message_is_acknowledged_once():
    run = subprocess.run(["node", str(ROOT / "tests/client/concord_echo_paints_once_runtime.mjs")],
                         capture_output=True, text=True, timeout=60, cwd=ROOT)
    assert run.returncode == 0, run.stdout + run.stderr
    assert "OK a sent message is acknowledged once" in run.stdout
