#!/usr/bin/env python3
"""Build PosterChanOS FROM SCRATCH in a VM: stock Gentoo ISO, blank disk, `gentoo.sh scratch`.

    venv-unified/bin/python scripts/check_scratch_install_vm.py --keep-disk

THIS IS A DIFFERENT GATE FROM `check_livecd_install_vm.py`, AND IT IS THE ONE THAT COVERS THE BUILD.
That one boots a PosterChanOS ISO and runs `install-live`, which COPIES an already-built live image
onto a disk; it proves the deployer works and says nothing about whether the operating system can
still be built. This one starts where a new machine starts -- a stock Gentoo minimal install CD and
an empty disk -- and runs the installer's own from-scratch path: partition, LUKS, stage3, portage
against our mirror, @world, the kernel, the PosterChanOS package set, the bootloader. If a USE flag,
a masked atom, a profile choice or a repository URL has rotted, this is what says so, and nothing
else does. It is also what BUILDS the image the other gate then installs.

HOW IT DRIVES THE GUEST. The stock ISO's own boot menu has no serial entry, so the kernel and
initramfs are read straight out of the ISO and booted with `-kernel`/`-initrd` plus
`console=ttyS0,115200n8` -- the medium is unmodified, and the live image autologins root on whatever
console it is given. UEFI, not BIOS: the installer writes an ESP and a systemd-boot entry, and a
SeaBIOS guest would boot through a path the product never uses.

HOW THE CHECKOUT REACHES THE GUEST. Over HTTP from this host, on QEMU's own gateway (10.0.2.2) --
no image to build, no share to mount, nothing left behind on the host but a temporary port bound to
loopback. The same little server accepts the install log back by PUT at the end, because a build
this long that fails is worth nothing without its log and the serial console is far too slow to
carry it.

WHY THE CONSOLE ONLY CARRIES A HEARTBEAT. 115200 baud is 11 KB/s. A full Gentoo build prints
hundreds of megabytes; sending that down the serial line would make the LINE the bottleneck and turn
a four-hour build into a multi-day one. The install runs in the background writing to a file, and
the console gets one line a minute with the tail of it.

Exit 0 the build finished (and, unless --no-boot-check, the installed disk booted to a desktop),
1 it did not, 2 could not run.
"""
from __future__ import annotations

import argparse
import http.server
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import threading
import time


HERE = Path(__file__).resolve().parent
REPO = HERE.parent
MIRROR = "https://gentoo.poster.place"
ISO_INDEX = f"{MIRROR}/releases/amd64/autobuilds/current-install-amd64-minimal/"


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


def iso_label(iso: Path) -> str:
    """The ISO9660 volume id, read out of the Primary Volume Descriptor.

    `root=live:CDLABEL=...` must name the medium EXACTLY or dracut drops to an emergency shell with
    no root filesystem -- and the label is not the file name. Deriving it from the name worked for
    the releases whose name happened to encode the date and would break silently on any other.
    """
    with iso.open("rb") as fh:
        fh.seek(32768 + 40)
        return fh.read(32).decode("ascii", "replace").strip()


class Serve(http.server.BaseHTTPRequestHandler):
    """GET the installer tree, PUT the evidence back. Loopback only."""

    root: Path = Path("/")
    evidence: Path = Path("/")

    def log_message(self, *a):  # noqa: D102 - a build log, not an access log
        pass

    def do_GET(self):
        target = Path(self.root, self.path.lstrip("/")).resolve()
        if not str(target).startswith(str(self.root.resolve())) or not target.is_file():
            self.send_error(404)
            return
        data = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_PUT(self):
        name = Path(self.path).name or "upload"
        size = int(self.headers.get("Content-Length") or 0)
        Path(self.evidence, name).write_bytes(self.rfile.read(size))
        self.send_response(204)
        self.end_headers()


class Serial:
    """The guest's console as a line-oriented conversation; everything read is kept."""

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
        time.sleep(0.2)


def fetch_iso(cache: Path) -> Path | None:
    """The current minimal install CD, from our own mirror, cached between runs."""
    import urllib.request

    try:
        with urllib.request.urlopen(ISO_INDEX, timeout=30) as r:
            index = r.read().decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001 - any failure here is "cannot run"
        print(f"SKIP  could not list {ISO_INDEX}: {exc}")
        return None
    names = sorted(set(re.findall(r"install-amd64-minimal-[0-9TZ]+\.iso", index)))
    if not names:
        print(f"SKIP  no minimal install ISO listed at {ISO_INDEX}")
        return None
    iso = Path(cache, names[-1])
    if iso.is_file() and iso.stat().st_size > 100 * 1024 * 1024:
        return iso
    cache.mkdir(parents=True, exist_ok=True)
    part = iso.with_suffix(".part")
    print(f"..    fetching {names[-1]}")
    try:
        urllib.request.urlretrieve(ISO_INDEX + names[-1], part)
    except Exception as exc:  # noqa: BLE001
        print(f"SKIP  could not download the install ISO: {exc}")
        return None
    part.rename(iso)
    return iso


def installer_tarball(dest: Path) -> Path:
    """This checkout's os/ tree, which is what the guest will install FROM.

    The tree, not just gentoo.sh: the script copies pc-* helpers out of bin/ and the boot theme out
    of plymouth/, and finalization REFUSES to report success without the theme. A tarball of the
    script alone would fail hours in.
    """
    tar = Path(dest, "pcos.tar.gz")
    with tarfile.open(tar, "w:gz") as tf:
        for entry in sorted(Path(REPO, "os").iterdir()):
            if entry.name == "__pycache__":
                continue
            tf.add(entry, arcname=entry.name)
    return tar


def qemu_args(disk, iso, kernel, initrd, append, serial_path, code, vars_copy, memory, cpus, port):
    return [
        "qemu-system-x86_64", "-machine", "q35,accel=kvm:tcg", "-cpu", "max",
        "-m", str(memory), "-smp", str(cpus), "-display", "none", "-no-reboot",
        "-drive", f"if=pflash,format=raw,unit=0,readonly=on,file={code}",
        "-drive", f"if=pflash,format=raw,unit=1,file={vars_copy}",
        "-drive", f"file={disk},if=virtio,format=qcow2",
        "-drive", f"file={iso},media=cdrom,readonly=on",
        "-kernel", str(kernel), "-initrd", str(initrd), "-append", append,
        "-netdev", "user,id=n0",
        "-device", "virtio-net-pci,netdev=n0",
        "-chardev", f"socket,id=pcserial,path={serial_path},server=on,wait=off",
        "-serial", "chardev:pcserial",
    ]


# Every tool the installer reaches for on the live medium. Asked for by name before the first byte
# is written, because the alternative is finding out at `mkfs.vfat` -- after the disk has been
# repartitioned and LUKS-formatted, which is the one state a person cannot simply retry from.
LIVE_TOOLS = "wipefs parted cryptsetup mkfs.btrfs mkfs.vfat wget chroot blkid findmnt partprobe"


def install(iso, disk, boot, td, evidence, args, port, password):
    code, vars_src = ovmf()
    vars_copy = Path(td, "OVMF_VARS.fd")
    shutil.copyfile(vars_src, vars_copy)
    sock = Path(td, "console.sock")
    label = iso_label(iso)
    append = (f"root=live:CDLABEL={label} rd.live.dir=/ rd.live.squashimg=image.squashfs cdroot "
              "console=ttyS0,115200n8")
    log = open(Path(evidence, "scratch-console.log"), "w", encoding="utf-8")
    proc = subprocess.Popen(
        qemu_args(disk, iso, boot / "gentoo", boot / "gentoo.igz", append, sock, code, vars_copy,
                  args.memory, args.cpus, port),
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
        if con.expect(r"root@livecd", args.boot_timeout) is None:
            print(f"FAIL  the live medium never reached a root shell on the serial console within "
                  f"{args.boot_timeout}s — transcript in {evidence}/scratch-console.log")
            return 1

        # THE ANSWER IS COLLECTED INTO ONE LINE, not printed per tool. The console echoes back
        # everything sent to it, so a per-tool `echo NEED-$t` puts the literal string "NEED-$t" on
        # the transcript before a single tool has been looked at -- which the first version read as
        # a missing tool called "$t;" and reported this medium as unusable. Reading the LAST
        # occurrence of a marker the guest fills in cannot confuse the question with its answer.
        con.send("m=; for t in " + LIVE_TOOLS + "; do command -v $t >/dev/null || m=\"$m $t\"; "
                 "done; echo \"PCTOOLS:[$m]\"")
        at = con.expect(r"PCTOOLS:\[[^\]]*\]\s*\r?\n", 120)
        if at is None:
            print("FAIL  the live shell did not answer a tool check")
            return 1
        missing = re.findall(r"PCTOOLS:\[([^\]]*)\]", con.buf)[-1].split()
        if missing:
            print("SKIP  this live medium is missing " + " ".join(missing)
                  + " — the installer cannot run on it")
            return 2

        # The guest reaches this host at QEMU's gateway. A failure here is the whole run, so it is
        # checked rather than assumed: wget's exit status, not the presence of a file.
        con.send(f"wget -q -O /tmp/pcos.tar.gz http://10.0.2.2:{port}/pcos.tar.gz; echo GET-$?")
        if con.expect(r"GET-0\b", 180, at) is None:
            print("FAIL  the guest could not fetch the installer tree from this host — no guest "
                  "network, or the host firewall dropped QEMU's user-mode gateway")
            return 1
        con.send("mkdir -p /usr/local/share/posterchanos && "
                 "tar xzf /tmp/pcos.tar.gz -C /usr/local/share/posterchanos && "
                 "chmod +x /usr/local/share/posterchanos/gentoo.sh && echo UNPACK-$?")
        if con.expect(r"UNPACK-0\b", 120) is None:
            print("FAIL  the installer tree did not unpack in the guest")
            return 1

        # The installer's own scripting hook: disk, BTRFS root volume name, swap choice, one per
        # line. `vda` is the virtio disk; the live medium is a cdrom, which setDevices refuses to
        # install onto anyway. The values are the ones the interactive prompts default to.
        con.send("printf 'vda\\ngentoo\\nnone\\n' >/tmp/disk; echo HOOK-$?")
        if con.expect(r"HOOK-0\b", 60) is None:
            print("FAIL  could not write the installer's /tmp/disk hook")
            return 1

        # THE BUILD, DETACHED FROM THE CONSOLE. setsid so a serial hiccup cannot signal it, output
        # to a file, and a status file written after it so the loop below has something unambiguous
        # to wait on -- an exit code echoed onto a line that also carries build output cannot be
        # told apart from build output.
        con.send(
            f"cd /tmp && (setsid env PC_ASSUME_YES=1 PC_INSTALL_PASSWORD={password} "
            "PC_REPO_CHOICE=local bash /usr/local/share/posterchanos/gentoo.sh scratch "
            ">/tmp/scratch.log 2>&1; echo $? >/tmp/scratch.rc) & echo LAUNCHED")
        if con.expect(r"LAUNCHED", 60) is None:
            print("FAIL  the installer did not start")
            return 1
        # ONE LINE A MINUTE, AND IT SAYS WHAT PORTAGE IS DOING -- not merely the last line written.
        # The first version tailed one line, which on a `--jobs 5` build is whatever thread happened
        # to be mid-sentence: it reported two red package names fifteen minutes apart and there was
        # no way to tell a compile from a collapse. `>>>` is portage's own progress marker and
        # `!!!` its own error marker, so a count of the second beside the last of the first
        # distinguishes "building" from "failing" without carrying the build output.
        #
        # AND THE LOG COMES BACK WHILE IT RUNS. Uploading only at the end means a run that has to be
        # abandoned -- or one that is still going when a person needs to know why -- leaves nothing
        # at all; the tail is enough to diagnose with and small enough to send every ten minutes.
        con.send("n=0; while [ ! -f /tmp/scratch.rc ]; do sleep 60; n=$((n+1)); "
                 "echo \"PCPROGRESS ${SECONDS}s $(stat -c %s /tmp/scratch.log) "
                 "err=$(grep -ac '^!!!' /tmp/scratch.log) "
                 "$(grep -a '^>>>' /tmp/scratch.log | tail -n 1 | tr -dc '[:print:]' | tail -c 90)\"; "
                 "[ $((n % 10)) -eq 0 ] && { tail -c 2000000 /tmp/scratch.log >/tmp/scratch.tail; "
                 f"wget -q --method=PUT --body-file=/tmp/scratch.tail http://10.0.2.2:{port}/scratch-tail.log "
                 "-O /dev/null; }; done; echo SCRATCH-EXIT-$(cat /tmp/scratch.rc)")

        started = time.monotonic()
        last_seen = ""
        deadline = started + args.timeout
        while time.monotonic() < deadline:
            end = con.expect(r"SCRATCH-EXIT-(\d+)", 60)
            if end is not None:
                break
            lines = re.findall(r"PCPROGRESS (\S+) (\d+) err=(\d+) ?(.*)", con.buf)
            if lines and lines[-1] != last_seen:
                last_seen = lines[-1]
                mins = int((time.monotonic() - started) / 60)
                errs = f"  {last_seen[2]} !!!" if last_seen[2] != "0" else ""
                print(f"..    {mins:>4}m  log {int(last_seen[1]) // 1024}KB{errs}  "
                      f"{last_seen[3][-90:]}", flush=True)
        else:
            print(f"FAIL  the build did not finish within {args.timeout}s")
            upload_log(con, port)
            return 1

        rc = re.findall(r"SCRATCH-EXIT-(\d+)", con.buf)[-1]
        upload_log(con, port)
        if rc != "0":
            print(f"FAIL  gentoo.sh scratch exited {rc}; build log in {evidence}/scratch.log, "
                  f"console transcript in {evidence}/scratch-console.log")
            return 1
        con.send("poweroff")
        try:
            proc.wait(timeout=180)
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


def upload_log(con, port):
    """Send the build log back to the host. Best effort -- a missing log must not fail a good run."""
    con.send(f"wget -q --method=PUT --body-file=/tmp/scratch.log "
             f"http://10.0.2.2:{port}/scratch.log -O /dev/null; echo PUT-$?")
    con.expect(r"PUT-\d", 600)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--iso", default=os.environ.get("PC_GENTOO_ISO", ""),
                    help="a Gentoo minimal install CD, or `auto` to fetch one from our mirror; "
                         "without it this gate skips")
    ap.add_argument("--cache-dir", default=os.environ.get("PC_ISO_CACHE", "/var/tmp/pc-iso-cache"))
    ap.add_argument("--disk", default=os.environ.get("PC_INSTALL_DISK", ""))
    ap.add_argument("--size", default="60G")
    ap.add_argument("--memory", type=int, default=8192)
    ap.add_argument("--cpus", type=int, default=os.cpu_count() or 4)
    ap.add_argument("--timeout", type=int, default=6 * 3600)
    ap.add_argument("--boot-timeout", type=int, default=600)
    ap.add_argument("--evidence-dir", default="")
    ap.add_argument("--keep-disk", action="store_true")
    ap.add_argument("--no-boot-check", action="store_true",
                    help="stop after the build instead of booting the installed disk")
    args = ap.parse_args()

    # OPT-IN, LIKE EVERY OTHER GATE THAT OWNS A GUEST. `./test.sh` DISCOVERS check_*.py, so a gate
    # that quietly downloads an ISO and starts a source build would turn a ten-minute suite into an
    # eight-hour one on any machine with /dev/kvm -- and it would do it the first time somebody ran
    # the suite after this landed, with nothing on screen to say why. Naming an ISO (or `auto`) is
    # how a person asks for it. ASKED FIRST, before the tool and firmware checks: it is the answer
    # that does not depend on the host, so it is the same answer on every machine in the fleet.
    if not args.iso:
        print("SKIP  a from-scratch build takes hours and is not part of the ordinary suite. "
              "Run it with --iso <gentoo-minimal.iso>, or --iso auto to fetch one. Nothing was "
              "verified about the from-scratch installer.")
        return 2

    for tool in ("qemu-system-x86_64", "qemu-img", "bsdtar"):
        if not shutil.which(tool):
            print(f"SKIP  {tool} is not installed on this host")
            return 2
    if ovmf() == (None, None):
        print("SKIP  no OVMF firmware on this host; a BIOS guest would not test the bootloader")
        return 2
    # A from-scratch Gentoo build under TCG emulation is not a long test, it is an impossible one.
    if not Path("/dev/kvm").exists():
        print("SKIP  no /dev/kvm on this host — a full source build without hardware "
              "virtualisation would take days")
        return 2

    # OPT-IN, LIKE EVERY OTHER GATE THAT OWNS A GUEST. `./test.sh` DISCOVERS check_*.py, so a gate
    # that quietly downloads an ISO and starts a source build would turn a ten-minute suite into an
    # eight-hour one on any machine with /dev/kvm -- and it would do it the first time somebody ran
    # the suite after this landed, with nothing on screen to say why. Naming an ISO (or `auto`) is
    # how a person asks for it.
    if not args.iso:
        print("SKIP  a from-scratch build takes hours and is not part of the ordinary suite. "
              "Run it with --iso <gentoo-minimal.iso>, or --iso auto to fetch one. Nothing was "
              "verified about the from-scratch installer.")
        return 2
    if args.iso == "auto":
        iso = fetch_iso(Path(args.cache_dir))
        if iso is None:
            return 2
    else:
        iso = Path(args.iso)
    if not iso.is_file():
        print(f"SKIP  no such ISO: {iso}")
        return 2

    evidence = Path(args.evidence_dir or tempfile.mkdtemp(prefix="pc-scratch-vm-"))
    evidence.mkdir(parents=True, exist_ok=True)
    disk = Path(args.disk or Path(evidence, "scratch.qcow2"))
    # A BLANK disk every run: installing over a previous install exercises the resume path.
    if disk.exists():
        disk.unlink()
    subprocess.run(["qemu-img", "create", "-q", "-f", "qcow2", str(disk), args.size], check=True)

    with tempfile.TemporaryDirectory(prefix="pc-scratch-") as td:
        boot = Path(td, "boot")
        boot.mkdir()
        # The kernel and initramfs out of the unmodified medium. --strip-components so they land
        # beside each other rather than under boot/boot/.
        r = subprocess.run(["bsdtar", "-xf", str(iso), "-C", str(boot), "--strip-components=1",
                            "boot/gentoo", "boot/gentoo.igz"],
                           capture_output=True, text=True)
        if r.returncode or not (boot / "gentoo").is_file():
            print(f"SKIP  could not read a kernel out of {iso.name}: {r.stderr.strip()[-200:]}")
            return 2

        installer_tarball(Path(td))
        Serve.root = Path(td)
        Serve.evidence = evidence
        httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Serve)
        port = httpd.server_address[1]
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            rc = install(iso, disk, boot, Path(td), evidence, args, port, "pc-vm-test-only")
        finally:
            httpd.shutdown()

    if rc:
        return rc
    print(f"OK    PosterChanOS built from scratch onto a blank UEFI disk ({disk}); build log in "
          f"{evidence}/scratch.log")
    if args.no_boot_check:
        return 0

    probe = HERE / "check_livecd_vm.py"
    if not probe.is_file():
        print("..    no check_livecd_vm.py beside this script; the installed disk was not booted")
        return 0
    print("..    booting the installed disk with no ISO")
    rc = subprocess.run([sys.executable, str(probe), "--disk", str(disk),
                         "--evidence-dir", str(Path(evidence, "boot"))]).returncode
    if rc:
        print("FAIL  the built system did not reach a graphical session")
        return rc
    if not args.keep_disk and not args.disk:
        disk.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
