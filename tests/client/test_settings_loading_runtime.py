from pathlib import Path
import subprocess

def test_settings_pending_signer_status_and_timer_cleanup():
    root=Path(__file__).resolve().parents[2]
    result=subprocess.run(['node',str(Path(__file__).with_name('settings_loading_runtime.cjs')),str(root/'static/js/client/app.js')],capture_output=True,text=True,timeout=10)
    assert result.returncode==0,result.stderr
