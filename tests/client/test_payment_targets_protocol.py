"""NIP-A3 interoperability with signed events, outages and adversarial relay responses."""
from pathlib import Path
import subprocess

import pytest

ROOT=Path(__file__).resolve().parents[2]


@pytest.mark.parametrize('case',[
    'format','malformed','safe_uri','preserve_extensions','newest','forgery','same_second',
    'outage_keeps_targets','negative_cache_recovers','live_update','empty_cache_live_update',
    'clear','coalesce','account_isolation','deletion_invalidates_cache','inflight_accept','inflight_live_update',
])
def test_payment_target_protocol(case):
    got=subprocess.run(['node',str(ROOT/'tests/client/payment_targets_protocol_runtime.mjs'),case],
                       capture_output=True,text=True,timeout=30,cwd=ROOT)
    assert got.returncode==0,got.stdout+got.stderr
