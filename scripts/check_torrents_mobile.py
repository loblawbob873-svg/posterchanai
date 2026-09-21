#!/usr/bin/env python3
"""EVERY TORRENTS BUTTON MUST BE ON THE SCREEN AT PHONE WIDTH.

    venv-unified/bin/python scripts/check_torrents_mobile.py
    venv-unified/bin/python scripts/check_torrents_mobile.py --simulate <bug>

Reported from Android: "when on downloading tab, buttons no longer fit on screen for that row".
check_torrents_show_what_you_are_downloading.py runs the view WITHOUT client.css, at 1280px, so it
could never see a layout — and check_client_mobile.py never opens Torrents at all.

This lifts the same shipped torrents region out of app.js (via that check's lifter — never a copy),
loads the REAL client.css and the REAL torrents.js (which adds 📡 Feeds to the tab strip and
🗂 Files to every row), and at 360/390/412px asserts, for every button on the Downloads tab — the
tab strip and every torrent row — and on the Nostr tab's cards:

  offscreen-button   a button whose box ends past the viewport's right edge (or starts left of 0),
                     including one parked off the end of the strip's own sideways scroller: a
                     phone has no scrollbar there, so it is unreachable in practice.
  page-overflow      the document itself scrolls sideways.
  missing-button     an expected control (Add torrent, Feeds, Files, Pause, Remove) is not drawn.

  --simulate strip-nowrap   put back the one-line tab strip that pushed Add torrent off screen

Exit 0 = clean, 1 = regressions, 2 = could not run.
"""
import argparse
import asyncio
import functools
import http.server
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CDP_PORT = int(os.environ.get("PC_CHECK_PORT") or 9495)
PROFILE = os.environ.get("PC_CHECK_PROFILE") or "/tmp/pc-torrents-mobile-check"
WIDTHS = [(360, 780), (390, 844), (412, 915)]
SIMULATIONS = ("strip-nowrap",)

_spec = importlib.util.spec_from_file_location(
    "tor_check", os.path.join(ROOT, "scripts", "check_torrents_show_what_you_are_downloading.py"))
tor = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tor)

SIM_CSS = {
    # What the strip was before: one sticky row that scrolls sideways, buttons at its far end.
    "strip-nowrap": ".tor-tabs{flex-wrap:nowrap!important}"
                    ".tor-tabs .tor-acts{display:contents!important}",
}

PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="stylesheet" href="/static/css/client.css">
<style>%s</style>
</head><body>
<main class="feed" id="feed"></main>
<script>
%s
%s
window.__PC = { enc, toast, uiConfirm, uiPrompt };
window.__T = { renderTorrents, _torRefresh, _torRow, get tab(){ return _torTab; },
               set view(v){ VIEW = v; }, get view(){ return VIEW; } };
window.__ready = true;
</script>
<script src="/static/js/client/torrents.js"></script>
</body></html>
"""


def page(sim):
    js = tor.lifted_app_js(None).replace("</", "<\\/")
    return PAGE % (SIM_CSS.get(sim, ""), tor.PRELUDE.replace("</", "<\\/"), js)


def serve(html):
    class H(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            if self.path.split("?")[0] in ("/", "/index.html"):
                raw = html.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
                return
            return super().do_GET()

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(H, directory=ROOT))
    srv.handle_error = lambda *a: None        # a missing /logo.png is not this check's business
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


MEASURE = r"""(async()=>{
  const vw = document.documentElement.clientWidth;
  const out = [];
  const btns = [...document.querySelectorAll(%s)];
  for (const b of btns){
    const cs = getComputedStyle(b);
    if (cs.display === 'none' || cs.visibility === 'hidden') continue;
    const r = b.getBoundingClientRect();
    if (!r.width) continue;
    const label = (b.id ? '#'+b.id+' ' : '') + (b.textContent||b.getAttribute('aria-label')||'').trim().slice(0,24);
    if (r.right > vw + 0.5 || r.left < -0.5)
      out.push(label + ' spans ' + Math.round(r.left) + '..' + Math.round(r.right) + ' of ' + vw);
  }
  return { bad: out, n: btns.length,
           page: document.documentElement.scrollWidth - vw,
           labels: btns.map(b=>(b.id||'') + ':' + (b.textContent||'').trim().slice(0,14)) };
})()"""


async def drive(port):
    import websockets
    td = PROFILE if os.environ.get("PC_CHECK_PROFILE") else tempfile.mkdtemp(prefix="pc-tormob-")
    shutil.rmtree(td, ignore_errors=True)
    os.makedirs(td, exist_ok=True)
    chrome = next((shutil.which(x) for x in
                   ("google-chrome-stable", "chromium", "google-chrome", "chromium-browser")
                   if shutil.which(x)), None)
    if not chrome:
        print("SKIP  no Chrome on this box")
        return 2
    proc = subprocess.Popen(
        [chrome, "--headless=new", "--disable-gpu", "--no-sandbox",
         "--remote-debugging-port=%d" % CDP_PORT, "--user-data-dir=%s" % td, "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        tab = None
        for _ in range(60):
            try:
                tabs = json.load(urllib.request.urlopen("http://127.0.0.1:%d/json/list" % CDP_PORT))
                tab = next(t for t in tabs if t.get("type") == "page")
                break
            except Exception:
                await asyncio.sleep(0.25)
        if not tab:
            print("SKIP  Chrome never opened a debuggable tab")
            return 2
        async with websockets.connect(tab["webSocketDebuggerUrl"], max_size=1 << 25) as ws:
            n = [0]

            async def call(method, params=None):
                n[0] += 1
                await ws.send(json.dumps({"id": n[0], "method": method, "params": params or {}}))
                while True:
                    msg = json.loads(await ws.recv())
                    if msg.get("id") == n[0]:
                        if msg.get("error"):
                            raise RuntimeError("%s: %s" % (method, msg["error"]))
                        return msg.get("result") or {}

            async def js(expr):
                r = await call("Runtime.evaluate",
                               {"expression": expr, "returnByValue": True, "awaitPromise": True})
                if r.get("exceptionDetails"):
                    d = (r["exceptionDetails"].get("exception") or {}).get("description")
                    return {"__throw": str(d or r["exceptionDetails"].get("text"))}
                return (r.get("result") or {}).get("value")

            await call("Runtime.enable")
            await call("Page.enable")
            bad = []
            seed = json.dumps(tor.SEED)
            evs = json.dumps(tor.NOSTR_EVENTS)
            for w, h in WIDTHS:
                await call("Emulation.setDeviceMetricsOverride",
                           {"width": w, "height": h, "deviceScaleFactor": 2, "mobile": True})
                await call("Page.navigate", {"url": "http://127.0.0.1:%d/" % port})
                for _ in range(80):
                    if await js("!!window.__ready && !!window.PCTorrents") is True:
                        break
                    await asyncio.sleep(0.1)
                else:
                    print("FAIL  the page never booted at %dpx" % w)
                    return 1
                r = await js("""(async()=>{
                  __srv.status=200; __srv.torrents=%s; __T.view='torrents';
                  await __T.renderTorrents();
                  await new Promise(r=>setTimeout(r,250));   // torrents.js attaches on rAF
                  return 1; })()""" % seed)
                if isinstance(r, dict) and r.get("__throw"):
                    print("FAIL  rendering threw: %s" % r["__throw"])
                    return 1
                m = await js(MEASURE % json.dumps(".tor-tabs button, .tm-item button"))
                for want in ("tm-add", "tmx-feeds"):
                    if not any(x.startswith(want + ":") for x in m["labels"]):
                        bad.append(("missing-button", "%dpx: #%s not drawn" % (w, want)))
                for want in ("Files", "Pause", "Remove"):
                    if sum(1 for x in m["labels"] if want in x) < 1:
                        bad.append(("missing-button", "%dpx: no %s button in any row" % (w, want)))
                for b in m["bad"]:
                    bad.append(("offscreen-button", "%dpx downloads: %s" % (w, b)))
                if m["page"] > 1:
                    bad.append(("page-overflow", "%dpx: the page scrolls %dpx sideways" % (w, m["page"])))

                # The 2s in-place repaint rebuilds each row; torrents.js must re-add Files and the
                # row must still fit afterwards.
                await js("""(async()=>{ await __T._torRefresh(false);
                  await new Promise(r=>setTimeout(r,250)); return 1; })()""")
                m = await js(MEASURE % json.dumps(".tm-item button"))
                for b in m["bad"]:
                    bad.append(("offscreen-button", "%dpx after repaint: %s" % (w, b)))

                await js("""(async()=>{ __relayEvents=%s;
                  const nt=[...document.querySelectorAll('.tor-tabs .ntab')].find(b=>b.dataset.tt==='nostr');
                  nt.click(); await new Promise(r=>setTimeout(r,300)); return 1; })()""" % evs)
                m = await js(MEASURE % json.dumps(".tor-tabs button, .tor-card button, .tor-card a.btn"))
                for b in m["bad"]:
                    bad.append(("offscreen-button", "%dpx nostr: %s" % (w, b)))
                if m["page"] > 1:
                    bad.append(("page-overflow", "%dpx nostr: page scrolls %dpx sideways" % (w, m["page"])))

            if bad:
                print("FAIL  %d problem(s)" % len(bad))
                for rule, d in bad:
                    print("  [%s] %s" % (rule, d))
                return 1
            print("OK  every Torrents button is on screen at %s px"
                  % "/".join(str(w) for w, _ in WIDTHS))
            return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--simulate", choices=SIMULATIONS)
    a = ap.parse_args()
    if importlib.util.find_spec("websockets") is None:
        print("SKIP  websockets not installed")
        return 2
    srv, port = serve(page(a.simulate))
    try:
        rc = asyncio.run(drive(port))
    finally:
        srv.shutdown()
    if a.simulate:
        if rc == 1:
            print("simulation %r was caught, as it must be" % a.simulate)
            return 0
        if rc == 2:
            return 2
        print("VACUOUS  simulation %r passed" % a.simulate)
        return 1
    return rc


if __name__ == "__main__":
    sys.exit(main())
