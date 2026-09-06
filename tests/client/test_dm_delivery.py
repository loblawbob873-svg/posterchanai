"""Exercise shipped DM startup with stalled cache/history and live server notifications."""
from pathlib import Path
import shutil
import subprocess
import pytest

@pytest.mark.skipif(not shutil.which('node'), reason='needs node')
def test_windows_dm_delivery_during_startup_and_history_failure():
    script = Path(__file__).with_name('dm_delivery_runtime.mjs')
    result = subprocess.run(['node', str(script)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'ok:' in result.stdout
