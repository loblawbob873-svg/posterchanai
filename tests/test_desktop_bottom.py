import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_wayfire_desktop_never_covers_normal_windows():
    run = subprocess.run(
        ["node", str(ROOT / "tests/client/desktop_bottom_sim.js")],
        cwd=ROOT, text=True, capture_output=True, timeout=10,
    )
    assert run.returncode == 0, run.stderr
    assert "behavioral simulation: ok" in run.stdout
