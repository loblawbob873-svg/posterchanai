"""The desktop Now-playing widget lets go of a Music window that is gone (TV, 2026-10-13: "music player shows playing
despite me closing the music player" / "the desktop widget won't even shuffle music or play it").
Runs tests/client/music_widget_stale_window_runtime.mjs against the shipped os.js."""
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_a_music_window_that_went_quiet_no_longer_owns_the_widget():
    r = subprocess.run(["node", str(ROOT / "tests/client/music_widget_stale_window_runtime.mjs")],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr
