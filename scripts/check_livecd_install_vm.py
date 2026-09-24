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

THE SERVER, FROM THE INSTALLED SYSTEM (`--server`). PosterChanOS carries the server's code and runs
none of it until somebody presses System Settings → PosterChan Server → Enable, which is `pc-server
enable`: a Postgres cluster, Tor, the project's own `install.sh --nostr-only`, a unit. That path runs
on an installed machine and nowhere else, so nothing here had ever exercised it -- an image whose
server could not be switched on passed every gate. With `--server` the booted disk is logged into as
root (the password the install was given), `pc-server enable` is run and waited for, and the server
must then be active and answering on 3051.

Exit 0 installed and the installed disk booted (and, with --server, served), 1 it did not, 2 could
not run.
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
# A disposable, VM-only credential: the disk's passphrase AND root's password on the installed guest.
INSTALL_PASSWORD = "pc-vm-test-only"
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
        try:
            self.sock.settimeout(1.0)
            self.sock.connect(str(path))
        except OSError:
            self.sock.close()
            raise
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


def qemu_args(disk, iso, serial_path, code, vars_copy, memory, cpus, usb=False, net=False):
    args = ["qemu-system-x86_64", "-machine", "q35,accel=kvm:tcg", "-cpu", "max",
            "-m", str(memory), "-smp", str(cpus), "-display", "none", "-no-reboot",
            "-drive", f"file={disk},if=virtio,format=qcow2",
            "-chardev", f"socket,id=pcserial,path={serial_path},server=on,wait=off",
            "-serial", "chardev:pcserial"]
    if net:
        # The server install fetches its Python packages: a NIC it can DHCP on, named rather than left
        # to QEMU's default (which a -nodefaults anywhere up the chain would silently remove).
        args += ["-nic", "user,model=virtio-net-pci"]
    if code:
        args[1:1] = ["-drive", f"if=pflash,format=raw,unit=0,readonly=on,file={code}",
                     "-drive", f"if=pflash,format=raw,unit=1,file={vars_copy}"]
    if iso:
        if usb:
            # A USB STICK IS NOT A CD, AND THE DIFFERENCE IS WHERE THE KERNEL LIVES.
            #
            # Attached as a cdrom the image is /dev/sr0, an iso9660 device that dracut mounts at
            # /run/initramfs/live and that blkid reports as iso9660. Written to a stick it is a
            # PARTITIONED disk: the iso9660 filesystem is on the whole device, hidden from blkid by
            # the protective MBR grub-mkrescue writes so one file boots BIOS and UEFI, and the
            # contents are reachable through the hfsplus partition. liveISOinstall looks for its
            # kernel through exactly that machinery, so the cdrom path exercised the one medium on
            # which it cannot fail -- and shipped an installer that told a user booted from USB
            # "No kernel found on this live medium".
            #
            # WRITABLE TO THE GUEST, NEVER TO THE FILE. A real stick is writable and mounting one
            # read-write is part of what the installer's search has to cope with -- but the file
            # here is the artifact under test, and a guest that can write to it can change the
            # bytes we are about to publish. `snapshot=on` opens the ISO READ-ONLY and puts the
            # guest's writes in a throwaway overlay: the guest sees a writable stick, the image
            # cannot be altered by its own test.
            #
            # It is also the difference between running and not running. An ISO built under sudo is
            # root-owned 644, so opening it read-write is EACCES for the user qemu runs as, and the
            # gate died on a bare ConnectionRefusedError from the console socket -- qemu had already
            # exited saying "Could not open ...: Permission denied". The cdrom branch never hit it
            # because it passes readonly=on.
            args += ["-drive", f"file={iso},if=none,id=pcusb,format=raw,snapshot=on",
                     "-device", "qemu-xhci,id=xhci",
                     "-device", "usb-storage,bus=xhci.0,drive=pcusb,bootindex=0"]
        else:
            args += ["-drive", f"file={iso},media=cdrom,readonly=on", "-boot", "order=d"]
    return args


def install(iso, disk, serial_dir, evidence, timeout, memory, cpus, usb=False):
    code, vars_src = ovmf()
    if not code:
        print("SKIP  no OVMF firmware on this host; a BIOS guest would not test the bootloader")
        return 2
    # KEPT, NOT SCRATCH. The boot phase reuses this exact variables file: the EFI entry the installer
    # creates lives in it, and giving the boot a fresh copy would silently test the fallback instead.
    vars_copy = Path(evidence, "OVMF_VARS.fd")
    shutil.copyfile(vars_src, vars_copy)
    sock = Path(serial_dir, "console.sock")
    log = open(Path(evidence, "install-console.log"), "w", encoding="utf-8")
    proc = subprocess.Popen(qemu_args(disk, iso, sock, code, vars_copy, memory, cpus, usb),
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
        # THE SOCKET EXISTING IS NOT QEMU LISTENING. qemu creates the path and can still exit
        # before accepting -- a bad -drive is exactly that shape -- and connecting then raises
        # ConnectionRefusedError, a traceback that names the socket and not the cause. Ask the
        # process what happened instead of guessing from the socket.
        if proc.poll() is not None:
            print("FAIL  qemu exited before accepting a console connection: "
                  + (proc.stderr.read() or "")[-300:])
            return 1
        try:
            con = Serial(sock, log)
        except OSError as exc:
            proc.kill()
            print(f"FAIL  could not attach to the guest console ({exc}): "
                  + (proc.stderr.read() or "")[-300:])
            return 1
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
        # Exercise the interactive password confirmation with a disposable VM-only credential.
        # PIPESTATUS[1] is the installer; [0] only reports whether printf wrote the answers.
        con.send("printf 'y\\npc-vm-test-only\\npc-vm-test-only\\n\\n\\n\\n\\n\\n' | sudo gentoo.sh install-live "
                 "2>&1 | tee /tmp/pc-install-test.log; echo INSTALL-EXIT-${PIPESTATUS[1]}")
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
        # ---- DID THE INSTALLER TELL THE FIRMWARE THIS SYSTEM EXISTS --------------------------
        #
        # THE ONE CHECK THAT SEPARATES A VM FROM A REAL MACHINE, AND THE BUG IT MISSED SHIPPED.
        #
        # `bootctl --esp-path=/boot --no-variables install` writes the loader to the ESP and
        # deliberately creates NO EFI boot variable. A guest with fresh OVMF variables has no boot
        # entries at all, so its firmware falls back to the removable-media path
        # EFI/BOOT/BOOTX64.EFI and boots perfectly -- which is why booting the disk is NOT sufficient
        # and why this install looked fine in every VM. A machine that has ever run another OS has a
        # populated NVRAM whose stale entries are tried first, and that fallback is never reached:
        # measured 2026-09-17 on an ex-Windows machine whose BootOrder began `Boot0000* Windows Boot
        # Manager` with Windows already erased, holding no PosterChanOS entry at all. The install was
        # otherwise complete and correct.
        #
        # So the VARIABLE is asserted directly, in the live session that just did the install, where
        # OVMF's NVRAM is readable. It cannot be inferred from a successful boot.
        # THE LABEL IS NOT AT THE END OF THE LINE. efibootmgr prints `Boot000C* PosterChanOS` followed
        # by a TAB and the device path, so an anchored `' PosterChanOS$'` matches nothing and reports a
        # missing entry on an install that made one. Match the Boot#### line carrying the label, and
        # echo the raw listing into the transcript so a disagreement can be read rather than guessed.
        con.send("sudo efibootmgr 2>&1 | sed 's/^/EFIBOOT| /'; "
                 "sudo efibootmgr 2>&1 | grep -cE '^Boot[0-9A-Fa-f]+\\*?[[:space:]]+PosterChanOS([[:space:]]|$)' "
                 "| sed 's/^/NVRAM-COUNT=/'")
        if con.expect(r"NVRAM-COUNT=\d+", 60) is None:
            print("FAIL  could not read the guest's EFI boot variables")
            return 1
        import re as _re
        found = _re.findall(r"NVRAM-COUNT=(\d+)", con.buf)[-1]
        if found == "0":
            print("FAIL  the installer created no PosterChanOS EFI boot entry. This disk boots in a "
                  "VM only because empty NVRAM falls back to EFI/BOOT/BOOTX64.EFI; on a machine with "
                  "existing boot entries (any ex-Windows machine) the firmware tries those first and "
                  "never reaches it. See bootctl's --no-variables in os/gentoo.sh.")
            return 1
        print(f"OK  the installer registered {found} PosterChanOS EFI boot entry/entries")
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


def _make_installed_boot_audible(disk, evidence):
    """Add a serial console to the INSTALLED loader entry, in the test's copy of the disk.

    Returns a short description of what changed, or "" when nothing could be edited (in which case
    the boot check still runs and simply has less to read).
    """
    mnt = Path(evidence, "espmnt")
    mnt.mkdir(exist_ok=True)
    nbd = None
    try:
        # qemu-nbd is the only way to reach a partition inside a qcow2 without booting it.
        subprocess.run(["modprobe", "nbd", "max_part=8"], capture_output=True, timeout=30)
        for dev in ("/dev/nbd0", "/dev/nbd1", "/dev/nbd2"):
            r = subprocess.run(["qemu-nbd", "--connect", dev, str(disk)],
                               capture_output=True, text=True, timeout=60)
            if r.returncode == 0:
                nbd = dev
                break
        if not nbd:
            return ""
        time.sleep(1.5)
        r = subprocess.run(["mount", "-o", "rw", nbd + "p1", str(mnt)],
                           capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            return ""
        changed = []
        for conf in sorted(Path(mnt, "loader/entries").glob("*.conf")):
            text = conf.read_text(encoding="utf-8", errors="replace")
            out = []
            for line in text.splitlines():
                if line.startswith("options ") and "console=ttyS0" not in line:
                    line = line.replace(" quiet", "").replace(" splash", "")
                    line += " console=tty0 console=ttyS0,115200n8"
                    changed.append(conf.name)
                out.append(line)
            conf.write_text("\n".join(out) + "\n", encoding="utf-8")
        subprocess.run(["sync"], capture_output=True, timeout=30)
        return ", ".join(changed)
    except (OSError, subprocess.SubprocessError):
        return ""
    finally:
        subprocess.run(["umount", str(mnt)], capture_output=True, timeout=60)
        if nbd:
            subprocess.run(["qemu-nbd", "--disconnect", nbd], capture_output=True, timeout=60)


def server_stage(con, password, timeout, evidence, *, poll=10.0, clock=time.monotonic, sleep=time.sleep):
    """Switch the bundled server on FROM the installed system, the way System Settings does it.

    `con` is the installed system's console, already at a login prompt. Returns 0 when the server was
    set up and answers, 1 otherwise -- with the job's own log in the transcript, because "the server
    did not start" cannot be acted on without it."""
    def ask(cmd, pattern, wait):
        mark = len(con.buf)
        con.send(cmd)
        if con.expect(pattern, wait, mark) is None:
            return None
        return re.findall(pattern, con.buf[mark:])[-1]

    # ---- log in as root: the install unlocked root with the password it was given
    mark = len(con.buf)
    con.send("")
    if con.expect(r"login:", 60, max(0, mark - 400)) is None:
        print("FAIL  the installed system gave no login prompt on its serial console")
        return 1
    con.send("root")
    if con.expect(r"[Pp]assword:", 30, mark) is None:
        print("FAIL  no password prompt for root on the installed system")
        return 1
    con.send(password)
    if ask("echo ROOT-$(id -u)", r"ROOT-(\d+)", 60) != "0":
        print("FAIL  could not log in as root on the installed system with the install's password")
        return 1
    print("OK  logged in to the installed system as root")

    status = ask("pc-server status | sed 's/^/PCSTATUS| /'", r"PCSTATUS\| (\{.*\})", 60)
    if status is None or '"code":true' not in status:
        print("FAIL  the server's code is not on the installed system (app-misc/posterchan-server): "
              f"{status!r}")
        return 1
    rc = ask("pc-server enable; echo ENABLE-RC=$?", r"ENABLE-RC=(\d+)", 120)
    if rc != "0":
        print(f"FAIL  `pc-server enable` would not start its job (exit {rc})")
        return 1
    print("OK  `pc-server enable` started -- waiting for the install")
    deadline = clock() + timeout
    job = None
    while clock() < deadline:
        job = ask("pc-server job | sed 's/^/PCJOB| /'", r"PCJOB\| (\{.*\})", 60)
        if job and '"running":false' in job and re.search(r'"rc":"\d+"', job):
            break
        sleep(poll)
    else:
        print(f"FAIL  the server install was still running after {timeout}s. Transcript in "
              f"{evidence}/boot-console.log")
        return 1
    job_rc = re.search(r'"rc":"(\d+)"', job).group(1)
    if job_rc != "0":
        ask("pc-server job-log | tail -n 120 | sed 's/^/JOBLOG| /'; echo JOBLOG-END", r"(JOBLOG-END)", 60)
        print(f"FAIL  the server install failed (rc={job_rc}); its log is in {evidence}/boot-console.log "
              "(lines starting JOBLOG|)")
        return 1
    print("OK  the server install finished")
    deadline = clock() + 300
    last = ""
    while clock() < deadline:
        last = ask("echo SVC=$(systemctl is-active posterchanai.service) "
                   "HTTP=$(curl -s -o /dev/null -m 10 -w '%{http_code}' http://127.0.0.1:3051/client)",
                   r"SVC=([a-z-]+) HTTP=(\d{3})", 30) or ""
        if last and last[0] == "active" and last[1] in ("200", "301", "302", "307", "308"):
            print(f"OK  the server is running and answering on 3051 (HTTP {last[1]})")
            return _posts_flow(ask, evidence, clock=clock, sleep=sleep, poll=poll)
        sleep(poll)
    ask("journalctl -u posterchanai.service -n 80 --no-pager | sed 's/^/SVCLOG| /'; echo SVCLOG-END",
        r"(SVCLOG-END)", 60)
    print(f"FAIL  the server was set up but is not answering on 3051 (last: {last!r}); its journal is in "
          f"{evidence}/boot-console.log (lines starting SVCLOG|)")
    return 1


POSTS_WANTED = 5


def _posts_flow(ask, evidence, *, clock, sleep, poll, timeout=900):
    """THE PASS CONDITION IS POSTS ARRIVING, NOT A UNIT BEING ACTIVE. A server can answer on 3051 with
    a relay that syncs nothing -- no upstream reachable, a database it cannot write, a gate that drops
    everything -- and that node is useless. So it must be SEEN receiving Nostr posts (kind 1, into its
    own Postgres). And the moment it is, the caller powers the guest off: a test node left syncing
    spends this project's rate limits with every upstream relay it talks to."""
    q = ("runuser -u postgres -- psql -d posterchan_relay -Atc "
         "\"select 'POSTS=' || count(*) from events where kind=1\" 2>&1 | tail -n1")
    deadline = clock() + timeout
    seen = -1
    while clock() < deadline:
        got = ask(q, r"POSTS=(\d+)", 60)
        if got is not None:
            seen = int(got)
            if seen >= POSTS_WANTED:
                print(f"OK  Nostr posts are flowing into the installed node ({seen} kind-1 events) -- stopping it now")
                return 0
        sleep(poll)
    ask("journalctl -u posterchanai.service -n 120 --no-pager | grep -iE 'relay|sync|upstream|error' "
        "| sed 's/^/SVCLOG| /'; echo SVCLOG-END", r"(SVCLOG-END)", 60)
    print(f"FAIL  the server runs but no Nostr posts arrived in {timeout}s (kind-1 events: {seen}); its "
          f"journal is in {evidence}/boot-console.log (lines starting SVCLOG|)")
    return 1


def boot_installed(disk, serial_dir, vars_copy, evidence, timeout, memory, cpus, *, server=False,
                   password="", server_timeout=5400):
    """Boot the INSTALLED disk with no ISO and require it to reach a running system.

    THIS FUNCTION IS WHY THIS FILE EXISTS AND IT WAS NEVER WRITTEN. The module docstring has always
    opened with "Install PosterChanOS from an ISO into a blank virtual disk, then boot that disk with
    no ISO" and promised "Exit 0 installed and the installed disk booted". `main()` installed, printed
    OK, deleted the disk and returned 0. The only mention of booting was `--keep-disk`'s "leave the
    qcow2 behind for check_livecd_vm.py" -- a separate command nobody runs. So the single gate whose
    stated purpose was proving an installed system boots had never booted one, and "installer failed
    to make a bootable system" reached a user with every gate green.

    THE SAME NVRAM the install wrote is reused, because the boot entry the installer creates is part
    of what is under test; a fresh variables file would quietly test the removable-media fallback.
    """
    code, _ = ovmf()
    # ---- MAKE THE INSTALLED SYSTEM AUDIBLE, OR THIS CHECK CANNOT SEE IT ------------------------
    #
    # The LIVE image carries `console=ttyS0,115200n8` from the ISO's own grub line; the INSTALLED
    # system does not -- its loader entry is `quiet splash ... root=UUID=... rw rd.luks.uuid=...`.
    # So the firmware and systemd-boot write to the serial port, the kernel takes over, and every
    # further word goes to the graphical console. Measured 2026-09-18: the boot console stopped after
    # 1137 bytes at systemd-boot's menu and stayed silent, which this check would have reported as
    # "printed nothing recognisable" -- a failure of the harness dressed up as a failure of the ISO.
    #
    # `quiet` goes too, for the same reason: it suppresses exactly the messages being waited for.
    # This edits the TEST COPY of the disk's boot entry, never the ISO, and changes no code path that
    # decides whether the system boots -- same kernel, same initramfs, same crypt setup, one extra
    # console and the ordinary log level.
    added = _make_installed_boot_audible(disk, evidence)
    if added:
        print(f"OK  boot entry made audible for the test ({added})")
    sock = Path(serial_dir, "boot-console.sock")
    log = open(Path(evidence, "boot-console.log"), "w", encoding="utf-8")
    # iso=None leaves out every medium drive, so the only bootable thing is the installed disk.
    proc = subprocess.Popen(qemu_args(disk, None, sock, code, vars_copy, memory, cpus, net=server),
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    try:
        for _ in range(100):
            if sock.exists() or proc.poll() is not None:
                break
            time.sleep(0.1)
        if proc.poll() is not None:
            print("FAIL  qemu exited before opening a console for the installed disk: "
                  + (proc.stderr.read() or "")[-300:])
            return 1
        try:
            con = Serial(sock, log)
        except OSError as exc:
            print(f"FAIL  could not attach to the installed system's console ({exc})")
            return 1
        # WHAT COUNTS AS BOOTED. The installed system carries `console=ttyS0` from the installer's own
        # kernel command line, so systemd's progress and any failure both arrive here. A login prompt
        # or the shell's readiness are both acceptable; the emergency shell, a cryptsetup failure and
        # silence are not -- and "Failed to start Cryptography Setup" is named because that is the
        # sentence a real machine printed while every gate was green.
        # WHAT COUNTS AS BOOTED, NARROWED — `Reached target` used to be in here and it is printed on
        # the way INTO an emergency shell as readily as into a desktop. Measured 2026-09-18: a guest
        # whose kernel had no dm-crypt module failed with `unknown target type: crypt`, reached
        # several targets on its way to emergency mode, and this check called it a successful boot.
        # That false pass is how an unbootable image reached a user. A login prompt or the shell's
        # own readiness are evidence; a target is not.
        good = r"(login:|pc-shell|posterchan-shell|Startup finished)"
        bad = (r"(Failed to start Cryptography Setup|emergency mode|Emergency Shell|"
               r"Kernel panic|Give root password|Failed to mount /sysroot|"
               # device-mapper's own words when dm-crypt is missing from the kernel it booted.
               r"unknown target type|Dependency failed for)")
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            con.read(1.0)
            hit = re.search(bad, con.buf)
            if hit:
                print(f"FAIL  the installed system did not boot: {hit.group(1)!r} on its console. "
                      f"Transcript in {evidence}/boot-console.log")
                return 1
            if re.search(good, con.buf):
                print("OK  the installed disk booted with no ISO attached")
                if server:
                    return server_stage(con, password, server_timeout, evidence)
                return 0
            if proc.poll() is not None:
                print("FAIL  the installed system's VM exited without booting. Transcript in "
                      f"{evidence}/boot-console.log")
                return 1
        print(f"FAIL  the installed disk printed nothing recognisable within {timeout}s -- neither a "
              f"login, a systemd target, nor an error. Transcript in {evidence}/boot-console.log")
        return 1
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
    ap.add_argument("--boot-timeout", type=int, default=600,
                    help="how long the INSTALLED disk gets to reach a login or a systemd target")
    ap.add_argument("--evidence-dir", default="")
    ap.add_argument("--usb", action="store_true",
                    help="attach the ISO as a USB disk instead of a CD-ROM — the medium people "
                         "actually boot, and the only one on which the live-medium search can fail")
    ap.add_argument("--server", action="store_true",
                    help="after booting the installed disk, switch the bundled server on from it "
                         "(pc-server enable) and require it to answer on 3051")
    ap.add_argument("--server-timeout", type=int, default=5400)
    ap.add_argument("--rounds", type=int, default=1,
                    help="repeat the whole run from a BLANK disk this many times (a server pass is "
                         "only believed after a second fresh install)")
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

    base_evidence = Path(args.evidence_dir or tempfile.mkdtemp(prefix="pc-install-vm-"))
    for round_no in range(1, max(1, args.rounds) + 1):
        evidence = base_evidence if args.rounds <= 1 else Path(base_evidence, f"round-{round_no}")
        if args.rounds > 1:
            print(f"=== round {round_no} of {args.rounds}: a blank disk")
        rc = _one_round(args, evidence)
        if rc:
            return rc
    return 0


def _one_round(args, evidence):
    evidence.mkdir(parents=True, exist_ok=True)
    disk = Path(args.disk or Path(evidence, "installed.qcow2"))
    # A BLANK disk every run. Installing over a previous install proves the resume path, not the
    # fresh one, and the resume path is the one that does not erase.
    if disk.exists():
        disk.unlink()
    subprocess.run(["qemu-img", "create", "-q", "-f", "qcow2", str(disk), args.size], check=True)

    with tempfile.TemporaryDirectory(prefix="pc-install-sock-") as td:
        rc = install(args.iso, disk, td, evidence, args.timeout, args.memory, args.cpus,
                     args.usb)
        if rc:
            return rc
        print(f"OK  PosterChanOS installed from {Path(args.iso).name} onto a blank UEFI disk "
              f"({disk}); console transcript in {evidence}/install-console.log")
        # AND THEN IT BOOTS IT, which is what this file has always claimed to do. See boot_installed.
        rc = boot_installed(disk, td, Path(evidence, "OVMF_VARS.fd"), evidence,
                            args.boot_timeout, args.memory, args.cpus, server=args.server,
                            password=INSTALL_PASSWORD, server_timeout=args.server_timeout)
        if rc:
            return rc
    if not args.keep_disk and not args.disk:
        disk.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
