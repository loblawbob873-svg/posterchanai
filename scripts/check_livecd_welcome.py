#!/usr/bin/env python3
"""Does the ISO -- and the disk it installs -- come up at the WELCOME SCREEN?

    venv-unified/bin/python scripts/check_livecd_welcome.py /path/to/posterchan-live-YYYYMMDD.iso
    venv-unified/bin/python scripts/check_livecd_welcome.py --disk /path/to/installed.qcow2 \
        --recovery-iso /path/to/live.iso --disk-key-file /path/to/vm-only-key

THE GATE NOTHING ELSE COVERS. `check_livecd_vm.py` proves a graphical frame appears and stays --
three consecutive non-black framebuffer samples. That passes just as happily on a desktop with no
wizard, on a stale session, or on an error dialog, so "it boots" has never been evidence that it
boots to the first-run wizard, which is the entire experience of a new machine.

Installed disks do not have the live account's serial reporter. Their check boots a temporary
qcow2 overlay, then opens that overlay read-only in the recovery ISO and reads only newly appended
shell.log bytes. A prefix hash proves an old cached verdict cannot satisfy the new boot. Offline
and online runs get separate overlays; the installed disk and production ISO remain unchanged.

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
import base64
import json
import shlex
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


def run_guest(args, boot_iso: str | None, disk: str | None, seconds: int,
              networked: bool = False):
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
        # A MACHINE NOBODY HAS SET UP HAS NO NETWORK, and QEMU hands every guest a working NAT
        # unless told otherwise. So this gate's own premise -- "the radio is asked first" -- was
        # false for the guest it booted: the wizard correctly SKIPPED the network step, because the
        # machine was already online, and the gate called that a failure. `-nic none` is what makes
        # the question real; the networked pass below asks the opposite question.
        cmd += ["-nic", "user"] if networked else ["-nic", "none"]
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
                proc.wait(timeout=15)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)



def recovery_collector(root, baseline=None):
    """Read append-only shell logs; never accept a verdict already on the installed disk."""
    return """
from pathlib import Path
import base64, hashlib, json
root = Path(ROOT)
baseline = BASELINE
result = {}
for home in sorted(root.iterdir()):
    path = home / '.config/posterchan-desktop/shell.log'
    if not path.is_file():
        continue
    size = path.stat().st_size
    old = (baseline or {}).get(home.name, {'size': 0, 'sha256': hashlib.sha256(b'').hexdigest()})
    count = old['size'] if baseline is not None else size
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        remaining = count
        while remaining:
            block = stream.read(min(remaining, 1048576))
            if not block:
                raise RuntimeError('shell log truncated: ' + home.name)
            digest.update(block)
            remaining -= len(block)
        if baseline is None:
            result[home.name] = {'size': count, 'sha256': digest.hexdigest()}
        else:
            if digest.hexdigest() != old['sha256']:
                raise RuntimeError('shell log prefix changed: ' + home.name)
            fresh = stream.read(4194305)
            if len(fresh) > 4194304:
                raise RuntimeError('new shell log exceeds capture limit: ' + home.name)
            result[home.name] = {'fresh': fresh.decode('utf-8', errors='replace')}
print('PC_WELCOME_DATA=' + base64.b64encode(json.dumps(result).encode()).decode(), flush=True)
""".replace('ROOT', repr(str(root))).replace('BASELINE', repr(baseline))


def disk_key_prompt():
    # The live shell emits OSC command markers without a newline. Put the probe marker on its
    # own line so an anchored observer sees actual output, never the echoed command text.
    return "printf '\\nPC_WELCOME_KEY_READY\\n'; read -r -s pc_welcome_key; echo; "


def recover_logs(args, disk, baseline=None):
    """Observe an installed disk using the live ISO, without host mounts or guest writes."""
    # This existing serial helper speaks to the live account, not the installed user.
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from check_livecd_install_vm import Serial, qemu_args, ovmf as install_ovmf
    code, data = install_ovmf()
    if not code:
        raise RuntimeError('no OVMF firmware for recovery observation')
    key = Path(args.disk_key_file).read_text().rstrip('\n')
    if not key or '\n' in key or '\r' in key:
        raise RuntimeError('disk key file must contain one nonempty passphrase line')
    with tempfile.TemporaryDirectory(prefix='pc-welcome-recovery-') as td:
        tmp = Path(td)
        nvram = tmp / 'vars.fd'
        shutil.copyfile(data, nvram)
        sock = tmp / 'console.sock'
        cmd = qemu_args(str(disk), args.recovery_iso, sock, code, nvram, args.memory, args.cpus)
        drive = f'file={disk},if=virtio,format=qcow2'
        cmd[cmd.index(drive)] = drive + ',readonly=on'
        cmd += ['-nic', 'none']
        with tempfile.TemporaryFile(mode='w+', encoding='utf-8') as transcript, tempfile.TemporaryFile() as errors:
            proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=errors)
            con = None
            try:
                deadline = time.monotonic() + args.seconds
                while not sock.exists() and proc.poll() is None and time.monotonic() < deadline:
                    time.sleep(.1)
                while con is None and proc.poll() is None and time.monotonic() < deadline:
                    try:
                        con = Serial(sock, transcript)
                    except OSError:
                        time.sleep(.1)
                if con is None:
                    raise RuntimeError('recovery QEMU did not open its serial socket')
                if con.expect(r'live@[-a-z0-9]+', args.seconds) is None:
                    raise RuntimeError('recovery ISO did not reach its live serial shell')
                # read -s prevents the disposable key from entering the evidence transcript.
                con.send(disk_key_prompt() +
                         "printf '%s' \"$pc_welcome_key\" | sudo cryptsetup open --readonly "
                         "/dev/vda2 pc_welcome_probe --key-file=- && "
                         "sudo mkdir -p /mnt/pc-welcome && "
                         "sudo mount -o ro,rescue=nologreplay,subvol=@home "
                         "/dev/mapper/pc_welcome_probe /mnt/pc-welcome && echo PC_WELCOME_MOUNT_OK; unset pc_welcome_key")
                if con.expect(r'(?m)^PC_WELCOME_KEY_READY', 30) is None:
                    raise RuntimeError('recovery shell did not ask for the disk key')
                start = len(con.buf)
                con.send(key)
                if con.expect(r'(?m)^PC_WELCOME_MOUNT_OK\r?$', 60, start) is None:
                    raise RuntimeError('could not unlock/mount installed Btrfs read-only')
                source = recovery_collector('/mnt/pc-welcome', baseline)
                encoded = base64.b64encode(source.encode()).decode()
                start = len(con.buf)
                con.send('sudo python3 -c ' + shlex.quote(
                    'import base64;exec(base64.b64decode(' + repr(encoded) + '))'))
                if con.expect(r'(?m)^PC_WELCOME_DATA=[A-Za-z0-9+/=]+\r?$', 60, start) is None:
                    raise RuntimeError('recovery log collector did not return a valid snapshot')
                payload = re.findall(r'(?m)^PC_WELCOME_DATA=([A-Za-z0-9+/=]+)\r?$', con.buf[start:])[-1]
                return json.loads(base64.b64decode(payload))
            except (OSError, RuntimeError, ValueError) as exc:
                errors.flush(); errors.seek(0)
                detail = errors.read().decode(errors='replace')
                if con is not None:
                    detail += '\n' + con.buf[-2500:]
                raise RuntimeError(f'{exc}\n{detail[-3000:].replace(key, "<redacted>")}') from exc
            finally:
                try:
                    if getattr(args, 'evidence_dir', None):
                        evidence = Path(args.evidence_dir)
                        evidence.mkdir(parents=True, exist_ok=True)
                        label = 'baseline' if baseline is None else Path(disk).parent.name
                        transcript.flush(); transcript.seek(0)
                        (evidence / (label + '-recovery.log')).write_text(transcript.read().replace(key, '<redacted>'))
                finally:
                    try:
                        if con is not None:
                            con.sock.close()
                    finally:
                        if proc.poll() is None:
                            proc.terminate()
                            try:
                                proc.wait(timeout=15)
                            except subprocess.TimeoutExpired:
                                proc.kill()
                                proc.wait(timeout=15)


def run_installed_guest(args, baseline, networked=False):
    # Each scenario starts from the same untouched installed image. Recovery opens even this
    # temporary overlay read-only, so mounting for observation cannot replay its Btrfs log.
    with tempfile.TemporaryDirectory(prefix='pc-welcome-disk-') as td:
        overlay = Path(td) / 'observation.qcow2'
        subprocess.run(['qemu-img', 'create', '-q', '-f', 'qcow2', '-F', 'qcow2',
                        '-b', str(Path(args.disk).resolve()), str(overlay)], check=True)
        run_guest(args, None, str(overlay), args.seconds, networked=networked)
        snapshot = recover_logs(args, overlay, baseline)
        fresh = '\n'.join(row['fresh'] for row in snapshot.values())
        if getattr(args, 'evidence_dir', None):
            evidence = Path(args.evidence_dir)
            evidence.mkdir(parents=True, exist_ok=True)
            (evidence / ('installed-online.log' if networked else 'installed-offline.log')).write_text(fresh)
        return fresh


def judge(console: str, what: str, expect: str = "network") -> int:
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
    if step != expect:
        if expect == "network":
            print(f"FAIL  {what}: the wizard opened on {step!r}, not 'network'. The radio is asked "
                  f"for first because every later question needs it; asked fourth, somebody types "
                  f"an instance URL at a machine that cannot reach one")
        else:
            # The networked pass. `tor` here is the specific regression that shipped: a build whose
            # instance was answered by a BUILT-IN DEFAULT rather than by the person, so every disc
            # came up pointed at the developer's server and never asked.
            extra = (" — an instance nobody chose was treated as an answer"
                     if step in ("tor", "signin", "account") else "")
            print(f"FAIL  {what}: online, the wizard opened on {step!r}, not {expect!r}{extra}")
        return 1
    if blocked != "0":
        print(f"FAIL  {what}: the wizard opened on {expect!r} already blocked — the first screen of "
              f"a new machine is a dead end")
        return 1
    print(f"OK    {what}: comes up at the welcome screen (step={expect})")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("iso", nargs="?", help="the LiveCD to boot")
    ap.add_argument("--disk", help="an installed virtual disk to boot instead (no ISO attached)")
    ap.add_argument("--recovery-iso", help="live ISO used to read installed logs without changing the disk")
    ap.add_argument("--disk-key-file", help="file containing the disposable installed VM passphrase")
    ap.add_argument("--evidence-dir", help="retain redacted recovery transcripts and fresh installed logs")
    ap.add_argument("--memory", type=int, default=4096)
    ap.add_argument("--cpus", type=int, default=2)
    ap.add_argument("--offline-only", action="store_true",
                    help="only the no-network pass (skip the online instance-question pass)")
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

    baseline = None
    if args.disk:
        if not args.recovery_iso or not args.disk_key_file:
            print('SKIP  --disk requires --recovery-iso and --disk-key-file; installed sessions have no live serial reporter')
            return 2
        if not Path(args.recovery_iso).is_file() or not Path(args.disk_key_file).is_file():
            print('SKIP  recovery ISO or disk key file is missing')
            return 2
        if not shutil.which('qemu-img') or not shutil.which('qemu-system-x86_64'):
            print('SKIP  installed observation requires qemu-img and qemu-system-x86_64')
            return 2
        try:
            baseline = recover_logs(args, Path(args.disk).resolve())
        except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
            print(f'SKIP  could not observe the installed disk: {exc}')
            return 2

    what = "installed disk" if args.disk else Path(args.iso).name

    # TWO GUESTS, BECAUSE THERE ARE TWO QUESTIONS AND ONE BOOT CANNOT ANSWER BOTH.
    #
    #   offline — a machine nobody has set up: the wizard must open on the radio.
    #   online  — the same image with a working link: the machine must NOTICE (so `network` is
    #             answered and skipped) and then ask the FIRST thing it does not know, which is
    #             which instance to talk to. That half is what caught a build shipping the
    #             developer's instance as a silent default.
    def observe(networked=False):
        try:
            if args.disk:
                return run_installed_guest(args, baseline, networked)
            return run_guest(args, args.iso, None, args.seconds, networked)
        except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
            print(f'SKIP  could not read this boot: {exc}')
            return None

    rc = judge(observe(),
               what + " (no network)", "network")
    if rc != 0:
        return rc
    if args.offline_only:
        return 0
    return judge(observe(networked=True),
                 what + " (online)", "instance")


if __name__ == "__main__":
    sys.exit(main())
