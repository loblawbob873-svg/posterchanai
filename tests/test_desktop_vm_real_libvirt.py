"""Virtual Machines against what a real PosterChanOS libvirt session actually does.

Measured on the test desktop (qemu 10.2.3 built USE=opengl without virgl, libvirt session):

  * Create failed for EVERY VM: "3d acceleration is not supported by this QEMU binary". The domain
    always asked for SPICE GL + an accelerated virtio GPU. `qemu3d()` asks the binary, and a QEMU
    without virgl now gets a 2D virtio GPU instead of no VM at all.
  * "Gaming mouse" was reported ON for every VM and could not be turned on: libvirt adds an implicit
    `<input type='mouse' bus='ps2'/>` to every x86 domain (so reading it proved nothing), and writes
    the tablet back as `<input …><address/></input>`, which the self-closing-only regex never
    removed.

After the fix the same device created the VM, booted the attached Alpine ISO in virt-viewer, ejected
it with bootDisk, and booted the installed disk (screenshots in the session log). This file RUNS the
shipped vm.js against a stateful fake `virsh` serving the domain XML captured from that machine.
"""
import json
import os
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
VM = ROOT / "desktop/vm.js"
REAL = ROOT / "tests/fixtures/desktop_vm_real/session_domain_no_virgl.xml"

FAKE_VIRSH = r"""#!/usr/bin/env python3
import os, sys, re, shutil
state = os.environ["FAKE_VIRSH_STATE"]
args = sys.argv[1:]
if args[:1] == ["--connect"]:
    args = args[2:]
cmd = args[0]
xml_path = os.path.join(state, "domain.xml")
if cmd == "dominfo":
    print("Name: pcprobe\nState: shut off\nCPU(s): 1\nMax memory: 1048576 KiB\nAutostart: disable")
elif cmd == "domblklist":
    print(" Type Device Target Source\n------\n file disk vda /x/disk.qcow2")
elif cmd == "dumpxml":
    print(open(xml_path).read())
elif cmd == "define":
    body = open(args[1]).read()
    # What libvirt does on every define of an x86 domain: the implicit PS/2 pointer comes back, and
    # a USB tablet is written back with an <address> child.
    body = re.sub(r"<input type=[\"']tablet[\"'] bus=[\"']usb[\"']\s*/>",
                  "<input type='tablet' bus='usb'>\n      <address type='usb' bus='0' port='1'/>\n    </input>", body)
    if not re.search(r"<input\s+type=['\"]mouse['\"]\s+bus=['\"]ps2['\"]", body):
        body = body.replace("</devices>", "  <input type='mouse' bus='ps2'/>\n  </devices>")
    open(xml_path, "w").write(body)
    print("Domain defined")
else:
    print("ok")
"""


def _node(expr, env):
    script = "const v=require(%s);(async()=>process.stdout.write(JSON.stringify(await (%s))))()" % (
        json.dumps(str(VM)), expr)
    out = subprocess.run(["node", "-e", script], env=env, capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


@pytest.fixture
def fake_libvirt():
    if not shutil.which("node"):
        pytest.skip("node is required")
    d = Path(tempfile.mkdtemp())
    try:
        bin_dir = d / "bin"
        bin_dir.mkdir()
        v = bin_dir / "virsh"
        v.write_text(FAKE_VIRSH)
        v.chmod(v.stat().st_mode | stat.S_IEXEC)
        shutil.copy(REAL, d / "domain.xml")
        env = dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}", FAKE_VIRSH_STATE=str(d), HOME=str(d))
        yield env, d
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_the_real_domain_is_not_gaming_mode_because_libvirt_added_a_ps2_mouse(fake_libvirt):
    env, _ = fake_libvirt
    assert "<input type='mouse' bus='ps2'/>" in REAL.read_text(), "fixture no longer shows libvirt's implicit mouse"
    assert _node("v.details('pcprobe').then(d=>d.gamingMouse)", env) is False


def test_gaming_mode_really_removes_the_tablet_and_really_puts_it_back(fake_libvirt):
    env, d = fake_libvirt
    on = _node("v.gamingMouse('pcprobe',true)", env)
    assert on == {"ok": True, "gamingMouse": True}
    body = (d / "domain.xml").read_text()
    assert "type='tablet'" not in body and 'type="tablet"' not in body
    off = _node("v.gamingMouse('pcprobe',false)", env)
    assert off == {"ok": True, "gamingMouse": False}
    assert (d / "domain.xml").read_text().count("tablet") == 1


@pytest.mark.skipif(not shutil.which("node"), reason="node is required")
def test_a_qemu_without_virgl_gets_a_2d_gpu_and_one_with_it_keeps_3d():
    script = """
const v=require(%s);
const probe=out=>{const m=require.cache[require.resolve(%s)];return out;};
(async()=>{
  const r={};
  // Each call gets a fresh module so the per-process cache does not carry across cases.
  const fresh=()=>{delete require.cache[require.resolve(%s)];return require(%s);};
  let m=fresh(); r.without=await m.qemu3d(async()=>({ok:true,out:'name "virtio-vga", bus PCI\\nname "virtio-gpu-pci"',error:''}));
  m=fresh(); r.withGl=await m.qemu3d(async()=>({ok:true,out:'name "virtio-vga-gl", bus PCI',error:''}));
  m=fresh(); r.unknown=await m.qemu3d(async()=>({ok:false,out:'',error:'ENOENT'}));
  r.xml2d=m.displayXml(false); r.xml3d=m.displayXml(true);
  process.stdout.write(JSON.stringify(r));
})();
""" % tuple([json.dumps(str(VM))] * 4)
    got = json.loads(subprocess.check_output(["node", "-e", script], text=True))
    assert got["without"] is False and got["withGl"] is True
    assert got["unknown"] is True, "an unanswerable probe keeps the configuration that shipped"
    assert "accel3d" not in got["xml2d"] and "<gl " not in got["xml2d"]
    assert '<model type="virtio"' in got["xml2d"] and '<graphics type="spice"' in got["xml2d"]
    assert 'accel3d="yes"' in got["xml3d"] and '<gl enable="yes"/>' in got["xml3d"]


def test_create_uses_the_probe():
    src = VM.read_text()
    create = src[src.index("async function create("):src.index("function launchViewer(")]
    assert "await qemu3d()" in create
    assert "displayXml(gl3d)" in create
