#!/usr/bin/env python3
"""Startup apps tiled at login fill the screen the monitor ENDS UP at, not the one it started in.

#163, PosterChanOS: "there is a bug with autostart -> tiling, it looks like it tiles before the monitor
chooses the final resolution at start. All the tiled windows use only about 75 percent of the screen."
At login the monitor comes up in one mode and the saved display layout switches it to its final one a
moment later; os.js laid the startup Grid ONCE, as soon as the apps' windows existed, and nothing tiled
again -- so the grid stayed the size of the mode the session started in.

Loads the SHIPPED os.js over check_os_desktop.py's stub client, in a page whose viewport plays the shell
surface (it is resized with its output on PosterChanOS) and a fake window manager whose arrange() tiles the
compositor's CURRENT output with the shipped desktop/tile.js -- which is what the real one does. Then:

  grows      The screen starts at 75% (1440x900), the startup Grid is laid, the monitor settles at
             1920x1200: the tiles must fill 1920 x (1200 - taskbar).
  hands-off  Same, but the person has dragged one of the tiles before the monitor settles: the layout is
             theirs, and nothing may re-tile it.

The other half (a work area the renderer measured in the EARLIER mode must not shrink an arrange the
compositor runs in the final one) is desktop/wm.js, covered by tests/test_desktop_arrange_real_desk.py.

Exit 0 = clean, 1 = problems, 2 = could not run (no Chrome / websockets).
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

PORT = int(os.environ.get("PC_CHECK_PORT") or 9583)
PROFILE = os.environ.get("PC_CHECK_PROFILE") or "/tmp/pc-os-startup-tiling"
BAR = 48            # the fake taskbar band, in compositor px
VIEWS = ["messages", "notes", "calendar", "contacts"]
START = (1440, 900)  # 75% of the final mode
FINAL = (1920, 1200)

# desktop/tile.js is CommonJS; give it a `module` to fill and keep what it exports.
TILE_SHIM = """<script>window.module={exports:{}};</script><script src="/desktop/tile.js"></script>
<script>window.__tile=window.module.exports;delete window.module;</script>"""

#: The window manager the startup code talks to, faked at the one seam it uses (pcWM.windows/arrange),
#: plus which monitor this renderer is and the per-device settings that ask for a Grid.
SETUP = r"""(() => {
  const views = %(views)s, BAR = %(bar)d;
  ClientSettings.set('startupApps', views);
  ClientSettings.set('startupTiling', { 'DP-1': 'grid' });
  window.pcShell = { outputName: async () => 'DP-1', backgroundOwner: true };
  window.pcDisplays = { status: async () => [{ name: 'DP-1', active: true, primary: true }] };
  window.PCOSWin = Object.assign({}, window.PCOSWin || {}, { enabled: () => true });
  // Every app window opens wherever the compositor put it: small and stacked, as at a real login.
  const rows = views.map((v, i) => ({ id: 100 + i, title: 'PosterChan Window — ' + v,
                                       rect: { x: 40 + i * 30, y: 40 + i * 30, width: 800, height: 600 } }));
  window.__wm = { rows, arranged: [] };
  window.pcWM = Object.assign({}, window.pcWM || {}, {
    windows: async () => rows.map(r => ({ id: r.id, title: r.title, rect: Object.assign({}, r.rect) })),
    // The output is the shell surface (this viewport), measured NOW -- as wm-wayfire.js reads list-outputs.
    arrange: async (layout) => {
      const W = window.innerWidth, H = window.innerHeight;
      const t = __tile.tileRects(layout, rows.length, { x: 0, y: 0, w: W, h: H - BAR });
      rows.forEach((r, i) => { r.rect = { x: t[i].x, y: t[i].y, width: t[i].w, height: t[i].h }; });
      __wm.arranged.push({ layout, W, H });
      return { ok: true, count: rows.length };
    },
  });
  return PCOS.runStartupApps();
})()"""

STATE = """(() => ({ arranged: __wm.arranged.length, W: innerWidth, H: innerHeight,
  rects: __wm.rows.map(r => r.rect) }))()"""


def covers(rects, w, h):
    right = max(r["x"] + r["width"] for r in rects)
    bottom = max(r["y"] + r["height"] for r in rects)
    area = sum(r["width"] * r["height"] for r in rects)
    return right == w and bottom == h and area == w * h


async def rpc(ws, method, params=None, ident=[0]):
    ident[0] += 1
    mine = ident[0]
    await ws.send(json.dumps({"id": mine, "method": method, "params": params or {}}))
    while True:
        msg = json.loads(await ws.recv())
        if msg.get("id") == mine:
            if "error" in msg:
                raise RuntimeError(msg["error"])
            return msg.get("result", {})


async def js(ws, expr):
    got = await rpc(ws, "Runtime.evaluate",
                    {"expression": expr, "awaitPromise": True, "returnByValue": True})
    if got.get("exceptionDetails"):
        raise RuntimeError(got["result"].get("description") or got["exceptionDetails"])
    return got.get("result", {}).get("value")


async def viewport(ws, size):
    w, h = size
    await rpc(ws, "Emulation.setDeviceMetricsOverride",
              {"width": w, "height": h, "screenWidth": w, "screenHeight": h,
               "deviceScaleFactor": 1, "mobile": False})


async def boot(ws, url):
    await viewport(ws, START)
    await rpc(ws, "Page.navigate", {"url": url})
    for _ in range(150):
        try:
            if await js(ws, "window.__ready === true && !!window.PCOS && !!window.__tile"):
                break
        except RuntimeError:
            pass
        await asyncio.sleep(.1)
    else:
        return False
    await js(ws, "PCOS.enter()")
    await asyncio.sleep(.3)
    opened = await js(ws, SETUP % {"views": json.dumps(VIEWS), "bar": BAR})
    if opened != len(VIEWS):
        raise RuntimeError(f"runStartupApps opened {opened!r} apps, expected {len(VIEWS)}")
    for _ in range(300):                      # _tileStartup polls each second, then waits 600ms
        if (await js(ws, STATE))["arranged"] >= 1:
            return True
        await asyncio.sleep(.1)
    raise RuntimeError("the startup Grid was never laid")


async def scenario_grows(ws, url, problems):
    if not await boot(ws, url):
        return "the desktop module never loaded"
    first = await js(ws, STATE)
    if not covers(first["rects"], START[0], START[1] - BAR):
        problems.append(f"grows: the first Grid does not fill the starting screen: {first}")
    await asyncio.sleep(1.0)
    await viewport(ws, FINAL)                 # the monitor settles on its final mode
    end = None
    for _ in range(120):
        end = await js(ws, STATE)
        if covers(end["rects"], FINAL[0], FINAL[1] - BAR):
            break
        await asyncio.sleep(.1)
    else:
        right = max(r["x"] + r["width"] for r in end["rects"])
        bottom = max(r["y"] + r["height"] for r in end["rects"])
        problems.append(
            f"grows: after the monitor settled at {FINAL[0]}x{FINAL[1]} the startup tiles still end at "
            f"{right}x{bottom} ({round(100 * right / FINAL[0])}% of the width) -- tiled once, in the mode the "
            f"session started in, and never again (arranged {end['arranged']}x)")
    return None


async def scenario_hands_off(ws, url, problems):
    if not await boot(ws, url):
        return "the desktop module never loaded"
    await asyncio.sleep(1.0)
    # The person drags one tile somewhere else before the monitor settles.
    await js(ws, "(() => { const r = __wm.rows[1]; r.rect = Object.assign({}, r.rect, { x: r.rect.x + 200, y: r.rect.y + 90 }); })()")
    moved = await js(ws, STATE)
    await viewport(ws, FINAL)
    await asyncio.sleep(5.0)                  # well past the re-tile's debounce and its poll
    end = await js(ws, STATE)
    if end["arranged"] != moved["arranged"] or end["rects"] != moved["rects"]:
        problems.append(f"hands-off: a startup layout the person had already rearranged was re-tiled over "
                        f"them when the screen changed size: before {moved['rects']} after {end['rects']}")
    return None


async def drive(url):
    import websockets
    shutil.rmtree(PROFILE, ignore_errors=True)
    chrome = (shutil.which("google-chrome-stable") or shutil.which("google-chrome")
              or shutil.which("chromium"))
    if not chrome:
        print("SKIP  no Chrome")
        return 2
    proc = subprocess.Popen(
        [chrome, "--headless=new", "--disable-gpu", "--no-sandbox", "--window-size=1920,1200",
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
                await asyncio.sleep(.5)
        if not page:
            print("SKIP  could not start Chrome")
            return 2
        async with websockets.connect(page["webSocketDebuggerUrl"], max_size=64 * 1024 * 1024) as ws:
            await rpc(ws, "Runtime.enable")
            await rpc(ws, "Page.enable")
            for run in (scenario_grows, scenario_hands_off):
                why = await run(ws, url, problems)
                if why:
                    print(f"SKIP  {why}")
                    return 2
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
        shutil.rmtree(PROFILE, ignore_errors=True)
    for p in problems:
        print("FAIL ", p)
    if problems:
        return 1
    print("PASS  startup tiles fill the final screen, and a layout the person changed is left alone")
    return 0


def main():
    try:
        import websockets  # noqa: F401
    except ImportError:
        print("SKIP  websockets not installed")
        return 2
    import http.server
    tmp = tempfile.mkdtemp(prefix="osstarttile-")
    marker = '<script src="/static/js/client/os.js"></script>'
    if marker not in PAGE:
        print("SKIP  check_os_desktop's page no longer loads os.js the way this check injects into")
        return 2
    with open(os.path.join(tmp, "index.html"), "w") as fh:
        fh.write(PAGE.replace(marker, TILE_SHIM + marker))

    class H(http.server.SimpleHTTPRequestHandler):
        def translate_path(self, path):
            path = path.split("?")[0].split("#")[0]
            if path.startswith("/static/"):
                return os.path.join(ROOT, path.lstrip("/"))
            if path == "/desktop/tile.js":
                return os.path.join(ROOT, "desktop", "tile.js")
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
