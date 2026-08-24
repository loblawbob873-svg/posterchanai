#!/usr/bin/env python3
"""Games → Webxdc, the mini-app gallery, driven at real widths in a real browser.

    venv-unified/bin/python scripts/check_webxdc_gallery.py [base_url]

The generic check (check_client_mobile.py) never opens this screen — it runs as a guest on the
timeline — and tests/test_webxdc_gallery.py tests the DATA, not what the CSS does to it. This is the
half neither covers: a grid of cover art is the one thing in this client whose whole job is to look
like something, and every way it goes wrong is a layout fact no assertion about tags can see.

The tiles are painted by the SHIPPED galTile through the SHIPPED stylesheet, from a stub app list —
so this measures the real markup at real widths without needing the network to be carrying games.

Assertions, each one a way "a gallery of games" fails on a phone:

  one-column      minmax(190px,1fr) alone yields ONE column at 390px, turning nine games into nine
                  full-width banners nobody scrolls to the end of. Two up is the point of a grid.
  overflow        The page scrolls sideways. Nothing else feels as broken as quickly.
  squashed-cover  The cover box lost its 1:1 aspect-ratio, so art arrives and everything below it
                  jumps — the layout-stability rule the timeline already follows.
  clipped-title   A long app name pushes the tile wider than its column instead of ellipsing.
  tap-targets     Play/Post under the 44px a thumb needs.
  dead-cover      A cover whose host 404s must leave a tile, not a broken-image glyph.
  no-play         The tile has no Play control at all, which is the only reason it exists.

Exit 0 = clean, 1 = problems (printed), 2 = could not run (no Chrome / site unreachable).
"""
import os
import asyncio
import json
import shutil
import subprocess
import sys
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:3051"
WIDTHS = [(390, 844, "phone"), (820, 1180, "tablet"), (1400, 900, "desktop")]
PORT = int(os.environ.get("PC_CHECK_PORT") or 9497)
PROFILE = os.environ.get("PC_CHECK_PROFILE") or "/tmp/pc-xdc-check"

# A stub directory: one app with real cover art, one with none, and one with a name long enough to
# find a missing ellipsis. Painted through the shipped galTile so the markup is the real markup.
DRIVE = r"""(async () => {
  const W = window.PCWebxdc;
  if (!W || !W.__galTile) return { skip: 'PCWebxdc.__galTile is not exposed' };
  const apps = [
    { uuid:'a1', name:'Quake III Arena', url:'https://x/a.xdc', sha:'a'.repeat(64), plays:11,
      size:5825997, pubkey:'11'.repeat(32), evId:'e1', at:1,
      image:'data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7' },
    { uuid:'a2', name:'Shared Counter', url:'https://x/b.xdc', sha:'b'.repeat(64), plays:0,
      size:2656, pubkey:'22'.repeat(32), evId:'e2', at:2, image:'' },
    { uuid:'a3', name:'Command & Conquer: Red Alert Ultimate Edition Remastered',
      url:'https://x/c.xdc', sha:'c'.repeat(64), plays:3, size:0, pubkey:'33'.repeat(32),
      evId:'e3', at:3, image:'https://127.0.0.1:1/definitely-404.png' },
  ];
  const feed = document.getElementById('feed');
  if (!feed) return { skip: 'no #feed' };
  feed.innerHTML = '<div class="xdc-gal-top"><div class="muted small">Mini apps.</div>'
    + '<button class="btn btn-ghost small">Refresh</button></div>'
    + '<div class="xdc-grid">' + apps.map(a => W.__galTile(a)).join('') + '</div>';
  await new Promise(r => setTimeout(r, 350));

  const grid = feed.querySelector('.xdc-grid');
  const tiles = [...feed.querySelectorAll('.xdc-tile')];
  /* The real route renderer is asynchronous and may replace #feed while this harness deliberately
   * waits for a dead cover's onerror. That is neither a layout result nor an exception: tell the
   * driver to inject and measure again once the route has settled. */
  if (!grid || tiles.length < 3 || !tiles[0].querySelector('.xdc-cover')
      || !tiles[2].querySelector('.xdc-tmeta b') || !tiles[0].querySelector('.xdc-tplay'))
    return { retry:true };
  const gs = getComputedStyle(grid);
  // How many tiles share the topmost row — the honest way to count columns, since the
  // grid-template-columns string is the unresolved `repeat(auto-fill, …)` on some engines.
  const tops = tiles.map(t => Math.round(t.getBoundingClientRect().top));
  const cols = tops.filter(t => t === Math.min(...tops)).length;

  const cover = tiles[0].querySelector('.xdc-cover').getBoundingClientRect();
  const t3 = tiles[2].getBoundingClientRect();
  const col3 = tiles[2].parentElement.getBoundingClientRect();
  const title3 = tiles[2].querySelector('.xdc-tmeta b');
  const play = tiles[0].querySelector('.xdc-tplay').getBoundingClientRect();
  const post = tiles[0].querySelector('.xdc-tpost');

  return {
    vw: window.innerWidth,
    docW: document.documentElement.scrollWidth,
    cols, gap: gs.gap,
    coverW: Math.round(cover.width), coverH: Math.round(cover.height),
    noneCover: !!tiles[1].querySelector('.xdc-cover-none'),
    noneGlyph: !!tiles[1].querySelector('.xdc-cover-none svg'),
    tileRight: Math.round(t3.right), colRight: Math.round(col3.right),
    titleOverflow: getComputedStyle(title3).textOverflow,
    titleScroll: title3.scrollWidth > title3.clientWidth + 1,
    titleClient: Math.round(title3.getBoundingClientRect().width),
    playH: Math.round(play.height), playW: Math.round(play.width),
    hasPost: !!post,
    // The dead cover: onerror removes the <img>, so after a beat the tile still stands.
    deadImgGone: !tiles[2].querySelector('.xdc-cover img'),
    tileH: Math.round(tiles[0].getBoundingClientRect().height),
  };
})()"""


async def drive(url):
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
        import websockets
        async with websockets.connect(page["webSocketDebuggerUrl"], max_size=64 * 1024 * 1024) as ws:
            n = [0]

            async def call(method, params=None):
                n[0] += 1
                await ws.send(json.dumps({"id": n[0], "method": method, "params": params or {}}))
                while True:
                    msg = json.loads(await ws.recv())
                    if msg.get("id") == n[0]:
                        return msg.get("result")

            async def js(expr, awaited=False):
                r = await call("Runtime.evaluate",
                               {"expression": expr, "returnByValue": True, "awaitPromise": awaited})
                if r.get("exceptionDetails"):
                    print("  DEBUG:", json.dumps(r["exceptionDetails"])[:600])
                    return None
                return r["result"].get("value")

            await call("Runtime.enable")
            await call("Page.enable")
            for w, h, label in WIDTHS:
                await call("Emulation.setDeviceMetricsOverride",
                           {"width": w, "height": h, "deviceScaleFactor": 2 if w < 900 else 1,
                            "mobile": w < 900})
                await call("Page.navigate", {"url": url})
                ok = False
                for _ in range(80):
                    await asyncio.sleep(0.25)
                    if await js("!!(window.PCWebxdc && window.PCWebxdc.__galTile "
                                "&& document.getElementById('feed'))"):
                        ok = True
                        break
                if not ok:
                    print("SKIP  the client never exposed PCWebxdc.__galTile")
                    return 2
                r = None
                for _ in range(6):
                    r = await js(DRIVE, awaited=True)
                    if r and not r.get("retry"):
                        break
                    await asyncio.sleep(0.35)
                if not r:
                    problems.append((label, "harness", "the grid did not evaluate"))
                    continue
                if r.get("skip"):
                    print("SKIP  " + r["skip"])
                    return 2

                print(f"{label} {w}px: cols={r['cols']} cover={r['coverW']}x{r['coverH']} "
                      f"tile={r['tileH']}px play={r['playW']}x{r['playH']} gap={r['gap']}")

                if r["docW"] > r["vw"] + 1:
                    problems.append((label, "overflow",
                                     f"the page is {r['docW']}px wide in a {r['vw']}px viewport"))
                if w <= 820 and r["cols"] < 2:
                    problems.append((label, "one-column",
                                     f"{r['cols']} column at {w}px — nine games become nine banners"))
                # A TABLET IS NOT A BIG PHONE. Forcing `1fr 1fr` at the 820px breakpoint the rest of
                # the mobile CSS uses measured 391px covers here — two enormous tiles on a screen with
                # room for four — which is why the two-up rule stops at 600px.
                if w == 820 and r["cols"] < 3:
                    problems.append((label, "one-column",
                                     f"only {r['cols']} columns at 820px (covers {r['coverW']}px) — "
                                     "the phone rule is reaching up into tablet widths"))
                if w >= 1400 and r["cols"] < 3:
                    problems.append((label, "one-column",
                                     f"only {r['cols']} columns at {w}px — the grid is not filling"))
                if abs(r["coverW"] - r["coverH"]) > 2:
                    problems.append((label, "squashed-cover",
                                     f"the cover is {r['coverW']}x{r['coverH']} — not 1:1, so art "
                                     "arriving will shift everything under it"))
                if not r["noneCover"] or not r["noneGlyph"]:
                    problems.append((label, "dead-cover",
                                     "an app with no art did not fall back to the gamepad glyph"))
                if not r["deadImgGone"]:
                    problems.append((label, "dead-cover",
                                     "a cover whose host 404s left a broken <img> in the tile"))
                if r["tileRight"] > r["colRight"] + 1:
                    problems.append((label, "clipped-title",
                                     "a long app name pushed its tile past its grid column"))
                if r["titleScroll"] and r["titleOverflow"] != "ellipsis":
                    problems.append((label, "clipped-title",
                                     f"a long name overflows with text-overflow:{r['titleOverflow']}"))
                if w <= 820 and r["playH"] < 30:
                    problems.append((label, "tap-targets",
                                     f"Play is {r['playH']}px tall — a thumb needs ~44"))
                if not r["hasPost"]:
                    problems.append((label, "no-play", "the tile has no way to open the post"))
                if r["playW"] < 30:
                    problems.append((label, "no-play", f"Play is {r['playW']}px wide"))
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
        subprocess.run(["rm", "-rf", PROFILE], check=False)

    if problems:
        print()
        for label, kind, msg in problems:
            print(f"FAIL  [{label}] {kind}: {msg}")
        return 1
    print("OK  webxdc gallery checks passed")
    return 0


def main():
    try:
        urllib.request.urlopen(BASE + "/client", timeout=8)
    except Exception as e:
        print(f"SKIP  {BASE} unreachable ({e})")
        return 2
    return asyncio.run(drive(BASE + "/client"))


if __name__ == "__main__":
    sys.exit(main())
