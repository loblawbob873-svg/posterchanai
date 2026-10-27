#!/usr/bin/env python3
"""PosterChanOS -- `gentoo.sh anticheat` ending to end under REAL Secure Boot, on an INSTALLED disk.

    sudo venv-unified/bin/python scripts/check_secureboot_vm.py <installed.qcow2> --evidence-dir DIR

Asked 2026-10-27 ("did you test the secure boot and signed kernel?" / "we need to test both install method types").
The anti-cheat commit (c43eed0a7) said it plainly: "The end-to-end Secure Boot VM run (MOK enrol -> SB on ->
pc-eac-check) is still to be done." Unit tests ran the shipped functions on a fake root; one run on the ISO build VM
saw shim boot the signed image. Nothing had ever taken an installed machine through the whole thing. This does, on a
throwaway OVERLAY of the disk (the original is never written), with OVMF's Secure Boot build and Microsoft's keys:

  1  Secure Boot OFF: the normal boot, a root shell on a second serial port (systemd's debug shell -- an installed
     machine has no root login, by design), then `gentoo.sh anticheat` exactly as a person runs it: it emerges
     shim + mokutil + the Gentoo-signed kernel image, makes the "PosterChanOS (anti-cheat)" boot entry and queues
     Gentoo's key with a one-time password. The debug shell is also enabled on ttyS1 FROM INSIDE the system,
     because the signed image ignores any kernel command line (the only way the test can see that boot).
  2  Secure Boot ON in the SAME variable store (boot entries and the queued key kept): `virt-fw-vars --set-true
     SecureBootEnable`. Nothing else about the firmware changes.
  3  MokManager, driven over the serial console the way a person drives it: a key, Enroll MOK, Continue, Yes, the
     password, Reboot.
  4  shim -> the signed image. Its initramfs asks for the disk passphrase on the SCREEN (no serial console under a
     fixed command line), so the screen is watched (QEMU screendump) and the passphrase typed with QEMU key presses.
     Then, as root: Secure Boot on, lockdown=integrity, kexec off, `pc-eac-check`.
  5  `gentoo.sh anticheat-off`, Secure Boot OFF again, and the normal entry boots on its own (keyfile unlock back).

Exit 0 every step held · 1 a step failed (what and where is printed, transcripts and screenshots in the evidence
dir) · 2 could not run (no root, no Secure Boot firmware, no virt-fw-vars).
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import check_livecd_install_vm as gate  # noqa: E402  (Serial, _make_installed_boot_audible, _asker)

PASSWORD = "pc-vm-test-only"        # the install gates' disposable disk passphrase
MOK_PASSWORD = "pcmoktest"          # the one-time MokManager password this test chooses
FW_DIRS = ("/usr/share/edk2/OvmfX64", "/usr/share/edk2-ovmf", "/usr/share/OVMF", "/usr/share/qemu")


def secboot_firmware():
    for base in FW_DIRS:
        code, vars_ = Path(base, "OVMF_CODE.secboot.fd"), Path(base, "OVMF_VARS.secboot.fd")
        if code.is_file() and vars_.is_file():
            return code, vars_
    return None, None


def fw_vars_tool():
    for c in (shutil.which("virt-fw-vars"), os.path.expanduser("~/.local/bin/virt-fw-vars"),
              *(str(p) for p in Path("/home").glob("*/.local/bin/virt-fw-vars"))):
        if c and Path(c).is_file():
            return c
    return None


class Monitor:
    """QEMU's human monitor: key presses into the guest's keyboard and screenshots of its screen."""

    def __init__(self, path):
        self.path = path

    def cmd(self, line):
        s = socket.socket(socket.AF_UNIX)
        s.settimeout(5)
        s.connect(str(self.path))
        s.recv(4096)
        s.sendall((line + "\n").encode())
        time.sleep(0.15)
        try:
            s.recv(4096)
        except socket.timeout:
            pass
        s.close()

    KEYS = {" ": "spc", "-": "minus", ".": "dot", "/": "slash", "_": "shift-minus"}

    def type(self, text):
        for ch in text:
            key = self.KEYS.get(ch) or (("shift-" + ch.lower()) if ch.isupper() else ch)
            self.cmd("sendkey " + key)
            time.sleep(0.08)

    def shot(self, path):
        self.cmd("screendump " + str(path))
        for _ in range(20):
            if Path(path).exists() and Path(path).stat().st_size:
                return Path(path)
            time.sleep(0.2)
        return None


def qemu(disk, code, vars_file, run, tag, memory, cpus, net):
    con, shell, mon = Path(run, tag + "-console.sock"), Path(run, tag + "-shell.sock"), Path(run, tag + "-mon.sock")
    args = ["qemu-system-x86_64", "-machine", "q35,smm=on,accel=kvm:tcg", "-cpu", "max",
            "-global", "driver=cfi.pflash01,property=secure,value=on", "-global", "ICH9-LPC.disable_s3=1",
            "-drive", f"if=pflash,format=raw,unit=0,readonly=on,file={code}",
            "-drive", f"if=pflash,format=raw,unit=1,file={vars_file}",
            "-m", str(memory), "-smp", str(cpus), "-display", "none", "-vga", "std", "-no-reboot",
            "-drive", f"file={disk},if=virtio,format=qcow2",
            "-chardev", f"socket,id=c0,path={con},server=on,wait=off", "-serial", "chardev:c0",
            "-chardev", f"socket,id=c1,path={shell},server=on,wait=off", "-serial", "chardev:c1",
            "-monitor", f"unix:{mon},server,nowait"]
    if net:
        args += ["-nic", "user,model=virtio-net-pci"]
    proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    for _ in range(100):
        if con.exists() and mon.exists() or proc.poll() is not None:
            break
        time.sleep(0.1)
    return proc, con, shell, Monitor(mon)


def stop(proc, timeout=120):
    if proc.poll() is None:
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.terminate()
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()


def drain(con):
    """Keep reading a console nobody else reads. The installed entry sends the kernel's messages to ttyS0, and a
    serial socket whose buffer fills makes every printk wait on it: measured, the boot crawled at one line per ~30s
    and never reached a shell in 10 minutes."""
    def run():
        while True:
            try:
                con.read(1.0)
            except Exception:
                return
    threading.Thread(target=run, daemon=True).start()
    return con


def attach(path, log, wait=60):
    end = time.monotonic() + wait
    while time.monotonic() < end:
        try:
            return gate.Serial(path, log)
        except OSError:
            time.sleep(0.5)
    return None


def root_shell(sock, evidence, wait):
    """The debug shell on ttyS1, as root: (ask, serial) -- (None, None) when it never answers."""
    sh = attach(sock, open(Path(evidence, "root-shell.log"), "a", encoding="utf-8"))
    if sh is None:
        return None, None
    ask = gate._asker(sh)
    end = time.monotonic() + wait
    while time.monotonic() < end:
        if ask("echo ROOT-$(id -u)", r"ROOT-(\d+)", 10) == "0":
            ask("systemctl is-system-running --wait >/dev/null 2>&1; echo SETTLED", r"SETTLED", 600)
            return ask, sh
        time.sleep(5)
    return None, None


def _tool_env(tool):
    """A `pip install --user` virt-firmware run as root: root's Python does not search that user's site-packages."""
    env = dict(os.environ)
    sites = [str(p) for p in Path(tool).resolve().parents[1].glob("lib/python3*/site-packages")]
    if sites:
        env["PYTHONPATH"] = os.pathsep.join(sites + [env.get("PYTHONPATH", "")]).rstrip(os.pathsep)
    return env


def fw(tool, src, dst, *opts, evidence=None, label=""):
    env = _tool_env(tool)
    r = subprocess.run([tool, "-i", str(src), "-o", str(dst), *opts], capture_output=True, text=True, timeout=120,
                       env=env)
    if r.returncode != 0 and evidence:
        Path(evidence, f"vars-{label}.err").write_text(r.stdout + r.stderr)
    if evidence:
        p = subprocess.run([tool, "-i", str(dst), "--print"], capture_output=True, text=True, timeout=120, env=env)
        Path(evidence, f"vars-{label}.txt").write_text(p.stdout + p.stderr)
    return r.returncode == 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("disk", help="an INSTALLED PosterChanOS qcow2 (kept by the install gates); never written")
    ap.add_argument("--evidence-dir", default="")
    ap.add_argument("--memory", type=int, default=4096)
    ap.add_argument("--cpus", type=int, default=4)
    ap.add_argument("--label", default="", help="what the disk is, for the report (live-ISO / from-scratch)")
    args = ap.parse_args()
    tag = f" [{args.label}]" if args.label else ""
    if os.geteuid() != 0:
        print("SKIP  run as root: the boot entry is edited over qemu-nbd, as the install gates do")
        return 2
    code, vars_tpl = secboot_firmware()
    tool = fw_vars_tool()
    if not code or not tool or not shutil.which("qemu-img"):
        print(f"SKIP  needs OVMF's Secure Boot build ({code}), virt-fw-vars ({tool}) and qemu-img")
        return 2
    evidence = Path(args.evidence_dir or tempfile.mkdtemp(prefix="pc-secureboot-"))
    evidence.mkdir(parents=True, exist_ok=True)
    run = Path(tempfile.mkdtemp(prefix="pcsb-"))
    disk = Path(run, "overlay.qcow2")
    subprocess.run(["qemu-img", "create", "-q", "-f", "qcow2", "-b", str(Path(args.disk).resolve()), "-F", "qcow2",
                    str(disk)], check=True, timeout=60)
    v_off, v_on, v_off2 = Path(run, "vars-off.fd"), Path(run, "vars-on.fd"), Path(run, "vars-off2.fd")
    if not fw(tool, vars_tpl, v_off, "--set-false", "SecureBootEnable", evidence=evidence, label="1-off"):
        print("FAIL  could not prepare a variable store with Secure Boot off")
        return 1
    print(f"OK  firmware: {code.name} with Microsoft's keys{tag}; disk overlay over {args.disk}")

    # ---- 1. Secure Boot OFF: the normal boot, then `gentoo.sh anticheat` -------------------------------------------
    added = gate._make_installed_boot_audible(disk, evidence, extra="systemd.debug_shell=ttyS1")
    # "" also means "already there": a disk kept by the live-ISO gate carries both from its own server stage. The
    # boot below is what says whether the shell is really there.
    print(f"OK  boot entry made audible for the test ({added or 'already in place'})")
    proc, con_p, sh_p, mon = qemu(disk, code, v_off, run, "1", args.memory, args.cpus, net=True)
    try:
        con = attach(con_p, open(Path(evidence, "1-console.log"), "w", encoding="utf-8"))
        if con:
            drain(con)
        ask, sh = root_shell(sh_p, evidence, 900) if con else (None, None)
        if not ask:
            mon.shot(Path(evidence, "1-no-shell.ppm"))
            print(f"FAIL  the installed system never gave the test a root shell with Secure Boot off{tag}")
            return 1
        print(f"OK  booted normally with Secure Boot off{tag}")
        # The signed image ignores any command line, so this is the only way to see its boot: systemd's debug
        # shell on ttyS1, switched on inside the system. On the test's overlay only.
        ask("mkdir -p /etc/systemd/system/debug-shell.service.d && printf '[Service]\\nTTYPath=/dev/ttyS1\\n' "
            ">/etc/systemd/system/debug-shell.service.d/ttyS1.conf && systemctl enable debug-shell.service "
            ">/dev/null 2>&1; echo DBG-$?", r"DBG-(\d+)", 30)
        # An install from an ISO older than the anti-cheat mode (it arrived 2026-10-23) has a gentoo.sh that does not know
        # the command -- `gentoo.sh anticheat` then opens the interactive installer menu (measured on the 20261019 ISO's
        # install). The way such a machine gets it is the way its owner would: update-posterchan.
        has = ask("grep -c '^anticheatSetup()' /usr/bin/gentoo.sh; echo H-END", r"(\d+)\s*\r?\nH-END", 30)
        if has == "0":
            print(f"..  this install's gentoo.sh predates the anti-cheat mode: `update-posterchan` first{tag}")
            up = ask("update-posterchan >/root/update-posterchan.log 2>&1; echo UP-RC=$?", r"UP-RC=(\d+)", 5400)
            has = ask("grep -c '^anticheatSetup()' /usr/bin/gentoo.sh; echo H-END", r"(\d+)\s*\r?\nH-END", 30)
            if up != "0" or has == "0":
                print(f"FAIL  update-posterchan exited {up} and gentoo.sh still has no anticheat ({has}){tag}")
                return 1
            print(f"OK  update-posterchan brought the current gentoo.sh (anticheat included){tag}")
        print(f"..  running `gentoo.sh anticheat` (emerges shim, mokutil and the signed kernel image){tag}")
        # In the FOREGROUND, on the debug shell -- a real serial terminal, which is what mokutil reads its password
        # from (it refuses a pipe). The password is typed at mokutil's own prompts, as a person types it.
        mark = len(sh.buf)
        sh.send("set -o pipefail; gentoo.sh anticheat 2>&1 | tee /root/anticheat.log; echo AC-RC=$?")
        # mokutil's own "input password:" never shows: its stdout is the pipe into tee, so the prompt (no newline) sits in
        # its buffer while it waits on the terminal -- measured, 35 minutes of both sides waiting. anticheat's line just
        # before it is printed and flushed, so that is the cue; mokutil then reads the password twice from the tty.
        cue = r"Choose a one-time password|AC-RC=\d+|Your Choice:"
        if sh.expect(cue, 7200, mark) is not None and "Choose a one-time password" in sh.buf[mark:]:
            for _ in range(2):
                time.sleep(3)
                sh.sock.sendall((MOK_PASSWORD + "\n").encode())
        if "Your Choice:" in sh.buf[mark - 200:]:
            print(f"FAIL  `gentoo.sh anticheat` opened the installer menu instead of the anti-cheat setup{tag}")
            return 1
        end = sh.expect(r"AC-RC=(\d+)", 600, 0)
        rc = re.findall(r"AC-RC=(\d+)", sh.buf)[-1] if end else None
        done = "Done. Reboot, enrol the key" in sh.buf
        if rc != "0" or not done:
            print(f"FAIL  `gentoo.sh anticheat` exited {rc} (Done printed: {done}) -- see {evidence}/root-shell.log{tag}")
            return 1
        queued = ask("mokutil --list-new 2>/dev/null | grep -ci gentoo; echo Q-END", r"(\d+)\s*\r?\nQ-END", 30)
        entry = ask("efibootmgr | grep -c 'PosterChanOS (anti-cheat)'; echo E-END", r"(\d+)\s*\r?\nE-END", 30)
        if queued in (None, "0") or entry in (None, "0"):
            print(f"FAIL  after anticheat: key queued={queued}, boot entry={entry}{tag}")
            return 1
        print(f"OK  `gentoo.sh anticheat` finished: Gentoo's key queued for MOK, the anti-cheat boot entry made{tag}")
        ask("systemctl poweroff", r"$", 5)
        stop(proc, 180)
    finally:
        stop(proc, 5)

    # ---- 2. Secure Boot ON in the same store ----------------------------------------------------------------------
    if not fw(tool, v_off, v_on, "--set-true", "SecureBootEnable", evidence=evidence, label="2-on"):
        print("FAIL  could not turn Secure Boot on in the variable store")
        return 1
    print("OK  Secure Boot switched ON (same variable store: boot entries and the queued key kept)")

    # ---- 3. MokManager, over the serial console ------------------------------------------------------------------
    proc, con_p, sh_p, mon = qemu(disk, code, v_on, run, "3", args.memory, args.cpus, net=False)
    try:
        con = attach(con_p, open(Path(evidence, "3-mokmanager.log"), "w", encoding="utf-8"))
        if not con or con.expect(r"(?i)press any key to perform MOK management", 180) is None:
            mon.shot(Path(evidence, "3-no-mokmanager.ppm"))
            print(f"FAIL  shim never offered MokManager with Secure Boot on (the queued key is not being asked for){tag}")
            return 1
        con.sock.sendall(b" ")
        DOWN, ENTER = b"\x1b[B", b"\r"

        def pick(label, downs, wait=30):
            """Wait for `label` on MokManager's screen, move `downs` rows from the top, Enter. Fixed row counts, not a
            guess at the highlight from escape codes: a wrong guess would wrap round the menu."""
            if con.expect(re.escape(label), wait, max(0, len(con.buf) - 4000)) is None:
                return False
            time.sleep(1.0)
            for _ in range(downs):
                con.sock.sendall(DOWN)
                time.sleep(0.4)
            con.sock.sendall(ENTER)
            time.sleep(1.0)
            return True

        # Main menu: Continue boot / Enroll MOK / ...; Enroll MOK: View key 0 / Continue. Those two screens reach the
        # serial console as text; from "Enroll the key(s)?" on, MokManager draws on the DISPLAY only (measured: no
        # 'Yes', 'Password' or 'Reboot' ever reaches ttyS0). So the rest goes by its fixed layout -- No / Yes, the
        # password, then Reboot as the first item -- with a screenshot of each screen as the evidence, and the
        # outcome below (the next boot does not stop here again, and the signed image boots) as the proof.
        for label, downs in (("Enroll MOK", 1), ("Continue", 1)):
            if not pick(label, downs):
                mon.shot(Path(evidence, f"3-{label.replace(' ', '-')}.ppm"))
                print(f"FAIL  MokManager: no '{label}' on its screen{tag}")
                return 1
        for step, keys in (("enroll-the-keys", [DOWN, ENTER]), ("password", [(MOK_PASSWORD + "\r").encode()]),
                           ("reboot", [ENTER])):
            time.sleep(3)
            mon.shot(Path(evidence, f"3-{step}.ppm"))
            for k in keys:
                con.sock.sendall(k)
                time.sleep(0.5)
        for _ in range(60):                      # MokManager reboots; with -no-reboot that ends this VM
            if proc.poll() is not None:
                break
            time.sleep(1)
        if proc.poll() is None:
            mon.shot(Path(evidence, "3-still-in-mokmanager.ppm"))
            print(f"FAIL  MokManager did not reboot after the password (wrong password, or a screen we did not expect){tag}")
            return 1
        stop(proc, 60)
        print(f"OK  MokManager: Enroll MOK -> Continue -> Yes -> password -> Reboot{tag}")
    finally:
        stop(proc, 5)

    # ---- 4. shim -> the signed image, Secure Boot ON ---------------------------------------------------------------
    proc, con_p, sh_p, mon = qemu(disk, code, v_on, run, "4", args.memory, args.cpus, net=True)
    try:
        c4 = attach(con_p, open(Path(evidence, "4-console.log"), "w", encoding="utf-8"))
        if c4:
            drain(c4)
        typed, prev, still, t0 = 0, None, 0, time.monotonic()
        ask = None
        while time.monotonic() - t0 < 900 and ask is None:
            time.sleep(5)
            if c4 and re.search(r"(?i)press any key to perform MOK management", c4.buf):
                mon.shot(Path(evidence, "4-mokmanager-again.ppm"))
                print(f"FAIL  shim asked for MOK management again: the key was not enrolled{tag}")
                return 1
            if proc.poll() is not None:
                print(f"FAIL  the VM exited during the Secure Boot boot (shim or the image was refused?){tag}")
                return 1
            shot = mon.shot(Path(run, "now.ppm"))
            # "Still" within a blinking cursor: the cursor alone changes every frame (a few hundred pixels), so an exact
            # comparison never matched and the prompt timed out into emergency mode untyped (measured). A changed area
            # under ~0.3% of the screen counts as still.
            img = None
            try:
                from PIL import Image, ImageChops
                img = Image.open(shot).convert("L") if shot else None
            except Exception:
                img = None
            moved = None
            if img is not None and prev is not None and prev.size == img.size:
                box = ImageChops.difference(prev, img).point(lambda v: 255 if v > 24 else 0).getbbox()
                moved = 0 if box is None else (box[2] - box[0]) * (box[3] - box[1])
            still = still + 1 if moved is not None and moved < img.size[0] * img.size[1] * 0.003 else 0
            prev = img
            # The passphrase prompt is a screen that stops changing after the firmware/shim/kernel text. Type once it
            # has held still for 15s (at most three times), then look for the shell.
            if still >= 3 and typed < 3 and time.monotonic() - t0 > 30:
                shutil.copy(shot, Path(evidence, f"4-prompt-{typed + 1}.ppm"))
                mon.type(PASSWORD)
                mon.cmd("sendkey ret")
                typed += 1
                still = 0
                ask, _ = root_shell(sh_p, evidence, 120)
        if not ask:
            mon.shot(Path(evidence, "4-no-shell.ppm"))
            print(f"FAIL  the signed boot never reached a running system (typed the passphrase {typed}x){tag}")
            return 1
        print(f"OK  shim -> the Gentoo-signed kernel image booted with Secure Boot ON (passphrase typed at its prompt){tag}")
        checks = {
            "Secure Boot enabled (mokutil)": ("mokutil --sb-state 2>&1; echo C-END", r"SecureBoot enabled"),
            "lockdown=integrity": ("cat /sys/kernel/security/lockdown; echo C-END", r"\[integrity\]"),
            "kexec off": ("sysctl -n kernel.kexec_load_disabled; echo C-END", r"(?m)^1\s*$"),
            "booted the signed image": ("cat /proc/cmdline; echo C-END", r"lockdown=integrity"),
            "kernel says Secure Boot": ("dmesg | grep -i 'secure boot' | head -3; echo C-END", r"(?i)secure boot enabled"),
        }
        bad = []
        for name, (cmd, want) in checks.items():
            # bash's bracketed-paste switch (ESC[?2004l) and the CR precede a one-line answer: "1" must still read as 1.
            out = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]|\r", "", ask(cmd, r"(?s)(.*?)C-END", 30) or "")
            if not re.search(want, out):
                bad.append(f"{name}: {out.strip()[-160:]!r}")
            else:
                print(f"OK  {name}{tag}")
        eac = ask("pc-eac-check 2>&1 | tail -20; echo EAC-RC=${PIPESTATUS[0]}; echo C-END", r"(?s)(.*?)C-END", 60) or ""
        Path(evidence, "pc-eac-check.txt").write_text(eac)
        print(f"..  pc-eac-check (full output in {evidence}/pc-eac-check.txt):")
        for line in eac.strip().splitlines()[-12:]:
            print("      " + line.strip()[:150])
        if bad:
            print(f"FAIL  under Secure Boot{tag}: " + "; ".join(bad))
            return 1
        # ---- 5. anticheat-off ------------------------------------------------------------------------------------
        print(f"..  `gentoo.sh anticheat-off`{tag}")
        off = ask("gentoo.sh anticheat-off >/root/anticheat-off.log 2>&1; echo OFF-RC=$?", r"OFF-RC=(\d+)", 5400)
        if off != "0":
            print(f"FAIL  `gentoo.sh anticheat-off` exited {off}{tag}")
            return 1
        ask("systemctl poweroff", r"$", 5)
        stop(proc, 180)
    finally:
        stop(proc, 5)
    if not fw(tool, v_on, v_off2, "--set-false", "SecureBootEnable", evidence=evidence, label="5-off"):
        print("FAIL  could not turn Secure Boot back off")
        return 1
    proc, con_p, sh_p, mon = qemu(disk, code, v_off2, run, "5", args.memory, args.cpus, net=False)
    try:
        c5 = attach(con_p, open(Path(evidence, "5-console.log"), "w", encoding="utf-8"))
        if c5:
            drain(c5)
        ask, _ = root_shell(sh_p, evidence, 600)
        if not ask:
            mon.shot(Path(evidence, "5-no-shell.ppm"))
            print(f"FAIL  after anticheat-off the normal entry did not boot on its own with Secure Boot off{tag}")
            return 1
        print(f"OK  anticheat-off: the normal entry boots again with Secure Boot off (no passphrase typed){tag}")
        ask("systemctl poweroff", r"$", 5)
        stop(proc, 120)
    finally:
        stop(proc, 5)
    print(f"PASS  Secure Boot end to end{tag}")
    shutil.rmtree(run, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
