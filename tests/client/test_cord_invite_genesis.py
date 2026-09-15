from pathlib import Path
import subprocess
ROOT = Path(__file__).resolve().parents[2]

def test_independent_split_genesis_and_invite_validation():
    run = subprocess.run(['node', str(ROOT / 'tests/client/cord_invite_genesis_runtime.mjs')], capture_output=True, text=True, timeout=30)
    assert run.returncode == 0, run.stdout + run.stderr
    assert 'bounds and UTF-8 passed' in run.stdout
