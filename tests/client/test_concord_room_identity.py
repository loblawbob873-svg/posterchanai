"""Concord uses decoded invite identity rather than routing hints or display names."""
import subprocess
from pathlib import Path


def test_real_cord_invite_identity():
    root = Path(__file__).resolve().parents[2]
    subprocess.run(['node', str(root / 'tests/client/concord_room_identity_runtime.mjs')],
                   cwd=root, check=True, capture_output=True, text=True, timeout=30)
