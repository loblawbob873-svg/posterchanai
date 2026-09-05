#!/usr/bin/env python3
"""A 4K-class panel at output scale 1 must not draw a one-third-size app.

    venv-unified/bin/python scripts/check_hidpi_ui_scale.py

The compositor's outputs run at scale 1 on PosterChanOS, deliberately: an output scale makes
Xwayland render every fullscreen game at the wrong size and then upscales it, which both blurs the
picture and makes DIRECT SCANOUT impossible. Readability is therefore the UI's job, and for this app
that means the `body{zoom}` tiers in client.css — which only ever scaled DOWN. There was no tier
above 1921px at all, so a 3840x2560 monitor was drawn at exactly the size a 1921px one was.

This is measured rather than eyeballed, in a real browser, against the SHIPPED stylesheet and the
SHIPPED os.js, at two viewports:

  small   1920x1080  — the reference, which is drawn at the shipped `zoom:.77` shrink tier.
                       Nothing here may move; that is half the check.
  huge    3840x2560  — the owner's panels.

Assertions:

  no-hidpi-tier      The huge viewport is drawn at the same scale as the small one — i.e. nothing
                     scaled up, which is the bug.
  zoom-zf-disagree   `zoom` and `--zf` are not the same number. Every full-height container is
                     `calc(100dvh / var(--zf))`, so half a pair means containers sized for a zoom
                     the page is not using — measured as a desktop whose top goes off screen.
  regressed-1920     Anything about the 1920px rendering changed.
  scale-not-settable The stored `osUiScale` override does not change what is drawn, or clearing it
                     does not go back to the tier's own default. A number nobody can adjust is the
                     thing this replaced.
  desktop-mismeasured The desktop's own helpers (zf/vwL/vhL) disagree with the browser, so window
                     placement, the icon grid and snapping are all computed against a screen size
                     that does not exist.
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
from check_os_desktop import PAGE, ROOT  # noqa: E402  (the same harness, one page, one os.js)

PORT = int(os.environ.get("PC_CHECK_PORT") or 9491)
PROFILE = os.environ.get("PC_CHECK_PROFILE") or "/tmp/pc-hidpi-scale-profile"

# Everything the two viewports are compared on. Physical pixels throughout (getBoundingClientRect is
# post-zoom), because "how big is it on the glass" is the entire question — a computed font-size in
# layout px is exactly what stayed constant while the screen got four times bigger.
MEASURE = r"""(() => {
  const cs = getComputedStyle(document.body);
  const zoom = parseFloat(cs.zoom || '1') || 1;
  const zf = parseFloat(cs.getPropertyValue('--zf') || '1') || 1;
  const label = document.querySelector('.sidebar .nav .nav-item span');
  const app = document.querySelector('.app');
  const bar = document.querySelector('.os-bar');
  const px = el => { if(!el) return null; const r = el.getBoundingClientRect();
                     return { w: +r.width.toFixed(2), h: +r.height.toFixed(2) }; };
  // Font size on the GLASS: the specified px times whatever zoom is in force.
  const fontPhys = el => el ? +(parseFloat(getComputedStyle(el).fontSize) * zoom).toFixed(2) : null;
  return {
    innerWidth: innerWidth, innerHeight: innerHeight,
    zoom, zf,
    navFont: fontPhys(label), navRow: px(label && label.parentElement),
    appH: app ? +app.getBoundingClientRect().height.toFixed(1) : null,
    taskbar: px(bar),
    // The desktop's own view of the world. It works in LAYOUT pixels and converts with zf(), so
    // these must divide the real viewport exactly — a stale zf() is how a "half screen" snap
    // covered a third of it.
    os: (window.PCOS && PCOS.metrics) ? PCOS.metrics() : null,
  };
})()"""


async def drive(url):
    import websockets  # noqa: F401
    subprocess.run(["rm", "-rf", PROFILE], check=False)
    chrome = (shutil.which("google-chrome-stable") or shutil.which("google-chrome")
              or shutil.which("chromium"))
    if not chrome:
        print("SKIP  no Chrome")
        return 2
    proc = subprocess.Popen(
        [chrome, "--headless=new", "--disable-gpu", "--no-sandbox",
         f"--remote-debugging-port={PORT}", f"--user-data-dir={PROFILE}", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    problems = []
    try:
        page = None
        for _ in range(60):
            try:
                tabs = json.load(urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/list"))
                page = [t for t in tabs if t["type"] == "page"][0]
                break
            except Exception:
                await asyncio.sleep(0.5)
        if not page:
            print("SKIP  could not start Chrome")
            return 2

        async with websockets.connect(page["webSocketDebuggerUrl"],
                                      max_size=64 * 1024 * 1024) as ws:
            n = [0]

            async def call(method, params=None):
                n[0] += 1
                await ws.send(json.dumps({"id": n[0], "method": method, "params": params or {}}))
                while True:
                    msg = json.loads(await ws.recv())
                    if msg.get("id") == n[0]:
                        return msg.get("result")

            async def js(expr, awaited=False):
                r = await call("Runtime.evaluate", {"expression": expr, "returnByValue": True,
                                                    "awaitPromise": awaited})
                if r.get("exceptionDetails"):
                    if os.environ.get("PC_DEBUG"):
                        print("  DEBUG:", json.dumps(r["exceptionDetails"])[:800])
                    return None
                return r["result"].get("value")

            await call("Runtime.enable")
            await call("Page.enable")

            async def load(w, h, stored=None):
                await call("Emulation.setDeviceMetricsOverride",
                           {"width": w, "height": h, "deviceScaleFactor": 1, "mobile": False})
                await call("Page.navigate", {"url": url})
                for _ in range(80):
                    await asyncio.sleep(0.25)
                    if await js("window.__ready === true && !!window.PCOS"):
                        break
                else:
                    return None
                # The stored override is written the way the settings control writes it, then the
                # applier is re-run — the same call the control makes.
                if stored is None:
                    await js("(()=>{const a=JSON.parse(localStorage.getItem('pc_nostr_settings')||'{}');"
                             "delete a.osUiScale;localStorage.setItem('pc_nostr_settings',JSON.stringify(a));"
                             "PCOS.applyUiScale();})()")
                else:
                    await js("(()=>{ClientSettings.set('osUiScale',%s);PCOS.applyUiScale();})()" % stored)
                await asyncio.sleep(0.1)
                # Enter the desktop so the taskbar exists to be measured.
                await js("try{PCOS.enter()}catch(_){}")
                await asyncio.sleep(0.25)
                return await js(MEASURE)

            small = await load(1920, 1080)
            huge = await load(3840, 2560)
            if not small or not huge:
                print("SKIP  the page never finished loading")
                return 2

            print(f"  1920x1080  zoom={small['zoom']} zf={small['zf']} "
                  f"nav label {small['navFont']}px  nav row {small['navRow']['h']}px  "
                  f"taskbar {small['taskbar'] and small['taskbar']['h']}px")
            print(f"  3840x2560  zoom={huge['zoom']} zf={huge['zf']} "
                  f"nav label {huge['navFont']}px  nav row {huge['navRow']['h']}px  "
                  f"taskbar {huge['taskbar'] and huge['taskbar']['h']}px")

            # 1920 is INCLUSIVE in the `max-width:1920px` shrink tier, so the reference is .77 —
            # not 1. Pinned by value: this whole change must be invisible below 3500px.
            if abs(small["zoom"] - 0.77) > 1e-6 or abs(small["zf"] - 0.77) > 1e-6:
                problems.append(("1920px", "regressed-1920",
                                 f"the reference viewport moved: zoom={small['zoom']} zf={small['zf']} "
                                 "(the shipped 821-1920px tier is .77)"))
            if huge["zoom"] <= 1.0001:
                problems.append(("3840px", "no-hidpi-tier",
                                 f"a 3840x2560 panel is still drawn at zoom {huge['zoom']} — the same "
                                 "size as a 1921px screen, at four times the pixels"))
            for tag, m in (("1920px", small), ("3840px", huge)):
                if abs(m["zoom"] - m["zf"]) > 1e-6:
                    problems.append((tag, "zoom-zf-disagree",
                                     f"zoom={m['zoom']} but --zf={m['zf']}"))
                # `.app`'s height is NOT asserted here. Measured in this Chrome, `100dvh` inside a
                # zoomed subtree already resolves in the zoomed space, so the tiers' `/ var(--zf)`
                # compensation cancels it exactly and `.app` renders at innerHeight * zoom at EVERY
                # tier, .77 included. That is pre-existing and identical above and below 3500px, so
                # a browser assertion on it would be pinning a browser version, not this change.
                # The pairing itself is checked as TEXT, where it is unambiguous:
                # tests/client/test_hidpi_ui_scale.py and test_zoom_and_zf_agree.py.
                os_m = m["os"]
                if not os_m:
                    problems.append((tag, "desktop-mismeasured", "PCOS.metrics() answered nothing"))
                else:
                    if abs(os_m["zf"] - m["zoom"]) > 1e-6:
                        problems.append((tag, "desktop-mismeasured",
                                         f"the desktop thinks the zoom is {os_m['zf']}, the page is "
                                         f"drawn at {m['zoom']}"))
                    if abs(os_m["vw"] * m["zoom"] - m["innerWidth"]) > 2 or \
                       abs(os_m["vh"] * m["zoom"] - m["innerHeight"]) > 2:
                        problems.append((tag, "desktop-mismeasured",
                                         f"vwL/vhL say {os_m['vw']}x{os_m['vh']} layout px at zoom "
                                         f"{m['zoom']} on a {m['innerWidth']}x{m['innerHeight']} screen"))
                    if os_m["desk"] and abs(os_m["desk"]["h"] - (m["innerHeight"] - os_m["taskbar"] * m["zoom"])) > 3:
                        problems.append((tag, "desktop-mismeasured",
                                         "the desktop area and the taskbar do not add up to the screen: "
                                         f"{os_m['desk']} + taskbar {os_m['taskbar']}"))

            if huge["navFont"] and small["navFont"] and huge["navFont"] <= small["navFont"] * 1.15:
                problems.append(("3840px", "no-hidpi-tier",
                                 f"the sidebar label is {huge['navFont']}px on the glass at 3840 vs "
                                 f"{small['navFont']}px at 1920 — not enough to be worth the tier"))
            if huge["taskbar"] and small["taskbar"] and \
                    huge["taskbar"]["h"] <= small["taskbar"]["h"] + 4:
                problems.append(("3840px", "no-hidpi-tier",
                                 f"the taskbar is {huge['taskbar']['h']}px tall on a 3840x2560 panel "
                                 f"and {small['taskbar']['h']}px on a 1920x1080 one"))

            # The setting, both ways round.
            picked = await load(3840, 2560, stored="1.75")
            cleared = await load(3840, 2560)
            if picked:
                print(f"  3840x2560  osUiScale=1.75 -> zoom={picked['zoom']} "
                      f"nav label {picked['navFont']}px")
            if not picked or abs(picked["zoom"] - 1.75) > 1e-6:
                problems.append(("3840px", "scale-not-settable",
                                 f"a stored osUiScale of 1.75 drew at zoom "
                                 f"{picked and picked['zoom']}"))
            elif abs(picked["zf"] - 1.75) > 1e-6:
                problems.append(("3840px", "zoom-zf-disagree",
                                 f"the override moved zoom to 1.75 and left --zf at {picked['zf']}"))
            if not cleared or abs(cleared["zoom"] - huge["zoom"]) > 1e-6:
                problems.append(("3840px", "scale-not-settable",
                                 "clearing the stored scale did not go back to the tier's default "
                                 f"({cleared and cleared['zoom']} vs {huge['zoom']})"))
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
        subprocess.run(["rm", "-rf", PROFILE], check=False)

    if problems:
        for where, tag, why in problems:
            print(f"FAIL  {where}  {tag}: {why}")
        return 1
    print("PASS  the app scales up on a 4K-class panel, and the scale is adjustable")
    return 0


def main():
    try:
        import websockets  # noqa: F401
    except ImportError:
        print("SKIP  websockets not installed")
        return 2
    import http.server
    import threading
    tmp = tempfile.mkdtemp(prefix="hidpicheck-")
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
    url = f"http://127.0.0.1:{srv.server_address[1]}/index.html"
    try:
        return asyncio.run(drive(url))
    finally:
        srv.shutdown()


if __name__ == "__main__":
    sys.exit(main())
