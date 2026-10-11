"""Unread DMs badge Messages only -- never the notification bell (see the .mjs)."""
import subprocess
from pathlib import Path


def test_dms_never_light_the_bell_and_mentions_still_do():
    r = subprocess.run(["node", str(Path(__file__).with_name("bell_dms_runtime.mjs"))], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr
