"""Real NIP-29 event folding across deferred history/live overlap and owner changes."""
import subprocess
from pathlib import Path

def test_nip29_history_overlap_keeps_messages_and_authorized_deletions():
    p=subprocess.run(['node',str(Path(__file__).with_name('concord_nip29_overlap_runtime.mjs'))],capture_output=True,text=True,timeout=30)
    assert p.returncode==0,p.stdout+p.stderr
