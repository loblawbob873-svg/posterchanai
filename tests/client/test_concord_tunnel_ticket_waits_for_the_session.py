"""A Concord tunnel ticket asked for before the app session exists makes the session and asks again, instead of
switching the tunnel off (which sent Concord DIRECT to outside relays for five minutes). Runs the shipped
refreshTunnel under node: tests/client/concord_tunnel_ticket_waits_for_the_session_runtime.mjs."""
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.skipif(not shutil.which("node"), reason="node required")
def test_a_ticket_refused_before_sign_in_is_asked_for_again():
    r = subprocess.run(["node", "tests/client/concord_tunnel_ticket_waits_for_the_session_runtime.mjs"],
                       cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0 and "OK" in r.stdout, r.stdout + r.stderr
