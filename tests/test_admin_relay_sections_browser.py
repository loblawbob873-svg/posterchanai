"""Runs scripts/check_admin_relay_sections.py (the shipped admin page in a real Chrome at 390px and 1280px):
the Relay tab's sections, Limits/Database contents, the Database status panel, and a Save round trip of a moved
field. Exit 2 from the check means it could not run here (no Chrome) and is a skip, never a pass."""
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_relay_sections_in_a_real_browser(tmp_path):
    env = dict(os.environ, PC_CHECK_SHOTS=str(tmp_path / "shots"))
    r = subprocess.run([sys.executable, str(ROOT / "scripts/check_admin_relay_sections.py")], cwd=ROOT, env=env,
                       capture_output=True, text=True, timeout=240)
    if r.returncode == 2:
        pytest.skip(r.stdout.strip() or r.stderr.strip()[-300:])
    assert r.returncode == 0, r.stdout[-3000:] + r.stderr[-3000:]
