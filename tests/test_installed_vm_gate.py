from pathlib import Path
import subprocess
import sys

from scripts.check_installed_vm import is_viewer_surface, viewer_frame_is_graphical


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


def test_mapped_viewer_must_contain_a_graphical_guest_frame(tmp_path, monkeypatch):
    node = {"rect": {"x": 20, "y": 30, "width": 800, "height": 600}}
    seen = []

    class Result:
        returncode = 0

    def black_runner(args, env=None, timeout=None):
        seen.append(args)
        path = Path(args[-1])
        width, height = 784, 552
        path.write_bytes(f"P6\n{width} {height}\n255\n".encode() + b"\0\0\0" * width * height)
        return Result()

    assert not viewer_frame_is_graphical(node, {}, runner=black_runner)
    assert seen[0][seen[0].index("-g") + 1] == "28,70 784x552"

    def desktop_runner(args, env=None, timeout=None):
        path = Path(args[-1])
        width, height = 784, 552
        pixels = bytearray()
        for y in range(height):
            for x in range(width):
                pixels.extend((8 + x % 120, 5 + y % 90, 24 + (x + y) % 100))
        path.write_bytes(f"P6\n{width} {height}\n255\n".encode() + pixels)
        return Result()

    assert viewer_frame_is_graphical(node, {}, runner=desktop_runner)
