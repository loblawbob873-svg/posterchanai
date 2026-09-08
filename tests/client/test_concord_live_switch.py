"""Deferred history cannot replace the selected stream or cross the account boundary."""
import subprocess
from pathlib import Path

def test_delayed_concord_history_keeps_current_room_and_account():
    p=subprocess.run(['node',str(Path(__file__).with_name('concord_live_switch_review.mjs'))],capture_output=True,text=True,timeout=30)
    assert p.returncode==0,p.stdout+p.stderr
