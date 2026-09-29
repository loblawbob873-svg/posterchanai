"""PosterChanOS Alt+Tab is ONE list across every monitor — tests/client/alt_tab_unified_sim.js runs the
shipped switcher (os.js) on two renderers joined by the shipped gatherSwitchRows (desktop/main.js)."""
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(not shutil.which("node"), reason="node required")
def test_alt_tab_lists_every_monitors_windows_and_focuses_them_where_they_live():
    r = subprocess.run(["node", "tests/client/alt_tab_unified_sim.js"], cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0 and "OK unified Alt+Tab" in r.stdout, (r.stdout[-3000:], r.stderr[-3000:])


def test_the_preload_bridge_exposes_both_calls_and_main_answers_them():
    pre = (ROOT / "desktop/preload.js").read_text()
    main = (ROOT / "desktop/main.js").read_text()
    for name, channel in (("switchRowsElsewhere", "pc:wm:switch-rows-elsewhere"), ("focusElsewhere", "pc:wm:focus-elsewhere")):
        assert name in pre and f"'{channel}'" in pre, name
        assert f"ipcMain.handle('{channel}'" in main, channel
