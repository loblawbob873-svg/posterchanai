"""Execute shipped notification routing and native window recipient handoff."""
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[2]


def test_sms_notification_routes_across_browser_cold_and_existing_native_windows():
    result = subprocess.run(['node', str(ROOT/'tests/client/sms_notification_routes_runtime.cjs'), str(ROOT)],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr
