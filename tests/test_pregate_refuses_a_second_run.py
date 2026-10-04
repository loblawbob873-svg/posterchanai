"""scripts/pregate.py refuses to start while another run holds the checkout.

Two runs share .pregate.<shard>.log. On 2026-10-04 a second run overwrote the first's logs, the first was
stopped as a duplicate, and a deploy went out on a run that never covered all of its commits -- deploy 81
then aborted in the 50-minute gate on exactly the tests the stopped run would have failed.
"""
import fcntl
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_a_second_run_is_refused_while_the_first_holds_the_lock():
    with open(ROOT / ".pregate.lock", "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        r = subprocess.run([sys.executable, "scripts/pregate.py", "--base", "HEAD"], cwd=ROOT,
                           capture_output=True, text=True, timeout=60)
    assert r.returncode == 2 and "already running" in r.stdout, (r.returncode, r.stdout, r.stderr)


def test_listing_is_never_blocked():
    with open(ROOT / ".pregate.lock", "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        r = subprocess.run([sys.executable, "scripts/pregate.py", "--list", "--base", "HEAD"], cwd=ROOT,
                           capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr
