"""The package gate must execute native Git from the immutable app.asar."""
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_installed_code_package_gate_extracts_the_native_bridge():
    gate = (ROOT / "scripts/check_installed_code_package.sh").read_text(encoding="utf-8")
    assert "PC_INSTALLED_ASAR:-/opt/posterchan/resources/app.asar" in gate
    assert "extract-file \"$asar\" hostfs.js" in gate
    assert "PC_INSTALLED_HOSTFS_JS=" in gate
    assert "installed_git_restore_sim.js" in gate


def test_installed_git_sim_uses_a_disposable_real_repository():
    sim = (ROOT / "tests/client/installed_git_restore_sim.js").read_text(encoding="utf-8")
    assert "process.env.PC_INSTALLED_HOSTFS_JS" in sim
    assert "fs.mkdtempSync" in sim
    assert "H.gitDiff" in sim and "H.gitAction" in sim
    assert "'restore', ['changed.js']" in sim
    assert "fs.rmSync(root, {recursive:true, force:true})" in sim
