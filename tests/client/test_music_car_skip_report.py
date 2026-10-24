"""A slow or failed skip from the car is written into the phone's Music & car controls report
(music_car_skip_report_runtime.mjs; the shipped MusicPlayer.play)."""
import subprocess
from pathlib import Path


def test_a_slow_or_failed_car_skip_is_reported():
    root = Path(__file__).resolve().parents[2]
    r = subprocess.run(["node", "tests/client/music_car_skip_report_runtime.mjs"], cwd=root, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr
