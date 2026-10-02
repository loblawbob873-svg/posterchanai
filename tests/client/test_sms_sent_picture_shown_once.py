"""'phone shows the image sent once, desktop shows twice' / 'it says sending': a picture sent from a
computer stayed "sending" beside the phone's re-encoded sent copy for ever. Runs the shipped rebuild()."""
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.skipif(not shutil.which("node"), reason="node is required")
def test_a_picture_sent_from_a_computer_is_shown_once():
    run = subprocess.run(["node", str(ROOT / "tests/client/sms_sent_picture_shown_once_runtime.mjs")],
                         capture_output=True, text=True, timeout=60, cwd=ROOT)
    assert run.returncode == 0, run.stdout + run.stderr
    assert "OK a picture sent from a computer is shown once" in run.stdout
