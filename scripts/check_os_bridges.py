#!/usr/bin/env python3
"""System Settings → Network → "Bridges for virtual machines", driven in headless Chrome.

The page is the shipped os.js over the same stub client check_os_desktop.py uses; `window.pcNet` is
stubbed exactly the way desktop/preload.js injects it (a plain object of promise-returning calls), and
every call is recorded. Settings is rendered the way a popped-out Settings window renders it
(`PCOS.renderExtra('__ossettings')` into its own #feed), at a phone width and a desktop width.

Asserted, each a way the card could fail silently:
  * the card appears on the Network page and lists the bridges — virbr0 WITHOUT a Delete button (it is
    libvirt's), an NM bridge WITH one, the VM-ready badge where the host says so;
  * the wired-card select offers only free ethernet cards (the stub offers no Wi-Fi, a port is hidden);
  * Create asks FIRST (PC.uiConfirm, never a native dialog) and only then calls createBridge with the spec
    the form holds — static addressing included; a cancelled confirm calls nothing;
  * Delete asks, then calls deleteBridge with that bridge's name;
  * no horizontal scroll at 390px or 1280px.
Exit 0 pass, 1 fail, 2 could not run.
"""
import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
ROOT = os.path.dirname(HERE)
PORT = int(os.environ.get("PC_CHECK_PORT") or 9531)
PROFILE = os.environ.get("PC_CHECK_PROFILE") or "/tmp/pc-os-bridges-check"

STUB = r"""
window.__netCalls=[];window.__confirms=[];window.__confirmAnswer=true;
window.pcNet={
  available:()=>Promise.resolve(true), status:()=>Promise.resolve({online:true}),
  wifi:()=>Promise.resolve([]), connect:()=>Promise.resolve({}), forget:()=>Promise.resolve({}), radio:()=>Promise.resolve({}),
  bridges:()=>{__netCalls.push(['bridges']);return Promise.resolve({available:true,helperSetuid:true,
    bridges:[{name:'virbr0',uuid:'v',device:'virbr0',active:true,state:'connected (externally)',ipv4:['192.168.122.1/24'],ports:[],managed:'libvirt',vmReady:false},
             {name:'br9',uuid:'b',device:'br9',active:true,state:'connected',ipv4:['192.168.0.102/24'],ports:['enp9s0'],managed:'nm',vmReady:true}],
    nics:[{device:'enp37s0',connection:'Wired connection 1',state:'connected',mac:'04:7c:16:c9:86:21',port:false},
          {device:'enp9s0',connection:'br9-port-enp9s0',state:'connected',mac:'04:7c:16:c9:86:22',port:true}]});},
  createBridge:(spec)=>{__netCalls.push(['create',spec,__confirms.length]);return Promise.resolve({ok:true,name:spec.name,ipv4:['192.168.0.50/24'],warnings:[]});},
  deleteBridge:(name)=>{__netCalls.push(['delete',name,__confirms.length]);return Promise.resolve({ok:true,name,restored:'Wired connection 1'});},
};
"""

DRIVE = r"""(async()=>{
  const sleep=ms=>new Promise(r=>setTimeout(r,ms));
  __PC.uiConfirm=(msg,o)=>{__confirms.push(msg);return Promise.resolve(window.__confirmAnswer);};
  PCOS.renderExtra('__ossettings');
  for(let i=0;i<60&&!document.querySelector('.os-set-nav [data-page="network"]');i++)await sleep(50);
  const nav=document.querySelector('.os-set-nav [data-page="network"]'), sel=document.querySelector('[data-settings-mobile]');
  if(innerWidth<700&&sel){sel.value='page:network';sel.dispatchEvent(new Event('change',{bubbles:true}));}
  else if(nav) nav.click();
  for(let i=0;i<60&&!document.querySelector('[data-bridges] .os-bridge-row');i++)await sleep(50);
  const card=document.querySelector('[data-settings-page="network"]:not([hidden]) [data-bridges]');
  const out={card:!!card};
  if(!card) return out;
  const rows=[...card.querySelectorAll('.os-bridge-row')].map(r=>({name:r.dataset.bridge,del:!!r.querySelector('[data-bridge-delete]'),ready:/VM-ready/.test(r.innerText)}));
  out.rows=rows;
  out.nics=[...card.querySelectorAll('[data-bridge-nic] option')].map(o=>o.value);
  out.warn=(card.querySelector('.os-bridge-warn')||{}).innerText||'';
  out.wifiSaid=/Wi-Fi cannot/.test(card.innerText);
  // cancelled confirm: nothing is created
  window.__confirmAnswer=false;
  card.querySelector('[data-bridge-create]').click();await sleep(150);
  out.afterCancel=__netCalls.filter(c=>c[0]==='create').length;
  window.__confirmAnswer=true;
  const f=card.querySelector('[data-bridge-form]');
  f.querySelector('[name=name]').value='br1';
  const mode=f.querySelector('[name=mode]');mode.value='static';mode.dispatchEvent(new Event('change',{bubbles:true}));
  out.staticShown=!f.querySelector('[data-bridge-static]').hidden;
  f.querySelector('[name=address]').value='192.168.0.50/24';
  f.querySelector('[name=gateway]').value='192.168.0.1';
  f.querySelector('[name=dns]').value='1.1.1.1';
  card.querySelector('[data-bridge-create]').click();await sleep(250);
  out.create=__netCalls.find(c=>c[0]==='create')||null;
  const d=card.querySelector('[data-bridge-delete]');
  if(d){d.click();await sleep(250);}
  out.del=__netCalls.find(c=>c[0]==='delete')||null;
  out.status=(card.querySelector('[data-bridge-status]')||{}).textContent||'';
  const r=card.getBoundingClientRect();
  out.overflow={doc:document.documentElement.scrollWidth-innerWidth,cardRight:Math.round(r.right-innerWidth),
                main:(()=>{const m=document.querySelector('.os-set-main');return m?m.scrollWidth-m.clientWidth:0;})()};
  out.confirms=__confirms.length;
  return out;
})()"""


async def drive(url):
    import websockets
    subprocess.run(["rm", "-rf", PROFILE], check=False)
    chrome = shutil.which("google-chrome-stable") or shutil.which("google-chrome") or shutil.which("chromium") \
        or ("/opt/google/chrome/chrome" if os.path.exists("/opt/google/chrome/chrome") else None)
    if not chrome:
        print("SKIP  no Chrome")
        return 2
    proc = subprocess.Popen([chrome, "--headless=new", "--disable-gpu", "--no-sandbox", f"--remote-debugging-port={PORT}",
                             f"--user-data-dir={PROFILE}", "about:blank"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    problems = []
    try:
        page = None
        for _ in range(60):
            try:
                page = [t for t in json.load(urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/list")) if t["type"] == "page"][0]
                break
            except Exception:
                await asyncio.sleep(0.5)
        if not page:
            print("SKIP  could not start Chrome")
            return 2
        async with websockets.connect(page["webSocketDebuggerUrl"], max_size=64 * 1024 * 1024) as ws:
            n = [0]

            async def call(method, params=None):
                n[0] += 1
                await ws.send(json.dumps({"id": n[0], "method": method, "params": params or {}}))
                while True:
                    msg = json.loads(await ws.recv())
                    if msg.get("id") == n[0]:
                        return msg.get("result") or {}

            async def js(expr):
                r = await call("Runtime.evaluate", {"expression": expr, "returnByValue": True, "awaitPromise": True})
                if r.get("exceptionDetails"):
                    return {"error": json.dumps(r["exceptionDetails"])[:500]}
                return r["result"].get("value")

            await call("Page.enable")
            await call("Page.addScriptToEvaluateOnNewDocument", {"source": STUB})
            for w, h in ((390, 844), (1280, 800)):
                label = f"{w}px"
                await call("Emulation.setDeviceMetricsOverride", {"width": w, "height": h, "deviceScaleFactor": 1, "mobile": w < 600})
                await call("Page.navigate", {"url": url})
                for _ in range(80):
                    await asyncio.sleep(0.25)
                    if await js("window.__ready===true && !!window.PCOS"):
                        break
                else:
                    print(f"SKIP  {label}: the page never finished loading")
                    return 2
                r = await js(DRIVE)
                if not isinstance(r, dict) or r.get("error"):
                    problems.append((label, f"the drive script failed: {r}"))
                    continue
                if not r.get("card"):
                    problems.append((label, "no 'Bridges for virtual machines' card on the Network page"))
                    continue
                rows = {x["name"]: x for x in r["rows"]}
                if rows.get("virbr0", {}).get("del", True):
                    problems.append((label, "libvirt's virbr0 is offered for deletion"))
                if not rows.get("br9", {}).get("del"):
                    problems.append((label, "an NM bridge has no Delete button"))
                if not rows.get("br9", {}).get("ready"):
                    problems.append((label, "the VM-ready badge is missing"))
                if r["nics"] != ["enp37s0"]:
                    problems.append((label, f"the wired-card select offers {r['nics']} (a port is not free)"))
                if "drops" not in r["warn"] or not r["wifiSaid"]:
                    problems.append((label, "the network-drop / Wi-Fi warning is missing"))
                if r["afterCancel"]:
                    problems.append((label, "a cancelled confirm still created a bridge"))
                if not r["staticShown"]:
                    problems.append((label, "static addressing fields never appear"))
                c = r.get("create")
                want = {"name": "br1", "nic": "enp37s0", "mode": "static", "address": "192.168.0.50/24",
                        "gateway": "192.168.0.1", "dns": "1.1.1.1", "allowVms": True}
                if not c or c[1] != want:
                    problems.append((label, f"createBridge got {c and c[1]} (want {want})"))
                elif c[2] < 2:
                    problems.append((label, "createBridge ran without being confirmed first"))
                d = r.get("del")
                if not d or d[1] != "br9" or d[2] < 3:
                    problems.append((label, f"deleteBridge was not called for br9 after a confirm: {d}"))
                ov = r["overflow"]
                if ov["doc"] > 1 or ov["cardRight"] > 1 or ov["main"] > 1:
                    problems.append((label, f"horizontal overflow {ov}"))
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
    if problems:
        for label, msg in problems:
            print(f"  [{label}] {msg}")
        print("FAIL  bridges card")
        return 1
    print("OK  System Settings bridges card")
    return 0


def main():
    try:
        import websockets  # noqa: F401
    except ImportError:
        print("SKIP  websockets not installed")
        return 2
    import http.server
    import threading
    from check_os_desktop import PAGE
    tmp = tempfile.mkdtemp(prefix="osbridges-")
    with open(os.path.join(tmp, "index.html"), "w") as fh:
        fh.write(PAGE)

    class H(http.server.SimpleHTTPRequestHandler):
        def translate_path(self, path):
            path = path.split("?")[0].split("#")[0]
            if path.startswith("/static/"):
                return os.path.join(ROOT, path.lstrip("/"))
            return os.path.join(tmp, path.lstrip("/") or "index.html")

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        return asyncio.run(drive(f"http://127.0.0.1:{srv.server_address[1]}/index.html"))
    finally:
        srv.shutdown()
        shutil.rmtree(tmp, True)


if __name__ == "__main__":
    sys.exit(main())
