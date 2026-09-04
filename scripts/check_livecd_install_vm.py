#!/usr/bin/env python3
"""Install PosterChanOS from an ISO into a blank virtual disk, then boot that disk with no ISO.

    venv-unified/bin/python scripts/check_livecd_install_vm.py posterchan-live-YYYYMMDD.iso \
        --disk /var/tmp/pc-install.qcow2

THIS IS THE GATE NOTHING ELSE COVERS. `check_livecd_vm.py` proves an ISO BOOTS to a graphical
session; it says nothing about whether the installer on it works, and an image that boots and cannot
install is the whole product missing. Every install before this was done by hand, which is why
`check_installed_vm.py` asks to be handed a domain that "already contains an installed system".

HOW IT DRIVES THE INSTALL. Not through the GUI -- sending synthetic keystrokes at a desktop is a
test of QEMU's keymap. The ISO's kernel command line already carries `console=ttyS0,115200n8`, and
the live image autologins on that console (see the serial-getty override in os/gentoo.sh), so this
gets a real root-capable shell and types the same commands a person would. `/tmp/disk` is the
installer's own scripting hook: disk, root name, swap choice, one per line.

UEFI, NOT BIOS, and that is the point of the second half. The installer writes an ESP and a
systemd-boot entry; a SeaBIOS guest would boot the disk through a path the product never uses and
prove nothing about the bootloader. OVMF variables are COPIED per run -- the firmware writes its
boot entries into them, so a shared file makes the second run's result depend on the first.

Exit 0 installed and the installed disk booted, 1 it did not, 2 could not run.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time


HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))


def ovmf():
    """(code, vars) for a UEFI guest, or (None, None) when this host has no firmware."""
    for base in ("/usr/share/edk2-ovmf", "/usr/share/edk2/OvmfX64", "/usr/share/OVMF",
                 "/usr/share/qemu"):
        for code, vars_ in (("OVMF_CODE.fd", "OVMF_VARS.fd"),
                            ("OVMF_CODE.4m.fd", "OVMF_VARS.4m.fd"),
                            ("OVMF_CODE_4M.fd", "OVMF_VARS_4M.fd")):
            c, v = Path(base, code), Path(base, vars_)
            if c.is_file() and v.is_file():
                return c, v
    return None, None


class Serial:
    """The guest's console, as a line-oriented conversation.

    Everything read is kept: on a failure the transcript is the only evidence there is, and a gate
    that says "the install did not finish" without it cannot be acted on.
    """

    def __init__(self, path, log):
        self.sock = socket.socket(socket.AF_UNIX)
        self.sock.settimeout(1.0)
        self.sock.connect(str(path))
        self.buf = ""
        self.log = log

    def read(self, seconds=1.0):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            try:
                chunk = self.sock.recv(65536)
            except socket.timeout:
                continue
            except OSError:
                break
            if not chunk:
                break
            text = chunk.decode("utf-8", "replace")
            self.buf += text
            self.log.write(text)
            self.log.flush()
        return self.buf

    def expect(self, pattern, timeout, since=0):
        """Wait for `pattern` to appear after offset `since`. Returns the new offset, or None."""
        deadline = time.monotonic() + timeout
        rx = re.compile(pattern)
        while time.monotonic() < deadline:
            self.read(0.5)
            m = rx.search(self.buf, since)
            if m:
                return m.end()
        return None

    def send(self, line):
        self.sock.sendall((line + "\n").encode())
        time.sleep(0.15)


def qemu_args(disk, iso, serial_path, code, vars_copy, memory, cpus):
    args = ["qemu-system-x86_64", "-machine", "q35,accel=kvm:tcg", "-cpu", "max",
            "-m", str(memory), "-smp", str(cpus), "-display", "none", "-no-reboot",
            "-drive", f"file={disk},if=virtio,format=qcow2",
            "-chardev", f"socket,id=pcserial,path={serial_path},server=on,wait=off",
            "-serial", "chardev:pcserial"]
    if code:
        args[1:1] = ["-drive", f"if=pflash,format=raw,unit=0,readonly=on,file={code}",
                     "-drive", f"if=pflash,format=raw,unit=1,file={vars_copy}"]
    if iso:
        args += ["-drive", f"file={iso},media=cdrom,readonly=on", "-boot", "order=d"]
    return args


def install(iso, disk, serial_dir, evidence, timeout, memory, cpus):
    code, vars_src = ovmf()
    if not code:
        print("SKIP  no OVMF firmware on this host; a BIOS guest would not test the bootloader")
        return 2
    vars_copy = Path(serial_dir, "OVMF_VARS.fd")
    shutil.copyfile(vars_src, vars_copy)
    sock = Path(serial_dir, "console.sock")
    log = open(Path(evidence, "install-console.log"), "w", encoding="utf-8")
    proc = subprocess.Popen(qemu_args(disk, iso, sock, code, vars_copy, memory, cpus),
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    try:
        for _ in range(100):
            if sock.exists():
                break
            if proc.poll() is not None:
                print("FAIL  qemu exited before opening a console: "
                      + (proc.stderr.read() or "")[-300:])
                return 1
            time.sleep(0.1)
        con = Serial(sock, log)
        # THE SHELL, not a login prompt. `live` is password-locked on purpose; the image autologins
        # it on the serial console, and if that override is missing this is where it shows up.
        at = con.expect(r"live@[-a-z0-9]+", timeout)
        if at is None:
            print("FAIL  the live image never reached a shell on the serial console — a login "
                  "prompt here means the serial-getty autologin override is missing from the image")
            return 1
        # THE INSTALLER'S OWN SCRIPTING HOOK: disk, BTRFS root volume name, swap choice, one per
        # line. The values are the ones its INTERACTIVE path writes when the prompts are answered
        # with their defaults (`gentoo`, `none`) -- invented ones would exercise a configuration
        # nobody ships. `vda` is the virtio disk; the live medium is the cdrom, which setDevices
        # refuses to install onto anyway.
        con.send("printf 'vda\\ngentoo\\nnone\\n' | sudo tee /tmp/disk >/dev/null; echo HOOK-$?")
        if con.expect(r"HOOK-0", 60, at) is None:
            print("FAIL  could not write the installer's /tmp/disk hook")
            return 1
        con.send("sudo -n true && echo SUDO-OK")
        if con.expect(r"SUDO-OK", 30) is None:
            print("FAIL  the live account cannot become root — the NOPASSWD drop-in is missing")
            return 1
        # `install-live` asks to erase; every other answer it needs comes from /tmp/disk.
        con.send("printf 'y\\n\\n\\n\\n\\n\\n\\n\\n' | sudo gentoo.sh install-live "
                 "2>&1 | tail -40; echo INSTALL-EXIT-${PIPESTATUS[0]}")
        done = con.expect(r"INSTALL-EXIT-(\d+)", timeout)
        if done is None:
            print(f"FAIL  the installer did not finish within {timeout}s — console transcript in "
                  f"{evidence}/install-console.log")
            return 1
        exit_code = re.findall(r"INSTALL-EXIT-(\d+)", con.buf)[-1]
        if exit_code != "0":
            print(f"FAIL  the installer exited {exit_code}; transcript in "
                  f"{evidence}/install-console.log")
            return 1
        con.send("sudo poweroff")
        try:
            proc.wait(timeout=120)
        except subprocess.TimeoutExpired:
            proc.terminate()
        return 0
    finally:
        log.close()
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("iso", nargs="?", default=os.environ.get("PC_LIVECD_ISO", ""))
    ap.add_argument("--disk", default=os.environ.get("PC_INSTALL_DISK", ""))
    ap.add_argument("--size", default="40G")
    ap.add_argument("--memory", type=int, default=4096)
    ap.add_argument("--cpus", type=int, default=4)
    ap.add_argument("--timeout", type=int, default=3600)
    ap.add_argument("--evidence-dir", default="")
    ap.add_argument("--keep-disk", action="store_true",
                    help="leave the installed qcow2 behind for check_livecd_vm.py --disk")
    args = ap.parse_args()

    if not args.iso or not Path(args.iso).is_file():
        print("SKIP  no ISO to install — set PC_LIVECD_ISO=<iso>. Nothing was verified about the "
              "installer.")
        return 2
    for tool in ("qemu-system-x86_64", "qemu-img"):
        if not shutil.which(tool):
            print(f"SKIP  {tool} is not installed on this host")
            return 2

    evidence = Path(args.evidence_dir or tempfile.mkdtemp(prefix="pc-install-vm-"))
    evidence.mkdir(parents=True, exist_ok=True)
    disk = Path(args.disk or Path(evidence, "installed.qcow2"))
    # A BLANK disk every run. Installing over a previous install proves the resume path, not the
    # fresh one, and the resume path is the one that does not erase.
    if disk.exists():
        disk.unlink()
    subprocess.run(["qemu-img", "create", "-q", "-f", "qcow2", str(disk), args.size], check=True)

    with tempfile.TemporaryDirectory(prefix="pc-install-sock-") as td:
        rc = install(args.iso, disk, td, evidence, args.timeout, args.memory, args.cpus)
    if rc:
        return rc
    print(f"OK  PosterChanOS installed from {Path(args.iso).name} onto a blank UEFI disk "
          f"({disk}); console transcript in {evidence}/install-console.log")
    if not args.keep_disk and not args.disk:
        disk.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
