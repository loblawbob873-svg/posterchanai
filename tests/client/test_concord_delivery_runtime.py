"""Actual Concord handlers plus actual room-scoped Relay publication; no network posts."""
from pathlib import Path
import subprocess

ROOT=Path(__file__).resolve().parents[2]


def test_concord_signer_ack_retry_reload_and_account_ownership(tmp_path):
    setup=(ROOT/'tests/client/concord_runtime.mjs').read_text().split('// Message actions stay collapsed',1)[0]
    scenario=(ROOT/'tests/client/concord_delivery_runtime.mjs').read_text()
    script=tmp_path/'delivery.mjs';script.write_text(setup+'\n'+scenario)
    result=subprocess.run(['node',str(script),'static/js/client/concord.js'],cwd=ROOT,capture_output=True,text=True,timeout=30)
    assert result.returncode==0,result.stdout+'\n'+result.stderr
