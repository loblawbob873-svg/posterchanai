"""The VM network choice (NAT network / Linux bridge / user-mode NAT) in the SHIPPED vms.js — which
options each host offers, what Create and Settings send, and that "This computer" hands the choice to
window.pcVM. See tests/client/vms_network_runtime.mjs for the scenarios."""
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.skipif(not shutil.which("node"), reason="node is required")
def test_vm_network_choice():
    r = subprocess.run(["node", str(ROOT / "tests/client/vms_network_runtime.mjs")],
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "ALL OK" in r.stdout
