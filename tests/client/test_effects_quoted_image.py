"""Execute the actual Effects action and image resolver, including the reported quote."""
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[2]


def test_effects_use_quoted_images_and_keep_original_reply_target():
    result = subprocess.run(
        ['node', str(ROOT / 'tests/client/effects_quoted_image_runtime.mjs'),
         str(ROOT / 'static/js/client/app.js')],
        capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.count('PASS ') == 15
