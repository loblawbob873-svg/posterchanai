"""Signed-event reciprocal filtering and the real User Settings control flow."""
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[2]


def test_signed_public_mute_reconciliation():
    result = subprocess.run(
        ['node', str(ROOT / 'tests/client/auto_mute_runtime.mjs'),
         str(ROOT / 'static/js/client/auto-mute.js'),
         str(ROOT / 'static/vendor/nostr/nostr.bundle.js')],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'TOTAL 27' in result.stdout
