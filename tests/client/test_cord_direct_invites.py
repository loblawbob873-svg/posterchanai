"""Independent standard NIP-59 producer and actual worker exercise CORD-05 invites."""
from pathlib import Path
import subprocess


def test_direct_invitation_wire_and_worker():
    script = Path(__file__).with_suffix('.mjs')
    result = subprocess.run(['node', str(script)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'direct invitation wire passed' in result.stdout
