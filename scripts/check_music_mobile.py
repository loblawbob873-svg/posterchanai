#!/usr/bin/env python3
"""The music player as a phone/tablet app — driven at real widths.

    venv-unified/bin/python scripts/check_music_mobile.py [base_url]

The generic check (check_client_mobile.py) never opens this surface, and the player is not a normal
view: it is a fixed-position panel the client mounts on <html>, so nothing else in the app tests it.

Assertions, each one a way "use it as your music player on a phone" fails:

  not-full-screen   Expanded, the player is a small card floating over the feed — a scaled-down
                    desktop window on a device where the app IS the screen. It must fill it.
  it-bounces        `mpwiggle` rocks the whole box while a track plays. Charming on a 338px window
                    parked in a corner; motion sickness on a full screen you are reading a track
                    list on.
  list-cannot-scroll  The panel scrolls instead of the list inside it, or the list drags the page
                    behind it (overscroll chaining) — on a phone that reads as the app fighting you.
  tap-targets       Controls or track rows too small to hit with a thumb.
  offscreen         The panel is positioned off the edge — the desktop drag writes inline left/top,
                    and a window narrowed afterwards would keep them.
  mini-not-floating The MINI bar must stay a floating pill above the bottom nav: that one is meant
                    to sit over whatever you are doing, and full-screening it would trap you.

Exit 0 = clean, 1 = problems (printed), 2 = could not run (no Chrome / site unreachable).
"""
import asyncio
import json
import shutil
import subprocess
import sys
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:3051"
WIDTHS = [(390, 844, "phone"), (820, 1180, "tablet"), (1400, 900, "desktop")]
PORT = 9494
PROFILE = "/tmp/pc-music-check"

# The player mounts itself on demand; nothing in a guest session opens it, so the harness builds the
# real markup through the client's own MusicPlayer and inspects what the CSS does to it.
DRIVE = r"""(async () => {
  const sleep = ms => new Promise(r=>setTimeout(r,ms));
  const P = window.__PC;
  if (!P || !P.MusicPlayer) return { skip: 'MusicPlayer is not exposed' };
  const MP = P.MusicPlayer;
  const el = MP.ensure();
  el.classList.remove('hidden');
  MP.setMin(false);
  MP._render();
  await sleep(120);
  const r = el.getBoundingClientRect();
  const cs = getComputedStyle(el);
  const list = el.querySelector('.mp-list');
  const lcs = list ? getComputedStyle(list) : null;
  const big = el.querySelector('.mp-controls .mp-big');
  const seek = el.querySelector('.mp-seek');
  const out = {
    w: Math.round(r.width), h: Math.round(r.height),
    left: Math.round(r.left), top: Math.round(r.top), right: Math.round(r.right),
    vw: window.innerWidth, vh: window.innerHeight,
    anim: cs.animationName,
    playingAnim: (() => { el.classList.add('playing');
      const a = getComputedStyle(el).animationName; el.classList.remove('playing'); return a; })(),
    listOverflow: lcs ? lcs.overflowY : '', listOverscroll: lcs ? lcs.overscrollBehaviorY : '',
    listMaxH: lcs ? lcs.maxHeight : '',
    bigH: big ? Math.round(big.getBoundingClientRect().height) : 0,
    seekH: seek ? Math.round(seek.getBoundingClientRect().height) : 0,
    trackH: (() => { const t = el.querySelector('.mp-track');
                     return t ? Math.round(t.getBoundingClientRect().height) : 0; })(),
  };
  // …and the mini bar, which must NOT become full screen.
  MP.setMin(true); MP._render(); await sleep(80);
  const m = el.getBoundingClientRect();
  out.miniW = Math.round(m.width); out.miniH = Math.round(m.height);
  out.miniBottomGap = Math.round(window.innerHeight - m.bottom);
  MP.setMin(false);
  return out;
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
                    print("  DEBUG:", json.dumps(r["exceptionDetails"])[:500])
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
                    if await js("!!(window.__PC && window.__PC.MusicPlayer)"):
                        ok = True
                        break
                if not ok:
                    print("SKIP  the client never exposed MusicPlayer")
                    return 2
                r = await js(DRIVE, awaited=True)
                if not r:
                    problems.append((label, "harness", "the player did not evaluate"))
                    continue
                if r.get("skip"):
                    print("SKIP  " + r["skip"])
                    return 2
                phone = w <= 820
                if phone:
                    if r["w"] < r["vw"] - 2 or r["h"] < r["vh"] - 2:
                        problems.append((label, "not-full-screen",
                                         f"the expanded player is {r['w']}x{r['h']} in a "
                                         f"{r['vw']}x{r['vh']} screen — still a floating card"))
                    if r["playingAnim"] not in ("none", ""):
                        problems.append((label, "it-bounces",
                                         f"the full-screen player animates ({r['playingAnim']}) while playing"))
                    if r["listOverflow"] not in ("auto", "scroll"):
                        problems.append((label, "list-cannot-scroll",
                                         f"the track list has overflow-y:{r['listOverflow']}"))
                    if r["listOverscroll"] != "contain":
                        problems.append((label, "list-cannot-scroll",
                                         "flicking the track list drags the page behind it "
                                         f"(overscroll-behavior-y:{r['listOverscroll']})"))
                    if r["bigH"] < 44:
                        problems.append((label, "tap-targets",
                                         f"play button is {r['bigH']}px — under the 44px a thumb needs"))
                    if r["trackH"] and r["trackH"] < 40:
                        problems.append((label, "tap-targets", f"track rows are {r['trackH']}px"))
                    if r["seekH"] < 8:
                        problems.append((label, "tap-targets", f"the scrubber is {r['seekH']}px tall"))
                    if r["left"] < -1 or r["right"] > r["vw"] + 1:
                        problems.append((label, "offscreen",
                                         f"the panel sits at left={r['left']} right={r['right']}"))
                    if r["miniW"] >= r["vw"] - 2:
                        problems.append((label, "mini-not-floating",
                                         "the MINI bar went full-width; it is meant to float"))
                    if r["miniBottomGap"] < 40:
                        problems.append((label, "mini-not-floating",
                                         f"the mini bar sits {r['miniBottomGap']}px from the bottom — "
                                         "under the mobile nav"))
                else:
                    # Desktop keeps the floating window, wiggle and all — that is the design there.
                    if r["w"] >= r["vw"] - 2:
                        problems.append((label, "not-full-screen",
                                         "the desktop player went full-screen; it is a window there"))
    finally:
        proc.terminate()
        subprocess.run(["rm", "-rf", PROFILE], check=False)

    if problems:
        print(f"FAIL  {len(problems)} problem(s):")
        for label, kind, msg in problems:
            print(f"  [{label}] {kind}: {msg}")
        return 1
    print("OK  music player works at phone, tablet and desktop widths")
    return 0


def main():
    try:
        import websockets  # noqa: F401
    except ImportError:
        print("SKIP  websockets not installed")
        return 2
    try:
        with urllib.request.urlopen(BASE + "/client", timeout=8) as r:
            if r.status != 200:
                raise RuntimeError(r.status)
    except Exception as e:
        print(f"SKIP  {BASE}/client is not reachable ({e})")
        return 2
    return asyncio.run(drive(BASE + "/client"))


if __name__ == "__main__":
    sys.exit(main())
