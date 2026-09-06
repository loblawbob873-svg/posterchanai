import shutil
import subprocess
from pathlib import Path

import pytest


def test_terminal_hosts_survive_remount_and_signer_recovery():
    if not shutil.which('node'):
        pytest.skip('Node unavailable')
    script = Path(__file__).with_name('terminal_hosts_runtime.mjs')
    result = subprocess.run(['node', str(script)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
