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


def hmp(sock_path: Path, command: str, timeout: float = 5) -> None:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        sock.connect(str(sock_path))
        sock.recv(4096)
        sock.sendall(command.encode("utf-8") + b"\n")
        sock.recv(4096)


def wait_for_socket(path: Path, proc: subprocess.Popen, seconds: int = 20) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if path.exists():
            return
        if proc.poll() is not None:
            raise RuntimeError("QEMU exited before its monitor opened")
        time.sleep(0.1)
    raise RuntimeError("QEMU monitor did not open")


def qemu_command(source: Path, installed_disk: bool, monitor: Path, serial: Path) -> list[str]:
    qemu = os.environ.get("QEMU", "qemu-system-x86_64")
    cmd = [qemu, "-machine", "q35,accel=kvm" if Path("/dev/kvm").exists() else "q35",
           "-m", "4096", "-smp", "2"]
    if installed_disk:
        # Deliberately NO cdrom device: this is the post-installer eject/reboot gate, not another
        # successful boot from the LiveCD masquerading as proof that the installed disk works.
        cmd += ["-drive", f"file={source},if=virtio,format=qcow2", "-boot", "c"]
    else:
        cmd += ["-cdrom", str(source), "-boot", "d"]
    return cmd + ["-display", "none", "-monitor", f"unix:{monitor},server=on,wait=off",
                  "-serial", f"file:{serial}", "-no-reboot"]


def run(source: Path, timeout: int, interval: int, installed_disk: bool = False,
        boot_grace: int = 60, stable_samples: int = 6) -> int:
    with tempfile.TemporaryDirectory(prefix="pc-livecd-smoke-") as raw:
        work = Path(raw)
        monitor, serial = work / "monitor.sock", work / "serial.log"
        cmd = qemu_command(source, installed_disk, monitor, serial)
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


def main() -> int:
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
    args = parser.parse_args()
    source = args.disk or args.iso
    if not source.is_file():
        parser.error(f"{'disk' if args.disk else 'ISO'} not found: {source}")
    if (args.timeout < 15 or args.interval < 1 or args.boot_grace < 0
            or args.stable_samples < 3 or args.boot_grace >= args.timeout):
        parser.error("timeout must be >= 15, interval >= 1, stable samples >= 3, and boot grace within timeout")
    try:
        return run(source.resolve(), args.timeout, args.interval, installed_disk=bool(args.disk),
                   boot_grace=args.boot_grace, stable_samples=args.stable_samples)
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"PosterChanOS VM smoke failed: {exc}", file=__import__("sys").stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
