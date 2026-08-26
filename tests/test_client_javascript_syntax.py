from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required for the desktop build")
@pytest.mark.parametrize("script", ["static/js/client/app.js", "static/js/client/os.js"])
def test_primary_client_controllers_parse(script):
    result = subprocess.run(
        ["node", "--check", str(ROOT / script)],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
