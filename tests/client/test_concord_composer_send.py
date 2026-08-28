import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_concord_send_clears_immediately_and_failure_restores_safely():
    node = shutil.which("node")
    assert node, "node is required for the Concord composer runtime regression"
    result = subprocess.run(
        [node, str(ROOT / "tests/client/concord_composer_send_runtime.mjs")],
        cwd=ROOT,
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Concord composer send transaction ok" in result.stdout
