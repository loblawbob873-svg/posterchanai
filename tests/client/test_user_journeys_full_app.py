"""Runs scripts/check_user_journeys.py in the deploy gate.

The gate runs `tests/**/test_*.py`, not `scripts/check_*.py`; this file is what puts the user journeys (sign in
with an nsec, post, reply, react, DM, Bad Post vote, Concord with many rooms live, Files, a mention to the bell and
into its thread -- on a phone and on a desktop, against a full local node with a real relay) in front of every
deploy that changes anything a browser loads. It is a BROWSER test by the gate's own rule (it names a
scripts/check_*.py), so a server-only deploy leaves it out, like every other browser test.

Exit 2 from the check = could not run here (no Chrome / app deps): a skip with its reason, never a pass.
"""
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def test_the_user_journeys_pass_on_a_phone_and_a_desktop():
    env = dict(os.environ)
    r = subprocess.run([sys.executable, str(ROOT / 'scripts/check_user_journeys.py')], cwd=ROOT, env=env,
                       capture_output=True, text=True, timeout=600)
    if r.returncode == 2:
        pytest.skip(r.stdout.strip()[-300:] or r.stderr.strip()[-300:])
    assert r.returncode == 0, r.stdout[-6000:] + r.stderr[-3000:]
