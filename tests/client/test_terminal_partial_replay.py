from pathlib import Path
import subprocess

def test_partial_replay_overlap_is_not_drawn_twice():
    result=subprocess.run(['node',str(Path(__file__).with_name('terminal_partial_replay_runtime.mjs'))],capture_output=True,text=True,timeout=30)
    assert result.returncode==0,result.stdout+result.stderr
