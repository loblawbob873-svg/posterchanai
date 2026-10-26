"""A GIF community banner keeps its animation (Vyram, "Community Banners Do Not Animate", 2026-10-26).
Runs the shipped bannerImageBytes under node: concord_gif_banner_runtime.mjs."""
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.skipif(not shutil.which("node"), reason="node required")
def test_a_gif_banner_is_not_flattened_and_a_photo_still_is():
    r = subprocess.run(["node", "tests/client/concord_gif_banner_runtime.mjs"], cwd=ROOT, capture_output=True, text=True,
                       timeout=60)
    assert r.returncode == 0 and "OK" in r.stdout, r.stdout + r.stderr
