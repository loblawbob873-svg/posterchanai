"""ClientSettings.get() must not re-parse the whole settings blob per read (see settings_cache_runtime.cjs)."""
import shutil
import subprocess
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent


@pytest.mark.skipif(not shutil.which("node"), reason="node required")
def test_settings_are_parsed_once_and_stay_correct_runtime():
    r = subprocess.run(["node", str(HERE / "settings_cache_runtime.cjs")], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr
