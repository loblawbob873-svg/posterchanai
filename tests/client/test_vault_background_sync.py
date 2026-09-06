from pathlib import Path
import shutil
import subprocess
import pytest

@pytest.mark.skipif(shutil.which('node') is None, reason='Node is required')
def test_autofill_syncs_without_opening_passwords():
    script = Path(__file__).with_name('vault_background_sync_runtime.mjs')
    result = subprocess.run(['node', str(script)], capture_output=True, text=True, timeout=45)
    assert result.returncode == 0, result.stdout + result.stderr
