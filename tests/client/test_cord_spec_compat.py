from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[2]


def test_independent_current_cord_wire_compatibility():
    result = subprocess.run(['node', str(ROOT / 'tests/client/cord_spec_compat_runtime.mjs')], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'metadata preservation passed' in result.stdout
