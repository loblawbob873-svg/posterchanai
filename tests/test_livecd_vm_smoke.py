from pathlib import Path
import importlib.util
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check_livecd_vm.py"
SPEC = importlib.util.spec_from_file_location("check_livecd_vm", SCRIPT)
MOD = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(MOD)


def ppm(path, width, height, pixel):
    path.write_bytes(f"P6\n# fixture\n{width} {height}\n255\n".encode() + pixel * (width * height))


def test_black_frame_is_rejected(tmp_path):
    image = tmp_path / "black.ppm"
    ppm(image, 32, 32, b"\0\0\0")
    assert not MOD.frame_is_graphical(image)


def test_realistically_varied_dark_desktop_is_accepted(tmp_path):
    image = tmp_path / "desktop.ppm"
    pixels = bytearray()
    for y in range(64):
        for x in range(64):
            pixels.extend((8 + x * 2, 5 + y, 24 + ((x + y) % 80)))
    image.write_bytes(b"P6\n64 64\n255\n" + pixels)
    assert MOD.frame_is_graphical(image)


def test_truncated_frame_is_not_mistaken_for_black(tmp_path):
    image = tmp_path / "bad.ppm"
    image.write_bytes(b"P6\n20 20\n255\n\0")
    try:
        MOD.frame_is_graphical(image)
    except ValueError as exc:
        assert "truncated" in str(exc)
    else:
        raise AssertionError("truncated framebuffer was accepted")


def test_missing_iso_fails_before_qemu_is_started(tmp_path):
    result = subprocess.run([sys.executable, str(SCRIPT), str(tmp_path / "missing.iso")],
                            capture_output=True, text=True)
    assert result.returncode == 2
    assert "ISO not found" in result.stderr


def test_probe_ignores_boot_graphics_and_requires_post_grace_stability():
    source = SCRIPT.read_text()
    assert "eligible = time.monotonic() - started >= boot_grace" in source
    assert "stable >= stable_samples" in source
    assert "if desktop_seen and not graphical" in source
    assert "default=60" in source
    assert "default=6" in source
    assert "screendump" in source


def test_invalid_grace_cannot_make_a_vacuous_gate(tmp_path):
    iso = tmp_path / "fixture.iso"
    iso.write_bytes(b"not booted because arguments are rejected first")
    result = subprocess.run([sys.executable, str(SCRIPT), str(iso), "--timeout", "30",
                             "--boot-grace", "30"], capture_output=True, text=True)
    assert result.returncode == 2
    assert "boot grace within timeout" in result.stderr


def test_installed_disk_gate_has_no_live_media(tmp_path):
    """The second boot must not accidentally prove the ISO works for a second time."""
    disk = tmp_path / "installed.qcow2"
    monitor, serial = tmp_path / "monitor", tmp_path / "serial"
    command = MOD.qemu_command(disk, True, monitor, serial)
    assert "-cdrom" not in command
    assert ["-boot", "c"] == command[command.index("-boot"):command.index("-boot") + 2]
    drive = command[command.index("-drive") + 1]
    assert f"file={disk}" in drive
    assert "if=virtio" in drive and "format=qcow2" in drive


def test_live_gate_still_attaches_iso_and_boots_it_first(tmp_path):
    iso = tmp_path / "live.iso"
    command = MOD.qemu_command(iso, False, tmp_path / "monitor", tmp_path / "serial")
    assert command[command.index("-cdrom") + 1] == str(iso)
    assert ["-boot", "d"] == command[command.index("-boot"):command.index("-boot") + 2]


def test_missing_installed_disk_fails_before_qemu(tmp_path):
    missing = tmp_path / "missing.qcow2"
    result = subprocess.run([sys.executable, str(SCRIPT), "--disk", str(missing)],
                            capture_output=True, text=True)
    assert result.returncode == 2
    assert "disk not found" in result.stderr
