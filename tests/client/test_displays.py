import shutil
import subprocess
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parents[2]

@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_real_display_backend_validates_previews_persists_and_reverts():
    r = subprocess.run(["node", str(ROOT / "tests/client/test_displays.js")],
                       cwd=ROOT, capture_output=True, text=True, timeout=20)
    assert r.returncode == 0, r.stderr
    assert "ALL OK" in r.stdout

def test_display_bridge_is_available_only_to_the_bundled_page():
    preload=(ROOT/"desktop/preload.js").read_text()
    main=(ROOT/"desktop/main.js").read_text()
    assert "contextBridge.exposeInMainWorld('pcDisplays'" in preload
    for verb in ("status","preview","confirm","revert"):
        assert f"pc:display:{verb}" in main
