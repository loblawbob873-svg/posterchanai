from pathlib import Path
import subprocess
ROOT=Path(__file__).resolve().parents[2]
def test_private_channel_key_and_role_proofs():
    run=subprocess.run(['node',str(ROOT/'tests/client/concord_private_channels_runtime.mjs')],capture_output=True,text=True,timeout=30)
    assert run.returncode==0,run.stdout+run.stderr
    assert 'invite secret allowlist passed' in run.stdout
