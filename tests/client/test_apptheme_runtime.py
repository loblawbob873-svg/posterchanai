"""Settings -> Appearance's painter -- see apptheme_runtime.mjs."""
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_the_look_paints_scopes_and_syncs():
    r = subprocess.run(["node", "tests/client/apptheme_runtime.mjs"], cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0 and r.stdout.strip().endswith("OK"), r.stdout + r.stderr
