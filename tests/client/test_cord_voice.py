from pathlib import Path
import subprocess


def test_independent_concord_voice_vectors():
    root=Path(__file__).resolve().parents[2]
    result=subprocess.run(['node',str(root/'tests/client/cord_voice_runtime.mjs')],cwd=root,capture_output=True,text=True,timeout=30)
    assert result.returncode==0,result.stdout+result.stderr
    assert 'broker grants passed' in result.stdout


def test_shipped_livekit_frame_worker_sender_interoperability():
    root=Path(__file__).resolve().parents[2]
    result=subprocess.run(['node',str(root/'tests/client/cord_voice_frame_runtime.mjs')],cwd=root,capture_output=True,text=True,timeout=15)
    assert result.returncode==0,result.stdout+result.stderr
    assert 'nonce separation passed' in result.stdout
