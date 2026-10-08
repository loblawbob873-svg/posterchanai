"""The 1.5 s celebration sweep reads layout inside requestAnimationFrame, not off its timer (see the .cjs)."""
import shutil
import subprocess
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent


@pytest.mark.skipif(not shutil.which("node"), reason="node required")
def test_the_sweep_reads_layout_only_inside_a_frame_runtime():
    r = subprocess.run(["node", str(HERE / "celebration_sweep_runtime.cjs")], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr
