#!/usr/bin/env python3
"""Boot a PosterChanOS ISO or installed disk and prove its graphical session stays visible.

This is intentionally a release *probe*, not an ISO builder.  QEMU's HMP ``screendump`` command
samples the guest framebuffer without requiring a working guest network or an interactive viewer.
Once a graphical frame has appeared, a later black frame is the compositor/VT regression this gate
exists to catch.  Three consecutive graphical frames are required before success.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import socket
import subprocess
import shutil
import tempfile
import time


def ppm_pixels(path: Path) -> tuple[int, int, bytes]:
    """Read the P6 files emitted by QEMU, including comment-bearing headers."""
    data = path.read_bytes()
    if not data.startswith(b"P6"):
        raise ValueError("QEMU framebuffer is not a P6 PPM")
    pos, words = 2, []
    while len(words) < 3:
        while pos < len(data) and chr(data[pos]).isspace():
            pos += 1
        if pos < len(data) and data[pos] == 35:  # # comment
            pos = data.find(b"\n", pos)
            if pos < 0:
                raise ValueError("truncated PPM comment")
            continue
        end = pos
        while end < len(data) and not chr(data[end]).isspace():
            end += 1
        words.append(int(data[pos:end]))
        pos = end
    width, height, maximum = words
    if maximum != 255:
        raise ValueError("unsupported PPM channel depth")
    # Exactly one whitespace delimiter follows maxval. Pixel byte values can themselves be ASCII
    # whitespace, so consuming a run here corrupts legitimate dark frames.
    if pos >= len(data) or not chr(data[pos]).isspace():
        raise ValueError("missing PPM pixel delimiter")
    if data[pos:pos + 2] == b"\r\n":
        pos += 2
    else:
        pos += 1
    pixels = data[pos:]
    if width <= 0 or height <= 0 or len(pixels) != width * height * 3:
        raise ValueError("truncated QEMU framebuffer")
    return width, height, pixels


def frame_is_graphical(path: Path) -> bool:
    """Reject black/blank VTs while tolerating dark PosterChan themes.

    Sampling every 97th pixel keeps a 4K framebuffer cheap.  A real desktop has both illuminated
    pixels and meaningful colour/luma variation; a black console with one cursor does not.
    """
    _width, _height, pixels = ppm_pixels(path)
    rgb = [pixels[i:i + 3] for i in range(0, len(pixels), 3 * 97)]
    luma = [(54 * p[0] + 183 * p[1] + 19 * p[2]) // 256 for p in rgb]
    lit = sum(v >= 12 for v in luma) / len(luma)
    spread = max(luma) - min(luma)
    colours = len({bytes(p) for p in rgb})
    return lit >= 0.025 and spread >= 24 and colours >= 16


def _hmp_prompt(sock: socket.socket, timeout: float) -> bytes:
    sock.settimeout(timeout)
    data = bytearray()
    while b"(qemu)" not in data:
        chunk = sock.recv(4096)
        if not chunk:
            raise RuntimeError("QEMU monitor closed before its prompt")
        data.extend(chunk)
    return bytes(data)


def hmp(sock_path: Path, command: str, timeout: float = 5) -> bytes:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.connect(str(sock_path))
        _hmp_prompt(sock, timeout)
        sock.sendall(command.encode("utf-8") + b"\n")
        return _hmp_prompt(sock, timeout)


def wait_for_socket(path: Path, proc: subprocess.Popen, seconds: int = 20) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if path.exists():
            return
        if proc.poll() is not None:
            raise RuntimeError("QEMU exited before its monitor opened")
        time.sleep(0.1)
    raise RuntimeError("QEMU monitor did not open")


def ovmf_firmware() -> tuple[Path, Path]:
    """Return the distro's OVMF code and writable-variable templates."""
    pairs = (
        (Path("/usr/share/edk2/OvmfX64/OVMF_CODE.fd"),
         Path("/usr/share/edk2/OvmfX64/OVMF_VARS.fd")),
        (Path("/usr/share/OVMF/OVMF_CODE.fd"), Path("/usr/share/OVMF/OVMF_VARS.fd")),
        (Path("/usr/share/edk2/x64/OVMF_CODE.fd"), Path("/usr/share/edk2/x64/OVMF_VARS.fd")),
    )
    for code, variables in pairs:
        if code.is_file() and variables.is_file():
            return code, variables
    raise RuntimeError("OVMF firmware is required to boot the installed UEFI disk")


def qemu_command(source: Path, installed_disk: bool, monitor: Path, serial: Path,
                 firmware: tuple[Path, Path] | None = None) -> list[str]:
    qemu = os.environ.get("QEMU", "qemu-system-x86_64")
    kvm = Path("/dev/kvm").exists()
    cmd = [qemu, "-machine", "q35,accel=kvm" if kvm else "q35"]
    # QEMU's generic virtual CPU is deliberately ancient.  The release image is built for the
    # machine that produced it and its userspace can legally use newer instructions; booting that
    # image with the generic CPU made init take SIGILL and panic with "Attempted to kill init".
    # KVM host passthrough is also the profile used by the independent UEFI boot gate.  TCG cannot
    # use `host`, but its default CPU is old enough to SIGILL on the x86-64-v3 systemd shipped by a
    # host-built image. `max` is TCG's own complete emulated CPU and keeps the software-only gate
    # useful on builders where nested KVM is unavailable.
    if kvm:
        cmd += ["-cpu", "host"]
    else:
        cmd += ["-cpu", "max"]
    # The implicit stdvga device can reach a text console and then leave wlroots with no usable DRM
    # output, producing a permanently black framebuffer after systemd reaches graphical.target.
    # Match the virtio GPU used by PosterChan's libvirt domains so the boot gate tests the actual
    # Wayland desktop rather than firmware-era VGA compatibility.
    cmd += ["-m", "4096", "-smp", "2", "-device", "virtio-vga"]
    if installed_disk:
        # Deliberately NO cdrom device: this is the post-installer eject/reboot gate, not another
        # successful boot from the LiveCD masquerading as proof that the installed disk works.
        # PosterChanOS installs systemd-boot on the ESP and has no legacy BIOS loader.  Supplying a
        # writable copy of OVMF's variable store is therefore part of the installed-disk gate.
        if firmware is None:
            raise RuntimeError("installed-disk gate requires OVMF firmware")
        code, variables = firmware
        cmd += ["-drive", f"if=pflash,format=raw,readonly=on,file={code}",
                "-drive", f"if=pflash,format=raw,file={variables}"]
        cmd += ["-drive", f"file={source},if=virtio,format=qcow2", "-boot", "c"]
    else:
        cmd += ["-cdrom", str(source), "-boot", "d"]
    return cmd + ["-display", "none", "-monitor", f"unix:{monitor},server=on,wait=off",
                  "-serial", f"file:{serial}", "-no-reboot"]


def run(source: Path, timeout: int, interval: int, installed_disk: bool = False,
        boot_grace: int = 60, stable_samples: int = 6,
        evidence_dir: Path | None = None) -> int:
    with tempfile.TemporaryDirectory(prefix="pc-livecd-smoke-") as raw:
        work = Path(raw)
        monitor, serial = work / "monitor.sock", work / "serial.log"
        firmware = None
        if installed_disk:
            code, template = ovmf_firmware()
            variables = work / "OVMF_VARS.fd"
            shutil.copy2(template, variables)
            firmware = code, variables
        cmd = qemu_command(source, installed_disk, monitor, serial, firmware)
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        try:
            wait_for_socket(monitor, proc)
            started = time.monotonic()
            deadline, stable, desktop_seen = started + timeout, 0, False
            sample = 0
            while time.monotonic() < deadline:
                if proc.poll() is not None:
                    err = (proc.stderr.read() if proc.stderr else "").strip()
                    raise RuntimeError("QEMU exited during boot" + (": " + err if err else ""))
                shot = work / f"frame-{sample:03d}.ppm"
                hmp(monitor, f"screendump {shot}")
                shot_deadline = time.monotonic() + 5
                while not shot.is_file() and time.monotonic() < shot_deadline:
                    time.sleep(0.05)
                if not shot.is_file():
                    raise RuntimeError(f"QEMU did not write framebuffer sample {sample}")
                graphical = frame_is_graphical(shot)
                # Firmware and GRUB are graphical too. They must neither satisfy the desktop gate
                # nor make the ordinary boot-menu -> kernel modeset blank transition a failure.
                # Only classify frames after the guest has had time to reach its graphical target.
                eligible = time.monotonic() - started >= boot_grace
                if desktop_seen and not graphical:
                    raise RuntimeError(f"framebuffer became black/blank after graphics appeared (sample {sample})")
                if eligible and graphical:
                    desktop_seen, stable = True, stable + 1
                    if stable >= stable_samples:
                        label = "installed-disk" if installed_disk else "LiveCD"
                        print(f"{label} graphical boot stable across {stable} post-grace samples")
                        return 0
                else:
                    stable = 0
                sample += 1
                time.sleep(interval)
            tail = serial.read_text(errors="replace")[-4000:] if serial.exists() else ""
            raise RuntimeError("graphical desktop did not stabilize before timeout\n" + tail)
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
            if evidence_dir:
                evidence_dir.mkdir(parents=True, exist_ok=True)
                if serial.exists():
                    shutil.copy2(serial, evidence_dir / "serial.log")
                frames = sorted(work.glob("frame-*.ppm"))[-12:]
                for frame in frames:
                    shutil.copy2(frame, evidence_dir / frame.name)


# A GATE THAT NEEDS AN ARGUMENT NOBODY SUPPLIES IS NOT A GATE.
#
# This is the only check in the tree that boots a real PosterChanOS and proves its session comes up,
# and it SKIPPED in every single suite run — `error: one of the arguments iso --disk is required`,
# because checkall runs a check with no arguments and nothing ever supplied one. So installer
# regressions reached a person's machine instead of a build, which is exactly how they were
# reported: "the new installed system does not boot, no EFI entries", "why does this keep breaking".
#
# The target therefore comes from the environment when the command line does not carry it:
#
#   PC_LIVECD_ISO=/path/to.iso        the live image boots to a desktop
#   PC_INSTALLED_DISK=/path/to.qcow2  a disk the installer produced boots with NO media attached
#
# Unset on a machine with no image is a SKIP (exit 2), never a pass — the same rule every other
# environment-dependent check here follows. Both have now been run for real on hardware with KVM and
# both passed; what was missing was ever running them.
def _from_environment():
    iso = os.environ.get("PC_LIVECD_ISO", "").strip()
    disk = os.environ.get("PC_INSTALLED_DISK", "").strip()
    return iso, disk


def main() -> int:
    import sys
    if len(sys.argv) == 1:
        iso, disk = _from_environment()
        if not iso and not disk:
            print("SKIP  no image to boot — set PC_LIVECD_ISO=<iso> and/or "
                  "PC_INSTALLED_DISK=<qcow2>. Nothing was verified about installing or booting "
                  "PosterChanOS; this is not a pass.")
            return 2
        for path in (iso, disk):
            if path and not Path(path).is_file():
                print("SKIP  %s does not exist" % path)
                return 2
        rc = 0
        if iso:
            sys.argv = [sys.argv[0], iso]
            rc = main() or rc
        if disk:
            sys.argv = [sys.argv[0], "--disk", disk]
            rc = main() or rc
        return rc
    parser = argparse.ArgumentParser(description=__doc__)
    inputs = parser.add_mutually_exclusive_group(required=True)
    inputs.add_argument("iso", type=Path, nargs="?", help="LiveCD ISO to boot")
    inputs.add_argument("--disk", type=Path,
                        help="installed qcow2 disk to boot with no installer media attached")
    parser.add_argument("--timeout", type=int, default=240)
    parser.add_argument("--interval", type=int, default=5)
    parser.add_argument("--boot-grace", type=int, default=60,
                        help="seconds before graphical frames can count as the desktop")
    parser.add_argument("--stable-samples", type=int, default=6,
                        help="consecutive post-grace graphical frames required")
    parser.add_argument("--evidence-dir", type=Path,
                        help="preserve serial log and final framebuffer samples here")
    args = parser.parse_args()
    source = args.disk or args.iso
    if not source.is_file():
        parser.error(f"{'disk' if args.disk else 'ISO'} not found: {source}")
    if (args.timeout < 15 or args.interval < 1 or args.boot_grace < 0
            or args.stable_samples < 3 or args.boot_grace >= args.timeout):
        parser.error("timeout must be >= 15, interval >= 1, stable samples >= 3, and boot grace within timeout")
    try:
        return run(source.resolve(), args.timeout, args.interval, installed_disk=bool(args.disk),
                   boot_grace=args.boot_grace, stable_samples=args.stable_samples,
                   evidence_dir=args.evidence_dir)
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"PosterChanOS VM smoke failed: {exc}", file=__import__("sys").stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
