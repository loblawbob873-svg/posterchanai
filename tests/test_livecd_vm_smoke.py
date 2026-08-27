from pathlib import Path
import importlib.util
import socket
import subprocess
import sys
import threading


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
    assert "while not shot.is_file()" in source


def test_hmp_waits_for_the_command_prompt_not_an_arbitrary_socket_chunk(tmp_path):
    monitor = tmp_path / "monitor.sock"
    ready = threading.Event()

    def server():
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
            listener.bind(str(monitor))
            listener.listen(1)
            ready.set()
            conn, _ = listener.accept()
            with conn:
                conn.sendall(b"QEMU monitor")
                conn.sendall(b"\r\n(qemu)")
                command = conn.recv(4096)
                assert b"screendump" in command
                conn.sendall(b"command accepted\r\n")
                conn.sendall(b"(qemu)")

    thread = threading.Thread(target=server)
    thread.start()
    assert ready.wait(2)
    response = MOD.hmp(monitor, "screendump /tmp/frame.ppm")
    thread.join(2)
    assert not thread.is_alive()
    assert b"command accepted" in response


def test_invalid_grace_cannot_make_a_vacuous_gate(tmp_path):
    iso = tmp_path / "fixture.iso"
    iso.write_bytes(b"not booted because arguments are rejected first")
    result = subprocess.run([sys.executable, str(SCRIPT), str(iso), "--timeout", "30",
                             "--boot-grace", "30"], capture_output=True, text=True)
    assert result.returncode == 2
    assert "boot grace within timeout" in result.stderr


def test_failed_probe_can_preserve_serial_and_final_frames():
    source = SCRIPT.read_text()
    assert 'shutil.copy2(serial, evidence_dir / "serial.log")' in source
    assert 'sorted(work.glob("frame-*.ppm"))[-12:]' in source
    assert '"--evidence-dir"' in source


def test_installed_disk_gate_has_no_live_media(tmp_path):
    """The second boot must not accidentally prove the ISO works for a second time."""
    disk = tmp_path / "installed.qcow2"
    monitor, serial = tmp_path / "monitor", tmp_path / "serial"
    code, variables = tmp_path / "OVMF_CODE.fd", tmp_path / "OVMF_VARS.fd"
    command = MOD.qemu_command(disk, True, monitor, serial, (code, variables))
    assert "-cdrom" not in command
    assert ["-boot", "c"] == command[command.index("-boot"):command.index("-boot") + 2]
    drive = next(arg for arg in command if arg.startswith(f"file={disk},"))
    assert f"file={disk}" in drive
    assert "if=virtio" in drive and "format=qcow2" in drive
    assert f"if=pflash,format=raw,readonly=on,file={code}" in command
    assert f"if=pflash,format=raw,file={variables}" in command


def test_installed_disk_refuses_a_seabios_false_negative(tmp_path):
    disk = tmp_path / "installed.qcow2"
    try:
        MOD.qemu_command(disk, True, tmp_path / "monitor", tmp_path / "serial")
    except RuntimeError as exc:
        assert "requires OVMF" in str(exc)
    else:
        raise AssertionError("UEFI-only installed disk was launched with legacy SeaBIOS")


def test_live_gate_still_attaches_iso_and_boots_it_first(tmp_path):
    iso = tmp_path / "live.iso"
    command = MOD.qemu_command(iso, False, tmp_path / "monitor", tmp_path / "serial")
    assert command[command.index("-cdrom") + 1] == str(iso)
    assert ["-boot", "d"] == command[command.index("-boot"):command.index("-boot") + 2]


def test_kvm_gate_uses_the_host_cpu_that_the_release_image_targets(tmp_path, monkeypatch):
    iso = tmp_path / "live.iso"
    real_exists = MOD.Path.exists

    def exists(path):
        return True if str(path) == "/dev/kvm" else real_exists(path)

    monkeypatch.setattr(MOD.Path, "exists", exists)
    command = MOD.qemu_command(iso, False, tmp_path / "monitor", tmp_path / "serial")
    assert command[command.index("-cpu"):command.index("-cpu") + 2] == ["-cpu", "host"]


def test_tcg_gate_does_not_request_an_unsupported_host_cpu(tmp_path, monkeypatch):
    iso = tmp_path / "live.iso"
    real_exists = MOD.Path.exists

    def exists(path):
        return False if str(path) == "/dev/kvm" else real_exists(path)

    monkeypatch.setattr(MOD.Path, "exists", exists)
    command = MOD.qemu_command(iso, False, tmp_path / "monitor", tmp_path / "serial")
    assert "-cpu" not in command


def test_missing_installed_disk_fails_before_qemu(tmp_path):
    missing = tmp_path / "missing.qcow2"
    result = subprocess.run([sys.executable, str(SCRIPT), "--disk", str(missing)],
                            capture_output=True, text=True)
    assert result.returncode == 2
    assert "disk not found" in result.stderr
