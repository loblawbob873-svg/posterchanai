import shutil
import subprocess
from pathlib import Path
import pytest


def test_authentication_does_not_wait_for_background_decryption():
    if not shutil.which('node'):
        pytest.skip('Node unavailable')
    result = subprocess.run(['node', str(Path(__file__).with_name('extension_interactive_priority_runtime.mjs'))],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
