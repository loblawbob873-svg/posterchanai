"""Opening Notifications empties the bell, unread DMs included (see the .mjs)."""
import subprocess
from pathlib import Path


def test_opening_notifications_empties_the_bell_of_dms_too():
    r = subprocess.run(["node", str(Path(__file__).with_name("bell_dms_runtime.mjs"))], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr
