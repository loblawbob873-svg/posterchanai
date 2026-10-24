"""The real reader honours the send-time floor (concord_send_stamp_runtime.mjs); see concord.js sendStamp."""
import subprocess
from pathlib import Path


def test_the_reader_stamps_a_message_no_earlier_than_its_floor():
    root = Path(__file__).resolve().parents[2]
    r = subprocess.run(["node", "tests/client/concord_send_stamp_runtime.mjs"], cwd=root, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr
