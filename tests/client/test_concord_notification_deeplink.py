import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_exact_concord_notification_route_executes():
    node = shutil.which("node")
    assert node
    result = subprocess.run(
        [node, str(ROOT / "tests/client/concord_notification_deeplink_runtime.mjs")],
        cwd=ROOT, text=True, capture_output=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "runtime: ok" in result.stdout


def test_android_phone_shell_consumes_exact_route_once():
    js = (ROOT / "static/js/client/phoneshell.js").read_text()
    assert "v.indexOf('concord:')===0" in js
    assert "PCConcord.openNotification" in js
    assert "landView('concord')" in js
