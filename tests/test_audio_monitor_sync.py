"""PosterChanOS: a mute or volume change on one monitor shows on every monitor at once.

Reported: "Audio mixer is not in sync with other monitor, one says muted, the other says unmuted".
Each monitor is its own page with its own copy of the audio state, which it re-read only on a
compositor event or a 30s timer. tests/client/audio_monitor_sync_runtime.cjs loads the SHIPPED
osshell.js twice (two monitors, one machine, one localStorage) and mutes on one."""
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(not shutil.which("node"), reason="node required")
def test_muting_on_one_monitor_shows_on_the_other():
    r = subprocess.run(["node", "tests/client/audio_monitor_sync_runtime.cjs"], cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0 and "OK audio sync" in r.stdout, (r.stdout[-2000:], r.stderr[-2000:])
