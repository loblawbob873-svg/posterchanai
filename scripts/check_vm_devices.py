#!/usr/bin/env python3
"""A VM's DEVICES — "Add USB device" into a RUNNING VM, the PCI checklist, detach, and the read-only view an
assigned user gets — measured in the REAL client, in real Chrome.

    venv-unified/bin/python scripts/check_vm_devices.py [--shots DIR]

"make sure we can add USB drive to the VM" / "we need a way to add devices to a VM. Like GPU Passthrough, USB
Drives". Boots the shipped /client exactly like tests/client/test_vms_full_app.py (same signed kind-5310/6310 fixture
host, which now announces the `devices` feature), with the VM RUNNING, and asserts at 1280x900 and 390x844:

  * the VM page has a Devices section and an "Add device" button while the VM runs (no shutdown, unlike Settings);
  * the picker lists the host's USB devices with human names, the one the host is using DISABLED with the host's
    reason, and a "Keep attached after the VM restarts" checkbox that is sent as `persist`;
  * attaching sends kind/vendor/product/bus/device ids only — never XML — and the device then shows on the VM page
    as "attached now";
  * the PCI tab shows the host checklist and each card's checks with the fix, and refuses a card while the VM runs;
  * Detach asks in the app's own dialog (never window.confirm) and the device leaves the list;
  * an ASSIGNED USER sees the list with no Add or Detach;
  * no horizontal page scroll, no page errors, no native dialog.

Exit 0 pass, 1 fail, 2 could not run. Reads PC_CHECK_PORT / PC_CHECK_PROFILE.
"""
import asyncio
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
PORT = int(os.environ.get("PC_CHECK_PORT") or 9507)
PROFILE = os.environ.get("PC_CHECK_PROFILE") or "/tmp/pc-vm-devices-profile"

EXTRA = r'''
(function(){
  window.__vm.state='running';
  window.__devs=[]; window.__devArgs=[]; window.__native=0;
  window.confirm=window.alert=window.prompt=function(){window.__native++;return false;};
  const USB=[{kind:'usb',id:'0781:5581@1-4',vendor:'0781',product:'5581',bus:1,device:4,label:'SanDisk Ultra (0781:5581)',
              class:'08',class_name:'storage',speed:'480',busy:'',live:true,used_by:null},
             {kind:'usb',id:'174c:55aa@6-2',vendor:'174c',product:'55aa',bus:6,device:2,label:'ASMT ASMT105x (174c:55aa)',
              class:'08',class_name:'storage',speed:'5000',busy:'the host has it mounted at /raid (through vg-nas)',live:true,used_by:null}];
  const PCI={live:false,checks:[{id:'iommu',ok:true,label:'IOMMU is on (33 groups)',fix:''},{id:'vfio',ok:true,label:'vfio-pci is available',fix:''}],
    vm_checks:[{id:'efi',ok:true,label:'UEFI firmware',fix:''},{id:'q35',ok:true,label:'pc-q35-10.2 machine',fix:''}],
    devices:[{kind:'pci',id:'0000:01:00.0',address:'0000:01:00.0',vendor:'10de',product:'2504',label:'NVIDIA Corporation GA106 [GeForce RTX 3060] (10de:2504)',
              class:'030000',class_name:'graphics card',driver:'nvidia',group:'12',busy:"the host's nvidia driver is using it",gpu:true,live:false,used_by:null,
              with:['0000:01:00.1'],passable:false,blockers:["0000:01:00.0: the host's nvidia driver is using it"],
              checks:[{id:'group',ok:true,label:'IOMMU group 12 can be given whole',fix:''},
                      {id:'host-gpu',ok:false,label:"The host's nvidia driver is using it",fix:'Hand it to vfio-pci at boot instead: create /etc/modprobe.d/vfio.conf containing `options vfio-pci ids=10de:2504,10de:228e`'}]},
             {kind:'pci',id:'0000:0e:00.0',address:'0000:0e:00.0',vendor:'1022',product:'43f7',label:'AMD 600 Series Chipset USB 3.2 Controller (1022:43f7)',
              class:'0c0330',class_name:'USB controller',driver:'xhci_hcd',group:'24',busy:'',gpu:false,live:false,used_by:null,with:[],passable:true,blockers:[],
              checks:[{id:'group',ok:true,label:'IOMMU group 24 can be given whole',fix:''}]}]};
  const devView=()=>window.__devs.map(d=>Object.assign({},d));
  const _hostOp=hostOp;
  hostOp=function(op,args,who){
    if(who&&who.name!=='vmhost.invalid')return _hostOp(op,args,who);
    if(op==='host.whoami'){const r=_hostOp(op,args,who);r.result.role=window.__asUser?'user':'admin';r.result.host.features=r.result.host.features.concat(['devices']);return r;}
    if(op==='vm.get'){const r=_hostOp(op,args,who);r.result.vm.devices=devView();if(window.__asUser)delete r.result.vm.hardware;return r;}
    if(op==='vm.list'){const r=_hostOp(op,args,who);r.result.vms.forEach(v=>{v.autostart=!!window.__autostart;});return r;}
    if(op==='host.devices.list'){__vm.ops.push(op);if(window.__asUser)return {ok:false,error:{code:'forbidden',message:'only a host admin can do that'}};
      return {ok:true,result:{kinds:{usb:{live:true,checks:[{id:'qemu-usb',ok:true,label:'QEMU supports USB passthrough (usb-host)',fix:''}],devices:USB,error:''},pci:PCI}}};}
    if(op==='vm.device.attach'){__vm.ops.push(op);window.__devArgs.push(args);
      if(args.kind==='pci'&&__vm.state!=='shutoff')return {ok:false,error:{code:'conflict',message:'shut the VM down to add a PCI device'}};
      const d=USB.find(x=>x.vendor===args.vendor&&x.product===args.product);
      if(!d||d.busy)return {ok:false,error:{code:'conflict',message:'in use by the host'}};
      window.__devs.push({kind:'usb',vendor:d.vendor,product:d.product,bus:d.bus,device:d.device,label:d.label,present:true,key:d.vendor+':'+d.product,live:true,persistent:!!args.persist});
      const r=_hostOp('vm.list',{},who);return {ok:true,result:{vm:Object.assign(r.result.vms[0],{devices:devView()})}};}
    if(op==='vm.device.detach'){__vm.ops.push(op);window.__devArgs.push(args);
      window.__devs=window.__devs.filter(d=>!(d.vendor===args.vendor&&d.product===args.product));
      const r=_hostOp('vm.list',{},who);return {ok:true,result:{vm:Object.assign(r.result.vms[0],{devices:devView()})}};}
    return _hostOp(op,args,who);
  };
})();
'''


async def check(width, height, shots, fails):
    from tests.client.test_effects_full_app import Browser, Handler
    from tests.client.test_vms_full_app import INIT
    import httpx
    import websockets
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    prof = PROFILE + "-" + str(width) + "-" + str(os.getpid())
    subprocess.run(["rm", "-rf", prof], check=False)
    chrome = shutil.which("google-chrome-stable") or "/opt/google/chrome/chrome"
    proc = subprocess.Popen([chrome, "--headless=new", "--no-sandbox", "--disable-gpu", f"--remote-debugging-port={PORT}",
                             f"--user-data-dir={prof}", "about:blank"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    label = f"{width}x{height}"

    def fail(msg):
        fails.append(f"{label}: {msg}")
    try:
        for _ in range(100):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/version", timeout=1)
                break
            except Exception:
                time.sleep(0.1)
        async with httpx.AsyncClient() as h:
            pages = (await h.get(f"http://127.0.0.1:{PORT}/json")).json()
        page = next(p for p in pages if p.get("type") == "page")
        async with websockets.connect(page["webSocketDebuggerUrl"], max_size=50_000_000) as ws:
            b = Browser(ws)
            await b.call("Page.enable")
            await b.call("Network.enable")
            await b.call("Network.setBlockedURLs", {"urls": ["https://*", "wss://*"]})
            await b.call("Emulation.setDeviceMetricsOverride",
                         {"width": width, "height": height, "deviceScaleFactor": 1, "mobile": width < 600})
            await b.call("Page.addScriptToEvaluateOnNewDocument", {"source": INIT + EXTRA})
            await b.call("Page.navigate", {"url": f"http://127.0.0.1:{server.server_port}/client"})
            await b.until('!!window.__PC && !!window.NostrTools && document.readyState==="complete"')
            await b.until("document.body.classList.contains('guest')")
            await b.js("(()=>{const key=new Uint8Array(32).fill(3);window.__events=[];"
                       "document.querySelector('#nsec-input').value=NostrTools.nip19.nsecEncode(key);"
                       "document.querySelector('#btn-nsec-login').click()})()")
            await b.until("!!__PC.me()")
            await b.js("__PC.switchView('vms')")
            await b.until("!!window.PCVms && /Fixture host/.test(document.querySelector('#feed').innerText)")
            no_hscroll = ("document.querySelector('.vms').scrollWidth <= innerWidth / "
                          "(parseFloat(getComputedStyle(document.body).zoom)||1) + 1")
            if width < 1024:
                await b.until("!!document.querySelector('.vms-host[data-host]')")
                await b.js("document.querySelector('.vms-host[data-host]').click()")
                await b.until("[...document.querySelectorAll('.vms-vm')].some(e=>/alpha/.test(e.innerText))")
                await b.js("[...document.querySelectorAll('.vms-vm')].find(e=>/alpha/.test(e.innerText)).click()")
            else:
                await b.until("[...document.querySelectorAll('.vmx-leaf')].some(e=>/alpha/.test(e.innerText))")
                await b.js("[...document.querySelectorAll('.vmx-leaf')].find(e=>/alpha/.test(e.innerText)).click()")
            await b.until("!!document.querySelector('.vms-devs') && /No USB or PCI devices attached/.test(document.querySelector('.vms-devs').innerText)")
            if not await b.js("/Running/.test(document.querySelector('.vms-vmhead').innerText)"):
                fail("the fixture VM is not running")
            if not await b.js("!!document.querySelector('[data-act=dev-add]:not([disabled])')"):
                fail("no enabled Add device button on a RUNNING VM")
            if shots:
                await shot(b, shots, f"vmdev-{label}-vm.png")
            # ---- the picker
            await b.js("document.querySelector('[data-act=dev-add]').click()")
            await b.until("document.querySelectorAll('.vms-devopt').length===2")
            names = await b.js("[...document.querySelectorAll('.vms-devopt')].map(e=>e.innerText)")
            if not any("SanDisk Ultra (0781:5581)" in n for n in names):
                fail(f"the picker does not name the stick: {names}")
            raid = await b.js("(()=>{const e=[...document.querySelectorAll('.vms-devopt')].find(x=>/174c/.test(x.innerText));"
                              "return {dis:e.querySelector('input').disabled,t:e.innerText};})()")
            if not raid["dis"] or "/raid" not in raid["t"]:
                fail(f"the host's busy RAID box is selectable or silent: {raid}")
            if await b.js("!document.querySelector('[data-act=dev-attach]').disabled"):
                fail("Attach is enabled with nothing chosen")
            if not await b.js("!!document.querySelector('#dev-persist') && document.querySelector('#dev-persist').checked"):
                fail("no 'Keep attached after the VM restarts' checkbox (checked) for a running VM")
            if not await b.js(no_hscroll):
                fail("horizontal page scroll in the picker")
            # the PCI tab: host checklist, card checks with the fix, and a running VM refuses a card
            await b.js("document.querySelector('[data-devkind=pci]').click()")
            await b.until("/IOMMU is on/.test(document.querySelector('.vms').innerText)")
            pci = await b.js("document.querySelector('.vms').innerText")
            if "options vfio-pci ids=10de:2504,10de:228e" not in pci or "Shut the VM down first" not in pci:
                fail("the PCI tab does not show the GPU's fix and the shut-down rule")
            if await b.js("[...document.querySelectorAll('input[name=devsel]')].some(i=>!i.disabled)"):
                fail("a PCI card is selectable while the VM runs")
            if shots:
                await shot(b, shots, f"vmdev-{label}-pci.png")
            # an autostart VM: the picker says why nothing can be attached (libvirt would start it at boot)
            await b.js("window.__autostart=true;PCVms._state.data[PCVms._state.host].vms.forEach(v=>v.autostart=true);"
                       "document.querySelector('[data-devkind=usb]').click()")
            await b.until("!!document.querySelector('.vms-dev-auto')")
            await b.js("(()=>{const i=[...document.querySelectorAll('input[name=devsel]')].find(x=>x.value==='0781:5581@1-4');"
                       "i.checked=true;i.dispatchEvent(new Event('change',{bubbles:true}));})()")
            if not await b.js("document.querySelector('[data-act=dev-attach]').disabled"):
                fail("Attach is enabled for a VM that starts with the host")
            await b.js("window.__autostart=false;PCVms._state.data[PCVms._state.host].vms.forEach(v=>v.autostart=false);"
                       "document.querySelector('[data-devkind=pci]').click()")
            await b.until("/IOMMU is on/.test(document.querySelector('.vms').innerText)")
            await b.js("document.querySelector('[data-devkind=usb]').click()")
            await b.until("document.querySelectorAll('.vms-devopt').length===2")
            await b.js("(()=>{const i=[...document.querySelectorAll('input[name=devsel]')].find(x=>x.value==='0781:5581@1-4');"
                       "i.checked=true;i.dispatchEvent(new Event('change',{bubbles:true}));})()")
            await b.until("!document.querySelector('[data-act=dev-attach]').disabled")
            if shots:
                await shot(b, shots, f"vmdev-{label}-picker.png")
            await b.js("document.querySelector('[data-act=dev-attach]').click()")
            await b.until("!!document.querySelector('.vms-dev') && /SanDisk Ultra/.test(document.querySelector('.vms-devs').innerText)")
            args = await b.js("window.__devArgs[0]")
            want = {"vm": "11111111-1111-4111-8111-111111111111", "kind": "usb", "vendor": "0781", "product": "5581",
                    "bus": 1, "device": 4, "persist": True}
            if args != want:
                fail(f"attach sent {args}, not the ids {want}")
            if "attached now" not in await b.js("document.querySelector('.vms-devs').innerText"):
                fail("the attached stick does not say it is attached now")
            if not await b.js(no_hscroll):
                fail("horizontal page scroll on the VM page")
            if shots:
                await shot(b, shots, f"vmdev-{label}-attached.png")
            # ---- detach, through the app's own dialog
            await b.js("document.querySelector('[data-dev-detach]').click()")
            await b.until("!!document.querySelector('.uiconfirm [data-uc=\"1\"]')")
            await b.js("document.querySelector('.uiconfirm [data-uc=\"1\"]').click()")
            await b.until("/No USB or PCI devices attached/.test(document.querySelector('.vms-devs').innerText)")
            det = await b.js("window.__devArgs[1]")
            if not det or det.get("vendor") != "0781" or "persist" in det:
                fail(f"detach sent {det}")
            # ---- an assigned user: the list, and nothing to change it with
            await b.js("window.__asUser=true;window.__devs=[{kind:'usb',vendor:'0781',product:'5581',bus:1,device:4,"
                       "label:'SanDisk Ultra (0781:5581)',present:true,key:'0781:5581',live:true,persistent:true}];"
                       "PCVms._state.data[PCVms._state.host].whoami.role='user';")
            await b.js("document.querySelector('[data-act=back]').click()")
            if width < 1024:
                await b.until("[...document.querySelectorAll('.vms-vm')].some(e=>/alpha/.test(e.innerText))")
                await b.js("[...document.querySelectorAll('.vms-vm')].find(e=>/alpha/.test(e.innerText)).click()")
            else:
                await b.js("[...document.querySelectorAll('.vmx-leaf')].find(e=>/alpha/.test(e.innerText)).click()")
            await b.until("!!document.querySelector('.vms-dev')")
            if await b.js("!!document.querySelector('[data-act=dev-add],[data-dev-detach]')"):
                fail("an assigned user is offered Add or Detach")
            errs = await b.js("__errors.filter(e=>!/ResizeObserver/.test(e))")
            if errs:
                fail("page errors: " + json.dumps(errs)[:300])
            if await b.js("window.__native"):
                fail("a native dialog was used")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
        server.shutdown()
        subprocess.run(["rm", "-rf", prof], check=False)


async def shot(b, d, name):
    import base64
    r = await b.call("Page.captureScreenshot", {"format": "png"})
    with open(os.path.join(d, name), "wb") as f:
        f.write(base64.b64decode(r["data"]))


def main():
    if not (shutil.which("google-chrome-stable") or os.path.exists("/opt/google/chrome/chrome")):
        print("SKIP: no Chrome")
        return 2
    try:
        import websockets  # noqa: F401
        import httpx  # noqa: F401
    except ImportError as e:
        print("SKIP: " + str(e))
        return 2
    shots = None
    if "--shots" in sys.argv:
        shots = sys.argv[sys.argv.index("--shots") + 1]
        os.makedirs(shots, exist_ok=True)
    fails = []
    for w, h in ((1280, 900), (390, 844)):
        try:
            asyncio.run(check(w, h, shots, fails))
        except AssertionError as e:
            fails.append(f"{w}x{h}: {str(e)[:400]}")
    if fails:
        print("FAIL")
        for f in fails:
            print("  " + f)
        return 1
    print("PASS: Devices section, USB picker with busy/persist, attach into a running VM, PCI checklist, detach, "
          "read-only for an assigned user")
    return 0


if __name__ == "__main__":
    sys.exit(main())
