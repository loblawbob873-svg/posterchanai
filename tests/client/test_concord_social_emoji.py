from pathlib import Path
import shutil
import subprocess
import pytest

@pytest.mark.skipif(shutil.which('node') is None, reason='Node is required')
def test_social_emoji_render_and_publish_in_concord():
    result = subprocess.run(['node', str(Path(__file__).with_name('concord_social_emoji_runtime.mjs'))],
                            capture_output=True, text=True, timeout=45)
    assert result.returncode == 0, result.stdout + result.stderr
