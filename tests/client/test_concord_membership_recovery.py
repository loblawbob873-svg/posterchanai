from pathlib import Path
import subprocess
ROOT = Path(__file__).resolve().parents[2]

def test_actual_signed_membership_fragment_recovery():
    run = subprocess.run(['node', str(ROOT / 'tests/client/concord_membership_recovery_runtime.mjs')], capture_output=True, text=True, timeout=30)
    assert run.returncode == 0, run.stdout + run.stderr
    assert 'account isolation passed' in run.stdout
