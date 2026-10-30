#!/usr/bin/env python3
"""System Settings → Window effects, driven in a real headless Chrome against the SHIPPED os.js + winfx.js.

"we need the window effects on posterchan os, wobbly windows, exploding windows, etc. its own settings menu"
(2026-10-29). The page chooses among Wayfire's effects through pc-window-effects (tests/test_window_effects_helper.py
runs the helper against a fake compositor); here a stateful fake of `window.pcWindowEffects` (the preload bridge --
Electron cannot be driven here) records what the page asks for:

  nav         a Window effects page in the nav and the phone's category list, alone on screen when open
  shown       it draws what the helper OFFERS (Explode, Burn ...), and the current choice selected
  preset      "Explosive" sends shatter + burn + wobbly in ONE call, and the button reads as chosen
  change      changing one select sends the whole choice with that one field changed
  wobbly      the wobbliness choice is disabled while wobbly windows are off, enabled when switched on
  fire        Burn shows a colour picker and Rainbow fire; Rainbow disables the single colour
  warn        the helper's own sentence (e.g. saved, applies at the next sign-in) is shown
  absent      no helper (Windows, another Linux, the web) = no Window effects page at all
  no-native   not one window.confirm/alert/prompt; the page does not scroll sideways

Exit 0 = clean, 1 = problems (printed), 2 = could not run (no Chrome / websockets).
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

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from check_os_desktop import PAGE, ROOT  # noqa: E402  (the shipped desktop, one definition)

PORT = int(os.environ.get("PC_CHECK_PORT") or 9487)
PROFILE = os.environ.get("PC_CHECK_PROFILE") or "/tmp/pc-os-window-effects"

STUBS = r"""<script>
window.__native = [];
window.confirm = t => { window.__native.push('confirm'); return true; };
window.alert = t => { window.__native.push('alert'); };
window.prompt = t => { window.__native.push('prompt'); return null; };
const W = window.__wfx = { calls: [], warn: '', chosen: { open:'zoom', close:'zoom', minimize:'squeezimize', speed:'normal', wobbly:false, wobbliness:'medium' } };
const OFFER = { open:['none','fade','zoom','fire','zap','spin','shatter','vortex','helix','blinds','melt'],
  close:['none','fade','zoom','fire','zap','spin','shatter','vortex','helix','blinds','melt'],
  minimize:['none','squeezimize','fade','zoom','zap','spin'], speed:['slow','normal','fast'], wobbliness:['low','medium','high'], wobbly:true };
const answer = () => ({ available:true, offered:OFFER, chosen:Object.assign({}, W.chosen), running:Object.assign({}, W.chosen), error:W.warn });
window.pcWindowEffects = {
  available: () => W.here !== false,
  status: async () => answer(),
  set: async (c) => { W.calls.push(JSON.parse(JSON.stringify(c))); W.chosen = Object.assign({}, W.chosen, c); return answer(); },
};
</script>
<script src="/static/js/client/os.js"></script>"""

DRIVE = r"""(async () => {
  const out = {}, sleep = ms => new Promise(r => setTimeout(r, ms)), W = window.__wfx;
  const $ = s => document.querySelector(s);
  const until = async (fn, ms) => { const t = Date.now() + (ms || 8000);
    while (Date.now() < t) { try { if (fn()) return true; } catch (_) {} await sleep(100); } return false; };
  const pane = () => $('[data-settings-page="effects"]');
  const text = () => (pane() || {}).textContent || '';
  PCOS.enter(); await sleep(150);
  PCOS.openSystemSettings(); await sleep(300);
  const nav = $('.os-set-nav [data-page="effects"]');
  out.nav = !!nav; out.mobileOption = !!$('[data-settings-mobile] option[value="page:effects"]');
  if (!nav) return out;
  nav.click(); await until(() => $('[data-wfx="close"]'), 5000);
  out.shown = { text: text(), alone: document.querySelectorAll('[data-settings-page]:not([hidden])').length === 1,
    closeOptions: [...document.querySelectorAll('[data-wfx="close"] option')].map(o => o.textContent),
    closeValue: $('[data-wfx="close"]').value, wobblinessDisabled: $('[data-wfx="wobbliness"]').disabled };
  const preset = [...document.querySelectorAll('[data-wfx-preset]')].find(b => b.textContent === 'Explosive');
  preset.click(); await until(() => W.calls.length === 1, 3000); await sleep(200);
  out.preset = { calls: W.calls.slice(), on: (([...document.querySelectorAll('[data-wfx-preset].on')].map(b => b.textContent))),
    closeValue: $('[data-wfx="close"]').value, wobbly: $('[data-wfx="wobbly"]').checked, wobblinessDisabled: $('[data-wfx="wobbliness"]').disabled,
    fireRows: !!$('[data-wfx="fire_color"]') };
  const sel = $('[data-wfx="open"]'); sel.value = 'vortex'; sel.dispatchEvent(new Event('change'));
  await until(() => W.calls.length === 2, 3000); await sleep(200);
  out.change = W.calls[1]; out.fireRowsGone = !$('[data-wfx="fire_color"]');
  const sw = $('[data-wfx="wobbly"]'); sw.checked = false; sw.dispatchEvent(new Event('change'));
  await until(() => W.calls.length === 3, 3000); await sleep(200);
  out.off = { call: W.calls[2], wobblinessDisabled: $('[data-wfx="wobbliness"]').disabled };
  const cl = $('[data-wfx="close"]'); cl.value = 'fire'; cl.dispatchEvent(new Event('change'));
  await until(() => W.calls.length === 4 && $('[data-wfx="fire_rainbow"]'), 3000); await sleep(200);
  out.fireBack = !!$('[data-wfx="fire_color"]');
  const fc = $('[data-wfx="fire_color"]'); fc.value = '#00f0ff'; fc.dispatchEvent(new Event('change'));
  await until(() => W.calls.length === 5, 3000); await sleep(150);
  const rb = $('[data-wfx="fire_rainbow"]'); rb.checked = true; rb.dispatchEvent(new Event('change'));
  await until(() => W.calls.length === 6, 3000); await sleep(150);
  out.fire = { colour: W.calls[4], rainbow: W.calls[5], colourDisabled: $('[data-wfx="fire_color"]').disabled };
  W.warn = 'Saved; it applies at the next sign-in (no compositor).';
  const sp = $('[data-wfx="speed"]'); sp.value = 'fast'; sp.dispatchEvent(new Event('change'));
  await until(() => $('[data-wfx-warn]'), 3000);
  out.warn = (($('[data-wfx-warn]') || {}).textContent || '');
  out.overflow = Math.max(0, pane().scrollWidth - pane().clientWidth);
  W.here = false;
  $('.os-set-nav [data-page="displays"]').click(); await sleep(400);
  out.notHereNav = !!$('.os-set-nav [data-page="effects"]');
  delete window.pcWindowEffects;
  $('.os-set-nav [data-page="displays"]').click(); await sleep(400);
  out.absentNav = !!$('.os-set-nav [data-page="effects"]');
  out.native = window.__native.slice();
  return out;
})()"""


def verdict(o):
    p = []
    if not o.get("nav") or not o.get("mobileOption"):
        return [f"no Window effects page in System Settings (nav={o.get('nav')} mobile={o.get('mobileOption')})"]
    s = o.get("shown", {})
    if not s.get("alone"):
        p.append("the Window effects page is not alone on screen")
    for need in ("Wobbly windows", "Explosive", "Closing a window"):
        if need not in s.get("text", ""):
            p.append(f"the page does not show {need!r}")
    if "Explode" not in s.get("closeOptions", []) or "Burn" not in s.get("closeOptions", []) or s.get("closeValue") != "zoom":
        p.append(f"the close choices are not what the helper offers, or the current one is not selected: {s}")
    if not s.get("wobblinessDisabled"):
        p.append("how-much-they-wobble is offered while wobbly windows are off")
    pr = o.get("preset", {})
    c = (pr.get("calls") or [{}])[0]
    if len(pr.get("calls", [])) != 1 or c.get("close") != "shatter" or c.get("open") != "fire" or c.get("wobbly") is not True:
        p.append(f"Explosive did not send shatter + burn + wobbly in one call: {pr.get('calls')}")
    if pr.get("on") != ["Explosive"] or pr.get("closeValue") != "shatter" or not pr.get("wobbly") or pr.get("wobblinessDisabled"):
        p.append(f"after Explosive the page does not show it: {pr}")
    ch = o.get("change") or {}
    if ch.get("open") != "vortex" or ch.get("close") != "shatter" or ch.get("wobbly") is not True:
        p.append(f"changing one select did not send the whole choice with that field changed: {ch}")
    off = o.get("off", {})
    if (off.get("call") or {}).get("wobbly") is not False or not off.get("wobblinessDisabled"):
        p.append(f"switching wobbly off: {off}")
    if not pr.get("fireRows") or not o.get("fireRowsGone") or not o.get("fireBack"):
        p.append(f"Burn's colour rows do not follow the choice of Burn: preset={pr.get('fireRows')} gone={o.get('fireRowsGone')} back={o.get('fireBack')}")
    fi = o.get("fire", {})
    if (fi.get("colour") or {}).get("fire_color") != "#00f0ff" or (fi.get("rainbow") or {}).get("fire_rainbow") is not True \
            or not fi.get("colourDisabled"):
        p.append(f"fire colour / rainbow: {fi}")
    if "next sign-in" not in o.get("warn", ""):
        p.append(f"the helper's own warning is not shown: {o.get('warn')!r}")
    if o.get("overflow", 0) > 1:
        p.append(f"the page scrolls sideways by {o['overflow']}px")
    if o.get("notHereNav") or o.get("absentNav"):
        p.append("a desktop without the helper still shows a Window effects page")
    if o.get("native"):
        p.append(f"native dialogs were used: {o.get('native')} (they wedge the desktop's focus)")
    return p


async def drive(url):
    import websockets
    shutil.rmtree(PROFILE, ignore_errors=True)
    chrome = (shutil.which("google-chrome-stable") or shutil.which("google-chrome")
              or shutil.which("chromium"))
    if not chrome:
        print("SKIP  no Chrome")
        return 2
    proc = subprocess.Popen(
        [chrome, "--headless=new", "--disable-gpu", "--no-sandbox", "--window-size=1024,768",
         f"--remote-debugging-port={PORT}", f"--user-data-dir={PROFILE}", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        page = None
        for _ in range(60):
            try:
                tabs = json.load(urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/list"))
                page = [t for t in tabs if t["type"] == "page"][0]
                break
            except Exception:
                await asyncio.sleep(.5)
        if not page:
            print("SKIP  could not start Chrome")
            return 2
        async with websockets.connect(page["webSocketDebuggerUrl"], max_size=64 * 1024 * 1024) as ws:
            ident = [0]

            async def call(method, params=None):
                ident[0] += 1
                mine = ident[0]
                await ws.send(json.dumps({"id": mine, "method": method, "params": params or {}}))
                while True:
                    msg = json.loads(await ws.recv())
                    if msg.get("id") == mine:
                        return msg.get("result", {})

            await call("Runtime.enable")
            await call("Page.enable")
            await call("Emulation.setDeviceMetricsOverride",
                       {"width": 1024, "height": 768, "deviceScaleFactor": 1, "mobile": False})
            await call("Page.navigate", {"url": url})
            for _ in range(120):
                r = await call("Runtime.evaluate", {"expression": "window.__ready === true && !!window.PCOS",
                                                    "returnByValue": True})
                if r.get("result", {}).get("value"):
                    break
                await asyncio.sleep(.1)
            else:
                print("SKIP  the desktop module never loaded")
                return 2
            r = await call("Runtime.evaluate", {"expression": DRIVE, "awaitPromise": True, "returnByValue": True})
            if r.get("exceptionDetails"):
                print("FAIL  the drive script threw: " + json.dumps(r["exceptionDetails"])[:800])
                return 1
            out = r.get("result", {}).get("value") or {}
            shots = os.environ.get("PC_CHECK_SHOTS")
            if shots:
                import base64
                r = await call("Page.captureScreenshot", {"format": "png"})
                with open(os.path.join(shots, "window-effects-last.png"), "wb") as fh:
                    fh.write(base64.b64decode(r.get("data", "")))
    finally:
        proc.terminate()
        try:
            proc.wait(3)
        except subprocess.TimeoutExpired:
            proc.kill()
        shutil.rmtree(PROFILE, ignore_errors=True)

    problems = verdict(out)
    if os.environ.get("PC_DEBUG"):
        print(json.dumps(out, indent=1)[:6000])
    if problems:
        print("FAIL " + "\nFAIL ".join(problems))
        return 1
    print("OK  System Settings → Window effects: offered choices, Explosive preset, one change, wobbly switch, "
          "fire colour + rainbow, helper warning, no sideways scroll, absent helper")
    return 0


def main():
    try:
        import websockets  # noqa: F401
    except ImportError:
        print("SKIP  websockets not installed")
        return 2
    import http.server
    marker = '<script src="/static/js/client/os.js"></script>'
    if marker not in PAGE:
        print("SKIP  check_os_desktop's page no longer loads os.js the way this check injects into")
        return 2
    tmp = tempfile.mkdtemp(prefix="oswinfx-")
    with open(os.path.join(tmp, "index.html"), "w") as fh:
        fh.write(PAGE.replace(marker, STUBS, 1))

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
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
