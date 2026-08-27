"""The installed WM package gate must exercise live late-fork ancestry."""
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_gate_extracts_wm_from_the_immutable_asar():
    gate = (ROOT / "scripts/check_installed_wm_package.sh").read_text(encoding="utf-8")
    assert "PC_INSTALLED_ASAR:-/opt/posterchan/resources/app.asar" in gate
    assert "extract-file \"$asar\" wm.js" in gate
    assert "PC_INSTALLED_WM_JS=" in gate
    assert "installed_wm_ancestry_sim.js" in gate


def test_sim_uses_a_live_disposable_process_family():
    sim = (ROOT / "tests/client/installed_wm_ancestry_sim.js").read_text(encoding="utf-8")
    assert "process.env.PC_INSTALLED_WM_JS" in sim
    assert "cp.spawn(process.execPath" in sim
    assert "spawn('sleep'" in sim
    assert "wm.waitForWindow(root.pid" in sim
    assert "root.kill('SIGTERM')" in sim
