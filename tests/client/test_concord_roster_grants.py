"""Real encrypted control editions project authenticated, current silent members."""
from pathlib import Path
import subprocess

def test_authenticated_grants_and_revocation_reach_member_projection():
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run(['node',str(Path(__file__).with_name('concord_roster_grants_runtime.mjs'))],cwd=root,text=True,capture_output=True,timeout=30)
    assert result.returncode == 0, result.stderr
