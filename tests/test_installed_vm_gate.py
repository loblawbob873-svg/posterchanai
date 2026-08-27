from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
GATE = ROOT / "scripts" / "check_installed_vm.py"


def test_gate_requires_explicit_existing_domain_iso_and_destructive_acknowledgement(tmp_path):
    result = subprocess.run([sys.executable, str(GATE)], text=True, capture_output=True)
    assert result.returncode == 2
    assert "--name" in result.stderr and "--expected-iso" in result.stderr
    source = GATE.read_text()
    assert 'action="store_true", required=True' in source
    assert "does not create or delete a VM" in source


def test_gate_executes_the_installed_asar_backend_not_the_source_copy():
    source = GATE.read_text()
    assert 'Path("/opt/posterchan/posterchan-desktop")' in source
    assert 'Path("/opt/posterchan/resources/app.asar")' in source
    assert 'ELECTRON_RUN_AS_NODE="1"' in source
    assert "'/vm.js'" in source


def test_gate_proves_both_visible_boots_and_persistent_eject():
    source = GATE.read_text()
    assert "start_and_view" in source
    assert "visible_viewer" in source
    assert 'node.get("app_id") == "virt-viewer"' in source
    assert 'v.bootDisk(' in source
    assert 'after.get("bootOrder") != "disk"' in source
    assert 'cd.get("source") != "-"' in source
    assert "force_after_timeout=True" in source
    assert "v.action({json.dumps(name)},'stop')" in source
