#!/usr/bin/env python3
"""Mobile regression check for /client — run this BEFORE deploying a UI change, not after.

Every assertion here corresponds to a bug that reached a user rather than a reviewer:

  horizontal-overflow  The page scrolls sideways at phone width. Nothing else about a layout feels as
                       broken this quickly.
  zero-sized-icon      A sprite <use> pointing at a missing symbol, or an icon squeezed to nothing by a
                       flex parent, draws 0x0 with NO console error. Shipped as "the hamburger menu is
                       an empty oval". Uses checkVisibility(), NOT offsetParent — offsetParent is always
                       null on SVG, which makes every hidden element look like a bug.
  off-centre-icon      An icon-only button whose glyph is not centred: line-height/text-align centred a
                       TEXT glyph and does nothing for an <svg>. Shipped as "the + on New post is not
                       centred".
  stretched-media      An <img> whose rendered aspect ratio is wildly off its CSS aspect-ratio, i.e. a
                       width=/height= presentation attribute is beating the stylesheet. Shipped as the
                       profile Media tab's "super long rectangles".
  trapped-hscroll      A horizontally scrollable strip inside the feed whose drag the swipe-to-navigate
                       handler steals, so it cannot be slid on a phone. Shipped as "can't slide to see
                       all the background choices".

Needs Chrome and a reachable instance:

    venv-unified/bin/python scripts/check_client_mobile.py [base_url]

Exit 0 = clean, 1 = regressions (printed), 2 = could not run (no Chrome / site unreachable).
"""
import asyncio
import json
import subprocess
import sys
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "https://poster.place"
WIDTHS = [(390, 844), (360, 780)]
PORT = 9471
PROFILE = "/tmp/pc-mobile-check"

AUDIT = r"""(() => {
  const out = {overflow:false, zero:[], offCentre:[], stretched:[], hscroll:[]};
  out.overflow = document.documentElement.scrollWidth > window.innerWidth + 1;

  const vis = el => !el.checkVisibility || el.checkVisibility();

  document.querySelectorAll('svg.ic').forEach(sv => {
    if (!vis(sv)) return;
    const r = sv.getBoundingClientRect();
    if (r.width < 4 || r.height < 4) {
      const u = sv.querySelector('use');
      out.zero.push({sym: u && u.getAttribute('href'), cls: sv.getAttribute('class')});
    }
  });

  document.querySelectorAll('svg.ic.b-ic, svg.ic.x-ic').forEach(sv => {
    const b = sv.parentElement;
    if (!b || b.tagName !== 'BUTTON' || b.children.length !== 1) return;
    if (b.textContent.trim() !== '' || !vis(sv)) return;   // labelled buttons: icon belongs left
    const br = b.getBoundingClientRect(), ir = sv.getBoundingClientRect();
    if (!br.width || !ir.width) return;
    const dx = (ir.left + ir.width/2) - (br.left + br.width/2);
    const dy = (ir.top + ir.height/2) - (br.top + br.height/2);
    if (Math.abs(dx) > 2.5 || Math.abs(dy) > 2.5)
      out.offCentre.push({cls: String(b.className), dx: +dx.toFixed(1), dy: +dy.toFixed(1)});
  });

  document.querySelectorAll('img').forEach(im => {
    if (!vis(im)) return;
    const cs = getComputedStyle(im), ar = cs.aspectRatio;
    if (!ar || ar === 'auto') return;                      // no declared ratio: nothing to violate
    const m = ar.match(/^([\d.]+)\s*\/\s*([\d.]+)$/);
    if (!m) return;
    const want = parseFloat(m[1]) / parseFloat(m[2]);
    const r = im.getBoundingClientRect();
    if (r.width < 20 || r.height < 20) return;
    const got = r.width / r.height;
    if (Math.abs(got - want) / want > 0.25)
      out.stretched.push({want: +want.toFixed(2), got: +got.toFixed(2),
                          box: Math.round(r.width) + 'x' + Math.round(r.height),
                          cls: String(im.className).slice(0, 30)});
  });

  // Horizontal scrollers inside the feed must be exempt from swipe-to-navigate, or the drag is stolen.
  const feed = document.getElementById('feed');
  if (feed) {
    feed.querySelectorAll('*').forEach(n => {
      if (n.scrollWidth <= n.clientWidth + 1) return;
      const ox = getComputedStyle(n).overflowX;
      if (ox !== 'auto' && ox !== 'scroll') return;
      if (!vis(n)) return;
      out.hscroll.push({cls: String(n.className).slice(0, 34),
                        scroll: n.scrollWidth + '>' + n.clientWidth});
    });
  }
  return out;
})()"""


async def run():
    try:
        import websockets  # noqa: F401
    except ImportError:
        print("SKIP  websockets not installed")
        return 2

    proc = subprocess.Popen(
        ["google-chrome-stable", "--headless=new", "--disable-gpu", "--no-sandbox",
         f"--remote-debugging-port={PORT}", f"--user-data-dir={PROFILE}", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        return await drive()
    finally:
        proc.terminate()


async def drive():
    import websockets
    subprocess.run(["rm", "-rf", PROFILE], check=False)
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

    problems = []
    async with websockets.connect(page["webSocketDebuggerUrl"], max_size=64 * 1024 * 1024) as ws:
        n = [0]

        async def call(method, params=None):
            n[0] += 1
            await ws.send(json.dumps({"id": n[0], "method": method, "params": params or {}}))
            while True:
                msg = json.loads(await ws.recv())
                if msg.get("id") == n[0]:
                    return msg.get("result")

        async def js(expr):
            r = await call("Runtime.evaluate", {"expression": expr, "returnByValue": True})
            if r.get("exceptionDetails"):
                return None
            return r["result"].get("value")

        await call("Runtime.enable")
        await call("Page.enable")
        for w, h in WIDTHS:
            await call("Emulation.setDeviceMetricsOverride",
                       {"width": w, "height": h, "deviceScaleFactor": 2, "mobile": True})
            await call("Emulation.setTouchEmulationEnabled", {"enabled": True, "maxTouchPoints": 5})
            await call("Page.navigate", {"url": BASE + "/client"})
            await asyncio.sleep(14)
            res = await js(AUDIT)
            if res is None:
                print(f"SKIP  {w}px: page did not evaluate (site unreachable?)")
                return 2
            label = f"{w}px"
            if res["overflow"]:
                problems.append((label, "horizontal-overflow", "the page scrolls sideways"))
            for z in res["zero"]:
                problems.append((label, "zero-sized-icon", f"{z['sym']} ({z['cls']})"))
            for o in res["offCentre"]:
                problems.append((label, "off-centre-icon", f"{o['cls']} dx={o['dx']} dy={o['dy']}"))
            for s in res["stretched"]:
                problems.append((label, "stretched-media",
                                 f"{s['cls'] or '<img>'} {s['box']} ratio {s['got']} vs {s['want']}"))
            print(f"{label}: overflow={res['overflow']} zero={len(res['zero'])} "
                  f"offCentre={len(res['offCentre'])} stretched={len(res['stretched'])} "
                  f"hscrollers={len(res['hscroll'])}")

    if not problems:
        print("OK  mobile checks passed")
        return 0
    print()
    for width, kind, detail in problems:
        print(f"FAIL  [{width}] {kind}: {detail}")
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
