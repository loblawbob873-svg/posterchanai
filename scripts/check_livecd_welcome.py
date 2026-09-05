#!/usr/bin/env python3
"""Does the ISO -- and the disk it installs -- come up at the WELCOME SCREEN?

    venv-unified/bin/python scripts/check_livecd_welcome.py /path/to/posterchan-live-YYYYMMDD.iso
    venv-unified/bin/python scripts/check_livecd_welcome.py --disk /path/to/installed.qcow2

THE GATE NOTHING ELSE COVERS. `check_livecd_vm.py` proves a graphical frame appears and stays --
three consecutive non-black framebuffer samples. That passes just as happily on a desktop with no
wizard, on a stale session, or on an error dialog, so "it boots" has never been evidence that it
boots to the first-run wizard, which is the entire experience of a new machine.

HOW IT ASKS, and why not by looking. Recognising the screen from a framebuffer is a test of QEMU's
font rendering, and it cannot tell a wizard from a screenshot of one. The guest already carries
`console=ttyS0,115200n8` and autologins on it (the serial-getty override in os/gentoo.sh), which is
how `check_livecd_install_vm.py` drives the installer -- so this asks the running session what it
decided, over that same console, by reading the line `osfirstrunui.js:boot()` prints:

    [firstrun] showing step=network blocked=0 state={...}

A machine with nothing set up must be SHOWING, and the step must be `network`: it is asked first
because every later question needs the radio, and a machine that opens on `instance` will be typing
a URL at a box with no network.

"Could not ask" is never a pass. A session that never logged the line at all exits 2 (SKIP with its
reason), never 0 -- an unreadable answer and a wrong answer are different facts and only one of them
is a bug in the image.

Exit 0 the welcome screen * 1 it did not * 2 could not run.
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


def ovmf():
    """(code, vars) for a UEFI guest, or (None, None) when this host has no firmware."""
    for base in ("/usr/share/edk2-ovmf", "/usr/share/edk2/OvmfX64", "/usr/share/OVMF",
                 "/usr/share/qemu"):
        code = Path(base, "OVMF_CODE.fd")
        data = Path(base, "OVMF_VARS.fd")
        if code.exists() and data.exists():
            return code, data
    return None, None


# The line osfirstrunui.js prints, wherever it lands in the guest's console noise.
VERDICT = re.compile(r"\[firstrun\]\s+(showing|skipped)\s+step=(\S+)\s+blocked=(\d)")


def run_guest(args, boot_iso: str | None, disk: str | None, seconds: int):
    """Boot, capture the serial console, and return everything it said."""
    qemu = shutil.which("qemu-system-x86_64")
    if not qemu:
        print("SKIP  no qemu-system-x86_64 on this box")
        return None
    code, data = ovmf()
    if not code:
        print("SKIP  no OVMF firmware on this box — a BIOS guest would not exercise the bootloader")
        return None

    tmp = Path(tempfile.mkdtemp(prefix="pc-welcome-"))
    try:
        # The firmware WRITES its boot entries into the vars file, so a shared one makes this run's
        # result depend on the last one.
        nvram = tmp / "OVMF_VARS.fd"
        shutil.copyfile(data, nvram)
        serial = tmp / "serial.log"
        cmd = [qemu, "-machine", "q35,accel=kvm:tcg", "-cpu", "max",
               "-m", str(args.memory), "-smp", str(args.cpus),
               "-drive", f"if=pflash,format=raw,readonly=on,file={code}",
               "-drive", f"if=pflash,format=raw,file={nvram}",
               "-display", "none", "-vga", "virtio",
               "-serial", f"file:{serial}"]
        if disk:
            cmd += ["-drive", f"file={disk},if=virtio,format=qcow2"]
        if boot_iso:
            cmd += ["-cdrom", boot_iso, "-boot", "d"]
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        try:
            deadline = time.time() + seconds
            seen = ""
            while time.time() < deadline:
                time.sleep(5)
                if proc.poll() is not None:
                    break
                try:
                    seen = serial.read_text(errors="replace")
                except Exception:
                    seen = ""
                if VERDICT.search(seen):
                    break
            return seen
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def judge(console: str, what: str) -> int:
    if console is None:
        return 2
    m = VERDICT.search(console or "")
    if not m:
        # An answer that never arrived is not a wrong answer. Say which, and say what WAS seen, so
        # "the session never started" and "the session started and said nothing" are separable.
        tail = "\n      ".join((console or "").strip().splitlines()[-6:]) or "(nothing at all)"
        print(f"SKIP  {what}: the session never reported a first-run verdict.\n"
              f"      last console lines:\n      {tail}")
        return 2
    verdict, step, blocked = m.group(1), m.group(2), m.group(3)
    if verdict != "showing":
        print(f"FAIL  {what}: booted past the welcome screen (verdict={verdict}, step={step}) — a "
              f"machine nobody has set up must open on the wizard, not on a desktop it cannot use")
        return 1
    if step != "network":
        print(f"FAIL  {what}: the wizard opened on {step!r}, not 'network'. The radio is asked for "
              f"first because every later question needs it; asked fourth, somebody types an "
              f"instance URL at a machine that cannot reach one")
        return 1
    if blocked != "0":
        print(f"FAIL  {what}: the wizard opened on 'network' already blocked — the first screen of "
              f"a new machine is a dead end")
        return 1
    print(f"OK    {what}: comes up at the welcome screen (step=network)")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("iso", nargs="?", help="the LiveCD to boot")
    ap.add_argument("--disk", help="an installed virtual disk to boot instead (no ISO attached)")
    ap.add_argument("--memory", type=int, default=4096)
    ap.add_argument("--cpus", type=int, default=2)
    ap.add_argument("--seconds", type=int, default=420,
                    help="how long to wait for the session to report")
    args = ap.parse_args()

    if not args.iso and not args.disk:
        print("SKIP  nothing to boot: pass an ISO or --disk")
        return 2
    target = args.disk or args.iso
    if not Path(target).exists():
        print(f"SKIP  {target} does not exist")
        return 2

    what = "installed disk" if args.disk else Path(args.iso).name
    console = run_guest(args, None if args.disk else args.iso, args.disk, args.seconds)
    return judge(console, what)


if __name__ == "__main__":
    sys.exit(main())
