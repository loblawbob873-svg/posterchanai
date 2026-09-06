from pathlib import Path
import subprocess

def test_terminal_utf8_survives_split_process_output():
    result=subprocess.run(['node',str(Path(__file__).with_name('terminal_utf8_stream_runtime.mjs'))],capture_output=True,text=True,timeout=30)
    assert result.returncode==0,result.stdout+result.stderr
