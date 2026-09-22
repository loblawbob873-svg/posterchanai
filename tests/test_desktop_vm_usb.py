"""USB passthrough on "This computer" (desktop/vmusb.js over qemu:///session) — RUN under node against a fake sysfs
tree, fake /dev/bus/usb nodes and a stateful fake `virsh` that keeps a saved and a live definition.

What it pins (each verified to fail without its rule):
  * hubs, root hubs and the disk this computer boots from are never listed; a RAID member under a mounted volume
    is listed BUSY and refused;
  * a SESSION QEMU opens the device as the user: a node the account cannot read-write is refused BEFORE virsh,
    naming the udev rule that fixes it (the one PosterChanOS installs);
  * a QEMU without `usb-host` (Gentoo USE=-usb) is refused before virsh, naming the USE flag;
  * attach to a RUNNING VM is `--live --config` (or `--live` alone with persist:false), a stopped VM `--config`,
    and both are READ BACK; detach mirrors it; ids are validated (no XML from the page);
  * PosterChanOS installs the uaccess rule, and builds QEMU with USE=usb.
"""
import json
import os
import shutil

import subprocess
import unittest
from pathlib import Path

from tests.test_vmhost_device_scan import build_usb

ROOT = Path(__file__).resolve().parents[1]

FAKE_VIRSH = r'''#!/usr/bin/env python3
import os, re, sys
F = os.environ["FAKE"]
a = sys.argv[3:]
open(os.path.join(F, "calls"), "a").write(" ".join(a) + "\n")
st = open(os.path.join(F, "state")).read().strip() if os.path.exists(os.path.join(F, "state")) else "shut off"
saved = os.path.join(F, "saved.xml"); live = os.path.join(F, "live.xml")
def rd(p): return open(p).read()
def wr(p, s): open(p, "w").write(s)
verb = a[0]
if verb == "list": print("vm1")
elif verb == "dominfo":
    if a[1] != "vm1": sys.stderr.write("error: failed to get domain\n"); sys.exit(1)
    print("Name: vm1\nState:          %s\nCPU(s): 2\nMax memory: 4194304 KiB\nAutostart: disable" % st)
elif verb == "dumpxml":
    print(rd(saved) if "--inactive" in a or st != "running" else rd(live))
elif verb in ("attach-device", "detach-device"):
    body = rd(a[2])
    if os.environ.get("FAKE_DROP"): sys.exit(0)
    for flag, p in (("--live", live), ("--config", saved)):
        if flag not in a: continue
        cur = rd(p)
        if verb == "attach-device":
            cur = cur.replace("</devices>", body + "</devices>", 1)
        else:
            vid = re.search(r"vendor id=\"(0x[0-9a-f]+)\"", body).group(1)
            blocks = [m.group(0) for m in re.finditer(r"<hostdev\b.*?</hostdev>", cur, re.S) if vid in m.group(0)]
            if not blocks: sys.stderr.write("error: device not found\n"); sys.exit(1)
            cur = cur.replace(blocks[0], "", 1)
        wr(p, cur)
else:
    sys.stderr.write("unsupported " + verb + "\n"); sys.exit(1)
'''
DOMAIN = "<domain type='kvm'><name>vm1</name><devices><disk type='file' device='disk'/></devices></domain>"


@unittest.skipUnless(shutil.which("node"), "node not installed")
class LocalVmUsb(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = Path(tempfile.mkdtemp(prefix="pc-vmusb-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.sys, self.mountinfo, _swaps, _ = build_usb(self.tmp)
        (self.tmp / "bin").mkdir()
        v = self.tmp / "bin" / "virsh"
        v.write_text(FAKE_VIRSH)
        v.chmod(0o755)
        self.qemu = self.tmp / "qemu"
        self.qemu_help('name "usb-host", bus usb-bus')
        self.fake = self.tmp / "libvirt"
        self.fake.mkdir()
        for f in ("saved.xml", "live.xml"):
            (self.fake / f).write_text(DOMAIN)
        (self.fake / "state").write_text("running")
        self.dev = self.tmp / "dev"
        for bus, d in ((1, 3), (1, 4), (1, 6), (1, 7), (1, 5)):
            n = self.dev / f"{bus:03d}" / f"{d:03d}"
            n.parent.mkdir(parents=True, exist_ok=True)
            n.write_text("")
            n.chmod(0o666)
        (self.tmp / "home").mkdir()
        self.env = dict(os.environ, PATH=str(self.tmp / "bin") + ":" + os.environ["PATH"], FAKE=str(self.fake),
                        HOME=str(self.tmp / "home"), PC_SYS_ROOT=self.sys, PC_MOUNTINFO=self.mountinfo,
                        PC_DEV_USB=str(self.dev), PC_QEMU_BIN=str(self.qemu))

    def qemu_help(self, line):
        self.qemu.write_text(f"#!/bin/sh\necho '{line}'\n")
        self.qemu.chmod(0o755)

    def js(self, body, **env):
        out = subprocess.run(["node", "-e", "const v=require('./desktop/vm');(async()=>{const r=await (async()=>{" + body +
                              "})();console.log(JSON.stringify(r));})().catch(e=>{console.log(JSON.stringify({thrown:String(e)}));});"],
                             cwd=ROOT, env=dict(self.env, **env), text=True, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        return json.loads(out.stdout.strip().splitlines()[-1])

    def calls(self):
        p = self.fake / "calls"
        return p.read_text().splitlines() if p.exists() else []

    def test_list_filters_and_reports_access(self):
        (self.dev / "001" / "007").chmod(0o444)                     # the keyboard node: not ours to open
        r = self.js("return v.usbList()")
        self.assertTrue(r["ok"], r)
        ids = {d["vendor"] + ":" + d["product"]: d for d in r["devices"]}
        self.assertEqual(sorted(ids), ["046d:c31c", "090c:1000", "174c:55aa"])   # no hub, no root hub, no boot disk
        self.assertIn("/raid", ids["174c:55aa"]["busy"])
        self.assertTrue(ids["090c:1000"]["access"])
        self.assertFalse(ids["046d:c31c"]["access"])
        self.assertIn("70-posterchan-usb-passthrough.rules", ids["046d:c31c"]["fix"])
        self.assertTrue(r["checks"][0]["ok"])

    def test_attach_to_a_running_vm_reads_back_then_detaches(self):
        r = self.js("return v.usbAttach('vm1',{vendor:'090c',product:'1000'})")
        self.assertTrue(r["ok"], r)
        att = [c for c in self.calls() if c.startswith("attach-device")]
        self.assertEqual(len(att), 1)
        self.assertTrue(att[0].endswith("--live --config"), att)
        self.assertIn('vendor id="0x090c"', (self.fake / "saved.xml").read_text())
        self.assertIn('startupPolicy="optional"', (self.fake / "saved.xml").read_text())
        self.assertEqual([(d["live"], d["persistent"]) for d in r["devices"]], [(True, True)])
        r = self.js("return v.usbList()")
        self.assertEqual(next(d for d in r["devices"] if d["vendor"] == "090c")["used_by"]["name"], "vm1")
        again = self.js("return v.usbAttach('vm1',{vendor:'090c',product:'1000'})")
        self.assertEqual(again.get("code"), "conflict")
        r = self.js("return v.usbDetach('vm1',{vendor:'090c',product:'1000'})")
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["devices"], [])
        self.assertNotIn("hostdev", (self.fake / "saved.xml").read_text() + (self.fake / "live.xml").read_text())

    def test_live_only_and_stopped(self):
        r = self.js("return v.usbAttach('vm1',{vendor:'090c',product:'1000',persist:false})")
        self.assertTrue(r["ok"], r)
        self.assertTrue([c for c in self.calls() if c.startswith("attach-device")][0].endswith("--live"))
        self.assertNotIn("hostdev", (self.fake / "saved.xml").read_text())
        self.js("return v.usbDetach('vm1',{vendor:'090c',product:'1000'})")
        (self.fake / "state").write_text("shut off")
        r = self.js("return v.usbAttach('vm1',{vendor:'090c',product:'1000',persist:false})")
        self.assertEqual(r.get("code"), "bad_request")
        r = self.js("return v.usbAttach('vm1',{vendor:'090c',product:'1000'})")
        self.assertTrue(r["ok"], r)
        self.assertTrue([c for c in self.calls() if c.startswith("attach-device")][-1].endswith("--config"))

    def test_an_attach_that_did_not_land_is_an_error(self):
        r = self.js("return v.usbAttach('vm1',{vendor:'090c',product:'1000'})", FAKE_DROP="1")
        self.assertFalse(r["ok"])
        self.assertIn("not in", r["error"])

    def test_refusals_happen_before_virsh(self):
        (self.dev / "001" / "003").chmod(0o444)
        r = self.js("return v.usbAttach('vm1',{vendor:'090c',product:'1000'})")
        self.assertEqual(r.get("code"), "forbidden")
        self.assertIn('TAG+="uaccess"', r["error"])
        r = self.js("return v.usbAttach('vm1',{vendor:'174c',product:'55aa'})")
        self.assertEqual(r.get("code"), "conflict")
        for bad in ("{vendor:'090C',product:'1000'}", "{vendor:'090c\\'/>',product:'1000'}",
                    "{vendor:'090c',product:'1000',bus:1}"):
            self.assertEqual(self.js(f"return v.usbAttach('vm1',{bad})").get("code"), "bad_request", bad)
        self.qemu_help('name "usb-redir", bus usb-bus')
        (self.dev / "001" / "003").chmod(0o666)
        r = self.js("return v.usbAttach('vm1',{vendor:'090c',product:'1000'})")
        self.assertEqual(r.get("code"), "unsupported")
        self.assertIn("USE=usb", r["error"])
        self.assertFalse([c for c in self.calls() if c.startswith("attach-device")])


def test_posterchanos_grants_the_seat_usb_and_builds_qemu_with_usb():
    text = (ROOT / "os" / "gentoo.sh").read_text()
    assert '"app-emulation/qemu spice usbredir pipewire virgl usb"' in text
    from_js = subprocess.run(["node", "-e", "process.stdout.write(require('./desktop/vmusb').RULE)"], cwd=ROOT,
                             capture_output=True, text=True).stdout
    assert from_js and from_js in text, "the rule vmusb.js tells people to install is the one PosterChanOS installs"
    assert "/etc/udev/rules.d/70-posterchan-usb-passthrough.rules" in text
    # it must sort BEFORE 73-seat-late.rules, which is what turns the uaccess tag into an ACL
    assert "70-posterchan-usb-passthrough.rules" < "73-seat-late.rules"
    assert "udevadm trigger --action=change --subsystem-match=usb" in text


def test_the_preload_bridge_carries_only_ids():
    pre = (ROOT / "desktop" / "preload.js").read_text()
    for name in ("usbList", "usbDevices", "usbAttach", "usbDetach"):
        assert f"    {name}: " in pre
    main = (ROOT / "desktop" / "main.js").read_text()
    for ch in ("pc:vm:usb-list", "pc:vm:usb-devices", "pc:vm:usb-attach", "pc:vm:usb-detach"):
        assert f"ipcMain.handle('{ch}', (e" in main and "fsGuard(e)" in main.split(ch, 1)[1].split("\n", 1)[0]
