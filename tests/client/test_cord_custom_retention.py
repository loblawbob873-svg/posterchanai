"""Concord Keep-history is stored the way the CORD spec says a client setting must be: inside `custom`
under a prefixed key, merged (never replacing other clients' keys), and never turned into message expiry.
See tests/client/cord_custom_retention_runtime.mjs for the rules and their spec sections."""
import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.skipif(not shutil.which("node"), reason="node required")
def test_keep_history_rides_custom_and_round_trips_other_clients_keys():
    r = subprocess.run(["node", str(Path(__file__).with_name("cord_custom_retention_runtime.mjs"))],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "cord custom retention ok" in r.stdout
