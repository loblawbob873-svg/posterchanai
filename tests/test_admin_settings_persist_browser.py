"""Runs scripts/check_admin_settings_persist.py: the shipped admin page in a real Chrome, served by the real admin
router over the real settings store and a relay on a socket. Every admin field added 2026-10-10/11 (Extra Tor
daemons, the PosterChanDB mode, the 17 relay limits/tunables) is typed, saved with the page's Save button, the
node's settings are restarted from the relay alone, the page reloads and each field must show what was typed --
then the values are put back ("auto", the blank Automatic mode, a default) and checked the same way; "1e3" is
stored as 1000 and the page still loads; an out-of-range value is refused with a reason and never reaches the
relay. Exit 2 from the check means it could not run here (no Chrome) and is a skip, never a pass."""
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_admin_settings_survive_save_restart_and_reload_in_a_real_browser():
    r = subprocess.run([sys.executable, str(ROOT / "scripts/check_admin_settings_persist.py")], cwd=ROOT,
                       env=dict(os.environ), capture_output=True, text=True, timeout=300)
    if r.returncode == 2:
        pytest.skip(r.stdout.strip() or r.stderr.strip()[-300:])
    assert r.returncode == 0, r.stdout[-3000:] + r.stderr[-3000:]
