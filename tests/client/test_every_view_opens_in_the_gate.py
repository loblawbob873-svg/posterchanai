"""scripts/check_every_view_opens.py opens every screen of the client and fails on any JavaScript error.
Chrome 153 stopped writing DevToolsActivePort for a fixed --remote-debugging-port, so the check SKIPPED
on every suite run and verified nothing -- and a skip blocks no deploy. Here, in the gate, on a machine
that HAS Chrome, "could not run" is a failure: a gate that cannot look must not report what it saw."""
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CHROME = Path("/opt/google/chrome/chrome")


@pytest.mark.skipif(not CHROME.exists(), reason="Chrome required")
def test_every_screen_opens_with_no_javascript_errors():
    env = dict(os.environ)
    env["PC_CHECK_PORT"] = str(9000 + os.getpid() % 900)        # a FIXED port, as checkall passes
    env["PC_CHECK_PROFILE"] = tempfile.mkdtemp(prefix="pc-every-view-")
    run = subprocess.run([sys.executable, str(ROOT / "scripts/check_every_view_opens.py")],
                         capture_output=True, text=True, timeout=900, cwd=ROOT, env=env)
    out = (run.stdout + run.stderr)[-3000:]
    assert run.returncode != 2, "the every-screen check could not run on a machine with Chrome:\n" + out
    assert run.returncode == 0, out
    assert "every screen opened" in run.stdout, out
