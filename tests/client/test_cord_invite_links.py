"""CORD-05 encrypted creator list, wire links, and authenticated registry updates."""
from pathlib import Path
import subprocess


def test_creator_invite_links():
    result = subprocess.run(['node', str(Path(__file__).with_suffix('.mjs'))], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'creator invite links passed' in result.stdout
