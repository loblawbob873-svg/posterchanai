"""Real user-wallet probes cannot adopt old-account or superseded responses."""
from pathlib import Path
import subprocess
import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("scenario", ["account_cache", "late_success", "late_failure", "account_pending",
    "late_status", "late_history", "late_history_failure", "render_success", "render_failure",
    "node_account_cache", "node_account_pending", "render_account_pending", "tip_account_pending", "logout_cache"])
def test_user_wallet_probe_account_and_response_order(scenario):
    result = subprocess.run(['node', str(Path(__file__).with_suffix('.mjs')), scenario],
                            cwd=ROOT, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert scenario + ' passed' in result.stdout
