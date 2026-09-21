"""Bridges for virtual machines (desktop/net.js), RUN against a stateful fake NetworkManager behind a fake sudo.

What it pins, each a way a real machine is left worse off:
  * every CHANGE goes through `sudo -n nmcli` — measured on the PosterChanOS machines, plain nmcli answers
    `auth` for settings.modify.system with no polkit agent to answer it, so an unprivileged add is refused;
    reads stay unprivileged;
  * the bridge copies the card's MAC (the router then hands it the address the machine already had), the
    card becomes its port, and the card's previous profile stops autoconnecting (or it steals the card back);
  * a bridge that gets NO address is ROLLED BACK: bridge and port deleted, the old profile switched back on
    and brought up — a half-made bridge is a machine with no network;
  * Wi-Fi cannot be enslaved, names are validated before anything runs, libvirt's virbr0 is listed but can
    never be deleted, and `allowVms` allows the bridge in qemu's bridge.conf and makes the helper setuid;
  * delete restores the remembered profile.
"""
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")

FAKE_NM = r'''#!/usr/bin/env node
const fs=require('fs'),F=process.env.FAKE+'/nm.json';
const S=JSON.parse(fs.readFileSync(F,'utf8'));const a=process.argv.slice(2);
fs.appendFileSync(process.env.FAKE+'/calls',(process.env.PC_AS_ROOT?'ROOT ':'USER ')+a.join(' ')+'\n');
const save=()=>fs.writeFileSync(F,JSON.stringify(S));
const esc=s=>String(s).replace(/:/g,'\\:');
const find=(how,x)=>S.conns.find(c=>how==='uuid'?c.uuid===x:c.name===x);
const j=a.join(' ');
const die=(m)=>{process.stderr.write('Error: '+m+'\n');process.exit(4);};
const priv=()=>{ if(!process.env.PC_AS_ROOT) die('Insufficient privileges.'); };
if(j==='-t -f DEVICE,TYPE,STATE,CONNECTION device status'){
  for(const d of S.devs)console.log([d.device,d.type,d.state,d.connection||'--'].map(esc).join(':'));process.exit(0);}
if(j==='-t -f NAME,UUID,TYPE,DEVICE,ACTIVE connection show'){
  for(const c of S.conns)console.log([c.name,c.uuid,c.type,c.device||'--',c.device?'yes':'no'].map(esc).join(':'));process.exit(0);}
if(a[0]==='-t'&&a[2]==='connection.uuid,connection.master,connection.slave-type'){
  for(const u of a.slice(5)){const c=find('uuid',u);if(!c)continue;
    console.log('connection.uuid:'+c.uuid);console.log('connection.master:'+(c.master||''));console.log('connection.slave-type:'+(c.slavetype||''));console.log('');}
  process.exit(0);}
if(a[0]==='-g'&&a[1]==='IP4.ADDRESS'){const d=S.devs.find(x=>x.device===a[4]);console.log(d&&d.ip?d.ip:'');process.exit(0);}
if(a[0]==='connection'&&a[1]==='add'){priv();
  const kv={};for(let i=2;i<a.length;i+=2)kv[a[i]]=a[i+1];
  const c={name:kv['con-name'],uuid:'u-'+kv['con-name'],type:kv.type==='bridge'?'bridge':'802-3-ethernet',device:'',
    master:kv.master||'',slavetype:kv['slave-type']||'',ifname:kv.ifname,kv};
  S.conns.push(c);save();process.exit(0);}
if(a[0]==='connection'&&a[1]==='modify'){priv();const c=find(a[2],a[3]);if(!c)die('no such connection');c.autoconnect=a[5];save();process.exit(0);}
if(a[0]==='connection'&&a[1]==='delete'){priv();const c=find(a[2],a[3]);if(!c)die('no such connection');
  S.conns=S.conns.filter(x=>x!==c);
  if(c.device){const d=S.devs.find(x=>x.device===c.device);if(d){if(c.type==='bridge')S.devs=S.devs.filter(x=>x!==d);else{d.connection='';d.state='disconnected';}}}
  save();process.exit(0);}
if(a[0]==='connection'&&a[1]==='up'){priv();const c=find(a[2],a[3]);if(!c)die('no such connection');
  if(c.slavetype==='bridge'){const br=find('id',c.master);
    for(const o of S.conns)if(o.device===c.ifname)o.device='';
    c.device=c.ifname;const d=S.devs.find(x=>x.device===c.ifname);d.connection=c.name;d.state='connected';
    br.device=br.ifname;if(!S.devs.find(x=>x.device===br.ifname))S.devs.push({device:br.ifname,type:'bridge',state:'connected',connection:br.name,ip:process.env.FAKE_BRIDGE_IP||''});
  }else{c.device=c.ifname||S.nicOf[c.uuid];const d=S.devs.find(x=>x.device===c.device);if(d){d.connection=c.name;d.state='connected';}}
  save();process.exit(0);}
die('unhandled: '+j);
'''

FAKE_SUDO = r'''#!/bin/bash
[ "$1" = -n ] || { echo "sudo called without -n" >&2; exit 9; }
shift
echo "$*" >> "$FAKE/sudo"
export PC_AS_ROOT=1
exec "$@"
'''

INITIAL = {
    "devs": [
        {"device": "enp37s0", "type": "ethernet", "state": "connected", "connection": "Wired connection 1", "ip": "192.168.0.102/24"},
        {"device": "wlp3s0", "type": "wifi", "state": "connected", "connection": "Tribble"},
        {"device": "virbr0", "type": "bridge", "state": "connected (externally)", "connection": "virbr0", "ip": "192.168.122.1/24"},
        {"device": "lo", "type": "loopback", "state": "connected (externally)", "connection": "lo"},
    ],
    "conns": [
        {"name": "Wired connection 1", "uuid": "wired-1", "type": "802-3-ethernet", "device": "enp37s0"},
        {"name": "Tribble", "uuid": "wifi-1", "type": "802-11-wireless", "device": "wlp3s0"},
        {"name": "virbr0", "uuid": "virbr-1", "type": "bridge", "device": "virbr0"},
    ],
    "nicOf": {"wired-1": "enp37s0"},
}


@unittest.skipUnless(NODE, "node not installed")
class Bridges(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="pc-br-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        b = self.tmp / "bin"
        b.mkdir()
        for name, body in (("nmcli", FAKE_NM), ("sudo", FAKE_SUDO)):
            (b / name).write_text(body)
            (b / name).chmod(0o755)
        self.fake = self.tmp / "fake"
        self.fake.mkdir()
        (self.fake / "nm.json").write_text(json.dumps(INITIAL))
        sysnet = self.tmp / "sys"
        (sysnet / "enp37s0").mkdir(parents=True)
        (sysnet / "enp37s0" / "address").write_text("04:7c:16:c9:86:21\n")
        self.conf = self.tmp / "bridge.conf"
        self.conf.write_text("# allow br0\n")
        self.helper = self.tmp / "qemu-bridge-helper"
        self.helper.write_text("#!/bin/sh\n")
        self.helper.chmod(0o755)
        self.env = dict(os.environ, PATH=str(b) + ":" + os.environ["PATH"], FAKE=str(self.fake),
                        PC_NMCLI=str(b / "nmcli"), PC_SUDO=str(b / "sudo"), PC_SYS_NET=str(sysnet),
                        PC_QEMU_BRIDGE_CONF=str(self.conf), PC_QEMU_BRIDGE_HELPER=str(self.helper),
                        PC_BRIDGE_STATE=str(self.tmp / "bridges.json"), PC_BRIDGE_WAIT_MS="600",
                        FAKE_BRIDGE_IP="192.168.0.102/24")

    def js(self, body, **env):
        out = subprocess.run([NODE, "-e", "const n=require('./desktop/net');(async()=>{const r=await (async()=>{" + body +
                              "})();console.log(JSON.stringify(r));})().catch(e=>console.log(JSON.stringify({thrown:String(e&&e.message||e)})));"],
                             cwd=ROOT, env=dict(self.env, **env), text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        return json.loads(out.stdout.strip().splitlines()[-1])

    def state(self):
        return json.loads((self.fake / "nm.json").read_text())

    def calls(self):
        p = self.fake / "calls"
        return p.read_text().splitlines() if p.exists() else []

    def test_lists_bridges_and_only_wired_cards(self):
        r = self.js("return n.bridges()")
        self.assertTrue(r["available"])
        self.assertEqual([(b["name"], b["managed"]) for b in r["bridges"]], [("virbr0", "libvirt")])
        self.assertEqual(r["bridges"][0]["ipv4"], ["192.168.122.1/24"])
        self.assertEqual([x["device"] for x in r["nics"]], ["enp37s0"], "Wi-Fi is never offered as a port")
        self.assertEqual(r["nics"][0]["mac"], "04:7c:16:c9:86:21")
        self.assertTrue(all(c.startswith("USER ") for c in self.calls()), "reading needs no privilege")

    def test_create_makes_the_bridge_through_sudo_and_keeps_the_mac(self):
        r = self.js("return n.createBridge({name:'br0',nic:'enp37s0',mode:'dhcp',allowVms:true})")
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["ipv4"], ["192.168.0.102/24"])
        st = self.state()
        br = next(c for c in st["conns"] if c["name"] == "br0")
        self.assertEqual(br["kv"]["bridge.mac-address"], "04:7c:16:c9:86:21")
        self.assertEqual(br["kv"]["bridge.stp"], "no")
        self.assertEqual(br["kv"]["ipv4.method"], "auto")
        port = next(c for c in st["conns"] if c.get("slavetype") == "bridge")
        self.assertEqual((port["master"], port["ifname"]), ("br0", "enp37s0"))
        wired = next(c for c in st["conns"] if c["uuid"] == "wired-1")
        self.assertEqual(wired.get("autoconnect"), "no", "the old profile must not steal the card back")
        for c in self.calls():
            if " add " in c or " modify " in c or " up " in c or " delete " in c:
                self.assertTrue(c.startswith("ROOT "), c)
        self.assertIn("allow br0", self.conf.read_text().splitlines())
        self.assertTrue(self.helper.stat().st_mode & 0o4000, "qemu-bridge-helper made setuid")
        self.assertTrue(r["vmReady"])
        self.assertEqual(json.loads((self.tmp / "bridges.json").read_text())["br0"]["previous"], "wired-1")
        listed = self.js("return n.bridges()")
        b = next(x for x in listed["bridges"] if x["name"] == "br0")
        self.assertEqual((b["managed"], b["ports"], b["vmReady"]), ("nm", ["enp37s0"], True))

    def test_a_bridge_with_no_address_is_rolled_back(self):
        r = self.js("return n.createBridge({name:'br0',nic:'enp37s0',mode:'dhcp'})", FAKE_BRIDGE_IP="")
        self.assertFalse(r["ok"])
        self.assertTrue(r["rolledBack"])
        st = self.state()
        self.assertFalse([c for c in st["conns"] if c["name"].startswith("br0")], "nothing half-made is left")
        wired = next(c for c in st["conns"] if c["uuid"] == "wired-1")
        self.assertEqual(wired.get("autoconnect"), "yes")
        self.assertEqual(wired["device"], "enp37s0", "the previous connection was brought back up")
        self.assertIn("ROOT connection up uuid wired-1", self.calls())
        self.assertNotIn("allow br0", self.conf.read_text().splitlines(), "a failed bridge is not allowed to VMs")

    def test_static_addressing_is_validated_and_passed(self):
        r = self.js("return n.createBridge({name:'br0',nic:'enp37s0',mode:'static',address:'192.168.0.50/24',"
                    "gateway:'192.168.0.1',dns:'1.1.1.1, 9.9.9.9',allowVms:false})")
        self.assertTrue(r["ok"], r)
        kv = next(c for c in self.state()["conns"] if c["name"] == "br0")["kv"]
        self.assertEqual((kv["ipv4.method"], kv["ipv4.addresses"], kv["ipv4.gateway"], kv["ipv4.dns"]),
                         ("manual", "192.168.0.50/24", "192.168.0.1", "1.1.1.1,9.9.9.9"))
        self.assertNotIn("allow br0", self.conf.read_text().splitlines())

    def test_bad_input_is_refused_before_anything_runs(self):
        for spec in ("{name:'br0; reboot',nic:'enp37s0'}", "{name:'-br0',nic:'enp37s0'}", "{name:'averyveryverylongname',nic:'enp37s0'}",
                     "{name:'br0',nic:'wlp3s0'}", "{name:'br0',nic:'eth9'}", "{name:'virbr0',nic:'enp37s0'}",
                     "{name:'br0',nic:'enp37s0',mode:'static',address:'192.168.0.300/24'}",
                     "{name:'br0',nic:'enp37s0',mode:'static',address:'192.168.0.5/24',dns:'x;y'}"):
            r = self.js("return n.createBridge(%s)" % spec)
            self.assertFalse(r["ok"], spec)
        self.assertFalse([c for c in self.calls() if c.startswith("ROOT")], "nothing privileged ran")
        self.assertIn("Wi-Fi cannot be bridged", self.js("return n.createBridge({name:'br0',nic:'wlp3s0'})")["error"])

    def test_delete_restores_the_previous_profile_and_refuses_virbr0(self):
        self.assertTrue(self.js("return n.createBridge({name:'br0',nic:'enp37s0'})")["ok"])
        r = self.js("return n.deleteBridge('br0')")
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["restored"], "Wired connection 1")
        st = self.state()
        self.assertFalse([c for c in st["conns"] if c["name"].startswith("br0")])
        wired = next(c for c in st["conns"] if c["uuid"] == "wired-1")
        self.assertEqual((wired.get("autoconnect"), wired["device"]), ("yes", "enp37s0"))
        v = self.js("return n.deleteBridge('virbr0')")
        self.assertFalse(v["ok"])
        self.assertIn("libvirt", v["error"])
        self.assertTrue(any(c["name"] == "virbr0" for c in self.state()["conns"]))

    def test_the_shell_reaches_it_through_preload(self):
        pre = (ROOT / "desktop" / "preload.js").read_text()
        main = (ROOT / "desktop" / "main.js").read_text()
        for ch in ("pc:net:bridges", "pc:net:bridge-create", "pc:net:bridge-delete"):
            self.assertIn(ch, pre)
            self.assertIn("ipcMain.handle('%s'" % ch, main)
