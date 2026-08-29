from pathlib import Path
import subprocess
import sys

from scripts.check_installed_vm import is_viewer_surface


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
    assert "is_viewer_surface(node, name)" in source
    assert 'v.bootDisk(' in source
    assert 'after.get("bootOrder") != "disk"' in source
    assert 'cd.get("source") != "-"' in source
    assert "force_after_timeout=True" in source
    assert "v.action({json.dumps(name)},'stop')" in source


def test_visible_viewer_accepts_native_wayland_and_xwayland_surfaces():
    rect = {"width": 1024, "height": 768}
    assert is_viewer_surface({"app_id": "virt-viewer", "name": "Installer — demo-vm",
                              "rect": rect}, "demo-vm")
    assert is_viewer_surface({"app_id": None, "name": "demo-vm",
                              "window_properties": {"class": "Virt-viewer"},
                              "rect": rect}, "demo-vm")


def test_visible_viewer_rejects_wrong_guest_or_unusable_surface():
    assert not is_viewer_surface({"app_id": "virt-viewer", "name": "other-vm",
                                  "rect": {"width": 1024, "height": 768}}, "demo-vm")
    assert not is_viewer_surface({"app_id": None, "name": "demo-vm",
                                  "window_properties": {"class": "Virt-viewer"},
                                  "rect": {"width": 320, "height": 200}}, "demo-vm")
