from pathlib import Path
import subprocess
ROOT=Path(__file__).resolve().parents[2]
def test_actual_timer_metadata_and_channel_notice_fanout():
    run=subprocess.run(['node',str(ROOT/'tests/client/concord_timer_settings_runtime.mjs')],capture_output=True,text=True,timeout=30)
    assert run.returncode==0,run.stdout+run.stderr
    assert 'account safety passed' in run.stdout
