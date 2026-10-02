"""scripts/check_quote_modal.py had been failing ("quote 740x380: textarea squeezed to 64px") with
nothing blocking a deploy: browser checks run from ./test.sh, the deploy gate runs pytest. This puts
the composer-fits check in the gate. Exit 2 is the check's "could not run" -- a skip, never a pass."""
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def test_the_compose_sheet_fits_every_screen_with_post_reachable():
    env = dict(os.environ)
    env.setdefault("PC_CHECK_PORT", str(9400 + os.getpid() % 500))
    env["PC_CHECK_PROFILE"] = tempfile.mkdtemp(prefix="pc-quote-modal-")
    run = subprocess.run([sys.executable, str(ROOT / "scripts/check_quote_modal.py")],
                         capture_output=True, text=True, timeout=600, cwd=ROOT, env=env)
    if run.returncode == 2:
        pytest.skip("check could not run: " + (run.stdout + run.stderr)[-400:])
    assert run.returncode == 0, (run.stdout + run.stderr)[-3000:]
