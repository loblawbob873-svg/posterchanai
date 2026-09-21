#!/usr/bin/env python3
"""The VM console's toolbar must be VISIBLE and CLICKABLE at every display scale, in a popped-out
PosterChanOS window and in a plain page.

    venv-unified/bin/python scripts/check_vm_console_layout.py

Three reports, one layout:
  * "I can see [the console's] button at the top on laptop but not on 4K monitor" — the overlay sat
    under the popped-out window's 38px title bar (#pc-oswin-chrome). The title bar is drawn in LAYOUT
    px inside a zoomed body, so how much of the toolbar it hid depended on the scale: at the laptop's
    .77 a sliver of the buttons showed below it, at the 4K desk's 1.25 all of them were covered.
  * "Close button at top does nothing" — raising the overlay's z-index over the title bar (74c0c3d6d)
    made the buttons PAINT on top, and `elementFromPoint` agreed, but the title bar is
    `-webkit-app-region: drag` and Electron routes a press by DRAG REGION, not by z-index: an element
    painted over a drag region that is not itself `no-drag` loses the press to "move the window".
    So the ✕ Close was drawn exactly where a click could only ever drag.
This check measures the SHIPPED markup (`PCVms._consoleShell`) and the SHIPPED styles (client.css for
the title bar, `PCVms._style()` for the console) in real Chrome, and folds the page's app-region rects
the way Chromium builds a window's draggable region (document order; `drag` adds, `no-drag`
subtracts). For every toolbar button, at each scale and window size:

  hidden-under-chrome   the button's box intersects the title bar
  off-screen            the button is not fully inside the viewport
  not-hit               elementFromPoint at its centre is something else (covered)
  in-drag-region        its centre falls inside the window's draggable region — a click would move
                        the window instead of pressing it

Exit 0 = pass, 1 = a failure, 2 = could not run (no Chrome / no websockets).
"""
import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import urllib.request
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORT = int(os.environ.get("PC_CHECK_PORT") or 9497)
PROFILE = os.environ.get("PC_CHECK_PROFILE") or "/tmp/pc-vm-console-layout-profile"

# The title bar is built here exactly as oswin.js installChrome builds it (same id, classes, buttons).
PAGE = """<!doctype html><html class="%(cls)s"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="stylesheet" href="/static/css/client.css"></head><body>
%(chrome)s
<script src="/static/js/client/vms.js"></script>
<script>
const st=document.createElement('style');st.textContent=PCVms._style();document.head.appendChild(st);
document.body.appendChild(PCVms._consoleShell('alpha-vm'));
window.__ready=true;
</script></body></html>"""
CHROME = ('<header id="pc-oswin-chrome"><span class="pc-oswin-title">vms</span><span class="pc-oswin-buttons">'
          '<button data-action="min">−</button><button data-action="max">□</button>'
          '<button data-action="close">×</button></span></header>')

MEASURE = r"""(() => {
  const region = el => { const cs = getComputedStyle(el);
    const v = (cs.getPropertyValue('app-region') || cs.getPropertyValue('-webkit-app-region') || '').trim();
    return v === 'drag' || v === 'no-drag' ? v : ''; };
  const rects = [];
  for(const el of document.querySelectorAll('*')){ const k = region(el); if(!k) continue;
    const r = el.getBoundingClientRect(); if(r.width && r.height) rects.push({k, l:r.left, t:r.top, r:r.right, b:r.bottom}); }
  const inDrag = (x, y) => { let d = false; for(const q of rects) if(x >= q.l && x < q.r && y >= q.t && y < q.b) d = q.k === 'drag'; return d; };
  const chrome = document.getElementById('pc-oswin-chrome');
  const cr = chrome ? chrome.getBoundingClientRect() : null;
  const out = [];
  for(const b of document.querySelectorAll('.vmc-bar button')){
    const r = b.getBoundingClientRect(), cx = r.left + r.width / 2, cy = r.top + r.height / 2;
    const hit = document.elementFromPoint(cx, cy);
    out.push({ c: b.dataset.c, l: r.left, t: r.top, r: r.right, b: r.bottom,
      underChrome: !!(cr && r.top < cr.bottom && r.bottom > cr.top && r.left < cr.right && r.right > cr.left),
      offscreen: r.left < 0 || r.top < 0 || r.right > innerWidth + 0.5 || r.bottom > innerHeight + 0.5 || !r.width,
      hit: !!(hit && (hit === b || b.contains(hit))), drag: inDrag(cx, cy) });
  }
  const scr = document.querySelector('.vmc-screen').getBoundingClientRect();
  return { buttons: out, zoom: parseFloat(getComputedStyle(document.body).zoom) || 1,
           chromeBottom: cr ? cr.bottom : 0, screen: { t: scr.top, h: scr.height } };
})()"""

# (label, popped-out window?, --ui-scale, viewport w, viewport h)
CASES = [
    ("laptop window .77", True, 0.77, 1100, 760),
    ("laptop window .67", True, 0.67, 1000, 700),
    ("4K desk window 1.25", True, 1.25, 3053, 2205),
    ("4K desk window 1.5", True, 1.5, 2400, 1600),
    ("4K desk maximised 1.25", True, 1.25, 3840, 2500),
    ("small window 1.0", True, 1.0, 640, 480),
    ("plain page 1920", False, None, 1920, 1080),
    ("plain page 3840x2560", False, None, 3840, 2560),
    ("phone 390", False, None, 390, 844),
]


class H(SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/vmc-popped", "/vmc-plain"):
            body = (PAGE % {"cls": "pc-oswin" if path == "/vmc-popped" else "",
                            "chrome": CHROME if path == "/vmc-popped" else ""}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(body)
            return
        super().do_GET()


async def cdp(ws, state, method, params=None):
    state["id"] += 1
    mid = state["id"]
    await ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
    while True:
        m = json.loads(await asyncio.wait_for(ws.recv(), 60))
        if m.get("id") == mid:
            if "error" in m:
                raise RuntimeError(m["error"])
            return m.get("result", {})


async def js(ws, state, expr):
    r = await cdp(ws, state, "Runtime.evaluate", {"expression": expr, "returnByValue": True, "awaitPromise": True})
    if "exceptionDetails" in r:
        raise RuntimeError(r["exceptionDetails"])
    return r["result"].get("value")


async def run(base):
    import websockets
    pages = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json").read())
    page = next(p for p in pages if p.get("type") == "page")
    fails = []
    async with websockets.connect(page["webSocketDebuggerUrl"], max_size=50_000_000) as ws:
        st = {"id": 0}
        await cdp(ws, st, "Page.enable")
        for label, popped, scale, w, h in CASES:
            await cdp(ws, st, "Emulation.setDeviceMetricsOverride",
                      {"width": w, "height": h, "deviceScaleFactor": 1, "mobile": w < 600})
            await cdp(ws, st, "Page.navigate", {"url": base + ("/vmc-popped" if popped else "/vmc-plain")})
            for _ in range(100):
                try:
                    if await js(ws, st, "window.__ready===true && document.readyState==='complete'"):
                        break
                except RuntimeError:
                    pass
                await asyncio.sleep(0.1)
            if scale is not None:
                # What oswin.js adopt() does: the desktop's computed zoom lands on <html> as --ui-scale.
                await js(ws, st, f"document.documentElement.style.setProperty('--ui-scale','{scale}')")
            await asyncio.sleep(0.15)
            m = await js(ws, st, MEASURE)
            for b in m["buttons"]:
                for key, name in (("underChrome", "hidden-under-chrome"), ("offscreen", "off-screen"),
                                  ("drag", "in-drag-region")):
                    if b[key]:
                        fails.append(f"{label}: [{b['c']}] {name} (box {b['l']:.0f},{b['t']:.0f}-{b['r']:.0f},{b['b']:.0f}; "
                                     f"title bar ends at {m['chromeBottom']:.0f}, zoom {m['zoom']})")
                if not b["hit"]:
                    fails.append(f"{label}: [{b['c']}] not-hit (something else is on top of it)")
            if m["screen"]["h"] < 50:
                fails.append(f"{label}: the VM screen area is {m['screen']['h']:.0f}px tall")
            print(f"  {label:26s} zoom {m['zoom']:<5} title bar→{m['chromeBottom']:>5.0f}  "
                  f"toolbar top {min(b['t'] for b in m['buttons']):>6.1f}  screen {m['screen']['h']:.0f}px")
    return fails


def main():
    chrome = shutil.which("google-chrome-stable") or shutil.which("google-chrome") or (
        "/opt/google/chrome/chrome" if os.path.exists("/opt/google/chrome/chrome") else None)
    if not chrome:
        print("SKIP: no Chrome")
        return 2
    try:
        import websockets  # noqa: F401
    except ImportError:
        print("SKIP: websockets is not installed")
        return 2
    srv = ThreadingHTTPServer(("127.0.0.1", 0), partial(H, directory=ROOT))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    subprocess.run(["rm", "-rf", PROFILE], check=False)
    proc = subprocess.Popen([chrome, "--headless=new", "--no-sandbox", "--disable-gpu", f"--remote-debugging-port={PORT}",
                             f"--user-data-dir={PROFILE}", "about:blank"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(100):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/version", timeout=1)
                break
            except Exception:
                import time
                time.sleep(0.1)
        fails = asyncio.run(run(f"http://127.0.0.1:{srv.server_port}"))
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
        srv.shutdown()
        subprocess.run(["rm", "-rf", PROFILE], check=False)
    if fails:
        print("FAIL")
        for f in fails:
            print("  " + f)
        return 1
    print("PASS: the console toolbar is visible, uncovered and outside every drag region at every scale")
    return 0


if __name__ == "__main__":
    sys.exit(main())
