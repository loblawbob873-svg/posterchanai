"""Choosing an area: only slurp's own "selection cancelled" is a change of mind; any other failure is
said out loud (desktop/screenshot.js). It used to read every slurp failure as Escape and return
nothing — on screen, a button that does nothing ("selecting region does nothing")."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _capture(tmp_path, stderr, code):
    slurp = tmp_path / "slurp"
    slurp.write_text(f"#!/bin/sh\necho '{stderr}' >&2\nexit {code}\n")
    slurp.chmod(0o755)
    js = ("require(" + json.dumps(str(ROOT / "desktop/screenshot.js")) + ").capture({mode:'region'})"
          ".then(r=>process.stdout.write(JSON.stringify(r)))")
    r = subprocess.run(["node", "-e", js], env={"PATH": "/usr/bin:/bin", "PC_SLURP": str(slurp), "HOME": str(tmp_path)},
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


@pytest.mark.skipif(not shutil.which("node"), reason="node required")
def test_escape_is_still_a_quiet_cancel(tmp_path):
    got = _capture(tmp_path, "selection cancelled", 1)
    assert got.get("cancelled") is True and not got.get("why"), got


@pytest.mark.skipif(not shutil.which("node"), reason="node required")
def test_a_real_failure_says_why(tmp_path):
    got = _capture(tmp_path, "failed to connect to display", 1)
    assert not got.get("cancelled") and "failed to connect to display" in got.get("why", ""), got
