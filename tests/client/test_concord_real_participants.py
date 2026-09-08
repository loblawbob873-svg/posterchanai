from pathlib import Path
import subprocess
ROOT=Path(__file__).resolve().parents[2]
def test_actual_cord_groups_are_not_human_members():
    result=subprocess.run(['node',str(ROOT/'tests/client/concord_real_participants_runtime.mjs')],capture_output=True,text=True,timeout=30)
    assert result.returncode==0,result.stdout+result.stderr
