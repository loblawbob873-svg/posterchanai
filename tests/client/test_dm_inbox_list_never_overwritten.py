"""'npub14w4q... is saying we changed his DM relays': a PosterChan client replaced his kind-10050 (0xchat,
keychat, nostr21) with one naming only our relay, because it read the existing list from our pool alone.
Runs the shipped ensureDmInboxList under node."""
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.skipif(not shutil.which("node"), reason="node is required")
def test_an_existing_dm_relay_list_is_never_replaced():
    run = subprocess.run(["node", str(ROOT / "tests/client/dm_inbox_list_never_overwritten_runtime.mjs")],
                         capture_output=True, text=True, timeout=60, cwd=ROOT)
    assert run.returncode == 0, run.stdout + run.stderr
    assert "OK an existing DM relay list is never replaced" in run.stdout
