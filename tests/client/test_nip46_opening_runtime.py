from pathlib import Path
import subprocess

ROOT=Path(__file__).resolve().parents[2]

def test_nip46_connection_attempt_ownership():
    result=subprocess.run(['node',str(ROOT/'tests/client/nip46_opening_runtime.cjs'),str(ROOT/'static/js/client/app.js')],capture_output=True,text=True,timeout=10)
    assert result.returncode==0,result.stderr
    assert 'PASS initial/revive' in result.stdout
