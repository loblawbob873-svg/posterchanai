"""NIP-51 private mutes — tests/client/private_mutes_runtime.cjs runs the shipped mute code under node.

Reported: a user muting a fediverse account got "safety: refused to erase your mute list" /
"replaceable-list shrink guard: 7<21". His newest mute list keeps most entries ENCRYPTED (NIP-51
private items, written by another client); the guard counted public tags only, and PosterChan never
applied the private mutes at all."""
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.skipif(not shutil.which("node"), reason="node required")
def test_private_mutes_are_applied_kept_private_and_counted_by_the_guard():
    r = subprocess.run(["node", "tests/client/private_mutes_runtime.cjs"], cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0 and "OK private mutes" in r.stdout, (r.stdout[-3000:], r.stderr[-3000:])
