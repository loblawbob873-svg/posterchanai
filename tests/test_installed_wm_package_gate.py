"""The installed WM package gate must exercise live late-fork ancestry."""
from pathlib import Path
import os
import subprocess
import sys

from scripts import checkall


ROOT = Path(__file__).resolve().parents[1]


def test_gate_extracts_wm_from_the_immutable_asar():
    gate = (ROOT / "scripts/check_installed_wm_package.sh").read_text(encoding="utf-8")
    assert "PC_INSTALLED_ASAR:-/opt/posterchan/resources/app.asar" in gate
    assert "extract-file \"$asar\" wm.js" in gate
    assert "PC_INSTALLED_WM_JS=" in gate
    assert "installed_wm_ancestry_sim.js" in gate
    assert 'extract-file "$asar" clipboard.js' in gate
    assert "PC_INSTALLED_CLIPBOARD_JS=" in gate
    assert "installed_clipboard_sim.js" in gate
    assert 'extract-file "$asar" www/static/js/client/os.js' in gate
    assert "PC_INSTALLED_OS_JS=" in gate
    assert "alt_tab_switcher_sim.js" in gate
    assert 'extract-file "$asar" www/static/css/client.css' in gate
    assert "PC_INSTALLED_CLIENT_CSS=" in gate
    assert "installed_alt_tab_cross_output_sim.js" in gate


def test_installed_wm_gate_is_discovered_by_the_release_runner():
    jobs = {job["name"]: job for job in checkall.discover()}
    job = jobs["check_installed_wm_release"]
    assert job["serial"] is True
    assert job["registered"] is True


def test_discoverable_gate_reports_missing_installed_artifact_as_a_skip(tmp_path):
    gate = ROOT / "scripts" / "check_installed_wm_release.py"
    env = {**os.environ, "PC_INSTALLED_ASAR": str(tmp_path / "missing.asar")}
    result = subprocess.run([sys.executable, str(gate)], cwd=ROOT, env=env,
                            capture_output=True, text=True)
    assert result.returncode == 2
    assert "SKIP installed ASAR is not available" in result.stdout


def test_sim_uses_a_live_disposable_process_family():
    sim = (ROOT / "tests/client/installed_wm_ancestry_sim.js").read_text(encoding="utf-8")
    assert "process.env.PC_INSTALLED_WM_JS" in sim
    assert "cp.spawn(process.execPath" in sim
    assert "spawn('sleep'" in sim
    assert "wm.waitForWindow(root.pid" in sim
    assert "root.kill('SIGTERM')" in sim


def test_live_pointer_snap_gate_requires_full_usable_output_height_for_frame_and_surface():
    gate = (ROOT / "scripts/check_installed_native_snap.py").read_text(encoding="utf-8")
    assert 'usable_height = shell["height"] - 72' in gate
    assert 'snapped["frame"]["h"] - usable_height' in gate
    assert 'native["height"] >= snapped["frame"]["h"] - 100' in gate
