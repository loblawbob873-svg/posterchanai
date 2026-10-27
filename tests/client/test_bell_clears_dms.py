"""Unread DMs light the bell in Desktop mode and PosterChanOS only; elsewhere they badge Messages (see the .mjs)."""
import subprocess
from pathlib import Path


def test_dms_light_the_bell_only_in_the_desktop_and_mentions_always_do():
    r = subprocess.run(["node", str(Path(__file__).with_name("bell_dms_runtime.mjs"))], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr
