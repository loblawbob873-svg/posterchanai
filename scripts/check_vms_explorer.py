#!/usr/bin/env python3
"""Virtual Machines is an EXPLORER (Proxmox-style) — measured in the REAL client, in real Chrome.

    venv-unified/bin/python scripts/check_vms_explorer.py [--shots DIR]

"UI still looks bad. Should be a nice detailed list of VMs in an explorer-type list similar to
Proxmox with columns that have the details like RAM, Disk, number of VMs running." The screen used to
be a column of cards (name, a state pill, one spec line). This boots the shipped /client exactly like
tests/client/test_vms_full_app.py (same signed kind-5310/6310 fixture host), gives the host twelve
VMs with IPs, uptimes and a bridged NIC, adds a second host, and asserts at 1280x900, 3840x2560
(zoom 1.25) and 390x844:

  wide    a TREE (All hosts → host → VM leaves), a summary with VM/CPU/memory/storage tiles, and a
          details TABLE whose columns are Status, Name, vCPU, Memory, Disk, Network / IP, Uptime,
          Assigned; sorting by Memory reorders the rows by ram_mib both ways; the filter box narrows
          by name or IP and keeps the caret; a tree leaf opens that VM with its action toolbar
          (Start / Shut down / Reboot / Force off / Console / Settings / Delete); "All hosts" is one
          table over every host with a Host column; no horizontal page scroll anywhere.
  phone   no table: the same rows as cards carrying the IP/uptime, no horizontal scroll.

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
PORT = int(os.environ.get("PC_CHECK_PORT") or 9498)
PROFILE = os.environ.get("PC_CHECK_PROFILE") or "/tmp/pc-vms-explorer-profile"

# Twelve VMs on the fixture host, the second host (vmhost2.invalid) answers as a user with none.
EXTRA = r'''
(function(){
  const names=['web-1','web-2','db-main','cache','build-agent','win11','ci-runner','dns','mail','proxy','media','backup'];
  window.__vmsMany=names.map((n,i)=>({uuid:'2222222'+i.toString(16)+'-1111-4111-8111-11111111111'+(i%10),name:n,
    state:i%3===0?'shutoff':'running',vcpus:1+(i%4),ram_mib:1024*(1+((i*5)%9)),disk_gib:10+i*7,managed:true,
    guest:n==='win11'?'windows':'linux',firmware:'efi',autostart:i%2===0,labels:[],assigned:i===2?['ab'.repeat(32)]:[],owner:'',iso:'',
    net:i===4?{type:'bridge',name:'br0'}:{type:'network',name:'default'},
    ips:i%3===0?[]:['192.168.122.'+(10+i)],uptime_s:i%3===0?null:3600*i+125}));
  const _hostOp=hostOp;
  hostOp=function(op,args,who){
    if(op==='vm.list'&&(!who||who.name==='vmhost.invalid'))return {ok:true,result:{vms:window.__vmsMany,next:null}};
    if(op==='host.info'&&(!who||who.name==='vmhost.invalid')){const r=_hostOp(op,args,who);r.result.vms={running:8,total:12};return r;}
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
            no_hscroll = "document.querySelector('.vms').scrollWidth <= innerWidth / (parseFloat(getComputedStyle(document.body).zoom)||1) + 1"
            wide = width >= 1024
            if not wide:
                await b.until("!!document.querySelector('.vms-host[data-host]')")
                await b.js("document.querySelector('.vms-host[data-host]').click()")
                await b.until("document.querySelectorAll('button.vms-vm').length===12")
                if await b.js("!!document.querySelector('.vmx-table')"):
                    fail("the phone layout draws the table")
                spec = await b.js("[...document.querySelectorAll('button.vms-vm')].map(e=>e.innerText).join('|')")
                if "192.168.122.11" not in spec or "up " not in spec:
                    fail("phone cards do not carry the IP and uptime: " + spec[:200])
                if not await b.js(no_hscroll):
                    fail("horizontal page scroll on the phone")
                if shots:
                    await shot(b, shots, f"vms-{label}-list.png")
                return
            await b.until("document.querySelectorAll('.vmx-table tbody tr').length===12")
            # --- tree
            leaves = await b.js("document.querySelectorAll('.vmx-tree .vmx-leaf').length")
            if leaves != 12:
                fail(f"the tree shows {leaves} VM leaves, not 12")
            # --- columns
            heads = await b.js("[...document.querySelectorAll('.vmx-table thead th')].map(t=>t.innerText.trim().toLowerCase())")
            for want in ("status", "name", "vcpu", "memory", "disk", "network / ip", "uptime", "assigned"):
                if not any(h.startswith(want) for h in heads):
                    fail(f"no '{want}' column (have {heads})")
            tiles = await b.js("(document.querySelector('.vmx-tiles')||{}).innerText||''")
            if "12" not in tiles or "8 running" not in tiles or "8 cores" not in tiles:
                fail("the summary does not say 12 VMs / 8 running / 8 cores: " + tiles.replace("\n", " ")[:200])
            # --- sort by memory, both directions
            rams = "[...document.querySelectorAll('.vmx-table tbody tr')].map(r=>window.__vmsMany.find(v=>v.uuid===r.dataset.vm).ram_mib)"
            await b.js("document.querySelector('[data-sort=ram]').click()")
            asc = await b.js(rams)
            await b.js("document.querySelector('[data-sort=ram]').click()")
            desc = await b.js(rams)
            if asc != sorted(asc) or desc != sorted(desc, reverse=True):
                fail(f"sorting by Memory does not order the rows: {asc} / {desc}")
            # --- filter by IP keeps the caret
            await b.js("(()=>{const q=document.querySelector('[data-vmq]');q.focus();q.value='122.14';q.dispatchEvent(new Event('input',{bubbles:true}));})()")
            n = await b.js("document.querySelectorAll('.vmx-table tbody tr').length")
            if n != 1:
                fail(f"filtering by an IP left {n} rows")
            if not await b.js("document.activeElement===document.querySelector('[data-vmq]')"):
                fail("the filter box lost focus while typing")
            await b.js("(()=>{const q=document.querySelector('[data-vmq]');q.value='';q.dispatchEvent(new Event('input',{bubbles:true}));})()")
            if not await b.js(no_hscroll):
                fail("horizontal page scroll on the host table")
            if shots:
                await shot(b, shots, f"vms-{label}-host.png")
            # --- a tree leaf opens the VM with its toolbar
            await b.js("[...document.querySelectorAll('.vmx-leaf')].find(e=>/db-main/.test(e.innerText)).click()")
            await b.until("!!document.querySelector('.vms-vmhead') && /db-main/.test(document.querySelector('.vms-vmhead').innerText)")
            acts = await b.js("[...document.querySelectorAll('.vmx-toolbar [data-power],.vmx-toolbar [data-act]')].map(e=>e.dataset.power||e.dataset.act)")
            for want in ("start", "shutdown", "reboot", "destroy", "console", "settings", "delete"):
                if want not in acts:
                    fail(f"the VM toolbar has no {want} ({acts})")
            # "Shutdown, reboot have no color like the rest of the buttons": every power button must be
            # styled, i.e. its computed colours differ from a plain .btn (compared, not class-matched).
            plain = await b.js("""(()=>{const t=document.createElement('button');t.className='btn small';
              document.querySelector('.vmx-toolbar').append(t);const c=getComputedStyle(t);
              const v=[c.backgroundColor,c.color,c.borderTopColor].join('|');t.remove();
              return [...document.querySelectorAll('.vmx-toolbar [data-power]')].filter(e=>{const k=getComputedStyle(e);
                return [k.backgroundColor,k.color,k.borderTopColor].join('|')===v;}).map(e=>e.dataset.power);})()""")
            if plain:
                fail(f"power buttons drawn as plain grey buttons, unlike the rest of the toolbar: {plain}")
            if not await b.js("/192\\.168\\.122\\.12/.test(document.querySelector('.vmx-tiles').innerText)"):
                fail("the VM page does not show its IP")
            if shots:
                await shot(b, shots, f"vms-{label}-vm.png")
            # --- add the second host → "All hosts" with a Host column
            host2 = await b.js("NostrTools.getPublicKey(Uint8Array.from('7'.repeat(64).match(/../g).map(x=>parseInt(x,16))))")
            await b.js("document.querySelector('[data-act=add]').click()")
            await b.until("!!document.querySelector('.uiprompt-in')")
            await b.js("document.querySelector('.uiprompt-in').value=NostrTools.nip19.npubEncode('%s');document.querySelector('.uiconfirm [data-uc=\"1\"]').click()" % host2)
            await b.until("!!document.querySelector('.uiprompt-in')")
            await b.js("document.querySelector('.uiprompt-in').value='wss://vmhost2.invalid/relay';document.querySelector('.uiconfirm [data-uc=\"1\"]').click()")
            await b.until("!!document.querySelector('[data-act=all]')")
            await b.js("document.querySelector('[data-act=all]').click()")
            await b.until("document.querySelectorAll('.vmx-table tbody tr').length===12")
            heads = await b.js("[...document.querySelectorAll('.vmx-table thead th')].map(t=>t.innerText.trim().toLowerCase())")
            if not any(h.startswith("host") for h in heads):
                fail(f"All hosts has no Host column ({heads})")
            await b.js("document.querySelector('.vmx-table tbody tr').click()")
            await b.until("!!document.querySelector('.vms-vmhead')")
            await b.js("document.querySelector('[data-act=back]').click()")
            await b.until("!!document.querySelector('[data-act=all].sel')")
            if not await b.js(no_hscroll):
                fail("horizontal page scroll on All hosts")
            if shots:
                await shot(b, shots, f"vms-{label}-all.png")
            errs = await b.js("__errors.filter(e=>!/ResizeObserver/.test(e))")
            if errs:
                fail("page errors: " + json.dumps(errs)[:300])
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
    for w, h in ((1280, 900), (3840, 2560), (390, 844)):
        asyncio.run(check(w, h, shots, fails))
    if fails:
        print("FAIL")
        for f in fails:
            print("  " + f)
        return 1
    print("PASS: tree, summary, sortable details table, VM toolbar, All hosts, phone cards")
    return 0


if __name__ == "__main__":
    sys.exit(main())
