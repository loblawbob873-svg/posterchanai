#!/usr/bin/env python3
"""Discover → Shorts on a phone: cards render from real-shaped Divine events, and at most one
<video> (decoder) is ever mounted.

Two 34236 events in Divine's measured shape are planted straight into the client's Store, the
Shorts view is opened at a phone viewport, and the page is asked what it actually mounted — the
generic mobile check never opens this screen. A feed that mounted a decoder per card is exactly
how a shorts session kills a WebView, so the mount count is the load-bearing assertion.

Exit 0 pass, 1 fail, 2 could-not-run.
"""
import asyncio
import json
import os
import shutil
import subprocess
import sys
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:3051"
PORT = int(os.environ.get("PC_CHECK_PORT") or 9536)
PROFILE = os.environ.get("PC_CHECK_PROFILE") or "/tmp/pc-shorts-check"

PLANT = r"""(() => {
  const mk = (i) => ({
    kind: 34236, pubkey: String(i).repeat(64).slice(0, 64), id: String(i).repeat(64).slice(0, 64),
    created_at: Math.floor(Date.now()/1000) - i, content: '#check',
    tags: [['d', 'chk' + i],
           ['imeta', 'url https://example.invalid/v' + i + '.mp4', 'm video/mp4',
            'image https://example.invalid/p' + i + '.jpg', 'dim 1080x1920'],
           ['title', 'planted short ' + i], ['duration', '5']],
    sig: '0'.repeat(128),
  });
  window.Store.saveEvent(mk(1)); window.Store.saveEvent(mk(2));
  return true;
})"""

SURVEY = r"""(() => {
  const wrap = document.getElementById('shorts-wrap');
  if (!wrap) return { wrap: false };
  const cards = [...wrap.querySelectorAll('.short-card')];
  const vids = wrap.querySelectorAll('video').length;
  const first = cards[0];
  const cs = first ? getComputedStyle(first) : null;
  return {
    wrap: true, cards: cards.length, videos: vids,
    titled: cards.filter(c => (c.querySelector('.short-title')||{}).textContent).length,
    snap: getComputedStyle(wrap).scrollSnapType.includes('y'),
    cardH: first ? first.offsetHeight : 0, wrapH: wrap.clientHeight,
    visible: first ? +getComputedStyle(first).opacity > 0.9 : false,
    overflowX: document.documentElement.scrollWidth <= window.innerWidth + 1,
  };
})"""


async def drive(url):
    subprocess.run(["rm", "-rf", PROFILE], check=False)
    chrome = (shutil.which("google-chrome-stable") or shutil.which("google-chrome")
              or shutil.which("chromium"))
    if not chrome:
        print("SKIP  no Chrome")
        return 2
    import websockets
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
                    r = json.loads(await ws.recv())
                    if r.get("id") == n[0]:
                        return r.get("result")

            async def js(expr, aw=False):
                r = await call("Runtime.evaluate",
                               {"expression": expr, "returnByValue": True, "awaitPromise": aw})
                if r.get("exceptionDetails"):
                    if os.environ.get("PC_DEBUG"):
                        print("  DEBUG:", json.dumps(r["exceptionDetails"])[:600])
                    return None
                return r["result"].get("value")

            await call("Runtime.enable")
            await call("Page.enable")
            await call("Emulation.setDeviceMetricsOverride",
                       {"width": 390, "height": 844, "deviceScaleFactor": 2, "mobile": True})
            await call("Page.navigate", {"url": url})
            for _ in range(80):
                await asyncio.sleep(0.25)
                if await js("!!(window.__PC && window.Store)"):
                    break
            else:
                print("SKIP  the client never finished loading")
                return 2
            if not await js(f"({PLANT})()"):
                print("SKIP  could not plant events (Store not reachable)")
                return 2
            await js("window.__PC.switchView('shorts')")
            await asyncio.sleep(2.5)
            out = await js(f"({SURVEY})()") or {}
            print("  survey:", json.dumps(out))
            if not out.get("wrap"):
                problems.append("the Shorts view never rendered its wrap")
            if (out.get("cards") or 0) < 2:
                problems.append(f"planted 2 shorts, drew {out.get('cards')}")
            if (out.get("videos") or 0) > 1:
                problems.append(f"{out['videos']} <video> elements mounted — one decoder per card "
                                "is how a shorts feed kills a WebView")
            if out.get("cards") and not out.get("titled"):
                problems.append("no card shows its title overlay")
            if out.get("cards") and not out.get("snap"):
                problems.append("the wrap does not snap-scroll")
            if out.get("cards") and abs(out.get("cardH", 0) - out.get("wrapH", 1)) > 40:
                problems.append(f"card height {out.get('cardH')} vs viewport {out.get('wrapH')} — "
                                "not a full-screen short")
            if out.get("cards") and not out.get("visible"):
                problems.append("the first card is transparent")
            if not out.get("overflowX", True):
                problems.append("the page scrolls horizontally on a phone")
    finally:
        proc.terminate()
        subprocess.run(["rm", "-rf", PROFILE], check=False)
    if problems:
        for p in problems:
            print("FAIL ", p)
        return 1
    print("PASS  Shorts renders Divine-shaped events with one decoder at a time")
    return 0


def main():
    try:
        urllib.request.urlopen(BASE + "/client", timeout=5)
    except Exception as e:
        print(f"SKIP  no instance at {BASE} ({e})")
        return 2
    return asyncio.run(drive(BASE + "/client"))


if __name__ == "__main__":
    sys.exit(main())
