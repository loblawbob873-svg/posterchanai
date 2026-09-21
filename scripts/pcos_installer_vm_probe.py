#!/usr/bin/env python3
"""Install a live ISO THROUGH THE GRAPHICAL INSTALLER'S BRIDGE, in a throwaway VM, then boot the disk.

    sudo venv-unified/bin/python scripts/pcos_installer_vm_probe.py posterchan-live-YYYYMMDD.iso \
        --evidence-dir /var/tmp/pcprobe-installer

`check_livecd_install_vm.py` proves `gentoo.sh install-live` works when a person TYPES the answers.
The graphical installer does not type: desktop/installer.js starts the same script with the answers
in its environment (PC_INSTALL_DISK, PC_ASSUME_YES, PC_INSTALL_PASSWORD_FILE, …) under a detached
supervisor, and reads its progress back from `::pc-install::` lines. This drives THAT path end to
end — the bridge's own `info()`, `disks()`, `start()` and `status()`, i.e. exactly what the wizard's
buttons call through preload.js/main.js — on a real live boot, then boots the installed disk with no
ISO and the NVRAM the install wrote.

WITH --inject (the default) it serves this checkout's os/gentoo.sh, desktop/installer.js and
desktop/liveusb-runner.js into the live session first, so an ISO built BEFORE the installer existed
can test the code under review. That is a probe of this checkout on that image's userland, not a
gate on the image: an ISO is only proven by running this with `--no-inject` against an image that
already carries these files.

The QEMU process is named `pcprobe-installer-<pid>` and touches nothing but its own qcow2, a copy of
the OVMF variables and the ISO opened read-only. Not a check_* script on purpose: it needs root (the
installed disk is made audible over qemu-nbd, as the install gate does) and takes tens of minutes.

Exit 0 installed through the bridge AND the installed disk booted · 1 did not · 2 could not run.
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
ROOT = HERE.parent
_spec = importlib.util.spec_from_file_location("pc_install_gate", HERE / "check_livecd_install_vm.py")
gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gate)

# What the wizard does, as a script: the same four bridge calls, in the same order, with the same
# refusals left to the bridge. Prints one marker per fact so the console can be parsed, not guessed.
DRIVER = r"""
'use strict';
const m = require('./installer.js');
const [disk, password] = process.argv.slice(2);
const say = (k, v) => console.log('DRV ' + k + ' ' + JSON.stringify(v));
(async () => {
  const info = m.info(); say('info', info);
  if (!info.available) { say('result', { ok: false, why: 'not available' }); process.exit(3); }
  const disks = await m.disks();
  say('disks', disks.map(d => ({ name: d.name, selectable: d.selectable, why: d.why, size: d.size })));
  const d = disks.find(x => x.name === disk);
  if (!d || !d.selectable) { say('result', { ok: false, why: 'target not offered' }); process.exit(4); }
  const st0 = await m.start({ disk: d.name, size: d.size, rootName: 'gentoo', password, mode: 'fresh' });
  say('started', { running: st0.running, disk: st0.disk });
  let last = '';
  for (;;) {
    await new Promise(r => setTimeout(r, 5000));
    const st = m.status();
    const p = st.progress || {};
    const line = p.stage + ' ' + p.percent + (p.copied != null ? ' copied=' + p.copied : '');
    if (line !== last) { say('progress', { stage: p.stage, percent: p.percent, label: p.label, copied: p.copied }); last = line; }
    if (!st.running && !st.launching && st.finished) {
      say('tail', st.log.split('\n').filter(Boolean).slice(-25));
      say('result', { ok: st.ok, exitCode: st.exitCode, message: st.message, secretLeft: require('fs').existsSync(require('path').join(process.env.HOME, '.local/state/posterchan/installer-secret')) });
      process.exit(st.ok ? 0 : 1);
    }
  }
})().catch(e => { say('result', { ok: false, why: String(e && e.message || e) }); process.exit(5); });
"""


class Serve(http.server.BaseHTTPRequestHandler):
    files: dict = {}

    def do_GET(self):
        body = self.files.get(self.path.lstrip("/"))
        if body is None:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def qemu_args(disk, iso, sock, code, vars_copy, memory, cpus, usb):
    args = gate.qemu_args(disk, iso, sock, code, vars_copy, memory, cpus, usb)
    # A name that says what this is on a host that runs somebody's VMs, and the user-mode network
    # the files arrive over (10.0.2.2 is this host, as seen from the guest).
    return args + ["-name", f"pcprobe-installer-{os.getpid()}",
                   "-netdev", "user,id=n0", "-device", "virtio-net-pci,netdev=n0"]


def run(args):
    code, vars_src = gate.ovmf()
    if not code:
        print("SKIP  no OVMF firmware on this host")
        return 2
    evidence = Path(args.evidence_dir or tempfile.mkdtemp(prefix="pcprobe-installer-"))
    evidence.mkdir(parents=True, exist_ok=True)
    disk = evidence / "installed.qcow2"
    disk.unlink(missing_ok=True)
    subprocess.run(["qemu-img", "create", "-q", "-f", "qcow2", str(disk), args.size], check=True)
    vars_copy = evidence / "OVMF_VARS.fd"
    shutil.copyfile(vars_src, vars_copy)

    Serve.files = {"drive.js": DRIVER.encode()}
    if args.inject:
        Serve.files.update({
            "gentoo.sh": (ROOT / "os/gentoo.sh").read_bytes(),
            "installer.js": (ROOT / "desktop/installer.js").read_bytes(),
            "liveusb-runner.js": (ROOT / "desktop/liveusb-runner.js").read_bytes(),
        })
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Serve)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]

    with tempfile.TemporaryDirectory(prefix="pcprobe-sock-") as td:
        sock = Path(td, "console.sock")
        log = open(evidence / "install-console.log", "w", encoding="utf-8")
        proc = subprocess.Popen(qemu_args(disk, args.iso, sock, code, vars_copy, args.memory, args.cpus, args.usb),
                                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        try:
            for _ in range(100):
                if sock.exists() or proc.poll() is not None:
                    break
                time.sleep(0.1)
            if proc.poll() is not None:
                print("FAIL  qemu exited: " + (proc.stderr.read() or "")[-400:])
                return 1
            con = gate.Serial(sock, log)
            at = con.expect(r"live@[-a-z0-9]+", args.boot_wait)
            if at is None:
                print("FAIL  the live image never reached a shell on the serial console")
                return 1
            print("OK  live session reached")
            con.send("stty cols 400; export TERM=dumb; mkdir -p /tmp/pcinst && cd /tmp/pcinst; echo CD-$?")
            if con.expect(r"CD-0", 30) is None:
                return 1
            # The desktop's own Electron, as node — the process the bridge really runs in.
            con.send("E=$(for c in /opt/posterchan/posterchan-desktop /opt/posterchan/posterchan "
                     "/opt/posterchan/*/posterchan-desktop; do [ -x \"$c\" ] && { echo $c; break; }; done); "
                     "A=$(ls -d /opt/posterchan/resources/app.asar 2>/dev/null); echo ELECTRON=[$E] ASAR=[$A]")
            m = con.expect(r"ELECTRON=\[[^\]\n]*\] ASAR", 30)
            electron = re.findall(r"ELECTRON=\[([^\]\n]*)\]", con.buf)[-1] if m else ""
            if not electron:
                print("FAIL  no Electron binary under /opt/posterchan in this image")
                return 1
            print(f"OK  the bridge runs in the image's own Electron: {electron}")
            names = ["drive.js"] + (["installer.js", "liveusb-runner.js", "gentoo.sh"] if args.inject else [])
            con.send("rc=0; for f in " + " ".join(names) + f"; do wget -q -O $f http://10.0.2.2:{port}/$f || rc=1; done; echo GET-$rc")
            if con.expect(r"GET-0", 120) is None:
                print("FAIL  could not fetch the files from the host")
                return 1
            if args.inject:
                con.send("sudo -n install -m 0755 /tmp/pcinst/gentoo.sh /usr/bin/gentoo.sh && echo INJ-$?")
                if con.expect(r"INJ-0", 30) is None:
                    print("FAIL  could not place the checkout's gentoo.sh")
                    return 1
            else:
                con.send("A=/opt/posterchan/resources/app.asar; E2=$(dirname $E); "
                         "ELECTRON_RUN_AS_NODE=1 $E -e \"const a=require('path').join('$A');"
                         "for(const f of ['installer.js','liveusb-runner.js'])require('fs').writeFileSync(f,require('fs').readFileSync(a+'/'+f))\" "
                         "&& echo ASAR-$?")
                if con.expect(r"ASAR-0", 60) is None:
                    print("FAIL  this image carries no desktop/installer.js — use --inject")
                    return 1
            print("OK  installer files in place; driving the bridge (info → disks → start → status)")
            con.send(f"ELECTRON_RUN_AS_NODE=1 $E /tmp/pcinst/drive.js {args.target} '{args.password}' 2>&1; echo DRIVE-EXIT-$?")
            end = con.expect(r"DRIVE-EXIT-(\d+)", args.timeout)
            if end is None:
                print(f"FAIL  the install did not finish within {args.timeout}s; transcript {evidence}/install-console.log")
                return 1
            for line in re.findall(r"DRV (info|disks|started|progress|result) (.*)", con.buf):
                print("   ", line[0], line[1][:300])
            rc = re.findall(r"DRIVE-EXIT-(\d+)", con.buf)[-1]
            if rc != "0":
                print(f"FAIL  the bridge reported a failed install (exit {rc}); transcript {evidence}/install-console.log")
                return 1
            if '"secretLeft":true' in con.buf:
                print("FAIL  the password file outlived the install")
                return 1
            con.send("sudo efibootmgr 2>&1 | grep -cE '^Boot[0-9A-Fa-f]+\\*?[[:space:]]+PosterChanOS([[:space:]]|$)' | sed 's/^/NVRAM-COUNT=/'")
            if con.expect(r"NVRAM-COUNT=\d+", 60) is None or re.findall(r"NVRAM-COUNT=(\d+)", con.buf)[-1] == "0":
                print("FAIL  no PosterChanOS EFI boot entry after the install")
                return 1
            print("OK  installed through the bridge; EFI boot entry registered")
            con.send("sudo poweroff")
            try:
                proc.wait(timeout=120)
            except subprocess.TimeoutExpired:
                proc.terminate()
        finally:
            log.close()
            srv.shutdown()
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    proc.kill()
        rc = gate.boot_installed(disk, td, vars_copy, evidence, args.boot_timeout, args.memory, args.cpus)
    if not args.keep_disk:
        disk.unlink(missing_ok=True)
    return rc


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("iso")
    ap.add_argument("--evidence-dir", default="")
    ap.add_argument("--size", default="40G")
    ap.add_argument("--memory", type=int, default=6144)
    ap.add_argument("--cpus", type=int, default=4)
    ap.add_argument("--timeout", type=int, default=3600)
    ap.add_argument("--boot-wait", type=int, default=600)
    ap.add_argument("--boot-timeout", type=int, default=600)
    ap.add_argument("--target", default="vda")
    ap.add_argument("--password", default="pc-vm-test-only")
    ap.add_argument("--usb", action="store_true")
    ap.add_argument("--keep-disk", action="store_true")
    ap.add_argument("--no-inject", dest="inject", action="store_false")
    args = ap.parse_args()
    if not Path(args.iso).is_file():
        print("SKIP  no such ISO")
        return 2
    for tool in ("qemu-system-x86_64", "qemu-img"):
        if not shutil.which(tool):
            print(f"SKIP  {tool} is not installed")
            return 2
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
