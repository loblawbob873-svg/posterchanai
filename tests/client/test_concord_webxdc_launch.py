from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[2]


def test_webxdc_cards_do_not_block_concord_launch():
    result = subprocess.run(["node", str(ROOT / "tests/client/concord_webxdc_launch_runtime.mjs")], cwd=ROOT, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
