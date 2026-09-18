#!/usr/bin/env python3
"""Boot a PosterChanOS ISO and require the SESSION to say it came up — in its own words.

WHY THIS IS NOT `check_livecd_vm.py`.

That gate samples the framebuffer and asks whether the picture looks graphical.  That is the right
question about a black VT and the wrong one about this failure: when the Wayfire session gives up it
prints a rescue screen, and a rescue screen is text on a console — lit pixels, colour, a cursor.  It
can pass while the machine is sitting at "the Wayfire session could not start".  This asks the
session instead.  The live image already streams its three logs to `/dev/ttyS0` (`live.bash_profile`)
and the ISO's own grub menu already appends `console=ttyS0,115200n8`, so the marker the launcher
writes when the desktop is genuinely up —

    health marker retired (the shell was declared ready)

— arrives on the serial line for free, and so does the rescue screen's reason and the tail of
`wayfire.log` when it does not.  Nothing had ever read it.

AND IT BOOTS A GPU THAT IS NOT VIRTIO, WHICH IS THE POINT.

`check_livecd_vm.py` boots `virtio-vga`, and `pc-compositor-session` decides whether to take Mesa's
software path by matching PCI vendor `0x1af4` — virtio, and only virtio.  So every VM gate this
repository has ever run exercised the one device the guard names, and the branch a real machine with
no usable GL driver takes had never been booted here at all.  Measured on the release ISO
(sha256 0ca85976…) on 2026-09-17: under `virtio-vga` the guard fires and wlroots renders GLES2 on
llvmpipe; under `-device VGA` (bochs-drm, PCI 1234:1111, no render node) it does not fire, wlroots
refuses GLES2 — *"Software rendering detected"* — and falls back to its pixman renderer.  Both reach
the desktop, and that second fact was not known before this check existed.  The default here is
therefore the UNWHITELISTED device; `--gpu virtio` is available for comparison.

    PC_LIVECD_ISO=/path/to.iso scripts/check_livecd_session_ready.py

`--gpu none` inverts the question: with no display device at all the session MUST give up, and the
check is that it gives up in WORDS ("no display driver claimed the GPU") rather than in a number.
That is the one half of the NVIDIA failure a VM can reproduce — to the compositor, a card taken by
the wrong driver and a guest with no card are the same machine.

Exit 0 the session declared itself ready, 1 it did not, **2 could not run** (no ISO, no qemu, no
KVM) — a SKIP with its reason, never a pass.  KVM is required rather than optional: under TCG the
Electron shell's startup on llvmpipe does not finish inside any timeout worth waiting for, and a
gate that times out on healthy software is a gate people switch off.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time

READY = "the shell was declared ready"
# GIVING UP IS SEEN THROUGH THE LOG, NOT THE SCREEN. The rescue banner is printed to the VT the
# session was started from — tty1 — and only the three LOG FILES are tailed to the serial line, so
# `the Wayfire session could not start` never arrives here. `compositor-fallback.log` does, and its
# `no desktop:` line carries the same reason. Matching the banner alone made a decided failure wait
# out the full timeout and then report "the session never said anything", which is the one thing it
# had not done. Both are matched: the banner would arrive if the session is ever started on ttyS0.
GAVE_UP = ("no desktop:", "the Wayfire session could not start")

# The device models this understands, and what each one is for. `VGA` is QEMU's stdvga, which the
# guest drives with bochs-drm: a real KMS device with no render node, i.e. the shape of a machine
# whose GPU has no Mesa driver in the image.
# `none` is the shape of a machine whose GPU driver did not load: no display device at all, so no
# DRM node for wlroots to open. It is the closest this repository can get to the NVIDIA failure that
# nothing here can emulate — measured on the release ISO, an NVIDIA laptop whose card had been taken
# by nouveau and a `-vga none` guest are the same thing to the compositor, and the old session
# reported both as a number (`Found 0 GPUs` -> SIGSEGV -> status 255). With this device the session
# is EXPECTED to give up; what is checked is that its reason is a sentence about the display driver.
GPUS = {"vga": ["-device", "VGA"], "virtio": ["-device", "virtio-vga"], "none": ["-vga", "none"]}

# The words the rescue screen owes a person whose GPU has no driver. Matched on the serial line,
# which carries compositor-fallback.log's `no desktop:` copy of the same reason.
NO_DRIVER = "no display driver claimed the GPU"

ANSI = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]|\x1b\][0-9;]*[^\x07\x1b]*(?:\x07|\x1b\\)")


def readable(path: Path) -> str:
    try:
        return ANSI.sub("", path.read_text(errors="replace"))
    except OSError:
        return ""


def skip(reason: str) -> int:
    print("SKIP  " + reason + "  Nothing was verified about booting PosterChanOS; this is not a "
          "pass.")
    return 2


def run(iso: Path, gpu: str, timeout: int, evidence: Path | None) -> int:
    # With no display device the ONLY acceptable outcome is a legible refusal; coming up would mean
    # the guest found a GPU this was meant to take away, and the check verified nothing.
    expect_refusal = gpu == "none"
    qemu = shutil.which(os.environ.get("QEMU", "qemu-system-x86_64"))
    if not qemu:
        return skip("qemu-system-x86_64 is not installed here.")
    if not Path("/dev/kvm").exists():
        return skip("this machine has no /dev/kvm.")

    with tempfile.TemporaryDirectory(prefix="pc-session-ready-") as raw:
        work = Path(raw)
        serial = work / "serial.log"
        cmd = [qemu, "-machine", "q35,accel=kvm", "-cpu", "host", "-m", "4096", "-smp", "4"]
        cmd += GPUS[gpu]
        cmd += ["-cdrom", str(iso), "-boot", "d", "-display", "none",
                "-serial", "file:" + str(serial), "-no-reboot"]
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        verdict, detail = 1, "the session never said anything before the timeout"
        try:
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if proc.poll() is not None:
                    err = (proc.stderr.read() if proc.stderr else "").strip()
                    # "COULD NOT RUN" IS NOT "THE IMAGE IS BROKEN". /dev/kvm existing says nothing
                    # about this account being allowed to open it — libvirt runs QEMU as its own
                    # user, so a machine that boots VMs all day long can still refuse this one.
                    # Reported as a failure it blames the ISO for a group membership.
                    if "kvm" in err.lower() and ("permission" in err.lower()
                                                 or "no such" in err.lower()
                                                 or "not access" in err.lower()):
                        verdict, detail = 2, "QEMU could not use KVM here: " + err.splitlines()[0]
                    else:
                        verdict, detail = 1, "QEMU exited during boot" + (": " + err if err else "")
                    break
                text = readable(serial)
                # THE RESCUE SCREEN IS CHECKED FIRST AND ON ITS OWN. A session that has given up
                # will never print the ready marker, so waiting out the timeout only delays a
                # verdict that is already decided — and loses the reason, which is on screen now.
                marker = next((m for m in GAVE_UP if m in text), None)
                if marker:
                    verdict = 1
                    said = text.split(marker, 1)[1].splitlines()
                    detail = ("the session gave up — " + (said[0].strip() if said else marker)
                              + "\n" + "\n".join("    " + line
                                                  for line in text.splitlines()[-24:]))
                    break
                if READY in text:
                    verdict, detail = 0, "the shell declared itself ready"
                    break
                time.sleep(2)
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
            if evidence:
                evidence.mkdir(parents=True, exist_ok=True)
                if serial.exists():
                    shutil.copy2(serial, evidence / ("serial-%s.log" % gpu))

        if verdict == 2:
            return skip(detail)
        if expect_refusal:
            text = readable(serial)
            if verdict == 0:
                print("with no display device the session claimed to be ready — this check "
                      "verified nothing about a machine with no DRM node.", file=sys.stderr)
                return 1
            if NO_DRIVER not in text:
                print("the session gave up without naming the cause. A person in front of this "
                      "machine needs %r, not an exit status:\n%s"
                      % (NO_DRIVER, "\n".join("    " + l for l in text.splitlines()[-24:])),
                      file=sys.stderr)
                return 1
            print("with no display device the session refused in words: " + NO_DRIVER)
            return 0
        if verdict == 0:
            print("PosterChanOS live session came up on a %s GPU: %s" % (gpu, detail))
            return 0
        print("PosterChanOS live session did NOT come up on a %s GPU — %s" % (gpu, detail),
              file=sys.stderr)
        if "gave up" not in detail:
            tail = readable(serial).splitlines()[-25:]
            print("    --- last serial lines ---", file=sys.stderr)
            for line in tail:
                print("    " + line, file=sys.stderr)
        return 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("iso", nargs="?", type=Path,
                        help="LiveCD ISO to boot (default: $PC_LIVECD_ISO)")
    parser.add_argument("--gpu", choices=sorted(GPUS), default="vga",
                        help="display device; the default is deliberately NOT virtio")
    parser.add_argument("--timeout", type=int, default=420)
    parser.add_argument("--evidence-dir", type=Path)
    args = parser.parse_args()

    iso = args.iso or Path(os.environ.get("PC_LIVECD_ISO", "").strip() or ".")
    if not os.environ.get("PC_LIVECD_ISO", "").strip() and args.iso is None:
        return skip("no image to boot — set PC_LIVECD_ISO=<iso> or pass one.")
    if not iso.is_file():
        return skip("%s does not exist." % iso)
    if args.timeout < 60:
        parser.error("a live boot needs at least 60 seconds")
    return run(iso.resolve(), args.gpu, args.timeout, args.evidence_dir)


if __name__ == "__main__":
    raise SystemExit(main())
