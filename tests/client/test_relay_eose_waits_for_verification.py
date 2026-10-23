"""An EOSE never overtakes its own events while they are being verified (relay.js `_drained`).

With "use my own relays" on no connection is trusted, so every event waits for a batch verification; a batch the
timer has already taken off the queue used to be invisible to the EOSE, and the one-shot query resolved EMPTY and
complete. Found by tests/client/test_profile_and_follows_cross_device_full_app.py (a device re-opening its profile
did not show another device's edit, 3 runs in 8). RELAY_JS=<old relay.js> runs the fail-first copy.
"""
import os
import subprocess
from pathlib import Path


def test_eose_waits_for_events_still_being_verified():
    root = Path(__file__).resolve().parents[2]
    subprocess.run(['node', str(root / 'tests/client/relay_eose_waits_for_verification_runtime.mjs')],
                   cwd=root, check=True, timeout=15, env={**os.environ})
