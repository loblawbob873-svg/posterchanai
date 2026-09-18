#!/usr/bin/env python3
"""THE FIRST-RUN WIZARD AT THE SIZE A TV ACTUALLY GAVE IT, CLICKED WITH A MOUSE.

    venv-unified/bin/python scripts/check_os_firstrun_on_a_tv.py

`check_os_firstrun.py` drives the same wizard and passes, and it could not have caught this for two
reasons, both of which are the point of this file:

  IT RUNS AT 1400x900. Nothing here had ever been drawn at the size the machine it was reported from
  was actually running at. A TV that flags no preferred mode gets 640x480 out of wlroots, which is
  below the client's own 821px breakpoint — so the wizard on that television was rendering the
  PHONE layout into a 640x480 window, a combination that exists on no developer's desk.

  IT PRESSES BUTTONS BY CALLING `t.onclick()`. That invokes the handler and proves the handler
  works. It cannot see a row that is off the bottom of a 480px-tall screen, a row underneath another
  element, or a row in a container that does not scroll — and "so glitchey and stopped responding to
  clicking the access point" is precisely the class of failure where the handler is perfect and the
  pixel belongs to something else.

So this asks the only question that matters to somebody standing in front of the television: if I
point at that network and click, does this machine join it? It hit-tests every access point with
elementFromPoint and then clicks with a REAL mouse event through CDP, at 640x480.

Exit 0 clean · 1 problems · 2 could not run.
"""
import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from check_os_firstrun import PAGE, ROOT  # one page stub, not a second copy of the stubs

PORT = int(os.environ.get("PC_CHECK_PORT") or 9494)
PROFILE = os.environ.get("PC_CHECK_PROFILE") or "/tmp/pc-osfirstrun-tv-check"

# The TV, measured: wlroots picks the LAST mode when the EDID flags none, and this panel's last mode
# is 640x480. 1280x720 is the same television one notch up, kept because a fix that only works at
# one exact size is not a fix.
SIZES = [(640, 480), (1280, 720)]

SETUP = r"""(async () => {
  try { localStorage.clear(); } catch (_) {}
  window.__net.readable = true; window.__net.online = false; window.__net.joined = null;
  window.ME = null; window.GUEST = true; window.__instance = '';
  await window.PCFirstRunUI.boot();
  await new Promise(r => setTimeout(r, 600));
  const card = document.querySelector('#osfr .osfr-card');
  return { up: !!card, title: card ? card.querySelector('.osfr-h').textContent : '' };
})()"""

# Where every access point actually IS, and what is actually on top of it.
PROBE = r"""(() => {
  const rows = [...document.querySelectorAll('#osfr [data-ssid]')];
  const vw = innerWidth, vh = innerHeight;
  return { vw, vh, rows: rows.map(r => {
    const b = r.getBoundingClientRect();
    const x = b.left + b.width / 2, y = b.top + b.height / 2;
    const hit = document.elementFromPoint(x, y);
    return {
      ssid: r.dataset.ssid, x, y,
      w: Math.round(b.width), h: Math.round(b.height),
      top: Math.round(b.top), bottom: Math.round(b.bottom),
      onScreen: b.top >= 0 && b.bottom <= vh && b.left >= 0 && b.right <= vw && b.width > 0 && b.height > 0,
      // The click lands on this row (or something inside it) rather than on whatever is covering it.
      hits: !!(hit && (hit === r || r.contains(hit))),
      hitTag: hit ? (hit.tagName + '.' + (hit.className || '').toString().split(' ')[0]) : null,
    };
  })};
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

        async with websockets.connect(page["webSocketDebuggerUrl"], max_size=64 * 1024 * 1024) as ws:
            n = [0]

            async def call(method, params=None):
                n[0] += 1
                await ws.send(json.dumps({"id": n[0], "method": method, "params": params or {}}))
                while True:
                    msg = json.loads(await ws.recv())
                    if msg.get("id") == n[0]:
                        return msg.get("result")

            async def ev(expr, await_promise=False):
                r = await call("Runtime.evaluate",
                               {"expression": expr, "returnByValue": True,
                                "awaitPromise": await_promise})
                if r.get("exceptionDetails"):
                    raise RuntimeError(json.dumps(r["exceptionDetails"])[:500])
                return r["result"].get("value")

            await call("Runtime.enable")
            await call("Page.enable")

            for (w, h) in SIZES:
                await call("Emulation.setDeviceMetricsOverride",
                           {"width": w, "height": h, "deviceScaleFactor": 1, "mobile": False})
                await call("Page.navigate", {"url": url})
                ok = False
                for _ in range(80):
                    await asyncio.sleep(0.25)
                    if await ev("window.__ready === true && !!window.PCFirstRunUI") is True:
                        ok = True
                        break
                if not ok:
                    print("SKIP  the page never became ready")
                    return 2

                where = f"{w}x{h}"
                s = await ev(SETUP, await_promise=True)
                if not s.get("up"):
                    problems.append((where, "no-wizard",
                                     "the first-run wizard did not appear at all"))
                    continue
                print(f"  {where}: first screen is {json.dumps(s['title'])}")

                p = await ev(PROBE)
                rows = p["rows"]
                if not rows:
                    problems.append((where, "no-networks",
                                     "the wifi list drew no access points on a machine with three "
                                     "networks in range"))
                    continue
                for r in rows:
                    print(f"    {r['ssid']:<10} {r['w']}x{r['h']} at y={r['top']}..{r['bottom']} "
                          f"onScreen={r['onScreen']} clickable={r['hits']} "
                          f"(point hits {r['hitTag']})")

                offscreen = [r for r in rows if not r["onScreen"]]
                if offscreen:
                    problems.append((where, "access-point-off-screen",
                                     "somebody cannot reach " + ", ".join(r["ssid"] for r in offscreen)
                                     + f" — the row sits at y={offscreen[0]['top']}..{offscreen[0]['bottom']}"
                                     f" on a {h}px-tall screen"))
                covered = [r for r in rows if not r["hits"]]
                if covered:
                    problems.append((where, "access-point-not-clickable",
                                     "clicking " + covered[0]["ssid"] + " lands on "
                                     + str(covered[0]["hitTag"]) + " instead — this is the reported "
                                     "'stopped responding to clicking the access point'"))

                # AND NOW ACTUALLY CLICK IT, with a mouse, at those coordinates.
                target = rows[0]
                for kind in ("mousePressed", "mouseReleased"):
                    await call("Input.dispatchMouseEvent",
                               {"type": kind, "x": target["x"], "y": target["y"],
                                "button": "left", "clickCount": 1})
                await asyncio.sleep(900 / 1000)
                joined = await ev("window.__net.joined")
                print(f"    real click on {target['ssid']}: joined={json.dumps(joined)}")
                if not joined:
                    problems.append((where, "click-joins-nothing",
                                     f"a real mouse click on {target['ssid']} joined no network — "
                                     "the handler works when called directly, so this is the pixel "
                                     "belonging to something else"))
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()

    if not problems:
        print("PASS  every access point is on screen and joins when clicked, at 640x480 and 1280x720")
        return 0
    for where, k, d in problems:
        print(f"FAIL  [{where}] {k}: {d}")
    return 1


def main():
    try:
        import websockets  # noqa: F401
    except ImportError:
        print("SKIP  websockets not installed")
        return 2
    import http.server
    import threading
    tmp = tempfile.mkdtemp(prefix="osfirstrun-tv-")
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
