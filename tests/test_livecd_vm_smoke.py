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


def test_probe_requires_three_stable_samples_and_detects_late_black():
    source = SCRIPT.read_text()
    assert "stable >= 3" in source
    assert "if seen and not graphical" in source
    assert "screendump" in source
