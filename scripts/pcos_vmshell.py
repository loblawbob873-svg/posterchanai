#!/usr/bin/env python3
"""Ask a question of an INSTALLED PosterChanOS disk, in minutes instead of a three-hour rebuild.

    venv-unified/bin/python scripts/pcos_vmshell.py --disk /var/tmp/pcvm/scratch.qcow2 \
        'emerge -uDNp @world' '!ls -l /tmp/install/etc/portage'

WHY THIS EXISTS, WRITTEN DOWN BECAUSE WE LEARNED IT THE EXPENSIVE WAY. `check_scratch_install_vm.py`
builds PosterChanOS from nothing, which takes about three hours — and almost every defect we found in
it lives in the LAST FIVE MINUTES of that run: the installer-version gate, the repos check, three
separate false greens in the @world conflict check, the ABI convergence sweep. Each of those was
tested by rebuilding the entire operating system to reach the phase that had failed. That is a
three-hour feedback loop on a five-minute question, and it is the single biggest cost in the work
this tool comes from.

The disk the gate leaves behind (`--keep-disk`) already contains the answer. This boots the live ISO
against THAT disk, opens the encrypted root with `gentoo.sh mount`, and runs whatever you ask inside
the installed system — the same repositories, make.conf, package database and USE flags the finished
machine has. A resolution question takes about ninety seconds.

WHAT IT IS NOT: a gate. It reports what it was asked and judges nothing, it is not discovered by
`./test.sh` (the name is deliberately not check_*), and it must never be cited as evidence that
something PASSED — only `check_scratch_install_vm.py` decides that, against a machine it built
itself. Use this to find out WHY, then let the gate say whether.

A command prefixed with `!` runs on the live medium instead of inside the target chroot, which is how
you inspect the installer's own environment rather than the installed system's.

Exit 0 if every command ran (whatever they returned), 1 if the guest could not be reached or the
disk could not be opened, 2 if this host cannot run a VM at all.
"""
from __future__ import annotations

import argparse
import http.server
import importlib.util
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time


HERE = Path(__file__).resolve().parent
# ONE implementation of the console protocol, the OVMF search and the ISO label, imported from the
# gate rather than copied. A second copy of "how to talk to the guest" is a second copy to keep in
# step, and the bugs we hit in that code (bracketed-paste framing, the echoed command matching its
# own marker) would have had to be found twice.
_SPEC = importlib.util.spec_from_file_location(
    "check_scratch_install_vm", HERE / "check_scratch_install_vm.py")
_GATE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_GATE)


class Serve(http.server.BaseHTTPRequestHandler):
    """Hands the os/ tree to the guest and takes command output back. Loopback only."""

    root: Path = Path("/")
    out: Path = Path("/")

    def log_message(self, *a):
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
        size = int(self.headers.get("Content-Length") or 0)
        Path(self.out, Path(self.path).name).write_bytes(self.rfile.read(size))
        self.send_response(204)
        self.end_headers()


def run_command(con, index, cmd, port, out):
    """Run one command, wait for a unique end marker, bring its output back by PUT.

    The marker carries the index so the console's echo of the command cannot be mistaken for its
    answer -- the same rule the gate learned when its tool probe read `NEED-$t` off its own echo.
    """
    inner = (f"/bin/bash -lc {shell_quote(cmd[1:])}" if cmd.startswith("!")
             else f"chroot /tmp/install /bin/bash -lc {shell_quote(cmd)}")
    con.send(f"{inner} >/tmp/vs{index}.log 2>&1; echo VSDONE{index}=$?")
    end = con.expect(rf"VSDONE{index}=\d+", 7200)
    rc = re.findall(rf"VSDONE{index}=(\d+)", con.buf)[-1] if end is not None else None
    con.send(f"wget -q --method=PUT --body-file=/tmp/vs{index}.log "
             f"http://10.0.2.2:{port}/cmd{index}.log -O /dev/null; echo VSPUT{index}")
    con.expect(rf"VSPUT{index}", 300)
    return rc, Path(out, f"cmd{index}.log")


def shell_quote(s):
    return "'" + s.replace("'", "'\"'\"'") + "'"


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("commands", nargs="*", help="run in the installed root; '!' prefix = live medium")
    ap.add_argument("--disk", default=os.environ.get("PC_INSTALL_DISK",
                                                    "/var/tmp/pcvm/scratch.qcow2"))
    ap.add_argument("--iso", default=os.environ.get("PC_GENTOO_ISO",
                                                   "/var/tmp/pcvm/gentoo-minimal.iso"))
    ap.add_argument("--password", default=os.environ.get("PC_INSTALL_PASSWORD", "pc-vm-test-only"))
    ap.add_argument("--out-dir", default="")
    # A SECOND DISK, BECAUSE SOME ANSWERS ARE TOO BIG FOR THE CONSOLE. Command output comes back
    # through a PUT over slirp, which is right for a log and wrong for a two-gigabyte ISO. A raw
    # image attached here is mkfs'd and mounted by the caller's own commands, and the host then
    # loop-mounts the same file directly -- no qemu-nbd, no format conversion. Raw, not qcow2, for
    # exactly that reason.
    ap.add_argument("--extra-drive", default="", help="raw image attached as a second virtio disk")
    ap.add_argument("--memory", type=int, default=8192)
    ap.add_argument("--cpus", type=int, default=min(8, os.cpu_count() or 4))
    args = ap.parse_args()

    if not args.commands:
        print("nothing to ask; give one or more commands")
        return 2
    for tool in ("qemu-system-x86_64", "bsdtar"):
        if not shutil.which(tool):
            print(f"SKIP  {tool} is not installed on this host")
            return 2
    if not Path("/dev/kvm").exists():
        print("SKIP  no /dev/kvm on this host")
        return 2
    code, vars_src = _GATE.ovmf()
    if not code:
        print("SKIP  no OVMF firmware on this host")
        return 2
    for path in (args.disk, args.iso):
        if not Path(path).is_file():
            print(f"SKIP  no such file: {path}")
            return 2

    out = Path(args.out_dir or tempfile.mkdtemp(prefix="pc-vmshell-"))
    out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="pc-vmshell-") as td:
        boot = Path(td, "boot")
        boot.mkdir()
        subprocess.run(["bsdtar", "-xf", args.iso, "-C", str(boot), "--strip-components=1",
                        "boot/gentoo", "boot/gentoo.igz"], check=True)
        subprocess.run(["tar", "czf", str(Path(td, "pcos.tar.gz")), "-C", str(HERE.parent / "os"),
                        "."], check=True)
        Serve.root, Serve.out = Path(td), out
        httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Serve)
        port = httpd.server_address[1]
        threading.Thread(target=httpd.serve_forever, daemon=True).start()

        vars_copy = Path(td, "OVMF_VARS.fd")
        shutil.copyfile(vars_src, vars_copy)
        sock = Path(td, "console.sock")
        append = (f"root=live:CDLABEL={_GATE.iso_label(Path(args.iso))} rd.live.dir=/ "
                  "rd.live.squashimg=image.squashfs cdroot console=ttyS0,115200n8")
        qargs = list(_GATE.qemu_args(args.disk, args.iso, boot / "gentoo", boot / "gentoo.igz",
                                     append, sock, code, vars_copy, args.memory, args.cpus, port))
        if args.extra_drive:
            qargs += ["-drive", f"file={args.extra_drive},if=virtio,format=raw"]
        proc = subprocess.Popen(qargs, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        try:
            for _ in range(200):
                if sock.exists():
                    break
                time.sleep(0.1)
            log = open(Path(out, "console.log"), "w", encoding="utf-8")
            con = _GATE.Serial(sock, log)
            if con.expect(r"root@livecd", 600) is None:
                print("FAIL  the live medium never reached a shell")
                return 1
            con.send(f"wget -q -O /tmp/p.tgz http://10.0.2.2:{port}/pcos.tar.gz "
                     "&& mkdir -p /usr/local/share/posterchanos "
                     "&& tar xzf /tmp/p.tgz -C /usr/local/share/posterchanos; echo FETCH-$?")
            if con.expect(r"FETCH-0\b", 300) is None:
                print("FAIL  could not hand the os/ tree to the guest")
                return 1
            con.send("printf 'vda\\ngentoo\\nnone\\n' >/tmp/disk; echo HOOK-$?")
            con.expect(r"HOOK-0\b", 60)
            # `gentoo.sh mount` exists for exactly this: open the encrypted root of an install that
            # is already finished (or half-finished) without re-deriving the cryptsetup and subvolume
            # sequence by hand. It prompts for the passphrase, so PC_INSTALL_PASSWORD answers it.
            con.send(f"PC_INSTALL_PASSWORD={args.password} bash "
                     "/usr/local/share/posterchanos/gentoo.sh mount >/tmp/mount.log 2>&1; "
                     "echo MOUNT-$?")
            if con.expect(r"MOUNT-0\b", 600) is None:
                con.send("tail -n 20 /tmp/mount.log")
                con.read(5)
                print("FAIL  could not open the installed root — wrong passphrase, or the disk holds "
                      f"no PosterChanOS install. Console transcript in {out}/console.log")
                return 1

            for i, cmd in enumerate(args.commands):
                rc, path = run_command(con, i, cmd, port, out)
                print(f"[{i}] rc={rc}  {cmd}\n     -> {path}", flush=True)
            con.send("poweroff")
            try:
                proc.wait(timeout=180)
            except subprocess.TimeoutExpired:
                proc.terminate()
        finally:
            httpd.shutdown()
            if proc.poll() is None:
                proc.terminate()
    return 0


if __name__ == "__main__":
    sys.exit(main())
