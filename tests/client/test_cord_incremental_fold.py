from pathlib import Path
import subprocess
ROOT=Path(__file__).resolve().parents[2]
def test_actual_encrypted_incremental_fold():
    result=subprocess.run(['node',str(ROOT/'tests/client/cord_incremental_fold_runtime.mjs')],capture_output=True,text=True,timeout=30)
    assert result.returncode==0,result.stdout+result.stderr
