from pathlib import Path
import subprocess
ROOT=Path(__file__).resolve().parents[2]
def test_actual_external_reads_and_room_publication_authenticate_as_current_user():
    result=subprocess.run(['node','tests/client/relay_external_auth_runtime.mjs','static/js/client/relay.js'],cwd=ROOT,capture_output=True,text=True,timeout=20)
    assert result.returncode==0,result.stdout+'\n'+result.stderr
