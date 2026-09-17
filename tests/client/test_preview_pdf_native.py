"""Puts preview_pdf_native_sim.js into the suite: a PDF that fails mid-document keeps its rendered
pages, an oversized PDF is saved with a reason instead of being pushed through the native bridge, and
a small one opens in the phone's viewer."""
import shutil
import subprocess
from pathlib import Path

import pytest

SIM = Path(__file__).parent / "preview_pdf_native_sim.js"


@pytest.mark.skipif(not shutil.which("node"), reason="node is required")
def test_pdf_native_preview():
    r = subprocess.run(["node", str(SIM)], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0 and "pdf native preview holds" in r.stdout, r.stdout + r.stderr
