"""Alt+Tab shows a picture for every window, on either monitor, without waiting on full-size captures.

Reported: "alt+tab communities, social, git showed no preview, preview generation was slow for all" and
"email showed no preview either". tests/client/alttab_preview_runtime.cjs runs the shipped handler."""
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
OS = (ROOT / "static/js/client/os.js").read_text()


@pytest.mark.skipif(not shutil.which("node"), reason="node required")
def test_the_preview_handler_serves_every_monitor_as_a_thumbnail():
    r = subprocess.run(["node", "tests/client/alttab_preview_runtime.cjs"], cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0 and "OK alttab preview" in r.stdout, (r.stdout[-2000:], r.stderr[-2000:])


def test_pictures_are_warmed_in_the_background_one_at_a_time():
    """A window that just lost focus is captured then, so the chooser opens with pictures."""
    assert "_warmNativePreviews(rows, _wasFocused);" in OS
    warm = OS[OS.index("function _warmNext(){"):OS.index("const _switchRows=()=>{")]
    assert "if(_warmBusy||!_warmQueue.length) return;" in warm, "captures must not pile up"
    assert "_nativePreviewCache.set(id,d)" in warm
