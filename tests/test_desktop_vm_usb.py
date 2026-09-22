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
                        PC_DEV_USB=str(self.dev), PC_QEMU_BIN=str(self.qemu), PC_ZPOOL_STATUS="",
                        PC_SWAPS=str(self.tmp / "swaps"), PC_USB_GRANT_BIN=str(self.tmp / "no-helper"))
        (self.tmp / "swaps").write_text("Filename Type Size Used Priority\n")

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
        self.assertIn("pc-usb-grant", ids["046d:c31c"]["fix"])
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
        self.assertIn("pc-usb-grant", r["error"])
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


def test_posterchanos_grants_one_device_at_a_time_never_a_blanket_rule():
    """Review, HIGH: tagging every non-hub USB device `uaccess` gave the seat raw usbfs on every USB disk (driver
    disconnect + raw SCSI) — root in all but name. PosterChanOS now installs NO usb udev rule; it ships
    pc-usb-grant with a sudoers line for exactly `grant *` / `revoke *`, and the scanner the host uses."""
    text = (ROOT / "os" / "gentoo.sh").read_text()
    assert '"app-emulation/qemu spice usbredir pipewire virgl usb"' in text
    assert "70-posterchan-usb-passthrough.rules" not in text
    assert not any('TAG+="uaccess"' in ln and "usb" in ln for ln in text.splitlines()), "a blanket usb uaccess rule"
    assert ('"%posterchan ALL=(root) NOPASSWD: /usr/local/bin/pc-usb-grant grant *, '
            '/usr/local/bin/pc-usb-grant revoke *"') in text
    assert "pc-usb-grant update-posterchan; do" in text, "the helper is not in the installed-helpers loop"
    assert '"$PCOS_TREE/../app/services/vmhost/usb.py" "${TARGET}/usr/local/lib/posterchan/pc_usb_scan.py"' in text
    assert os.access(ROOT / "os" / "bin" / "pc-usb-grant", os.X_OK)


def test_the_preload_bridge_carries_only_ids():
    pre = (ROOT / "desktop" / "preload.js").read_text()
    for name in ("usbList", "usbDevices", "usbAttach", "usbDetach"):
        assert f"    {name}: " in pre
    main = (ROOT / "desktop" / "main.js").read_text()
    for ch in ("pc:vm:usb-list", "pc:vm:usb-devices", "pc:vm:usb-attach", "pc:vm:usb-detach"):
        assert f"ipcMain.handle('{ch}', (e" in main and "fsGuard(e)" in main.split(ch, 1)[1].split("\n", 1)[0]


class LocalVmUsbGrantAndDisks(LocalVmUsb):
    """The per-device grant through the helper, and the desktop's own root-disk / swap / NIC checks."""

    def helper(self, ok=True):
        log = self.tmp / "helper.log"
        h = self.tmp / "pc-usb-grant"
        h.write_text("#!/bin/sh\necho \"$*\" >> %s\n%s\n" % (log, (
            'b=$(printf %03d "$2"); d=$(printf %03d "$3"); [ "$1" = grant ] && chmod 0666 "$PC_DEV_USB/$b/$d"; exit 0'
            if ok else 'echo "pc-usb-grant: refused" >&2; exit 1')))
        h.chmod(0o755)
        s = self.tmp / "bin" / "sudo"
        s.write_text('#!/bin/sh\n[ "$1" = -n ] || exit 9\nshift\nexec "$@"\n')
        s.chmod(0o755)
        self.env["PC_USB_GRANT_BIN"] = str(h)
        return log

    def test_a_node_the_account_cannot_open_is_granted_for_that_one_device(self):
        (self.dev / "001" / "003").chmod(0o444)
        log = self.helper()
        r = self.js("return v.usbAttach('vm1',{vendor:'090c',product:'1000'})")
        self.assertTrue(r["ok"], r)
        self.assertEqual(log.read_text().splitlines(), ["grant 1 3"])
        r = self.js("return v.usbDetach('vm1',{vendor:'090c',product:'1000'})")
        self.assertTrue(r["ok"], r)
        self.assertEqual(log.read_text().splitlines(), ["grant 1 3", "revoke 1 3"])

    def test_a_refused_grant_attaches_nothing(self):
        (self.dev / "001" / "003").chmod(0o444)
        self.helper(ok=False)
        r = self.js("return v.usbAttach('vm1',{vendor:'090c',product:'1000'})")
        self.assertEqual(r.get("code"), "forbidden")
        self.assertIn("refused", r["error"])
        self.assertFalse([c for c in self.calls() if c.startswith("attach-device")])

    def set_mountinfo(self, text):
        Path(self.mountinfo).write_text(text)

    def test_dev_root_through_major_minor_hides_the_disk(self):
        blk = os.path.realpath(os.path.join(self.sys, "class/block/sde1"))
        os.makedirs(os.path.join(self.sys, "dev/block"), exist_ok=True)
        os.symlink(blk, os.path.join(self.sys, "dev/block/8:65"))
        self.set_mountinfo("22 1 8:65 / / rw - ext4 /dev/root rw\n")
        ids = [d["vendor"] for d in self.js("return v.usbList()")["devices"]]
        self.assertNotIn("090c", ids, "the stick the host runs from was offered")

    def test_an_untraceable_root_refuses_every_disk(self):
        self.set_mountinfo("22 1 0:40 / / rw - zfs rpool/ROOT rw\n")
        devs = {d["vendor"]: d for d in self.js("return v.usbList()")["devices"]}
        self.assertIn("could not tell", devs["090c"]["busy"])
        self.assertEqual(devs["046d"]["busy"], "", "a keyboard holds no disk")

    def test_swap_and_an_up_nic_are_busy(self):
        (self.tmp / "swaps").write_text("Filename Type Size Used Priority\n/dev/sde1 partition 8G 0 -2\n")
        kbd = os.path.realpath(os.path.join(self.sys, "bus/usb/devices/1-14"))
        nr = os.path.join(kbd, "1-14:1.0", "net", "usb0")
        os.makedirs(nr)
        Path(nr, "operstate").write_text("up\n")
        os.makedirs(os.path.join(self.sys, "class/net"), exist_ok=True)
        os.symlink(nr, os.path.join(self.sys, "class/net/usb0"))
        devs = {d["vendor"]: d for d in self.js("return v.usbList()")["devices"]}
        self.assertIn("swap", devs["090c"]["busy"])
        self.assertIn("usb0", devs["046d"]["busy"])

    def test_pinned_twins_are_two_rows(self):
        two = ('<hostdev mode="subsystem" type="usb"><source><vendor id="0x090c"/><product id="0x1000"/>'
               '<address bus="1" device="%d"/></source></hostdev>')
        for f in ("saved.xml", "live.xml"):
            (self.fake / f).write_text(DOMAIN.replace("</devices>", two % 3 + two % 9 + "</devices>"))
        r = self.js("return v.usbDevices('vm1')")
        self.assertEqual(sorted((d["bus"], d["device"]) for d in r["devices"]), [(1, 3), (1, 9)])


def _grant(tmp, *args, uid="1000", env=None):
    import sys as _sys
    e = dict(os.environ, **(env or {}))
    if uid is None:
        e.pop("SUDO_UID", None)
    else:
        e["SUDO_UID"] = uid
    return subprocess.run([_sys.executable, "-I", str(ROOT / "os" / "bin" / "pc-usb-grant"), *args], env=e,
                          capture_output=True, text=True, timeout=30)


@unittest.skipIf(os.geteuid() == 0, "the helper ignores its test overrides as root")
class UsbGrantHelper(unittest.TestCase):
    """os/bin/pc-usb-grant — RUN (never as root) against a fake /sys and /dev with a stub setfacl."""

    def setUp(self):
        import tempfile
        self.tmp = Path(tempfile.mkdtemp(prefix="pc-usbgrant-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.sys, mountinfo, swaps, _ = build_usb(self.tmp)
        lib = self.tmp / "lib"
        lib.mkdir()
        shutil.copy(ROOT / "app" / "services" / "vmhost" / "usb.py", lib / "pc_usb_scan.py")
        self.dev = self.tmp / "dev"
        for d in (1, 3, 4, 5, 6, 7):
            n = self.dev / "001" / f"{d:03d}"
            n.parent.mkdir(parents=True, exist_ok=True)
            n.write_text("")
        self.log = self.tmp / "setfacl.log"
        sf = self.tmp / "setfacl"
        sf.write_text("#!/bin/sh\necho \"$*\" >> %s\n" % self.log)
        sf.chmod(0o755)
        self.env = {"PC_USB_GRANT_LIB": str(lib), "PC_USB_GRANT_SYS": self.sys, "PC_USB_GRANT_DEV": str(self.dev),
                    "PC_USB_GRANT_MOUNTINFO": mountinfo, "PC_USB_GRANT_SWAPS": swaps, "PC_USB_GRANT_SETFACL": str(sf)}

    def acl(self):
        return self.log.read_text().splitlines() if self.log.exists() else []

    def test_grants_the_callers_uid_on_that_one_node(self):
        r = _grant(self.tmp, "grant", "1", "3", env=self.env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.acl(), [f"-m u:1000:rw {self.dev}/001/003"])
        r = _grant(self.tmp, "revoke", "1", "3", env=self.env)
        self.assertEqual(self.acl()[-1], f"-x u:1000 {self.dev}/001/003")

    def test_refuses_hubs_host_disks_and_bad_arguments(self):
        cases = [(("grant", "1", "5"), "hub"),                    # the hub
                 (("grant", "1", "6"), "runs from"),              # the boot disk
                 (("grant", "1", "4"), "/raid"),                  # the RAID member
                 (("grant", "1", "1"), "root hub"),               # the root hub
                 (("grant", "1", "3;id"), "usage"), (("grant", "1"), "usage"), (("chmod", "1", "3"), "usage"),
                 (("grant", "0", "3"), "1 to 999"), (("grant", "1", "99"), "no USB device")]
        for args, why in cases:
            r = _grant(self.tmp, *args, env=self.env)
            self.assertNotEqual(r.returncode, 0, args)
            self.assertIn(why, r.stderr, (args, r.stderr))
        r = _grant(self.tmp, "grant", "1", "3", uid=None, env=self.env)
        self.assertIn("through sudo", r.stderr)
        r = _grant(self.tmp, "grant", "1", "3", uid="0", env=self.env)
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(self.acl(), [], "setfacl ran for a refused request")
