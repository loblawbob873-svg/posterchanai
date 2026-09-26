#!/usr/bin/env python3
"""How long a PHONE stares at a blank shell before the client can boot.

Reported twice as "poster.place looks stuck loading on mobile": the raw HTML's bottom nav, no header,
no feed, Chrome's progress bar still running. The client boots on DOMContentLoaded, which waits for
every one of ~70 blocking <script> tags, and a single `<meta http-equiv="Content-Security-Policy">`
in <head> had switched off Chrome's preload scanner, so those scripts were fetched ONE AT A TIME,
each a full round trip after the last. 15.7 s on 4G, ~60 s on 3G, 13 s on a repeat visit that
transferred 38 KB. Every other check loads pages over loopback, where a round trip costs nothing,
which is why none of them could see it.

This check puts the round trip back. It renders the SHIPPED shell (render_client_shell, the same
function the route calls) and serves it with this checkout's static/ behind a fixed delay per
request, then has Chrome load it and reports:

  concurrency   the most script requests in flight at once. 1 means the preloader is off.
  boot          time to DOMContentLoaded, compared with what the scripts would cost one at a time
                (count x delay). Parallel loading is a small fraction of that; serial is all of it.

    venv-unified/bin/python scripts/check_client_load_speed.py

The live variant (scripts/check_client_load_speed_live.py, `./test.sh --live URL`) measures a real
instance under a throttled 4G profile, cold and repeat.

Exit 0 = fast enough, 1 = regression (printed), 2 = could not run (no Chrome / websockets).
"""
import asyncio
import http.server
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORT = int(os.environ.get("PC_CHECK_PORT") or 9486)
PROFILE = os.environ.get("PC_CHECK_PROFILE") or "/tmp/pc-load-speed-check"
DELAY = 0.15            # seconds added to every request: a phone's round trip, roughly
MIN_CONCURRENCY = 4     # Chrome runs 6 per host over HTTP/1.1; the preloader-off shape is exactly 1
MAX_SERIAL_FRACTION = 0.5   # measured 0.21 with the preloader on, 1.0 with it off


def find_chrome():
    for c in ("google-chrome-stable", "google-chrome", "chromium", "chromium-browser"):
        if shutil.which(c):
            return c
    return None


def render_shell():
    """The HTML the route actually serves, rendered by the route's own function."""
    sys.path.insert(0, ROOT)
    from starlette.requests import Request
    from app.routers import client as client_router
    client_router._default_theme = lambda db: "cyberpunk"
    scope = {"type": "http", "method": "GET", "path": "/", "raw_path": b"/", "query_string": b"",
             "scheme": "http", "server": ("127.0.0.1", 80), "client": ("127.0.0.1", 1),
             "root_path": "", "headers": [(b"host", b"127.0.0.1"), (b"x-forwarded-proto", b"https")]}
    resp = asyncio.run(client_router.render_client_shell(Request(scope), db=None))
    # Rendered as https (so any https-only markup is present), served over http: drop nothing but the
    # header, which would upgrade the loopback URLs to an https:// port that does not exist.
    return resp.body


def serve(html: bytes):
    class H(http.server.SimpleHTTPRequestHandler):
        def __init__(s, *a, **k):
            super().__init__(*a, directory=ROOT, **k)

        def log_message(s, *a):
            pass

        def do_GET(s):
            time.sleep(DELAY)
            if s.path == "/":
                s.send_response(200)
                s.send_header("Content-Type", "text/html; charset=utf-8")
                s.send_header("Content-Length", str(len(html)))
                s.end_headers()
                s.wfile.write(html)
                return
            s.path = s.path.split("?")[0]
            if not s.path.startswith("/static/"):
                s.send_response(404)
                s.end_headers()
                return
            return super().do_GET()

    class Quiet(http.server.ThreadingHTTPServer):
        def handle_error(self, request, client_address):
            pass   # Chrome closing a keep-alive mid-body when the page is done is not a finding

    srv = Quiet(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


async def measure(ws_url, url, network=None, runs=("cold",), settle=None):
    """Load `url` once per run label; return [{label, boot, concurrency, scripts, painted}]."""
    import websockets
    out = []
    async with websockets.connect(ws_url, max_size=None) as ws:
        n = [0]
        waiters = {}
        inflight = {}
        stats = {"max": 0, "scripts": 0}

        async def call(m, p=None):
            n[0] += 1
            f = asyncio.get_event_loop().create_future()
            waiters[n[0]] = f
            await ws.send(json.dumps({"id": n[0], "method": m, "params": p or {}}))
            return await f

        async def reader():
            async for raw in ws:
                d = json.loads(raw)
                if d.get("id") in waiters:
                    waiters.pop(d["id"]).set_result(d.get("result") or {})
                    continue
                m, p = d.get("method"), d.get("params", {})
                if m == "Network.requestWillBeSent" and p.get("type") == "Script":
                    inflight[p["requestId"]] = 1
                    stats["scripts"] += 1
                    stats["max"] = max(stats["max"], len(inflight))
                elif m in ("Network.loadingFinished", "Network.loadingFailed"):
                    inflight.pop(p.get("requestId"), None)

        asyncio.ensure_future(reader())
        await call("Network.enable")
        await call("Page.enable")
        await call("Runtime.enable")
        await call("Emulation.setDeviceMetricsOverride",
                   {"width": 412, "height": 915, "deviceScaleFactor": 2, "mobile": True})
        if network:
            await call("Network.emulateNetworkConditions", network)
        for label in runs:
            inflight.clear()
            stats.update(max=0, scripts=0)
            t0 = time.time()
            await call("Page.navigate", {"url": url})
            boot = painted = None
            while time.time() - t0 < 180:
                await asyncio.sleep(0.25)
                r = await call("Runtime.evaluate", {"returnByValue": True, "expression": (
                    "JSON.stringify([document.readyState,"
                    " !!(window.__PC && document.querySelector('#feed') &&"
                    "    document.querySelector('#feed').children.length)])")})
                try:
                    state, shown = json.loads(r["result"]["value"])
                except Exception:
                    continue
                if boot is None and state != "loading":
                    boot = time.time() - t0
                    if not settle:
                        break
                if shown:
                    painted = time.time() - t0
                    break
            out.append({"label": label, "boot": boot, "painted": painted,
                        "concurrency": stats["max"], "scripts": stats["scripts"]})
            if settle:
                await asyncio.sleep(settle)
    return out


def launch_chrome(chrome):
    shutil.rmtree(PROFILE, ignore_errors=True)
    proc = subprocess.Popen([chrome, "--headless=new", "--disable-gpu", "--no-sandbox",
                             f"--remote-debugging-port={PORT}", f"--user-data-dir={PROFILE}",
                             "about:blank"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(75):
        try:
            tabs = json.load(urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json", timeout=2))
            return proc, [t for t in tabs if t["type"] == "page"][0]["webSocketDebuggerUrl"]
        except Exception:
            time.sleep(0.2)
    proc.terminate()
    return None, None


def main():
    chrome = find_chrome()
    if not chrome:
        print("SKIP: no Chrome/Chromium on PATH")
        return 2
    try:
        import websockets  # noqa: F401
    except ImportError:
        print("SKIP: python 'websockets' not installed")
        return 2
    try:
        html = render_shell()
    except Exception as e:
        print(f"SKIP: could not render the client shell: {e}")
        return 2
    srv = serve(html)
    proc, ws_url = launch_chrome(chrome)
    if not proc:
        srv.shutdown()
        print("SKIP: Chrome did not expose a debugging endpoint")
        return 2
    try:
        res = asyncio.run(measure(ws_url, f"http://127.0.0.1:{srv.server_address[1]}/"))[0]
    finally:
        proc.terminate()
        srv.shutdown()

    serial = res["scripts"] * DELAY
    print(f"scripts={res['scripts']} concurrency={res['concurrency']} boot={res['boot']:.1f}s "
          f"(one-at-a-time would be ~{serial:.1f}s at {DELAY * 1000:.0f} ms per request)")
    fails = []
    if res["boot"] is None:
        fails.append("the page never reached DOMContentLoaded")
    if res["concurrency"] < MIN_CONCURRENCY:
        fails.append(f"at most {res['concurrency']} script request(s) in flight: the preload scanner is "
                     "off (a CSP <meta>? a script loader?), so the scripts load one after another")
    if res["boot"] and serial and res["boot"] > serial * MAX_SERIAL_FRACTION:
        fails.append(f"boot took {res['boot']:.1f}s, over {MAX_SERIAL_FRACTION:.0%} of the one-at-a-time "
                     f"cost ({serial:.1f}s): on a phone that is a blank screen for most of a minute")
    for f in fails:
        print("FAIL:", f)
    if not fails:
        print("OK: the shell's scripts load in parallel")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
