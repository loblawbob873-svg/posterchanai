"""CORD-08 independent encrypted vectors, generated outside the CORD implementation."""
import subprocess
from pathlib import Path


def test_cord08_expiration_wire_and_policy():
    result = subprocess.run(['node', str(Path(__file__).with_name('cord_expiration_runtime.mjs'))], capture_output=True, text=True, timeout=40)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'CORD08_OK' in result.stdout
