"""Run the shipped Office session and click Close from each Files source/surface."""
from pathlib import Path
import shutil
import subprocess

import pytest


@pytest.mark.skipif(not shutil.which('node'), reason='node is required')
@pytest.mark.parametrize('source', ['public', 'computer', 'sync', 'office', 'native', 'modal'])
def test_office_close_returns_to_its_launcher(source):
    runtime = Path(__file__).with_name('office_close_return_runtime.mjs')
    result = subprocess.run(['node', str(runtime), source], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr
