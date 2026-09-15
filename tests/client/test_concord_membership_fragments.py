from pathlib import Path
import subprocess
ROOT = Path(__file__).resolve().parents[2]

def test_actual_encrypted_cord_membership_fragments():
    run = subprocess.run(['node', str(ROOT / 'tests/client/concord_membership_fragments_runtime.mjs')], capture_output=True, text=True, timeout=60)
    assert run.returncode == 0, run.stdout + run.stderr
    assert 'account races passed' in run.stdout
