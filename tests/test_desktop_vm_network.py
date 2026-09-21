"""This computer's VMs (desktop/vm.js, qemu:///session) can be put on a HOST BRIDGE, and moved between the
default user-mode NAT and a bridge — RUN under node against a fake `virsh` that stores what was defined.

What it pins:
  * create with `network: {type:'bridge', name}` defines `<interface type="bridge"><source bridge=…>`;
    the default stays user-mode NAT (no root needed);
  * a bridge name is checked against sysfs (PC_SYS_NET) — a name the host has no bridge by, or one shaped
    like markup, is refused with a sentence that says where bridges are made, and NOTHING is defined;
  * setNetwork retargets the FIRST adapter, keeps its MAC, refuses a running VM, and reads the result back;
  * details() reports `nic: {type, source}`.
"""
import json
import os
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

FAKE_VIRSH = r'''#!/bin/bash
# a one-domain libvirt: the definition lives in $FAKE/domain.xml, the state in $FAKE/state
shift 2   # --connect URI
verb=$1; shift
echo "$verb $*" >> "$FAKE/calls"
case $verb in
  version) echo "Compiled against library: libvirt 12.0.0";;
  dominfo)
    [ -f "$FAKE/domain.xml" ] || { echo "error: failed to get domain '$1'" >&2; exit 1; }
    st=$(cat "$FAKE/state" 2>/dev/null || echo "shut off")
    printf 'Name:           %s\nState:          %s\nCPU(s):         2\nMax memory:     4194304 KiB\nAutostart:      disable\n' "$1" "$st";;
  domblklist) printf ' Type   Device   Target   Source\n------------------------------------------------\n file   disk     vda      /x/disk.qcow2\n';;
  dumpxml) cat "$FAKE/domain.xml";;
  define) cp "$1" "$FAKE/domain.xml";;
  start) echo running > "$FAKE/state";;
  list) [ -f "$FAKE/domain.xml" ] && echo vm1;;
  *) echo "unsupported $verb" >&2; exit 1;;
esac
'''

DOMAIN = '''<domain type='kvm'><name>vm1</name><devices>
  <disk type='file' device='disk'><source file='/x/disk.qcow2'/><target dev='vda' bus='virtio'/></disk>
  <interface type='user'><mac address='52:54:00:12:34:56'/><model type='e1000e'/><address type='pci'/></interface>
  <interface type='user'><mac address='52:54:00:99:99:99'/><model type='e1000e'/></interface>
</devices></domain>'''


@unittest.skipUnless(shutil.which("node"), "node not installed")
class LocalVmNetwork(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = Path(tempfile.mkdtemp(prefix="pc-vmnet-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        (self.tmp / "bin").mkdir()
        for name, body in (("virsh", FAKE_VIRSH), ("qemu-img", "#!/bin/bash\ntouch \"${@: -2:1}\"\n")):
            p = self.tmp / "bin" / name
            p.write_text(body)
            p.chmod(0o755)
        self.fake = self.tmp / "libvirt"
        self.fake.mkdir()
        sysnet = self.tmp / "sys"
        (sysnet / "br0" / "bridge").mkdir(parents=True)
        (sysnet / "enp1s0").mkdir(parents=True)
        (self.tmp / "home").mkdir()
        self.iso = self.tmp / "installer.iso"
        self.iso.write_bytes(b"iso")
        self.env = dict(os.environ, PATH=str(self.tmp / "bin") + ":" + os.environ["PATH"], FAKE=str(self.fake),
                        HOME=str(self.tmp / "home"), PC_SYS_NET=str(sysnet))

    def js(self, body):
        out = subprocess.run(["node", "-e", "const v=require('./desktop/vm');(async()=>{const r=await (async()=>{" + body +
                              "})();console.log(JSON.stringify(r));})().catch(e=>{console.log(JSON.stringify({thrown:String(e)}));});"],
                             cwd=ROOT, env=self.env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        return json.loads(out.stdout.strip().splitlines()[-1])

    def defined(self):
        p = self.fake / "domain.xml"
        return p.read_text() if p.exists() else ""

    def test_create_on_a_bridge(self):
        r = self.js("return v.create({name:'vm1',iso:%s,network:{type:'bridge',name:'br0'}})" % json.dumps(str(self.iso)))
        self.assertTrue(r.get("ok"), r)
        self.assertIn('<interface type="bridge"><source bridge="br0"/><model type="e1000e"/></interface>', self.defined())

    def test_create_defaults_to_user_mode_nat(self):
        r = self.js("return v.create({name:'vm1',iso:%s})" % json.dumps(str(self.iso)))
        self.assertTrue(r.get("ok"), r)
        self.assertIn('<interface type="user"><model type="e1000e"/></interface>', self.defined())

    def test_create_refuses_a_bridge_this_computer_does_not_have(self):
        for name in ("br9", "enp1s0", 'br0"/><x', "../br0"):
            r = self.js("return v.create({name:'vm1',iso:%s,network:{type:'bridge',name:%s}})"
                        % (json.dumps(str(self.iso)), json.dumps(name)))
            self.assertFalse(r.get("ok"), (name, r))
            self.assertIn("System Settings", r["error"])
            self.assertEqual(self.defined(), "", "nothing was defined")

    def test_set_network_moves_the_first_adapter_and_keeps_its_mac(self):
        (self.fake / "domain.xml").write_text(DOMAIN)
        r = self.js("return v.setNetwork('vm1',{type:'bridge',name:'br0'})")
        self.assertTrue(r.get("ok"), r)
        self.assertEqual(r["nic"], {"type": "bridge", "source": "br0"})
        x = self.defined()
        self.assertIn('<interface type="bridge"><mac address="52:54:00:12:34:56"/><source bridge="br0"/>', x)
        self.assertIn("52:54:00:99:99:99", x, "the second adapter is untouched")
        self.assertEqual(x.count("<interface"), 2)
        back = self.js("return v.setNetwork('vm1',{type:'user'})")
        self.assertEqual(back["nic"], {"type": "user", "source": ""})
        self.assertIn('<interface type="user"><mac address="52:54:00:12:34:56"/>', self.defined())

    def test_set_network_refuses_a_running_vm_and_an_unknown_bridge(self):
        (self.fake / "domain.xml").write_text(DOMAIN)
        self.assertFalse(self.js("return v.setNetwork('vm1',{type:'bridge',name:'nope'})").get("ok"))
        self.assertEqual(self.defined(), DOMAIN)
        (self.fake / "state").write_text("running")
        r = self.js("return v.setNetwork('vm1',{type:'bridge',name:'br0'})")
        self.assertFalse(r.get("ok"))
        self.assertIn("Shut down", r["error"])
        self.assertEqual(self.defined(), DOMAIN)

    def test_details_reports_the_primary_adapter(self):
        (self.fake / "domain.xml").write_text(DOMAIN.replace("<interface type='user'>", "<interface type='bridge'><source bridge='br0'/>", 1))
        self.assertEqual(self.js("return v.details('vm1')")["nic"], {"type": "bridge", "source": "br0"})

    def test_the_bridge_reaches_the_page_through_preload(self):
        pre = (ROOT / "desktop" / "preload.js").read_text()
        main = (ROOT / "desktop" / "main.js").read_text()
        self.assertIn("pc:vm:set-network", pre)
        self.assertIn("ipcMain.handle('pc:vm:set-network'", main)
