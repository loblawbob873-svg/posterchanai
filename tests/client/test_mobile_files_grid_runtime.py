import subprocess, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_mobile_files_grid_runtime_geometry_and_interactions():
    p = subprocess.run(
        [sys.executable, str(ROOT / "scripts/check_mobile_files_grid.py")],
        cwd=ROOT, text=True, capture_output=True,
    )
    assert p.returncode in (0, 2), p.stdout + p.stderr
